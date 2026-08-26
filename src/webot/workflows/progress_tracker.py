from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Literal, TypedDict


TerminationRecommendation = Literal["continue", "warn", "terminate"]


class StepSignal(TypedDict, total=False):
    step: int
    action: str
    selector: str
    success: bool
    retries_used: int
    url: str
    dom_signature: str
    dom_delta_summary: str
    dom_delta_size: int
    interstitial_type: str
    anti_bot_detected: bool
    fill_value: str


class ProgressReport(TypedDict):
    progress_score: float
    stagnation_score: float
    loop_risk_score: float
    termination_recommendation: TerminationRecommendation
    summary: str
    signals: dict[str, float]


@dataclass(slots=True)
class ProgressTracker:
    """Tracks progress quality and stagnation risk for autonomous workflows."""

    window_size: int = 12
    repeat_action_threshold: int = 3
    repeat_failure_threshold: int = 3
    navigation_loop_threshold: int = 3
    unchanged_dom_threshold: int = 3
    meaningless_delta_threshold: int = 3
    selector_oscillation_threshold: int = 4
    retry_storm_threshold: int = 6
    anti_bot_threshold: int = 2
    repeated_fill_threshold: int = 3

    _steps: Deque[StepSignal] = field(default_factory=deque)

    def record_step(
        self,
        *,
        step: int,
        action: str,
        selector: str = "",
        success: bool,
        retries_used: int = 0,
        url: str = "",
        dom_signature: str = "",
        dom_delta_summary: str = "",
        dom_delta_size: int = 0,
        interstitial_type: str = "",
        anti_bot_detected: bool = False,
        fill_value: str = "",
    ) -> None:
        signal: StepSignal = {
            "step": step,
            "action": action,
            "selector": selector,
            "success": bool(success),
            "retries_used": max(int(retries_used), 0),
            "url": url,
            "dom_signature": dom_signature,
            "dom_delta_summary": dom_delta_summary,
            "dom_delta_size": max(int(dom_delta_size), 0),
            "interstitial_type": interstitial_type,
            "anti_bot_detected": bool(anti_bot_detected),
            "fill_value": fill_value,
        }
        self._steps.append(signal)
        while len(self._steps) > self.window_size:
            self._steps.popleft()

    def evaluate_progress(self) -> ProgressReport:
        steps = list(self._steps)
        if not steps:
            return {
                "progress_score": 1.0,
                "stagnation_score": 0.0,
                "loop_risk_score": 0.0,
                "termination_recommendation": "continue",
                "summary": "no_steps_recorded",
                "signals": {},
            }

        stagnation_signals = self.detect_stagnation()

        total = len(steps)
        successes = sum(1 for s in steps if s.get("success"))
        success_ratio = successes / total

        repeat_identical = self._repeated_identical_actions(steps)
        repeat_failed = self._repeated_failed_actions(steps)
        nav_loop = self._navigation_loop(steps)
        unchanged_dom = self._unchanged_dom_states(steps)
        meaningless_delta = self._meaningless_dom_deltas(steps)
        selector_oscillation = self._selector_oscillation(steps)
        retry_storm = self._retry_storm(steps)
        anti_bot_pressure = self._anti_bot_pressure(steps)
        repeated_fill_pressure = self._repeated_fill_pressure(steps)

        loop_risk = self._clamp(
            (
                repeat_identical * 0.16
                + repeat_failed * 0.22
                + nav_loop * 0.14
                + unchanged_dom * 0.14
                + meaningless_delta * 0.12
                + selector_oscillation * 0.10
                + retry_storm * 0.12
                + anti_bot_pressure * 0.20
                + repeated_fill_pressure * 0.20
            )
        )

        stagnation_score = self._clamp(
            (
                stagnation_signals["repeat_identical_actions"] * 0.22
                + stagnation_signals["repeated_failed_actions"] * 0.24
                + stagnation_signals["unchanged_dom_states"] * 0.18
                + stagnation_signals["meaningless_dom_deltas"] * 0.16
                + stagnation_signals["selector_oscillation"] * 0.10
                + stagnation_signals["retry_storm"] * 0.10
                + stagnation_signals["anti_bot_pressure"] * 0.24
                + stagnation_signals["repeated_fill_pressure"] * 0.25
            )
        )

        progress_score = self._clamp(success_ratio * (1.0 - 0.65 * stagnation_score) * (1.0 - 0.55 * loop_risk))

        recommendation = self._recommend(progress_score, stagnation_score, loop_risk)
        summary = self._compact_summary(
            progress_score=progress_score,
            stagnation_score=stagnation_score,
            loop_risk=loop_risk,
            recommendation=recommendation,
        )

        return {
            "progress_score": progress_score,
            "stagnation_score": stagnation_score,
            "loop_risk_score": loop_risk,
            "termination_recommendation": recommendation,
            "summary": summary,
            "signals": {
                "success_ratio": round(success_ratio, 3),
                "repeat_identical_actions": round(repeat_identical, 3),
                "repeated_failed_actions": round(repeat_failed, 3),
                "navigation_loops": round(nav_loop, 3),
                "unchanged_dom_states": round(unchanged_dom, 3),
                "meaningless_dom_deltas": round(meaningless_delta, 3),
                "selector_oscillation": round(selector_oscillation, 3),
                "retry_storm": round(retry_storm, 3),
                "anti_bot_pressure": round(anti_bot_pressure, 3),
                "repeated_fill_pressure": round(repeated_fill_pressure, 3),
            },
        }

    def detect_stagnation(self) -> dict[str, float]:
        steps = list(self._steps)
        if not steps:
            return {
                "repeat_identical_actions": 0.0,
                "repeated_failed_actions": 0.0,
                "unchanged_dom_states": 0.0,
                "meaningless_dom_deltas": 0.0,
                "selector_oscillation": 0.0,
                "retry_storm": 0.0,
                "anti_bot_pressure": 0.0,
                "repeated_fill_pressure": 0.0,
            }

        return {
            "repeat_identical_actions": self._repeated_identical_actions(steps),
            "repeated_failed_actions": self._repeated_failed_actions(steps),
            "unchanged_dom_states": self._unchanged_dom_states(steps),
            "meaningless_dom_deltas": self._meaningless_dom_deltas(steps),
            "selector_oscillation": self._selector_oscillation(steps),
            "retry_storm": self._retry_storm(steps),
            "anti_bot_pressure": self._anti_bot_pressure(steps),
            "repeated_fill_pressure": self._repeated_fill_pressure(steps),
        }

    def should_terminate(self) -> tuple[bool, TerminationRecommendation, str]:
        report = self.evaluate_progress()
        rec = report["termination_recommendation"]
        if rec == "terminate":
            return True, rec, report["summary"]
        if rec == "warn":
            return False, rec, report["summary"]
        return False, rec, report["summary"]

    def _repeated_identical_actions(self, steps: list[StepSignal]) -> float:
        streak = self._max_streak([
            f"{s.get('action','')}::{s.get('selector','')}" for s in steps
        ])
        return self._threshold_ratio(streak, self.repeat_action_threshold)

    def _repeated_failed_actions(self, steps: list[StepSignal]) -> float:
        sequence = [
            f"{s.get('action','')}::{s.get('selector','')}" if not s.get("success") else ""
            for s in steps
        ]
        streak = self._max_nonempty_streak(sequence)
        return self._threshold_ratio(streak, self.repeat_failure_threshold)

    def _navigation_loop(self, steps: list[StepSignal]) -> float:
        urls = [str(s.get("url", "")).rstrip("/") for s in steps if s.get("url")]
        if len(urls) < 4:
            return 0.0
        toggles = 0
        for idx in range(2, len(urls)):
            if urls[idx] == urls[idx - 2] and urls[idx] != urls[idx - 1]:
                toggles += 1
        return self._threshold_ratio(toggles, self.navigation_loop_threshold)

    def _unchanged_dom_states(self, steps: list[StepSignal]) -> float:
        signatures = [str(s.get("dom_signature", "")) for s in steps if s.get("dom_signature")]
        streak = self._max_streak(signatures)
        return self._threshold_ratio(streak, self.unchanged_dom_threshold)

    def _meaningless_dom_deltas(self, steps: list[StepSignal]) -> float:
        # Meaningless delta if summary repeated and size is near-zero.
        count = 0
        prev = ""
        for s in steps:
            summary = str(s.get("dom_delta_summary", ""))
            size = int(s.get("dom_delta_size", 0) or 0)
            if summary and summary == prev and size <= 1:
                count += 1
            prev = summary
        return self._threshold_ratio(count, self.meaningless_delta_threshold)

    def _selector_oscillation(self, steps: list[StepSignal]) -> float:
        selectors = [str(s.get("selector", "")) for s in steps if s.get("selector")]
        if len(selectors) < 4:
            return 0.0
        alternations = 0
        for idx in range(2, len(selectors)):
            if selectors[idx] == selectors[idx - 2] and selectors[idx] != selectors[idx - 1]:
                alternations += 1
        return self._threshold_ratio(alternations, self.selector_oscillation_threshold)

    def _retry_storm(self, steps: list[StepSignal]) -> float:
        retries = sum(int(s.get("retries_used", 0) or 0) for s in steps)
        return self._threshold_ratio(retries, self.retry_storm_threshold)

    def _anti_bot_pressure(self, steps: list[StepSignal]) -> float:
        count = sum(
            1
            for s in steps
            if bool(s.get("anti_bot_detected"))
            or str(s.get("interstitial_type", "")) in {"anti_bot", "captcha", "access_denied"}
        )
        return self._threshold_ratio(count, self.anti_bot_threshold)

    def _repeated_fill_pressure(self, steps: list[StepSignal]) -> float:
        seq: list[str] = []
        for step in steps:
            if str(step.get("action", "")) != "fill":
                seq.append("")
                continue
            selector = str(step.get("selector", ""))
            value = str(step.get("fill_value", ""))
            seq.append(f"{selector}::{value}")
        streak = self._max_nonempty_streak(seq)
        return self._threshold_ratio(streak, self.repeated_fill_threshold)

    @staticmethod
    def _max_streak(values: list[str]) -> int:
        if not values:
            return 0
        max_streak = 1
        current = 1
        for idx in range(1, len(values)):
            if values[idx] and values[idx] == values[idx - 1]:
                current += 1
                if current > max_streak:
                    max_streak = current
            else:
                current = 1
        return max_streak

    @staticmethod
    def _max_nonempty_streak(values: list[str]) -> int:
        max_streak = 0
        current = 0
        prev = ""
        for value in values:
            if value and value == prev:
                current += 1
            elif value:
                current = 1
            else:
                current = 0
            prev = value
            if current > max_streak:
                max_streak = current
        return max_streak

    @staticmethod
    def _threshold_ratio(value: int | float, threshold: int | float) -> float:
        if threshold <= 0:
            return 1.0
        return ProgressTracker._clamp(float(value) / float(threshold))

    @staticmethod
    def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
        if value < lo:
            return lo
        if value > hi:
            return hi
        return value

    @staticmethod
    def _recommend(progress_score: float, stagnation_score: float, loop_risk: float) -> TerminationRecommendation:
        if loop_risk >= 0.85 or stagnation_score >= 0.85 or progress_score <= 0.12:
            return "terminate"
        if loop_risk >= 0.6 or stagnation_score >= 0.6 or progress_score <= 0.35:
            return "warn"
        return "continue"

    @staticmethod
    def _compact_summary(
        *,
        progress_score: float,
        stagnation_score: float,
        loop_risk: float,
        recommendation: TerminationRecommendation,
    ) -> str:
        return (
            f"p={progress_score:.2f}|s={stagnation_score:.2f}|"
            f"l={loop_risk:.2f}|rec={recommendation}"
        )
