from __future__ import annotations

import asyncio
import json
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.runtime.test_runner import RuntimeTestRunner, WorkflowTestCase, WorkflowTestResult


@dataclass(slots=True)
class BenchmarkConfig:
    iterations: int = 3
    headless: bool = True
    output_dir: Path = PROJECT_ROOT / "artifacts" / "runtime" / "benchmark"


def _safe_mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return float(statistics.fmean(values))


def _rate(num: int, den: int) -> float:
    if den <= 0:
        return 0.0
    return num / den


def _result_to_metrics(result: WorkflowTestResult) -> dict[str, float]:
    m = result.metrics
    infra_failure = bool(result.infra_failure or m.get("infra_failure", False))
    workflow_success = bool(result.success) and not infra_failure
    return {
        "success": 1.0 if workflow_success else 0.0,
        "infra_failure": 1.0 if infra_failure else 0.0,
        "workflow_failure": 1.0 if (not workflow_success and not infra_failure) else 0.0,
        "steps": float(m["total_steps"]),
        "retries": float(m["retries"]),
        "recovery_attempts": float(m["recovery_attempts"]),
        "stagnation_events": float(m["stagnation_events"]),
        "anti_bot_detections": float(m["anti_bot_detections"]),
        "completion_confidence": float(result.completion_confidence),
        "runtime": float(m["execution_duration_seconds"]),
        "degraded_mode": 1.0 if bool(result.degraded_mode or m.get("degraded_mode", False)) else 0.0,
        "llm_available": 1.0 if bool(result.llm_available or m.get("llm_available", False)) else 0.0,
        "recovery_success_flag": 1.0 if (m["recovery_attempts"] > 0 and result.success) else 0.0,
    }


def terminal_summary_table(report: dict[str, Any]) -> str:
    headers = [
        "Workflow",
        "Runs",
        "SuccessRate",
        "AvgSteps",
        "AvgRetries",
        "RecoverySuccess",
        "StagnationFreq",
        "AntiBotRate",
        "AvgConfidence",
        "AvgRuntime(s)",
        "InfraFailRate",
        "WorkflowFailRate",
    ]
    lines = [" | ".join(headers), " | ".join(["---"] * len(headers))]
    for wf in report["workflows"]:
        lines.append(
            " | ".join(
                [
                    wf["workflow"],
                    str(wf["runs"]),
                    f"{wf['success_rate']:.2%}",
                    f"{wf['average_steps']:.2f}",
                    f"{wf['average_retries']:.2f}",
                    f"{wf['recovery_success_rate']:.2%}",
                    f"{wf['stagnation_frequency']:.2f}",
                    f"{wf['anti_bot_detection_rate']:.2f}",
                    f"{wf['average_completion_confidence']:.3f}",
                    f"{wf['average_runtime_seconds']:.2f}",
                    f"{wf['infra_failure_rate']:.2%}",
                    f"{wf['workflow_failure_rate']:.2%}",
                ]
            )
        )
    return "\n".join(lines)


def markdown_summary(report: dict[str, Any]) -> str:
    return (
        "# Reliability Benchmark Summary\n\n"
        f"- Generated: {report['generated_at']}\n"
        f"- Iterations per workflow: {report['iterations']}\n"
        f"- Total runs: {report['total_runs']}\n"
        f"- Overall success rate: {report['overall_success_rate']:.2%}\n\n"
        f"- Overall infra failure rate: {report['overall_infra_failure_rate']:.2%}\n"
        f"- Overall workflow failure rate: {report['overall_workflow_failure_rate']:.2%}\n\n"
        "## Metrics Table\n\n"
        f"{terminal_summary_table(report)}\n"
    )


