from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from webot.intelligence.dom_extractor import DomElement
from webot.workflows.action_executor import Action
from webot.workflows.goal_evaluator import GoalEvaluator
from webot.workflows.progress_tracker import ProgressTracker


class LoginState(str, Enum):
    START = "start"
    DETECT_CREDENTIAL_FIELDS = "detect_credential_fields"
    FILL_USERNAME = "fill_username"
    FILL_PASSWORD = "fill_password"
    SUBMIT_LOGIN = "submit_login"
    VERIFY_AUTHENTICATED_PAGE = "verify_authenticated_page"
    VALIDATE_SUCCESS = "validate_success"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(slots=True)
class LoginStateMachine:
    progress_tracker: ProgressTracker | None = None
    goal_evaluator: GoalEvaluator | None = None
    default_username: str = "tomsmith"
    default_password: str = "SuperSecretPassword!"
    _state: LoginState = LoginState.START
    _username_selector: str = ""
    _password_selector: str = ""
    _submit_selector: str = ""

    def next_action(
        self,
        *,
        user_goal: str,
        dom_state: dict[str, list[DomElement]],
        current_url: str,
        recent_actions: list[dict[str, Any]],
    ) -> Action | None:
        if not self._is_login_goal(user_goal):
            return None

        if self._state == LoginState.START:
            self._transition(LoginState.DETECT_CREDENTIAL_FIELDS)
        if self._state == LoginState.DETECT_CREDENTIAL_FIELDS:
            self._username_selector, self._password_selector = self._find_credential_fields(dom_state)
            self._submit_selector = self._find_submit_selector(dom_state)
            if not self._username_selector or not self._password_selector:
                self._transition(LoginState.FAILED, failed=True, reason="credential_fields_not_found")
                return {"action": "extract_text", "selector": "body"}
            self._transition(LoginState.FILL_USERNAME, completed=True)
        if self._state == LoginState.FILL_USERNAME:
            self._transition(LoginState.FILL_PASSWORD, completed=True)
            return {"action": "fill", "selector": self._username_selector, "value": self.default_username}
        if self._state == LoginState.FILL_PASSWORD:
            self._transition(LoginState.SUBMIT_LOGIN, completed=True)
            return {"action": "fill", "selector": self._password_selector, "value": self.default_password}
        if self._state == LoginState.SUBMIT_LOGIN:
            self._transition(LoginState.VERIFY_AUTHENTICATED_PAGE, completed=True)
            if self._submit_selector:
                return {"action": "click", "selector": self._submit_selector}
            return {"action": "extract_text", "selector": "body"}
        if self._state == LoginState.VERIFY_AUTHENTICATED_PAGE:
            if self._looks_authenticated(current_url, dom_state):
                self._transition(LoginState.VALIDATE_SUCCESS, completed=True)
            else:
                return {"action": "extract_text", "selector": "body"}
        if self._state == LoginState.VALIDATE_SUCCESS:
            if self._success_indicators(recent_actions):
                self._transition(LoginState.COMPLETED, completed=True)
            else:
                self._transition(LoginState.FAILED, failed=True, reason="login_success_not_confirmed")
            return {"action": "extract_text", "selector": "body"}
        return {"action": "extract_text", "selector": "body"}

    def _transition(self, state: LoginState, *, completed: bool = False, failed: bool = False, reason: str = "") -> None:
        from webot.utils.logger import get_logger

        logger = get_logger(__name__)
        if state != self._state:
            self._state = state
            logger.info("state_entered", extra={"machine": "login", "state": state.value})
        if completed:
            logger.info("state_completed", extra={"machine": "login", "state": state.value})
        if failed:
            logger.warning("state_failed", extra={"machine": "login", "state": state.value, "reason": reason})

    @staticmethod
    def _is_login_goal(goal: str) -> bool:
        lowered = goal.lower()
        return any(token in lowered for token in ("login", "log in", "sign in", "authenticate"))

    @staticmethod
    def _find_credential_fields(dom_state: dict[str, list[DomElement]]) -> tuple[str, str]:
        username_selector = ""
        password_selector = ""
        for item in dom_state.get("inputs", []):
            selector = str(item.get("selector", "")).strip()
            attrs = item.get("attributes", {}) if isinstance(item.get("attributes"), dict) else {}
            blob = f"{attrs.get('name','')} {attrs.get('id','')} {attrs.get('placeholder','')} {attrs.get('aria-label','')} {attrs.get('type','')}".lower()
            if selector and not username_selector and any(token in blob for token in ("username", "email", "user", "login")):
                username_selector = selector
            if selector and not password_selector and "password" in blob:
                password_selector = selector
        return username_selector, password_selector

    @staticmethod
    def _find_submit_selector(dom_state: dict[str, list[DomElement]]) -> str:
        for item in dom_state.get("buttons", []):
            selector = str(item.get("selector", "")).strip()
            text = str(item.get("text", "")).lower()
            if selector and any(token in text for token in ("login", "sign in", "submit", "continue")):
                return selector
        return ""

    @staticmethod
    def _looks_authenticated(current_url: str, dom_state: dict[str, list[DomElement]]) -> bool:
        lowered_url = current_url.lower()
        if "/secure" in lowered_url or "dashboard" in lowered_url or "account" in lowered_url:
            return True
        for node in dom_state.get("visible_text", []):
            text = str(node.get("text", "")).lower()
            if any(token in text for token in ("logout", "sign out", "welcome", "secure area")):
                return True
        return False

    @staticmethod
    def _success_indicators(recent_actions: list[dict[str, Any]]) -> bool:
        has_submit = any(item.get("action") == "click" and item.get("status") == "success" for item in recent_actions[-5:])
        has_fills = sum(1 for item in recent_actions[-8:] if item.get("action") == "fill" and item.get("status") == "success") >= 2
        return has_submit and has_fills
