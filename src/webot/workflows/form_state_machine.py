from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any

from webot.intelligence.dom_extractor import DomElement
from webot.workflows.action_executor import Action
from webot.workflows.goal_evaluator import GoalEvaluator
from webot.workflows.progress_tracker import ProgressTracker


class FormState(str, Enum):
    START = "start"
    DETECT_REQUIRED_FIELDS = "detect_required_fields"
    FILL_FIELDS = "fill_fields"
    SUBMIT_FORM = "submit_form"
    DETECT_CONFIRMATION = "detect_confirmation"
    VALIDATE_SUBMISSION = "validate_submission"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(slots=True)
class FormStateMachine:
    progress_tracker: ProgressTracker | None = None
    goal_evaluator: GoalEvaluator | None = None
    _state: FormState = FormState.START
    _required_selectors: list[str] = field(default_factory=list)
    _fill_index: int = 0

    def next_action(
        self,
        *,
        user_goal: str,
        dom_state: dict[str, list[DomElement]],
        current_url: str,
        recent_actions: list[dict[str, Any]],
    ) -> Action | None:
        if not self._is_form_goal(user_goal):
            return None

        if self._state == FormState.START:
            self._transition(FormState.DETECT_REQUIRED_FIELDS)
        if self._state == FormState.DETECT_REQUIRED_FIELDS:
            self._required_selectors = self._find_required_fields(dom_state)
            if not self._required_selectors:
                self._transition(FormState.FAILED, failed=True, reason="required_fields_not_found")
                return {"action": "extract_text", "selector": "body"}
            self._transition(FormState.FILL_FIELDS, completed=True)
        if self._state == FormState.FILL_FIELDS:
            if self._fill_index < len(self._required_selectors):
                selector = self._required_selectors[self._fill_index]
                self._fill_index += 1
                if self._fill_index >= len(self._required_selectors):
                    self._transition(FormState.SUBMIT_FORM, completed=True)
                return {"action": "fill", "selector": selector, "value": self._value_for_selector(selector)}
            self._transition(FormState.SUBMIT_FORM, completed=True)
        if self._state == FormState.SUBMIT_FORM:
            submit_selector = self._find_submit_selector(dom_state)
            self._transition(FormState.DETECT_CONFIRMATION, completed=True)
            if submit_selector:
                return {"action": "click", "selector": submit_selector}
            return {"action": "extract_text", "selector": "body"}
        if self._state == FormState.DETECT_CONFIRMATION:
            if self._has_confirmation_text(dom_state):
                self._transition(FormState.VALIDATE_SUBMISSION, completed=True)
            else:
                return {"action": "extract_text", "selector": "body"}
        if self._state == FormState.VALIDATE_SUBMISSION:
            if self._submission_looks_valid(recent_actions):
                self._transition(FormState.COMPLETED, completed=True)
            else:
                self._transition(FormState.FAILED, failed=True, reason="submission_not_confirmed")
            return {"action": "extract_text", "selector": "body"}
        return {"action": "extract_text", "selector": "body"}

    def _transition(self, state: FormState, *, completed: bool = False, failed: bool = False, reason: str = "") -> None:
        from webot.utils.logger import get_logger

        logger = get_logger(__name__)
        if state != self._state:
            self._state = state
            logger.info("state_entered", extra={"machine": "form", "state": state.value})
        if completed:
            logger.info("state_completed", extra={"machine": "form", "state": state.value})
        if failed:
            logger.warning("state_failed", extra={"machine": "form", "state": state.value, "reason": reason})

    @staticmethod
    def _is_form_goal(goal: str) -> bool:
        lowered = goal.lower()
        if any(token in lowered for token in ("fill", "submit", "apply")):
            return True
        return bool(re.search(r"\bform\b", lowered))

    @staticmethod
    def _find_required_fields(dom_state: dict[str, list[DomElement]]) -> list[str]:
        selectors: list[str] = []
        for item in dom_state.get("inputs", []):
            selector = str(item.get("selector", "")).strip()
            attrs = item.get("attributes", {}) if isinstance(item.get("attributes"), dict) else {}
            blob = f"{attrs.get('name','')} {attrs.get('id','')} {attrs.get('placeholder','')} {attrs.get('aria-label','')}".lower()
            if selector and any(token in blob for token in ("name", "email", "phone", "mobile")):
                selectors.append(selector)
        return selectors[:4]

    @staticmethod
    def _value_for_selector(selector: str) -> str:
        lowered = selector.lower()
        if "email" in lowered:
            return "demo@example.com"
        if "phone" in lowered or "mobile" in lowered or "number" in lowered:
            return "9999999999"
        if "last" in lowered:
            return "User"
        return "Demo"

    @staticmethod
    def _find_submit_selector(dom_state: dict[str, list[DomElement]]) -> str:
        for item in dom_state.get("buttons", []):
            selector = str(item.get("selector", "")).strip()
            text = str(item.get("text", "")).lower()
            if selector and any(token in text for token in ("submit", "save", "continue", "apply")):
                return selector
        return ""

    @staticmethod
    def _has_confirmation_text(dom_state: dict[str, list[DomElement]]) -> bool:
        for node in dom_state.get("visible_text", []):
            text = str(node.get("text", "")).lower()
            if any(token in text for token in ("thank", "submitted", "success", "saved")):
                return True
        return False

    @staticmethod
    def _submission_looks_valid(recent_actions: list[dict[str, Any]]) -> bool:
        has_submit_click = any(
            item.get("action") == "click" and item.get("status") == "success"
            for item in recent_actions[-6:]
        )
        has_fill = any(item.get("action") == "fill" and item.get("status") == "success" for item in recent_actions[-8:])
        return has_submit_click and has_fill
