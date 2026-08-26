from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
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
from webot.observability.execution_trace import ExecutionTrace
from webot.workflows.action_executor import ActionExecutor
from webot.workflows.action_validator import ActionValidator
from webot.workflows.agent_loop import AgentLoop
from webot.workflows.goal_evaluator import GoalEvaluator
from webot.workflows.progress_tracker import ProgressTracker
from webot.workflows.recovery_engine import RecoveryEngine

from tests.runtime.result_validator import ResultValidator, WorkflowValidationInput


@dataclass(slots=True)
class BrowserController:
    headless: bool = False
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
        await self.page.click(selector)

    async def fill(self, selector: str, value: str) -> None:
        await self.page.fill(selector, value)

    async def extract_text(self, selector: str) -> str:
        return (await self.page.locator(selector).first.inner_text()).strip()


async def extract_top_5_duckduckgo_results(page: Page) -> list[dict[str, str]]:
    js = """
    () => {
      const selectors = [
        '[data-testid="result"] h2 a',
        '.react-results--main .result h2 a',
        '.results .result h2 a',
        'article h2 a',
      ];
      let nodes = [];
      for (const sel of selectors) {
        nodes = Array.from(document.querySelectorAll(sel));
        if (nodes.length > 0) break;
      }
      return nodes.slice(0, 5).map((a) => ({
        title: (a.innerText || a.textContent || '').trim(),
        url: (a.href || '').trim(),
      })).filter((x) => x.title && x.url);
    }
    """
    raw = await page.evaluate(js)
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        url = item.get("url")
        if isinstance(title, str) and isinstance(url, str) and title.strip() and url.strip():
            out.append({"title": title.strip(), "url": url.strip()})
    return out[:5]


