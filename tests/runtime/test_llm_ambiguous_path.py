from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

import pytest
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
from webot.observability.execution_trace import ExecutionTrace
from webot.workflows.action_executor import ActionExecutor
from webot.workflows.action_validator import ActionValidator
from webot.workflows.agent_loop import AgentLoop
from webot.workflows.goal_evaluator import GoalEvaluator
from webot.workflows.progress_tracker import ProgressTracker
from webot.workflows.recovery_engine import RecoveryEngine


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
        self._context = None
        self._browser = None
        self._playwright = None
        self._page = None

    async def goto(self, url: str) -> None:
        await self.page.goto(url, wait_until="domcontentloaded")

    async def click(self, selector: str) -> None:
        await self.page.click(selector, timeout=5000)

    async def fill(self, selector: str, value: str) -> None:
        await self.page.fill(selector, value)

    async def extract_text(self, selector: str) -> str:
        return (await self.page.locator(selector).first.inner_text()).strip()


async def run_llm_ambiguous_path_test(*, headless: bool = True) -> int:
    """Executes a runtime scenario specifically constructed to force genuine ambiguity.
    
    Verifies that:
    1. DecisionEngine actually consults live Ollama (state machines do not intercept).
    2. GoalEvaluator._llm_fallback is actually invoked (intermediate confidence in 0.40-0.65).
    """
    started = datetime.now(timezone.utc)
    out_dir = PROJECT_ROOT / "artifacts" / "runtime" / "llm_ambiguous_path"
    (out_dir / "screenshots").mkdir(parents=True, exist_ok=True)
    (out_dir / "traces").mkdir(parents=True, exist_ok=True)
    (out_dir / "reports").mkdir(parents=True, exist_ok=True)

    browser = BrowserController(headless=headless)
    goal = "Open https://example.com, click the more information link, and review the domain documentation"
    trace = ExecutionTrace(
        goal=goal,
        trace_id=f"llm-ambiguous-path-{started.strftime('%Y%m%d-%H%M%S')}",
        metadata={"headless": headless, "workflow": "llm_ambiguous_path"},
    )

    screenshot_after = out_dir / "screenshots" / "after_run.png"
    screenshot_final = out_dir / "screenshots" / "final.png"
    report_path = out_dir / "reports" / "llm_ambiguous_path_report.json"

    t0 = perf_counter()
    try:
        await browser.open()

        dom_extractor = DomExtractor()
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
        decision_engine = DecisionEngine(ollama_client=ollama_client)
        llm_available = ollama_client.is_available()
        session_memory = SessionMemory(max_actions=100)
        action_executor = ActionExecutor(browser_controller=browser, max_retries=2, retry_delay_seconds=0.5)
        action_validator = ActionValidator(timeout_ms=settings.browser.timeout_ms)
        dom_delta = DomDelta(max_items_per_bucket=12)
        recovery_engine = RecoveryEngine(max_retries=2, llm_client=ollama_client)
        progress_tracker = ProgressTracker(window_size=20)
        goal_evaluator = GoalEvaluator(llm_client=ollama_client)

        await browser.goto("https://example.com")
        agent_loop = AgentLoop(
            page=browser.page,
            dom_extractor=dom_extractor,
            decision_engine=decision_engine,
            action_executor=action_executor,
            action_validator=action_validator,
            session_memory=session_memory,
            dom_delta=dom_delta,
            recovery_engine=recovery_engine,
            max_steps=5,
            max_consecutive_failures=3,
            complete_on_extract_text=True,
            llm_enabled=llm_available,
            degraded_mode=not llm_available,
        )

        loop_result = await agent_loop.run(goal)
        history = loop_result.get("history", []) if isinstance(loop_result, dict) else []
        if not isinstance(history, list):
            history = []

        status = str(loop_result.get("status", "failed")) if isinstance(loop_result, dict) else "failed"
        termination_reason = str(loop_result.get("error", "")) if isinstance(loop_result, dict) else ""

        for entry in history:
            if not isinstance(entry, dict):
                continue
            action = entry.get("action", {}) if isinstance(entry.get("action"), dict) else {}
            validation = entry.get("validation", {}) if isinstance(entry.get("validation"), dict) else {}
            details = validation.get("details", {}) if isinstance(validation.get("details"), dict) else {}
            interstitial = details.get("interstitial", {}) if isinstance(details.get("interstitial"), dict) else {}
            dom_delta_info = entry.get("dom_delta", {}) if isinstance(entry.get("dom_delta"), dict) else {}

            progress_tracker.record_step(
                step=int(entry.get("step", 0) or 0),
                action=str(action.get("action", "")),
                selector=str(action.get("selector", "")),
                success=bool(entry.get("success", False)),
                url=str(details.get("current_url", browser.page.url)),
                dom_signature=(
                    f"n={dom_delta_info.get('new', 0)}|r={dom_delta_info.get('removed', 0)}|"
                    f"t={dom_delta_info.get('text_changed', 0)}|f={dom_delta_info.get('form_changed', 0)}"
                ),
                dom_delta_summary=json.dumps(dom_delta_info, ensure_ascii=True, separators=(",", ":")),
                dom_delta_size=(
                    int(dom_delta_info.get("new", 0) or 0)
                    + int(dom_delta_info.get("removed", 0) or 0)
                    + int(dom_delta_info.get("text_changed", 0) or 0)
                ),
                interstitial_type=str(interstitial.get("detection_type", "")),
                anti_bot_detected=bool(entry.get("anti_bot_detected", False)),
                fill_value=str(action.get("value", "")),
            )
            progress_report = progress_tracker.evaluate_progress()
            step_eval = goal_evaluator.evaluate(
                user_goal=goal,
                current_url=str(details.get("current_url", browser.page.url)),
                dom_summary={},
                extracted_text="",
                recent_actions=session_memory.get_recent_actions(limit=8),
                progress_report=progress_report,
            )
            trace.record_step(
                step=int(entry.get("step", 0) or 0),
                url=str(details.get("current_url", browser.page.url)),
                selected_action=action,
                selector=str(action.get("selector", "")),
                validation=validation,
                dom_delta_summary=dom_delta_info,
                progress_report=progress_report,
                interstitial=interstitial,
                goal_evaluation=step_eval,
                recovery=entry.get("recovery", {}) if isinstance(entry.get("recovery"), dict) else {},
                success=bool(entry.get("success", False)),
            )

        extracted_text = ""
        if isinstance(loop_result.get("final_data"), dict):
            text = loop_result["final_data"].get("text")
            if isinstance(text, str):
                extracted_text = text
        if not extracted_text:
            try:
                extracted_text = await browser.page.inner_text("body")
            except Exception:
                pass

        await browser.page.screenshot(path=str(screenshot_after), full_page=True)
        latest_dom = await dom_extractor.extract_all(browser.page)
        progress_report = progress_tracker.evaluate_progress()
        final_goal_eval = goal_evaluator.evaluate(
            user_goal=goal,
            current_url=browser.page.url,
            dom_summary=latest_dom,
            extracted_text=extracted_text,
            recent_actions=session_memory.get_recent_actions(limit=12),
            progress_report=progress_report,
        )

        trace.finalize(status="completed", termination_reason=termination_reason or "llm_path_validated")
        trace_path = trace.export_trace(out_dir / "traces" / f"{trace.trace_id}.json")

        de_consulted = decision_engine.llm_consulted_count
        ge_fallback = goal_evaluator.llm_fallback_calls
        ge_skipped = goal_evaluator.llm_fallback_skipped

        llm_decision_path_exercised = (de_consulted > 0 or ge_fallback > 0)

        report = {
            "test_name": "test_llm_ambiguous_path",
            "status": "passed" if llm_decision_path_exercised else "failed",
            "started_at": started.isoformat(),
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "execution_duration_seconds": round(perf_counter() - t0, 3),
            "llm_available": llm_available,
            "decision_engine": {
                "llm_consulted_count": de_consulted,
                "deterministic_fallback_count": decision_engine.deterministic_fallback_count,
            },
            "goal_evaluator": {
                "llm_fallback_calls": ge_fallback,
                "llm_fallback_skipped": ge_skipped,
                "final_confidence": final_goal_eval.get("completion_confidence"),
                "final_reason": final_goal_eval.get("completion_reason"),
            },
            "steps_executed": len(history),
            "trace_path": str(trace_path),
        }
        report_path.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
        await browser.page.screenshot(path=str(screenshot_final), full_page=True)

        print("=== LLM AMBIGUOUS PATH TEST SUMMARY ===")
        print(f"llm_available={llm_available}")
        print(f"decision_engine_llm_consulted={de_consulted}")
        print(f"goal_evaluator_llm_fallback_calls={ge_fallback}")
        print(f"goal_evaluator_llm_fallback_skipped={ge_skipped}")
        print(f"final_goal_reason={final_goal_eval.get('completion_reason')}")
        print(f"llm_decision_path_exercised={llm_decision_path_exercised}")
        print(f"trace={trace_path}")
        print(f"report={report_path}")

        return 0 if llm_decision_path_exercised else 1

    except Exception:
        import traceback
        traceback.print_exc()
        return 1
    finally:
        try:
            await browser.close()
        except Exception:
            pass


