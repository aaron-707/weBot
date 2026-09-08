from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Literal, TypedDict

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
from webot.workflows.form_filler import FormFiller
from webot.workflows.goal_evaluator import GoalEvaluator
from webot.workflows.progress_tracker import ProgressTracker
from webot.workflows.recovery_engine import RecoveryEngine


WorkflowType = Literal["search", "form_fill", "login", "wikipedia"]


class RuntimeMetrics(TypedDict):
    total_steps: int
    retries: int
    recovery_attempts: int
    stagnation_events: int
    anti_bot_detections: int
    execution_duration_seconds: float
    llm_available: bool
    degraded_mode: bool
    infra_failure: bool


@dataclass(slots=True)
class WorkflowTestCase:
    name: str
    workflow_type: WorkflowType
    goal: str
    start_url: str = ""
    max_steps: int = 10
    min_completion_confidence: float = 0.65
    expected_status: set[str] = field(default_factory=lambda: {"completed"})
    profile: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class WorkflowTestResult:
    name: str
    workflow_type: WorkflowType
    success: bool
    status: str
    termination_reason: str
    completion_confidence: float
    metrics: RuntimeMetrics
    trace_path: str
    screenshot_path: str
    started_at: str
    ended_at: str
    llm_available: bool
    degraded_mode: bool
    infra_failure: bool
    error: str = ""
    provider: str = ""
    provider_metrics: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class SuiteReport:
    suite_name: str
    started_at: str
    ended_at: str
    total: int
    passed: int
    failed: int
    results: list[WorkflowTestResult]


class BrowserController:
    """Minimal runtime browser controller used by the runner."""

    def __init__(self, *, headless: bool) -> None:
        self._headless = headless
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Browser page is not initialized")
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
        return (await self.page.locator(selector).first.inner_text()).strip()


