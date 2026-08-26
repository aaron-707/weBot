"""
Probe script: proves whether AgentLoop ever actually reaches DecisionEngine/Ollama.

Why this exists:
- search/form/login goals are always intercepted by their dedicated state machines
  before DecisionEngine is ever consulted.
- The README's benchmark numbers were captured with Ollama unreachable
  (see docs/DEGRADED_MODE_ANALYSIS.md), so they say nothing about the LLM path.
- The goal below deliberately avoids every keyword branch in:
    workflows/search_state_machine.py  (_is_search_goal)
    workflows/form_state_machine.py    (_is_form_goal)
    workflows/login_state_machine.py   (_is_login_goal)
    workflows/agent_loop.py            (_deterministic_action)
  so the only place left for AgentLoop to go is DecisionEngine.choose_next_action().

Run with Ollama already serving (ollama serve) and the configured model pulled:
    python -m tests.manual.test_llm_path_probe
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from webot.config.settings import resolve_ollama_config_sources, settings
from webot.intelligence.dom_delta import DomDelta
from webot.intelligence.dom_extractor import DomExtractor
from webot.llm.decision_engine import DecisionEngine
from webot.llm.ollama_client import OllamaClient
from webot.memory.session_memory import SessionMemory
from webot.workflows.action_executor import ActionExecutor
from webot.workflows.action_validator import ActionValidator
from webot.workflows.agent_loop import AgentLoop
from webot.workflows.goal_evaluator import GoalEvaluator
from webot.workflows.progress_tracker import ProgressTracker
from webot.workflows.recovery_engine import RecoveryEngine

# Deliberately avoids every keyword bucket checked by the state machines and
# agent_loop._deterministic_action (search/find/look up/query, form/fill/submit/apply,
# login/log in/sign in/authenticate).
PROBE_GOAL = "Locate the primary heading on the page and report its text"
PROBE_URL = "https://example.com"


@dataclass(slots=True)
class BrowserController:
    headless: bool = True
    _playwright: Playwright | None = None
    _browser: Browser | None = None
    _context: BrowserContext | None = None
    _page: Page | None = None

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Browser not initialized")
        return self._page

    async def open(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        self._context = await self._browser.new_context(user_agent=settings.browser.user_agent)
        self._page = await self._context.new_page()
        self._page.set_default_timeout(settings.browser.timeout_ms)
        self._page.set_default_navigation_timeout(settings.browser.navigation_timeout_ms)

    async def close(self) -> None:
        if self._context is not None:
            await self._context.close()
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()

    async def goto(self, url: str) -> None:
        await self.page.goto(url, wait_until="domcontentloaded")

    async def click(self, selector: str) -> None:
        await self.page.click(selector)

    async def fill(self, selector: str, value: str) -> None:
        await self.page.fill(selector, value)

    async def extract_text(self, selector: str) -> str:
        return (await self.page.locator(selector).first.inner_text()).strip()


async def run_probe() -> int:
    browser = BrowserController(headless=True)

    ollama_info = resolve_ollama_config_sources(settings)
    ollama_client = OllamaClient(
        base_url=ollama_info["base_url"],
        model=ollama_info["model"],
        timeout_seconds=float(settings.ollama.timeout_seconds),
    )

    # --- Instrumentation: wrap generate() so we can PROVE a call happened,
    # independent of AgentLoop's own (currently silent-on-success) logging. ---
    calls: list[str] = []
    original_generate = OllamaClient.generate

    def instrumented_generate(self, prompt: str, *, model: str | None = None) -> str:
        calls.append(prompt)
        print("\n--- OLLAMA CALL FIRED ---")
        print(f"model requested: {model or self.model}")
        print(f"prompt sent:\n{prompt}\n")
        result = original_generate(self, prompt, model=model)
        print(f"raw response:\n{result}\n")
        return result

    import unittest.mock
    unittest.mock.patch.object(OllamaClient, "generate", new=instrumented_generate).start()

    llm_available = ollama_client.is_available()
    print(f"ollama base_url  = {ollama_info['base_url']} (source: {ollama_info['base_url_source']})")
    print(f"ollama model     = {ollama_info['model']} (source: {ollama_info['model_source']})")
    print(f"is_available()   = {llm_available}")
    if not llm_available:
        print("\nOllama is NOT reachable. Start it first: `ollama serve` (and `ollama pull "
              f"{ollama_info['model']}` if needed), then re-run this probe.")
        return 1

    try:
        await browser.open()
        await browser.goto(PROBE_URL)

        dom_extractor = DomExtractor()
        decision_engine = DecisionEngine(ollama_client=ollama_client)
        session_memory = SessionMemory(max_actions=100)
        action_executor = ActionExecutor(browser_controller=browser, max_retries=2, retry_delay_seconds=0.5)
        action_validator = ActionValidator(timeout_ms=settings.browser.timeout_ms)
        dom_delta = DomDelta(max_items_per_bucket=12)
        recovery_engine = RecoveryEngine(max_retries=2, llm_client=ollama_client)
        progress_tracker = ProgressTracker(window_size=20)
        goal_evaluator = GoalEvaluator(llm_client=ollama_client)

        agent_loop = AgentLoop(
            page=browser.page,
            dom_extractor=dom_extractor,
            decision_engine=decision_engine,
            action_executor=action_executor,
            action_validator=action_validator,
            session_memory=session_memory,
            dom_delta=dom_delta,
            recovery_engine=recovery_engine,
            max_steps=4,
            llm_enabled=True,
            degraded_mode=False,
            progress_tracker=progress_tracker,
            goal_evaluator=goal_evaluator,
        )

        result = await agent_loop.run(PROBE_GOAL)

        print("\n=== PROBE RESULT ===")
        print(f"decision_engine.generate() calls made: {len(calls)}")
        print(f"loop status: {result.get('status')}")
        print(f"steps executed: {result.get('steps_executed')}")
        if calls:
            print("CONFIRMED: the LLM path fired at least once.")
        else:
            print("NOT CONFIRMED: state machine or deterministic layer handled every step "
                  "without ever reaching DecisionEngine. Try a different probe goal or "
                  "inspect the DOM your state machines are matching against.")
        return 0
    finally:
        await browser.close()


def main() -> None:
    raise SystemExit(asyncio.run(run_probe()))


if __name__ == "__main__":
    main()
