from __future__ import annotations

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
from webot.intelligence.dom_delta import DomDelta
from webot.intelligence.dom_extractor import DomExtractor
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


class BrowserController:
    """Minimal controller for manual integration workflow testing."""

    def __init__(self, *, headless: bool) -> None:
        self._headless = headless
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Browser not initialized")
        return self._page

    async def open(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self._headless)
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
        self._context = None
        self._browser = None
        self._playwright = None
        self._page = None

    async def goto(self, url: str) -> None:
        await self.page.goto(url, wait_until="domcontentloaded")

    async def click(self, selector: str) -> None:
        await self.page.click(selector)

    async def fill(self, selector: str, value: str) -> None:
        await self.page.fill(selector, value)

    async def extract_text(self, selector: str) -> str:
        text = await self.page.locator(selector).first.inner_text()
        return text.strip()


async def run_manual_workflow() -> int:
    log_file = PROJECT_ROOT / "logs" / "manual_google_search.log"
    setup_logging(
        logger_name=settings.logging.logger_name,
        level=settings.logging.level,
        log_file=log_file,
    )
    logger = get_logger(__name__)

    browser = BrowserController(headless=False)

    try:
        await browser.open()

        dom_extractor = DomExtractor()
        ollama_info = resolve_ollama_config_sources(settings)
        logger.info(
            "ollama_runtime_config",
            extra={
                "active_base_url": ollama_info["base_url"],
                "active_model": ollama_info["model"],
                "base_url_source": ollama_info["base_url_source"],
                "model_source": ollama_info["model_source"],
            },
        )

        ollama_client = OllamaClient(
            base_url=ollama_info["base_url"],
            model=ollama_info["model"],
            timeout_seconds=float(settings.ollama.timeout_seconds),
            max_retries=2,
            retry_delay_seconds=0.5,
            model_source=ollama_info["model_source"],
            base_url_source=ollama_info["base_url_source"],
        )
        decision_engine = DecisionEngine(ollama_client=ollama_client)
        session_memory = SessionMemory(max_actions=400)
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
            max_steps=10,
            max_consecutive_failures=3,
            complete_on_extract_text=True,
        )

        goal = "Open Google and search for Python internships"
        logger.info("manual_test_started", extra={"goal": goal})

        result = await agent_loop.run(goal)

        for entry in result.get("history", []):
            action = entry.get("action", {}) if isinstance(entry.get("action"), dict) else {}
            dom_delta_info = entry.get("dom_delta", {}) if isinstance(entry.get("dom_delta"), dict) else {}
            dom_delta_size = int(dom_delta_info.get("new", 0)) + int(dom_delta_info.get("removed", 0)) + int(dom_delta_info.get("text_changed", 0))

            progress_tracker.record_step(
                step=int(entry.get("step", 0) or 0),
                action=str(action.get("action", "")),
                selector=str(action.get("selector", "")),
                success=bool(entry.get("success", False)),
                retries_used=0,
                url=browser.page.url,
                dom_signature=(
                    f"n={dom_delta_info.get('new', 0)}|r={dom_delta_info.get('removed', 0)}|"
                    f"t={dom_delta_info.get('text_changed', 0)}|f={dom_delta_info.get('form_changed', 0)}"
                ),
                dom_delta_summary=json.dumps(dom_delta_info, ensure_ascii=True, separators=(",", ":")),
                dom_delta_size=dom_delta_size,
            )

        progress_report = progress_tracker.evaluate_progress()

        latest_dom = await dom_extractor.extract_all(browser.page)
        extracted_text = ""
        if isinstance(result.get("final_data"), dict):
            maybe_text = result["final_data"].get("text")
            if isinstance(maybe_text, str):
                extracted_text = maybe_text

        goal_eval = goal_evaluator.evaluate(
            user_goal=goal,
            current_url=browser.page.url,
            dom_summary=latest_dom,
            extracted_text=extracted_text,
            recent_actions=session_memory.get_recent_actions(limit=10),
            progress_report=progress_report,
        )

        completion_confidence = float(goal_eval.get("completion_confidence", 0.0))

        print("\n=== FINAL RESULT ===")
        print(json.dumps(result, indent=2, ensure_ascii=True))

        print("\n=== COMPLETION CONFIDENCE ===")
        print(f"{completion_confidence:.3f}")

        print("\n=== STEP HISTORY ===")
        print(json.dumps(result.get("history", []), indent=2, ensure_ascii=True))

        print("\n=== PROGRESS REPORT ===")
        print(json.dumps(progress_report, indent=2, ensure_ascii=True))

        print("\n=== GOAL EVALUATION ===")
        print(json.dumps(goal_eval, indent=2, ensure_ascii=True))

        logger.info(
            "manual_test_finished",
            extra={
                "status": result.get("status", "unknown"),
                "completion_confidence": completion_confidence,
                "log_file": str(log_file),
            },
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("manual_test_failed", extra={"error": str(exc)})
        print("\nManual workflow failed:")
        print(str(exc))
        return 1
    finally:
        try:
            await browser.close()
        except Exception as close_exc:  # noqa: BLE001
            logger.exception("manual_test_browser_close_failed", extra={"error": str(close_exc)})


def main() -> None:
    exit_code = asyncio.run(run_manual_workflow())
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