@dataclass(slots=True)
class RuntimeTestRunner:
    """Autonomous workflow test runner with tracing, screenshots, and reports."""

    output_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "artifacts" / "runtime")
    headless: bool = True

    async def run_test(self, case: WorkflowTestCase) -> WorkflowTestResult:
        started_dt = datetime.now(timezone.utc)
        started_at = started_dt.isoformat()
        t0 = perf_counter()

        test_dir = self.output_dir / case.name
        trace_dir = test_dir / "traces"
        screenshot_dir = test_dir / "screenshots"
        trace_dir.mkdir(parents=True, exist_ok=True)
        screenshot_dir.mkdir(parents=True, exist_ok=True)

        trace = ExecutionTrace(
            goal=case.goal,
            trace_id=f"{case.name}-{started_dt.strftime('%Y%m%d-%H%M%S')}",
            metadata={"workflow_type": case.workflow_type, "start_url": case.start_url},
        )

        browser = BrowserController(headless=self.headless)
        screenshot_path = screenshot_dir / f"{case.name}.png"

        status = "failed"
        termination_reason = ""
        completion_confidence = 0.0
        metrics: RuntimeMetrics = {
            "total_steps": 0,
            "retries": 0,
            "recovery_attempts": 0,
            "stagnation_events": 0,
            "anti_bot_detections": 0,
            "execution_duration_seconds": 0.0,
            "llm_available": False,
            "degraded_mode": False,
            "infra_failure": False,
        }
        error_message = ""
        llm_available = False
        degraded_mode = False
        infra_failure = False

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
            session_memory = SessionMemory(max_actions=500)
            action_executor = ActionExecutor(browser_controller=browser, max_retries=2, retry_delay_seconds=0.5)
            action_validator = ActionValidator(timeout_ms=settings.browser.timeout_ms)
            dom_delta = DomDelta(max_items_per_bucket=12)
            recovery_engine = RecoveryEngine(max_retries=2, llm_client=ollama_client)
            progress_tracker = ProgressTracker(window_size=24)
            goal_evaluator = GoalEvaluator(llm_client=ollama_client)
            form_filler = FormFiller(llm_client=ollama_client)

            goal = self._build_goal(case)
            if case.start_url.strip():
                await browser.goto(case.start_url.strip())
                if "demoqa.com" in case.start_url:
                    try:
                        await browser.page.evaluate(
                            """
                            () => {
                              const ids = ['fixedban', 'adplus-anchor'];
                              for (const id of ids) {
                                const el = document.getElementById(id);
                                if (el) el.remove();
                              }
                              const footer = document.querySelector('footer');
                              if (footer) footer.style.position = 'static';
                            }
                            """
                        )
                    except Exception:
                        pass

            if case.workflow_type == "form_fill" and case.profile:
                await form_filler.fill(browser.page, case.profile)

            agent_loop = AgentLoop(
                page=browser.page,
                dom_extractor=dom_extractor,
                decision_engine=decision_engine,
                action_executor=action_executor,
                action_validator=action_validator,
                session_memory=session_memory,
                dom_delta=dom_delta,
                recovery_engine=recovery_engine,
                max_steps=max(int(case.max_steps), 1),
                max_consecutive_failures=3,
                complete_on_extract_text=True,
                llm_enabled=llm_available,
                degraded_mode=not llm_available,
            )

            loop_result = await agent_loop.run(goal)
            degraded_mode = bool(agent_loop.degraded_mode)
            history = loop_result.get("history", []) if isinstance(loop_result, dict) else []
            status = str(loop_result.get("status", "failed")) if isinstance(loop_result, dict) else "failed"
            termination_reason = str(loop_result.get("error", "")) if isinstance(loop_result, dict) else ""

            extracted_text = ""
            if isinstance(loop_result.get("final_data"), dict):
                maybe_text = loop_result["final_data"].get("text")
                if isinstance(maybe_text, str):
                    extracted_text = maybe_text

            for entry in history:
                if not isinstance(entry, dict):
                    continue
                action = entry.get("action", {}) if isinstance(entry.get("action"), dict) else {}
                validation = entry.get("validation", {}) if isinstance(entry.get("validation"), dict) else {}
                details = validation.get("details", {}) if isinstance(validation.get("details"), dict) else {}
                interstitial = details.get("interstitial", {}) if isinstance(details.get("interstitial"), dict) else {}
                dom_delta_info = entry.get("dom_delta", {}) if isinstance(entry.get("dom_delta"), dict) else {}

                dom_signature = (
                    f"n={dom_delta_info.get('new', 0)}|r={dom_delta_info.get('removed', 0)}|"
                    f"t={dom_delta_info.get('text_changed', 0)}|f={dom_delta_info.get('form_changed', 0)}"
                )
                dom_delta_size = (
                    int(dom_delta_info.get("new", 0) or 0)
                    + int(dom_delta_info.get("removed", 0) or 0)
                    + int(dom_delta_info.get("text_changed", 0) or 0)
                )

                progress_tracker.record_step(
                    step=int(entry.get("step", 0) or 0),
                    action=str(action.get("action", "")),
                    selector=str(action.get("selector", "")),
                    success=bool(entry.get("success", False)),
                    retries_used=0,
                    url=str(details.get("current_url", browser.page.url)),
                    dom_signature=dom_signature,
                    dom_delta_summary=json.dumps(dom_delta_info, ensure_ascii=True, separators=(",", ":")),
                    dom_delta_size=dom_delta_size,
                    interstitial_type=str(interstitial.get("detection_type", "")),
                    anti_bot_detected=bool(entry.get("anti_bot_detected", False)),
                    fill_value=str(action.get("value", "")),
                )

                progress_report = progress_tracker.evaluate_progress()
                step_goal_eval = goal_evaluator.evaluate(
                    user_goal=goal,
                    current_url=str(details.get("current_url", browser.page.url)),
                    dom_summary={},
                    extracted_text=extracted_text,
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
                    goal_evaluation=step_goal_eval,
                    recovery=entry.get("recovery", {}) if isinstance(entry.get("recovery"), dict) else {},
                    termination_reason=termination_reason if int(entry.get("step", 0) or 0) == len(history) else "",
                    success=bool(entry.get("success", False)),
                )

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

            if "loop_guard" in termination_reason and len(history) > 0:
                trace.record_loop_guard(
                    step=int(len(history) + 1),
                    guard_reason=termination_reason,
                    url=browser.page.url,
                )

            try:
                await browser.page.screenshot(path=str(screenshot_path), timeout=5000)
            except Exception:
                pass

            metrics = self._compute_metrics(
                history=history,
                session_memory=session_memory,
                progress_report=progress_report,
                duration_seconds=perf_counter() - t0,
                llm_available=llm_available,
                degraded_mode=degraded_mode,
            )
            success = self._is_success(case, status, completion_confidence)
            # Only treat LLM unavailability as an infra failure when the workflow
            # itself did not succeed — degraded_mode_rate / llm_available_rate
            # already capture the LLM-down signal without penalising good runs.
            infra_failure = (not llm_available) and bool(history) and not success
            metrics["infra_failure"] = infra_failure
            trace.finalize(status=self._normalize_trace_status(status), termination_reason=termination_reason)

        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            success = False
            status = "failed"
            error_message = str(exc)
            termination_reason = termination_reason or f"runner_exception: {error_message}"
            infra_failure = "ollama" in error_message.lower() and "unavailable" in error_message.lower()
            metrics["infra_failure"] = infra_failure
            metrics["execution_duration_seconds"] = round(perf_counter() - t0, 3)
            trace.finalize(status="failed", termination_reason=termination_reason)
            try:
                if browser._page is not None:
                    await browser.page.screenshot(path=str(screenshot_path), full_page=True)
            except Exception:
                pass
        finally:
            try:
                await browser.close()
            except Exception:
                pass

        trace_path = trace.export_trace(trace_dir / f"{trace.trace_id}.json")
        ended_at = datetime.now(timezone.utc).isoformat()

        prov_metrics: list[dict[str, Any]] = []
        try:
            if agent_loop is not None and getattr(agent_loop, "search_state_machine", None) is not None:
                prov_metrics = list(agent_loop.search_state_machine.provider_metrics())
        except Exception:
            pass

        provider_name = ""
        if prov_metrics:
            provider_name = str(prov_metrics[0].get("provider", ""))
        if not provider_name:
            if case.workflow_type == "search":
                if "duckduckgo" in case.start_url or "duckduckgo" in case.name:
                    provider_name = "duckduckgo"
                elif "wikipedia" in case.start_url or "wikipedia" in case.name:
                    provider_name = "wikipedia"
                else:
                    provider_name = "duckduckgo"
            elif case.workflow_type == "wikipedia":
                provider_name = "wikipedia"
            elif case.workflow_type == "form_fill":
                provider_name = "demoqa"
            elif case.workflow_type == "login":
                provider_name = "the_internet"
            else:
                provider_name = case.workflow_type

        if not prov_metrics and provider_name:
            prov_metrics = [
                {
                    "provider": provider_name,
                    "success": success,
                    "completion_confidence": round(completion_confidence, 4),
                    "anti_bot_detections": int(metrics.get("anti_bot_detections", 0)),
                    "retries": int(metrics.get("retries", 0)),
                    "duration_seconds": float(metrics.get("execution_duration_seconds", 0.0)),
                    "reason": termination_reason,
                }
            ]

        return WorkflowTestResult(
            name=case.name,
            workflow_type=case.workflow_type,
            success=success,
            status=status,
            termination_reason=termination_reason,
            completion_confidence=round(completion_confidence, 4),
            metrics=metrics,
            trace_path=str(trace_path),
            screenshot_path=str(screenshot_path),
            started_at=started_at,
            ended_at=ended_at,
            llm_available=llm_available,
            degraded_mode=degraded_mode,
            infra_failure=infra_failure,
            error=error_message,
            provider=provider_name,
            provider_metrics=prov_metrics,
        )

    async def run_suite(self, suite_name: str, cases: list[WorkflowTestCase]) -> SuiteReport:
        started_at = datetime.now(timezone.utc).isoformat()
        results: list[WorkflowTestResult] = []

        for case in cases:
            result = await self.run_test(case)
            results.append(result)

        passed = sum(1 for item in results if item.success)
        failed = len(results) - passed
        ended_at = datetime.now(timezone.utc).isoformat()

        return SuiteReport(
            suite_name=suite_name,
            started_at=started_at,
            ended_at=ended_at,
            total=len(results),
            passed=passed,
            failed=failed,
            results=results,
        )

    def export_report(self, suite: SuiteReport, path: str | Path) -> Path:
        report_path = Path(path)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "suite_name": suite.suite_name,
            "started_at": suite.started_at,
            "ended_at": suite.ended_at,
            "total": suite.total,
            "passed": suite.passed,
            "failed": suite.failed,
            "results": [self._serialize_result(item) for item in suite.results],
            "terminal_summary": self.render_terminal_summary(suite),
        }
        report_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
        return report_path

    def render_terminal_summary(self, suite: SuiteReport) -> str:
        lines = [
            f"suite={suite.suite_name}",
            f"total={suite.total} passed={suite.passed} failed={suite.failed}",
            f"started_at={suite.started_at}",
            f"ended_at={suite.ended_at}",
        ]

        for item in suite.results:
            lines.append(
                " | ".join(
                    [
                        item.name,
                        "PASS" if item.success else "FAIL",
                        f"status={item.status}",
                        f"conf={item.completion_confidence:.2f}",
                        f"steps={item.metrics['total_steps']}",
                        f"duration={item.metrics['execution_duration_seconds']:.2f}s",
                        f"llm={str(item.llm_available).lower()}",
                        f"degraded={str(item.degraded_mode).lower()}",
                    ]
                )
            )

        return "\n".join(lines)

    @staticmethod
    def _serialize_result(item: WorkflowTestResult) -> dict[str, Any]:
        payload = asdict(item)
        payload["metrics"] = dict(item.metrics)
        return payload

    @staticmethod
    def _build_goal(case: WorkflowTestCase) -> str:
        goal = case.goal.strip()
        if case.start_url.strip() and case.start_url.strip() not in goal:
            return f"Open {case.start_url.strip()} then {goal}".strip()
        return goal

    @staticmethod
    def _is_success(case: WorkflowTestCase, status: str, confidence: float) -> bool:
        if status not in case.expected_status:
            return False
        return confidence >= case.min_completion_confidence

    @staticmethod
    def _normalize_trace_status(status: str) -> Literal[
        "running", "completed", "failed", "max_steps_reached", "cancelled", "blocked_by_anti_bot"
    ]:
        if status in {"completed", "failed", "max_steps_reached", "cancelled", "blocked_by_anti_bot"}:
            return status  # type: ignore[return-value]
        return "failed"

    @staticmethod
    def _compute_metrics(
        *,
        history: list[Any],
        session_memory: SessionMemory,
        progress_report: dict[str, Any],
        duration_seconds: float,
        llm_available: bool,
        degraded_mode: bool,
    ) -> RuntimeMetrics:
        recovery_attempts = 0
        anti_bot_detections = 0
        stagnation_events = 0

        for entry in history:
            if not isinstance(entry, dict):
                continue
            recovery = entry.get("recovery", {})
            if isinstance(recovery, dict) and recovery:
                recovery_attempts += 1

            if bool(entry.get("anti_bot_detected", False)):
                anti_bot_detections += 1

            reason = str(entry.get("reason", ""))
            if reason in {"click_no_observable_change", "fill_value_mismatch", "goto_validation_failed"}:
                stagnation_events += 1

        retries = sum(int(v) for v in session_memory.retry_counts.values() if isinstance(v, int))

        if str(progress_report.get("termination_recommendation", "")) in {"warn", "terminate"}:
            stagnation_events += 1

        return {
            "total_steps": len(history),
            "retries": retries,
            "recovery_attempts": recovery_attempts,
            "stagnation_events": stagnation_events,
            "anti_bot_detections": anti_bot_detections,
            "execution_duration_seconds": round(duration_seconds, 3),
            "llm_available": llm_available,
            "degraded_mode": degraded_mode,
            "infra_failure": False,
        }


async def _demo() -> None:
    runner = RuntimeTestRunner(headless=True)
    suite = await runner.run_suite(
        "sample-suite",
        [
            WorkflowTestCase(
                name="search_google_python_internships",
                workflow_type="search",
                goal="Open Google and search for Python internships",
                max_steps=10,
            )
        ],
    )
    report_path = runner.export_report(suite, PROJECT_ROOT / "artifacts" / "runtime" / "suite_report.json")
    print(runner.render_terminal_summary(suite))
    print(f"\nreport={report_path}")


def main() -> None:
    asyncio.run(_demo())


if __name__ == "__main__":
    main()
