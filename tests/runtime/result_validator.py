from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, TypedDict
from urllib.parse import urlparse


WorkflowType = Literal["search", "form_fill", "login"]


class WorkflowValidationResult(TypedDict):
    validation_passed: bool
    confidence_score: float
    validation_reason: str
    detected_failures: list[str]


class WorkflowValidationInput(TypedDict, total=False):
    workflow_type: WorkflowType
    goal: str
    status: str
    current_url: str
    extracted_text: str
    completion_confidence: float
    validation_reasons: list[str]
    progress_summary: str
    anti_bot_detections: int
    form_fill_result: dict[str, Any]
    trace_steps: list[dict[str, Any]]


@dataclass(slots=True)
class ResultValidator:
    """Deterministic workflow outcome validator for runtime test results."""

    min_pass_score: float = 0.70

    def validate(self, data: WorkflowValidationInput) -> WorkflowValidationResult:
        workflow_type = str(data.get("workflow_type", "")).strip()
        if workflow_type == "search":
            return self._validate_search(data)
        if workflow_type == "form_fill":
            return self._validate_form(data)
        if workflow_type == "login":
            return self._validate_login(data)

        return {
            "validation_passed": False,
            "confidence_score": 0.0,
            "validation_reason": "unsupported_workflow_type",
            "detected_failures": [f"Unsupported workflow_type: {workflow_type}"],
        }

    def _validate_search(self, data: WorkflowValidationInput) -> WorkflowValidationResult:
        failures: list[str] = []
        score = 0.15

        text = self._lower(data.get("extracted_text", ""))
        url = self._lower(data.get("current_url", ""))
        status = self._lower(data.get("status", ""))
        anti_bot_hits = int(data.get("anti_bot_detections", 0) or 0)

        no_anti_bot = anti_bot_hits == 0 and not self._looks_like_anti_bot(url=url, text=text)
        if not no_anti_bot:
            failures.append("anti_bot_detected")
            score -= 0.45

        results_appeared = self._search_results_appeared(text=text, url=url)
        if results_appeared:
            score += 0.35
        else:
            failures.append("search_results_not_detected")

        meaningful_extract = self._meaningful_extraction(text)
        if meaningful_extract:
            score += 0.25
        else:
            failures.append("extracted_results_not_meaningful")

        if status in {"completed", "max_steps_reached"}:
            score += 0.10
        if float(data.get("completion_confidence", 0.0) or 0.0) >= 0.70:
            score += 0.10

        final = self._build(score=score, failures=failures, success_reason="search_validated")
        if not no_anti_bot and final["validation_passed"]:
            final["validation_passed"] = False
            final["validation_reason"] = "search_blocked_by_anti_bot"
        return final

    def _validate_form(self, data: WorkflowValidationInput) -> WorkflowValidationResult:
        failures: list[str] = []
        score = 0.20

        text = self._lower(data.get("extracted_text", ""))
        status = self._lower(data.get("status", ""))
        reasons = [self._lower(x) for x in data.get("validation_reasons", []) if isinstance(x, str)]
        form_fill_result = data.get("form_fill_result", {})

        fields_ok = self._fields_filled_correctly(form_fill_result)
        if fields_ok:
            score += 0.25
        else:
            failures.append("fields_not_filled_reliably")

        submitted = self._form_submission_indicator(text)
        if submitted:
            score += 0.30
        else:
            failures.append("submission_indicator_missing")

        validator_success = self._form_validation_success(text=text, reasons=reasons)
        if validator_success:
            score += 0.20
        else:
            failures.append("form_validation_not_confirmed")

        if status == "completed":
            score += 0.10

        return self._build(score=score, failures=failures, success_reason="form_validated")

    def _validate_login(self, data: WorkflowValidationInput) -> WorkflowValidationResult:
        failures: list[str] = []
        score = 0.20

        text = self._lower(data.get("extracted_text", ""))
        url = self._lower(data.get("current_url", ""))
        status = self._lower(data.get("status", ""))

        authenticated = self._authenticated_page_reached(url=url, text=text)
        if authenticated:
            score += 0.35
        else:
            failures.append("authenticated_page_not_reached")

        success_indicators = self._login_success_indicators(text)
        if success_indicators:
            score += 0.25
        else:
            failures.append("login_success_indicators_missing")

        no_login_error = not self._login_error_indicators(text)
        if no_login_error:
            score += 0.20
        else:
            failures.append("login_error_detected")

        if status == "completed":
            score += 0.10

        return self._build(score=score, failures=failures, success_reason="login_validated")

    def _build(self, *, score: float, failures: list[str], success_reason: str) -> WorkflowValidationResult:
        confidence = self._clamp(score)
        passed = confidence >= self.min_pass_score and not failures
        return {
            "validation_passed": passed,
            "confidence_score": confidence,
            "validation_reason": success_reason if passed else self._failure_reason(failures),
            "detected_failures": failures,
        }

    @staticmethod
    def _failure_reason(failures: list[str]) -> str:
        if not failures:
            return "validation_threshold_not_met"
        return failures[0]

    @staticmethod
    def _meaningful_extraction(text: str) -> bool:
        compact = re.sub(r"\s+", " ", text).strip()
        if len(compact) < 80:
            return False
        alpha_tokens = re.findall(r"[a-zA-Z]{4,}", compact)
        return len(alpha_tokens) >= 10

    @staticmethod
    def _search_results_appeared(*, text: str, url: str) -> bool:
        url_hit = any(token in url for token in ["/search", "q=", "bing.com/search", "duckduckgo.com/"])
        text_hit = any(
            token in text
            for token in ["results", "showing", "top stories", "people also ask", "about ", "seconds)"]
        )
        return url_hit or text_hit

    @staticmethod
    def _fields_filled_correctly(form_fill_result: Any) -> bool:
        if not isinstance(form_fill_result, dict):
            return False
        filled = int(form_fill_result.get("filled", 0) or 0)
        failed = int(form_fill_result.get("failed", 0) or 0)
        return filled > 0 and failed == 0

    @staticmethod
    def _form_submission_indicator(text: str) -> bool:
        return any(
            token in text
            for token in ["thank you", "thanks", "thank", "submitted", "submitting", "application received", "saved", "success", "we received"]
        )

    @staticmethod
    def _form_validation_success(*, text: str, reasons: list[str]) -> bool:
        has_error = any(token in text for token in ["required", "invalid", "error", "please enter"])
        reason_success = any("validated" in r for r in reasons)
        return not has_error and reason_success

    @staticmethod
    def _authenticated_page_reached(*, url: str, text: str) -> bool:
        parsed = urlparse(url if url.startswith(("http://", "https://")) else f"https://{url}")
        path = parsed.path.lower()
        not_login_path = all(token not in path for token in ["login", "signin", "sign-in", "auth"])
        dashboard_text = any(token in text for token in ["dashboard", "my account", "profile", "logout", "sign out"])
        return not_login_path and (dashboard_text or bool(path and path != "/"))

    @staticmethod
    def _login_success_indicators(text: str) -> bool:
        return any(token in text for token in ["welcome", "dashboard", "my account", "logout", "sign out", "profile"])

    @staticmethod
    def _login_error_indicators(text: str) -> bool:
        return any(
            token in text
            for token in ["incorrect", "invalid password", "authentication failed", "try again", "wrong password"]
        )

    @staticmethod
    def _looks_like_anti_bot(*, url: str, text: str) -> bool:
        hay = f"{url} {text}".lower()
        return any(
            token in hay
            for token in ["/sorry/", "captcha", "unusual traffic", "verify you are human", "access denied", "cf-challenge"]
        )

    @staticmethod
    def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
        if value < lo:
            return lo
        if value > hi:
            return hi
        return value

    @staticmethod
    def _lower(value: Any) -> str:
        return value.strip().lower() if isinstance(value, str) else ""
