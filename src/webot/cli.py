"""General-purpose entrypoint: free-text prompt in, autonomous run out.

This is the missing link between the existing runtime test scripts
(each of which hardcodes a goal + starting URL for one specific site)
and the "type a prompt, it just does the work" experience.

Usage:
    python -m webot.cli "Open duckduckgo and search for python internships"
    python -m webot.cli "Login to the-internet.herokuapp.com with demo credentials" --headless

Scope note: this wires the same AgentLoop/state-machine engine the
runtime tests use, with the same dependency graph. It intentionally
does NOT replicate the full per-step ExecutionTrace/progress recording
those tests do (that logic includes site-specific result-extraction
JS that doesn't generalize to an arbitrary site) — it reports a final
status/confidence summary and a screenshot instead. Wire in
ExecutionTrace recording later if step-by-step traces are needed for
an arbitrary prompt.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from webot.config.settings import resolve_ollama_config_sources, settings
from webot.intelligence.dom_extractor import DomExtractor
from webot.intelligence.dom_delta import DomDelta
from webot.llm.decision_engine import DecisionEngine
from webot.llm.ollama_client import OllamaClient
from webot.memory.session_memory import SessionMemory
from webot.utils.logger import get_logger, setup_logging
from webot.workflows.action_executor import ActionExecutor
from webot.workflows.action_validator import ActionValidator
from webot.workflows.agent_loop import AgentLoop
from webot.workflows.goal_evaluator import GoalEvaluator
from webot.workflows.progress_tracker import ProgressTracker
from webot.workflows.recovery_engine import RecoveryEngine
from webot.workflows.task_interpreter import TaskInterpreter


class BrowserController:
    """Minimal Playwright lifecycle wrapper (mirrors main.py / runtime tests)."""

    def __init__(self, headless: bool = False) -> None:
        self.headless = headless
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Browser not initialized. Call open() first.")
        return self._page

    async def open(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        self._context = await self._browser.new_context(user_agent=settings.browser.user_agent)
        self._page = await self._context.new_page()
        self._page.set_default_timeout(settings.browser.timeout_ms)
        self._page.set_default_navigation_timeout(settings.browser.navigation_timeout_ms)

    async def goto(self, url: str) -> None:
        await self.page.goto(url, wait_until="domcontentloaded")

    async def click(self, selector: str) -> None:
        await self.page.click(selector)

    async def fill(self, selector: str, value: str) -> None:
        await self.page.fill(selector, value)

    async def extract_text(self, selector: str) -> str:
        return (await self.page.locator(selector).first.inner_text()).strip()

    async def close(self) -> None:
        errors: list[str] = []
        for closer, label in (
            (self._context.close if self._context else None, "context"),
            (self._browser.close if self._browser else None, "browser"),
            (self._playwright.stop if self._playwright else None, "playwright"),
        ):
            if closer is None:
                continue
            try:
                await closer()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{label}: {exc}")
        self._context = self._browser = self._playwright = self._page = None
        if errors:
            raise RuntimeError("; ".join(errors))


async def run_prompt(prompt: str, *, headless: bool = False, max_steps: int = 10) -> dict[str, Any]:
    """Resolve a starting URL from `prompt`, then run AgentLoop against it."""
    logger = get_logger(__name__)
    interpreter = TaskInterpreter()
    start_url = interpreter.extract_start_url(prompt)
    if not start_url:
        raise ValueError(
            "Could not determine a starting site from the prompt. "
            "Mention a URL or a known site name, e.g. "
            "\"search for python internships on duckduckgo\" or "
            "\"open https://example.com and ...\"."
        )

    browser = BrowserController(headless=headless)
    out_dir = PROJECT_ROOT / "artifacts" / "runtime" / "cli"
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        await browser.open()
        logger.info("navigating", extra={"url": start_url})
        await browser.goto(start_url)

        ollama_info = resolve_ollama_config_sources(settings)
        ollama_client = OllamaClient(
            base_url=ollama_info["base_url"],
            model=ollama_info["model"],
            timeout_seconds=float(settings.ollama.timeout_seconds),
            max_retries=2,
            retry_delay_seconds=0.5,
            model_source=ollama_info["model_source"],
            base_url_source=ollama_info["base_url_source"],
        )
        llm_available = ollama_client.is_available()

        dom_extractor = DomExtractor()
        session_memory = SessionMemory(max_actions=400)
        action_executor = ActionExecutor(browser_controller=browser, max_retries=2, retry_delay_seconds=0.5)
        action_validator = ActionValidator(timeout_ms=settings.browser.timeout_ms)
        dom_delta = DomDelta(max_items_per_bucket=12)
        recovery_engine = RecoveryEngine(max_retries=2, llm_client=ollama_client)
        progress_tracker = ProgressTracker(window_size=20)
        goal_evaluator = GoalEvaluator(llm_client=ollama_client)
        decision_engine = DecisionEngine(ollama_client=ollama_client)

        agent_loop = AgentLoop(
            page=browser.page,
            dom_extractor=dom_extractor,
            decision_engine=decision_engine,
            action_executor=action_executor,
            action_validator=action_validator,
            session_memory=session_memory,
            dom_delta=dom_delta,
            recovery_engine=recovery_engine,
            max_steps=max_steps,
            max_consecutive_failures=3,
            complete_on_extract_text=True,
            llm_enabled=llm_available,
            degraded_mode=not llm_available,
            progress_tracker=progress_tracker,
            goal_evaluator=goal_evaluator,
        )

        loop_result = await agent_loop.run(prompt)

        latest_dom = await dom_extractor.extract_all(browser.page)
        progress_report = progress_tracker.evaluate_progress()
        extracted_text = ""
        final_data = loop_result.get("final_data") if isinstance(loop_result, dict) else None
        if isinstance(final_data, dict) and isinstance(final_data.get("text"), str):
            extracted_text = final_data["text"]

        goal_eval = goal_evaluator.evaluate(
            user_goal=prompt,
            current_url=browser.page.url,
            dom_summary=latest_dom,
            extracted_text=extracted_text,
            recent_actions=session_memory.get_recent_actions(limit=12),
            progress_report=progress_report,
        )

        screenshot_path = out_dir / "last_run.png"
        await browser.page.screenshot(path=str(screenshot_path), full_page=True)

        summary = {
            "prompt": prompt,
            "start_url": start_url,
            "status": loop_result.get("status") if isinstance(loop_result, dict) else "unknown",
            "steps_executed": loop_result.get("steps_executed") if isinstance(loop_result, dict) else None,
            "final_url": browser.page.url,
            "llm_available": llm_available,
            "degraded_mode": not llm_available,
            "completion_confidence": goal_eval.get("completion_confidence"),
            "screenshot": str(screenshot_path),
        }
        (out_dir / "last_run_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return summary
    finally:
        await browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="weBot: run a free-text browser task autonomously.")
    parser.add_argument("prompt", help="Natural-language task, e.g. 'search for X on duckduckgo'")
    parser.add_argument("--headless", action="store_true", help="Run without a visible browser window")
    parser.add_argument("--max-steps", type=int, default=10)
    args = parser.parse_args()

    setup_logging(
        logger_name=settings.logging.logger_name,
        level=settings.logging.level,
        log_file=settings.logging.log_file,
    )

    summary = asyncio.run(run_prompt(args.prompt, headless=args.headless, max_steps=args.max_steps))
    print(json.dumps(summary, indent=2))
    sys.exit(0 if summary.get("status") == "completed" else 1)


if __name__ == "__main__":
    main()