async def run_benchmark(config: BenchmarkConfig) -> dict[str, Any]:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    runner = RuntimeTestRunner(headless=config.headless, output_dir=config.output_dir / "runner_artifacts")

    templates: list[WorkflowTestCase] = [
        WorkflowTestCase(
            name="bench_search_duckduckgo",
            workflow_type="search",
            goal="Open DuckDuckGo and search for Python internships",
            start_url="https://duckduckgo.com",
            max_steps=10,
            min_completion_confidence=0.55,
            expected_status={"completed", "max_steps_reached"},
        ),
        WorkflowTestCase(
            name="bench_form_demoqa",
            workflow_type="form_fill",
            goal="Fill DemoQA form with profile details and submit",
            start_url="https://demoqa.com/automation-practice-form",
            max_steps=10,
            min_completion_confidence=0.45,
            expected_status={"completed", "max_steps_reached"},
            profile={
                "name": "Aaron Richard",
                "email": "aaron.richard@example.com",
                "phone": "9876543210",
                "education": "B.Tech",
                "skills": ["Python", "Automation"],
            },
        ),
        WorkflowTestCase(
            name="bench_login_the_internet",
            workflow_type="login",
            goal="Login to the-internet.herokuapp.com with demo credentials",
            start_url="https://the-internet.herokuapp.com/login",
            max_steps=8,
            min_completion_confidence=0.55,
            expected_status={"completed", "max_steps_reached"},
        ),
        WorkflowTestCase(
            name="bench_search_wikipedia",
            workflow_type="wikipedia",
            goal="Open Wikipedia and search for Python programming",
            start_url="https://www.wikipedia.org",
            max_steps=10,
            min_completion_confidence=0.50,
            expected_status={"completed", "max_steps_reached"},
        ),
    ]

    per_workflow_results: dict[str, list[WorkflowTestResult]] = {t.workflow_type: [] for t in templates}
    run_counter = 0
    for i in range(1, max(config.iterations, 1) + 1):
        for t in templates:
            case = WorkflowTestCase(
                name=f"{t.name}_iter{i}",
                workflow_type=t.workflow_type,
                goal=t.goal,
                start_url=t.start_url,
                max_steps=t.max_steps,
                min_completion_confidence=t.min_completion_confidence,
                expected_status=set(t.expected_status),
                profile=dict(t.profile),
            )
            result = await runner.run_test(case)
            per_workflow_results[t.workflow_type].append(result)
            run_counter += 1

    workflow_reports: list[dict[str, Any]] = []
    all_successes = 0
    all_infra_failures = 0
    all_workflow_failures = 0
    for wf, results in per_workflow_results.items():
        metrics = [_result_to_metrics(r) for r in results]
        runs = len(results)
        successes = int(sum(x["success"] for x in metrics))
        infra_failures = int(sum(x["infra_failure"] for x in metrics))
        workflow_failures = int(sum(x["workflow_failure"] for x in metrics))
        all_successes += successes
        all_infra_failures += infra_failures
        all_workflow_failures += workflow_failures
        recovery_attempt_runs = int(sum(1 for x in metrics if x["recovery_attempts"] > 0))
        recovery_success_runs = int(sum(x["recovery_success_flag"] for x in metrics))
        anti_bot_runs = int(sum(1 for x in metrics if x["anti_bot_detections"] > 0))

        workflow_reports.append(
            {
                "workflow": wf,
                "runs": runs,
                "success_rate": _rate(successes, runs),
                "average_steps": _safe_mean([x["steps"] for x in metrics]),
                "average_retries": _safe_mean([x["retries"] for x in metrics]),
                "recovery_success_rate": _rate(recovery_success_runs, recovery_attempt_runs),
                "stagnation_frequency": _safe_mean([x["stagnation_events"] for x in metrics]),
                "anti_bot_detection_rate": _rate(anti_bot_runs, runs),
                "average_completion_confidence": _safe_mean([x["completion_confidence"] for x in metrics]),
                "average_runtime_seconds": _safe_mean([x["runtime"] for x in metrics]),
                "infra_failure_rate": _rate(infra_failures, runs),
                "workflow_failure_rate": _rate(workflow_failures, runs),
                "degraded_mode_rate": _rate(int(sum(x["degraded_mode"] for x in metrics)), runs),
                "llm_available_rate": _rate(int(sum(x["llm_available"] for x in metrics)), runs),
                "sample_trace_paths": [r.trace_path for r in results[:3]],
            }
        )

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "iterations": max(config.iterations, 1),
        "headless": config.headless,
        "total_runs": run_counter,
        "overall_success_rate": _rate(all_successes, run_counter),
        "overall_infra_failure_rate": _rate(all_infra_failures, run_counter),
        "overall_workflow_failure_rate": _rate(all_workflow_failures, run_counter),
        "workflows": workflow_reports,
    }
    return report


def export_reports(report: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "reliability_benchmark_report.json"
    md_path = output_dir / "reliability_benchmark_summary.md"
    json_path.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
    md_path.write_text(markdown_summary(report), encoding="utf-8")
    return json_path, md_path


async def _main_async(iterations: int) -> int:
    config = BenchmarkConfig(iterations=iterations, headless=True)
    report = await run_benchmark(config)
    json_path, md_path = export_reports(report, config.output_dir)
    print(terminal_summary_table(report))
    print(f"\nJSON report: {json_path}")
    print(f"Markdown summary: {md_path}")
    return 0


def main() -> None:
    iterations = 3
    if len(sys.argv) >= 2:
        try:
            iterations = max(int(sys.argv[1]), 1)
        except ValueError:
            iterations = 3
    raise SystemExit(asyncio.run(_main_async(iterations)))


if __name__ == "__main__":
    main()