async def run_duckduckgo_search_test(*, headless: bool = False) -> int:
    started = datetime.now(timezone.utc)
    out_dir = PROJECT_ROOT / "artifacts" / "runtime" / "duckduckgo_search"
    (out_dir / "screenshots").mkdir(parents=True, exist_ok=True)
    (out_dir / "traces").mkdir(parents=True, exist_ok=True)
    (out_dir / "reports").mkdir(parents=True, exist_ok=True)

    browser = BrowserController(headless=headless)
    trace = ExecutionTrace(
        goal="Open DuckDuckGo, search for Python internships, and extract top 5 results",
        trace_id=f"duckduckgo-search-{started.strftime('%Y%m%d-%H%M%S')}",
        metadata={"headless": headless, "workflow": "search"},
    )

    screenshot_after = out_dir / "screenshots" / "after_run.png"
    screenshot_final = out_dir / "screenshots" / "final.png"
    report_path = out_dir / "reports" / "duckduckgo_search_report.json"

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
        session_memory = SessionMemory(max_actions=400)
        action_executor = ActionExecutor(browser_controller=browser, max_retries=2, retry_delay_seconds=0.5)
        action_validator = ActionValidator(timeout_ms=settings.browser.timeout_ms)
        dom_delta = DomDelta(max_items_per_bucket=12)
        recovery_engine = RecoveryEngine(max_retries=2, llm_client=ollama_client)
        progress_tracker = ProgressTracker(window_size=20)
        goal_evaluator = GoalEvaluator(llm_client=ollama_client)

        await browser.goto("https://duckduckgo.com")
        goal = "Search for Python internships"
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
            llm_enabled=llm_available,
            degraded_mode=not llm_available,
        )
        loop_result = await agent_loop.run(goal)
        degraded_mode = bool(agent_loop.degraded_mode)
        history = loop_result.get("history", []) if isinstance(loop_result, dict) else []
        if not isinstance(history, list):
            history = []

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

        status = str(loop_result.get("status", "failed")) if isinstance(loop_result, dict) else "failed"
        termination_reason = str(loop_result.get("error", "")) if isinstance(loop_result, dict) else ""
        extracted_text = ""
        if isinstance(loop_result.get("final_data"), dict):
            text = loop_result["final_data"].get("text")
            if isinstance(text, str):
                extracted_text = text

        await browser.page.wait_for_timeout(1200)
        top_5_results = await extract_top_5_duckduckgo_results(browser.page)
        if top_5_results:
            extracted_text += "\n" + "\n".join(item["title"] for item in top_5_results)

        await browser.page.screenshot(path=str(screenshot_after), full_page=True)
        latest_dom = await dom_extractor.extract_all(browser.page)
        progress_report = progress_tracker.evaluate_progress()
        goal_eval = goal_evaluator.evaluate(
            user_goal=goal,
            current_url=browser.page.url,
            dom_summary=latest_dom,
            extracted_text=extracted_text,
            recent_actions=session_memory.get_recent_actions(limit=12),
            progress_report=progress_report,
        )
        completion_confidence = float(goal_eval.get("completion_confidence", 0.0) or 0.0)

        validator = ResultValidator(min_pass_score=0.65)
        validation_input: WorkflowValidationInput = {
            "workflow_type": "search",
            "goal": goal,
            "status": status,
            "current_url": browser.page.url,
            "extracted_text": extracted_text,
            "completion_confidence": completion_confidence,
            "validation_reasons": [str(item.get("reason", "")) for item in history if isinstance(item, dict)],
            "progress_summary": str(progress_report.get("summary", "")),
            "anti_bot_detections": sum(1 for item in history if isinstance(item, dict) and bool(item.get("anti_bot_detected", False))),
            "trace_steps": [item for item in history if isinstance(item, dict)],
        }
        validation = validator.validate(validation_input)
        trace.finalize(status="completed" if validation["validation_passed"] else "failed", termination_reason=termination_reason or validation["validation_reason"])
        trace_path = trace.export_trace(out_dir / "traces" / f"{trace.trace_id}.json")

        report: dict[str, Any] = {
            "test_name": "duckduckgo_search_python_internships",
            "workflow": "search",
            "headless": headless,
            "started_at": started.isoformat(),
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "termination_reason": termination_reason,
            "completion_confidence": round(completion_confidence, 4),
            "validation": validation,
            "metrics": {
                "execution_duration_seconds": round(perf_counter() - t0, 3),
                "total_steps": len(history),
                "retries": sum(int(v) for v in session_memory.retry_counts.values() if isinstance(v, int)),
                "recovery_attempts": sum(1 for item in history if isinstance(item, dict) and isinstance(item.get("recovery"), dict) and bool(item.get("recovery"))),
                "stagnation_events": sum(1 for item in history if isinstance(item, dict) and str(item.get("reason", "")) in {"click_no_observable_change", "fill_value_mismatch", "goto_validation_failed"}),
                "anti_bot_detections": sum(1 for item in history if isinstance(item, dict) and bool(item.get("anti_bot_detected", False))),
                "llm_available": llm_available,
                "degraded_mode": degraded_mode,
            },
            "provider_metrics": agent_loop.search_state_machine.provider_metrics() if agent_loop.search_state_machine else [],
            "top_5_results": top_5_results,
            "artifacts": {
                "trace_path": str(trace_path),
                "screenshot_after_run": str(screenshot_after),
                "screenshot_final": str(screenshot_final),
                "report_path": str(report_path),
            },
            "trace_terminal_summary": trace.render_terminal_summary(),
            "trace_step_timeline": trace.render_step_timeline(),
            "progress_report": progress_report,
        }
        report_path.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
        await browser.page.screenshot(path=str(screenshot_final), full_page=True)

        print("=== TEST SUMMARY ===")
        print(f"validation_passed={validation['validation_passed']}")
        print(f"confidence_score={validation['confidence_score']:.3f}")
        print(f"status={status}")
        print(f"steps={len(history)}")
        print(f"trace={trace_path}")
        print(f"report={report_path}")

        return 0 if validation["validation_passed"] else 1

    except Exception as exc:  # noqa: BLE001
        import traceback
        trace.finalize(status="failed", termination_reason="runner_exception")
        trace_path = trace.export_trace(out_dir / "traces" / f"{trace.trace_id}.json")
        failure = {
            "test_name": "duckduckgo_search_python_internships",
            "status": "failed",
            "error": str(exc),
            "trace_path": str(trace_path),
            "execution_duration_seconds": round(perf_counter() - t0, 3),
        }
        report_path.write_text(json.dumps(failure, ensure_ascii=True, indent=2), encoding="utf-8")
        print("Test failed with exception:")
        traceback.print_exc()
        print(f"trace={trace_path}")
        print(f"report={report_path}")
        return 1
    finally:
        try:
            if browser._page is not None:
                await browser.page.screenshot(path=str(screenshot_final), full_page=True)
        except Exception:
            pass
        try:
            await browser.close()
        except Exception:
            pass


def main() -> None:
    exit_code = asyncio.run(run_duckduckgo_search_test(headless=False))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
