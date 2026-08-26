from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypedDict


TraceStatus = Literal[
    "running",
    "completed",
    "failed",
    "max_steps_reached",
    "cancelled",
    "blocked_by_anti_bot",
]


class StepTrace(TypedDict, total=False):
    step: int
    timestamp: str
    url: str
    selected_action: dict[str, Any]
    selector: str
    validation: dict[str, Any]
    dom_delta_summary: dict[str, Any]
    progress_report: dict[str, Any]
    interstitial: dict[str, Any]
    goal_evaluation: dict[str, Any]
    recovery: dict[str, Any]
    loop_guard_trigger: str
    termination_reason: str
    success: bool


class TraceDocument(TypedDict):
    trace_id: str
    started_at: str
    ended_at: str
    status: TraceStatus
    goal: str
    termination_reason: str
    metadata: dict[str, Any]
    step_count: int
    step_summaries: list[dict[str, Any]]
    steps: list[StepTrace]


@dataclass(slots=True)
class ExecutionTrace:
    """Records autonomous workflow execution traces for debugging and analysis."""

    goal: str
    trace_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    _started_at: datetime = field(init=False, repr=False)
    _ended_at: datetime | None = field(default=None, init=False, repr=False)
    _status: TraceStatus = field(default="running", init=False, repr=False)
    _termination_reason: str = field(default="", init=False, repr=False)
    _steps: list[StepTrace] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.goal, str) or not self.goal.strip():
            raise ValueError("goal must be a non-empty string")

        self.goal = self.goal.strip()
        self._started_at = datetime.now(timezone.utc)
        if not self.trace_id:
            self.trace_id = self._started_at.strftime("trace-%Y%m%d-%H%M%S")

    def record_step(
        self,
        *,
        step: int,
        timestamp: datetime | None = None,
        url: str = "",
        selected_action: dict[str, Any] | None = None,
        selector: str = "",
        validation: dict[str, Any] | None = None,
        dom_delta_summary: dict[str, Any] | None = None,
        progress_report: dict[str, Any] | None = None,
        interstitial: dict[str, Any] | None = None,
        goal_evaluation: dict[str, Any] | None = None,
        recovery: dict[str, Any] | None = None,
        loop_guard_trigger: str = "",
        termination_reason: str = "",
        success: bool | None = None,
    ) -> None:
        if step <= 0:
            raise ValueError("step must be > 0")

        step_time = timestamp or datetime.now(timezone.utc)
        item: StepTrace = {
            "step": int(step),
            "timestamp": self._iso(step_time),
            "url": self._as_str(url),
            "selected_action": dict(selected_action or {}),
            "selector": self._as_str(selector),
            "validation": dict(validation or {}),
            "dom_delta_summary": dict(dom_delta_summary or {}),
            "progress_report": dict(progress_report or {}),
            "interstitial": dict(interstitial or {}),
            "goal_evaluation": dict(goal_evaluation or {}),
            "recovery": dict(recovery or {}),
            "loop_guard_trigger": self._as_str(loop_guard_trigger),
            "termination_reason": self._as_str(termination_reason),
        }
        if success is not None:
            item["success"] = bool(success)

        self._steps.append(item)

    def record_loop_guard(self, *, step: int, guard_reason: str, url: str = "") -> None:
        self.record_step(
            step=step,
            url=url,
            loop_guard_trigger=guard_reason,
            termination_reason=guard_reason,
            success=False,
        )

    def finalize(self, *, status: TraceStatus, termination_reason: str = "") -> None:
        self._status = status
        self._termination_reason = termination_reason.strip()
        self._ended_at = datetime.now(timezone.utc)

    def export_trace(self, path: str | Path) -> Path:
        out_path = Path(path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
        return out_path

    def render_terminal_summary(self) -> str:
        doc = self.to_dict()
        action_counts = self._action_counts()
        action_blob = ", ".join(f"{k}:{v}" for k, v in action_counts.items()) if action_counts else "none"

        return (
            f"trace_id={doc['trace_id']}\n"
            f"status={doc['status']}\n"
            f"goal={doc['goal']}\n"
            f"steps={doc['step_count']}\n"
            f"started_at={doc['started_at']}\n"
            f"ended_at={doc['ended_at']}\n"
            f"termination_reason={doc['termination_reason'] or '<none>'}\n"
            f"actions={action_blob}"
        )

    def render_step_timeline(self) -> str:
        if not self._steps:
            return "(no steps recorded)"

        lines: list[str] = []
        for step in self._steps:
            number = int(step.get("step", 0) or 0)
            timestamp = self._as_str(step.get("timestamp", ""))
            url = self._trim(self._as_str(step.get("url", "")), 80)

            action = step.get("selected_action", {})
            action_name = ""
            if isinstance(action, dict):
                action_name = self._as_str(action.get("action", ""))
            selector = self._trim(self._as_str(step.get("selector", "")), 64)

            success_value = step.get("success")
            if success_value is True:
                mark = "ok"
            elif success_value is False:
                mark = "fail"
            else:
                mark = "unk"

            guard = self._as_str(step.get("loop_guard_trigger", ""))
            termination = self._as_str(step.get("termination_reason", ""))

            line = f"[{number:02d}] {timestamp} | {mark} | {action_name or '<none>'}"
            if selector:
                line += f" | sel={selector}"
            if url:
                line += f" | url={url}"
            if guard:
                line += f" | guard={guard}"
            if termination:
                line += f" | term={termination}"
            lines.append(line)

        return "\n".join(lines)

    def step_summaries(self) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for step in self._steps:
            action = step.get("selected_action", {}) if isinstance(step.get("selected_action"), dict) else {}
            validation = step.get("validation", {}) if isinstance(step.get("validation"), dict) else {}
            dom_delta = step.get("dom_delta_summary", {}) if isinstance(step.get("dom_delta_summary"), dict) else {}
            progress = step.get("progress_report", {}) if isinstance(step.get("progress_report"), dict) else {}
            recovery = step.get("recovery", {}) if isinstance(step.get("recovery"), dict) else {}
            goal_eval = step.get("goal_evaluation", {}) if isinstance(step.get("goal_evaluation"), dict) else {}
            interstitial = step.get("interstitial", {}) if isinstance(step.get("interstitial"), dict) else {}

            summaries.append(
                {
                    "step": int(step.get("step", 0) or 0),
                    "timestamp": self._as_str(step.get("timestamp", "")),
                    "action": self._as_str(action.get("action", "")),
                    "selector": self._as_str(step.get("selector", "")),
                    "url": self._as_str(step.get("url", "")),
                    "success": step.get("success"),
                    "validation_reason": self._as_str(validation.get("reason", "")),
                    "dom_delta_compact": self._dom_delta_compact(dom_delta),
                    "progress_summary": self._as_str(progress.get("summary", "")),
                    "interstitial_type": self._as_str(interstitial.get("detection_type", "")),
                    "goal_confidence": self._safe_float(goal_eval.get("completion_confidence")),
                    "recovery_strategy": self._as_str(recovery.get("strategy", "")),
                    "loop_guard_trigger": self._as_str(step.get("loop_guard_trigger", "")),
                    "termination_reason": self._as_str(step.get("termination_reason", "")),
                }
            )
        return summaries

    def to_dict(self) -> TraceDocument:
        ended_at = self._ended_at or datetime.now(timezone.utc)
        return {
            "trace_id": self.trace_id,
            "started_at": self._iso(self._started_at),
            "ended_at": self._iso(ended_at),
            "status": self._status,
            "goal": self.goal,
            "termination_reason": self._termination_reason,
            "metadata": dict(self.metadata),
            "step_count": len(self._steps),
            "step_summaries": self.step_summaries(),
            "steps": [dict(s) for s in self._steps],
        }

    def _action_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for step in self._steps:
            action = step.get("selected_action")
            if not isinstance(action, dict):
                continue
            name = self._as_str(action.get("action", ""))
            if not name:
                continue
            counts[name] = counts.get(name, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: item[0]))

    @staticmethod
    def _dom_delta_compact(dom_delta: dict[str, Any]) -> str:
        if not dom_delta:
            return ""
        if isinstance(dom_delta.get("token_optimized_summary"), str):
            return str(dom_delta.get("token_optimized_summary", ""))

        def _n(key: str) -> int:
            value = dom_delta.get(key, 0)
            try:
                return int(value)
            except Exception:
                return 0

        return (
            f"nav={str(bool(dom_delta.get('navigation_changed', False))).lower()}|"
            f"new={_n('new')}|removed={_n('removed')}|"
            f"text={_n('text_changed')}|form={_n('form_changed')}"
        )

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        try:
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _as_str(value: Any) -> str:
        return value.strip() if isinstance(value, str) else ""

    @staticmethod
    def _trim(value: str, max_len: int) -> str:
        if len(value) <= max_len:
            return value
        return value[: max_len - 3] + "..."