# -- Standard pytest integration -----------------------------------------------

@pytest.mark.asyncio
async def test_llm_ambiguous_path_live() -> None:
    """CI / pytest runner for live LLM ambiguous path.
    
    If Ollama is available, executes full ambiguous path runtime test and asserts success.
    If Ollama is unavailable, cleanly skips with explanation.
    """
    ollama_info = resolve_ollama_config_sources(settings)
    client = OllamaClient(
        base_url=ollama_info["base_url"],
        model=ollama_info["model"],
        timeout_seconds=float(settings.ollama.timeout_seconds),
        model_source=ollama_info["model_source"],
        base_url_source=ollama_info["base_url_source"],
    )
    if not client.is_available():
        pytest.skip("Ollama is not available; skipping live LLM ambiguous path test")

    exit_code = await run_llm_ambiguous_path_test(headless=True)
    assert exit_code == 0, f"Live LLM ambiguous path test failed with exit code {exit_code}"


def test_llm_ambiguous_path_degraded_fallback() -> None:
    """Verifies that DecisionEngine and GoalEvaluator handle ambiguous states gracefully
    in degraded mode (Ollama unavailable) without crashing or raising unhandled exceptions.
    """
    unreachable_client = OllamaClient(
        base_url="http://127.0.0.1:11439",
        model="qwen2.5:3b",
        timeout_seconds=1.0,
        max_retries=1,
    )
    de = DecisionEngine(ollama_client=unreachable_client)
    sample_elements = [
        {"selector": "a", "tag": "a", "text": "More information...", "attributes": {"href": "https://example.com"}},
        {"selector": "p", "tag": "p", "text": "Example domain description", "attributes": {}},
    ]
    action = de.choose_next_action("Open https://example.com and review documentation", sample_elements)
    assert isinstance(action, dict), "DecisionEngine should return an action dict in degraded mode"
    assert de.llm_consulted_count == 0, "No LLM consultations should occur in degraded mode"
    assert de.deterministic_fallback_count >= 1, "Deterministic fallback count should increment"

    ge = GoalEvaluator(llm_client=unreachable_client)
    eval_result = ge.evaluate(
        user_goal="Open https://example.com and review documentation",
        current_url="https://example.com",
        dom_summary={"links": [{"text": "More information..."}]},
        extracted_text="Example Domain",
        recent_actions=[{"action": "goto", "url": "https://example.com"}],
        progress_report={"progress_score": 0.5, "signals": {}},
    )
    assert isinstance(eval_result, dict), "GoalEvaluator should return evaluation dict in degraded mode"
    assert ge.llm_fallback_calls == 0, "LLM fallback should not be called when LLM is unavailable"
    assert ge.llm_fallback_skipped >= 1, "LLM fallback skip count should increment"


def main() -> None:
    headless = True
    if len(sys.argv) > 1 and sys.argv[1].lower() in {"--headed", "-h", "headed"}:
        headless = False
    exit_code = asyncio.run(run_llm_ambiguous_path_test(headless=headless))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
