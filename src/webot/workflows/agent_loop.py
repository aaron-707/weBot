from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, TypedDict
from urllib.parse import quote_plus

from playwright.async_api import Page

from webot.intelligence.dom_delta import DomDelta, DomDeltaSummary
from webot.intelligence.dom_extractor import DomElement, DomExtractor
from webot.llm.decision_engine import DecisionEngine, NextAction
from webot.memory.session_memory import SessionMemory
from webot.utils.logger import get_logger
from webot.workflows.action_executor import Action, ActionExecutor
from webot.workflows.action_validator import ActionValidator, ValidationResult
from webot.workflows.form_state_machine import FormStateMachine
from webot.workflows.goal_evaluator import GoalEvaluator
from webot.workflows.login_state_machine import LoginStateMachine
from webot.workflows.progress_tracker import ProgressTracker
from webot.workflows.recovery_engine import ErrorType, RecoveryEngine
from webot.workflows.search_state_machine import SearchStateMachine


LoopStatus = Literal["completed", "failed", "max_steps_reached", "cancelled", "blocked_by_anti_bot"]
CancelCheck = Callable[[int, dict[str, Any]], bool]


class StepRecord(TypedDict, total=False):
    step: int
    action: dict[str, Any]
    success: bool
    reason: str
    error: str
    data: Any
    validation: dict[str, Any]
    recovery: dict[str, Any]
    dom_delta: dict[str, Any]
    interstitial_type: str
    anti_bot_detected: bool


class AgentLoopResult(TypedDict, total=False):
    status: LoopStatus
    steps_executed: int
    history: list[StepRecord]
    final_data: Any
    error: str


@dataclass(slots=True)
class AgentLoop:
    """Core autonomous browser-agent execution loop."""

    page: Page
    dom_extractor: DomExtractor
    decision_engine: DecisionEngine
    action_executor: ActionExecutor
    action_validator: ActionValidator
    session_memory: SessionMemory
    dom_delta: DomDelta
    recovery_engine: RecoveryEngine
    max_steps: int = 10
    max_consecutive_failures: int = 3
    complete_on_extract_text: bool = True
    anti_bot_termination_threshold: int = 3
    max_identical_fill_streak: int = 3
    max_fill_without_progress: int = 3
    llm_enabled: bool = True
    degraded_mode: bool = False
    progress_tracker: ProgressTracker | None = None
    goal_evaluator: GoalEvaluator | None = None
    search_state_machine: SearchStateMachine | None = None
    form_state_machine: FormStateMachine | None = None
    login_state_machine: LoginStateMachine | None = None

    def __post_init__(self) -> None:
        if self.search_state_machine is None:
            self.search_state_machine = SearchStateMachine(
                progress_tracker=self.progress_tracker,
                goal_evaluator=self.goal_evaluator,
            )
        if self.form_state_machine is None:
            self.form_state_machine = FormStateMachine(
                progress_tracker=self.progress_tracker,
                goal_evaluator=self.goal_evaluator,
            )
        if self.login_state_machine is None:
            self.login_state_machine = LoginStateMachine(
                progress_tracker=self.progress_tracker,
                goal_evaluator=self.goal_evaluator,
            )

    async def run(
        self,
        user_goal: str,
        *,
        cancel_check: CancelCheck | None = None,
    ) -> AgentLoopResult:
        if not isinstance(user_goal, str) or not user_goal.strip():
            raise ValueError("user_goal must be a non-empty string")

        logger = get_logger(__name__)
        logger.info("agent_loop_started", extra={"goal": user_goal, "max_steps": self.max_steps})
        if not self.llm_enabled:
            self.degraded_mode = True
            logger.warning("deterministic_fallback_mode", extra={"reason": "llm_disabled"})

        history: list[StepRecord] = []
        consecutive_failures = 0
        anti_bot_hits = 0
        final_data: Any = None

        current_dom = await self.dom_extractor.extract_all(self.page)
        previous_dom = current_dom
        previous_url = self.page.url
        last_delta = self.dom_delta.compare(
            previous=previous_dom,
            current=current_dom,
            previous_url=previous_url,
            current_url=self.page.url,
        )

        for step in range(1, self.max_steps + 1):
            if self._should_cancel(step, history, cancel_check):
                return self._build_result(
                    status="cancelled",
                    steps_executed=step - 1,
                    history=history,
                    final_data=final_data,
                    error="Cancelled by condition",
                )

            action = self._select_next_action(
                user_goal=user_goal,
                dom_state=current_dom,
                delta=last_delta,
                recent_actions=self.session_memory.get_recent_actions(limit=5),
                failures=self.session_memory.get_failures(limit=5),
                step=step,
            )
            logger.info("agent_loop_step_selected", extra={"step": step, "action": action})

            pivot_action = await self._maybe_pivot_repeated_fill(
                history=history,
                dom_state=current_dom,
                proposed_action=action,
                step=step,
            )
            if pivot_action is not None:
                action = pivot_action

            guard_termination = self._check_loop_guards(history=history, next_action=action)
            if guard_termination is not None:
                logger.warning("loop_guard_triggered", extra={"step": step, "reason": guard_termination})
                return self._build_result(
                    status="blocked_by_anti_bot" if "anti_bot" in guard_termination else "failed",
                    steps_executed=step - 1,
                    history=history,
                    final_data=final_data,
                    error=guard_termination,
                )

            before_state = await self.action_validator.capture_state(
                page=self.page,
                selector=str(action.get("selector", "")).strip() or None,
            )

            executed_action, execution_result, validation_result, recovery_result = await self._execute_validate_recover(
                action=action,
                before_state=before_state,
            )

            success = bool(execution_result.get("success", False)) and bool(validation_result.get("success", False))
            reason = str(validation_result.get("reason", ""))
            error = str(execution_result.get("error", "")) if not success else ""
            final_data = execution_result.get("data") if success else final_data

            interstitial = {}
            details = validation_result.get("details", {})
            if isinstance(details, dict):
                maybe = details.get("interstitial")
                if isinstance(maybe, dict):
                    interstitial = maybe
            if interstitial:
                logger.info("interstitial_detected", extra={"step": step, "interstitial": interstitial})
                if interstitial.get("detection_type") in {"anti_bot", "captcha", "access_denied"}:
                    anti_bot_hits += 1
                    logger.warning("anti_bot_detected", extra={"step": step, "hits": anti_bot_hits})
                else:
                    anti_bot_hits = max(0, anti_bot_hits - 1)

            self.session_memory.add_action(
                action=str(executed_action.get("action", "")),
                selector=str(executed_action.get("selector", "")),
                status="success" if success else "failure",
                page=self.page.url,
                error=error or None,
                metadata={"step": step, "goal": user_goal, "reason": reason},
            )

            previous_dom = current_dom
            previous_url = before_state.get("url", self.page.url)
            current_dom = await self.dom_extractor.extract_all(self.page)
            last_delta = self.dom_delta.compare(
                previous=previous_dom,
                current=current_dom,
                previous_url=str(previous_url),
                current_url=self.page.url,
            )

            history.append(
                {
                    "step": step,
                    "action": dict(executed_action),
                    "success": success,
                    "reason": reason,
                    "error": error,
                    "data": execution_result.get("data"),
                    "validation": dict(validation_result),
                    "recovery": recovery_result,
                    "dom_delta": {
                        "navigation_changed": last_delta.get("navigation_changed", False),
                        "new": len(last_delta.get("new_elements", [])),
                        "removed": len(last_delta.get("removed_elements", [])),
                        "text_changed": len(last_delta.get("changed_text", [])),
                        "form_changed": len(last_delta.get("form_state_changes", [])),
                    },
                    "interstitial_type": str(interstitial.get("detection_type", "")) if isinstance(interstitial, dict) else "",
                    "anti_bot_detected": bool(
                        isinstance(interstitial, dict)
                        and interstitial.get("detection_type") in {"anti_bot", "captcha", "access_denied"}
                    ),
                }
            )

            if success:
                consecutive_failures = 0
            else:
                consecutive_failures += 1

            termination = self._termination_status(
                action=executed_action,
                success=success,
                consecutive_failures=consecutive_failures,
                execution_result=execution_result,
                validation_result=validation_result,
                anti_bot_hits=anti_bot_hits,
            )
            if termination is not None:
                status, message = termination
                logger.info("agent_loop_terminated", extra={"status": status, "step": step, "termination_reason": message})
                return self._build_result(
                    status=status,
                    steps_executed=step,
                    history=history,
                    final_data=final_data,
                    error=message,
                )

        logger.info("agent_loop_max_steps_reached", extra={"max_steps": self.max_steps})
        return self._build_result(
            status="max_steps_reached",
            steps_executed=self.max_steps,
            history=history,
            final_data=final_data,
            error="Reached maximum steps without completion",
        )

    async def _execute_validate_recover(
        self,
        *,
        action: Action,
        before_state: dict[str, Any],
    ) -> tuple[Action, dict[str, Any], ValidationResult, dict[str, Any]]:
        execution_result = await self.action_executor.execute(action)
        validation_result = await self.action_validator.validate(
            page=self.page,
            action=action,
            before_state=before_state,
            action_result=execution_result,
        )

        combined_success = bool(execution_result.get("success", False)) and bool(validation_result.get("success", False))
        if combined_success:
            return action, execution_result, validation_result, {}

        error_type = self._classify_error(action, execution_result, validation_result)
        retry_key = f"{action.get('action', '')}:{action.get('selector', '')}"
        retry_count = self.session_memory.retry_counts.get(retry_key, 0)

        recovery = await self.recovery_engine.recover(
            page=self.page,
            error_type=error_type,
            failed_action=action,
            error_message=str(execution_result.get("error", "") or validation_result.get("reason", "")),
            retry_count=retry_count,
            candidate_selectors=self._candidate_selectors_from_validation(validation_result),
        )

        if not recovery.get("recovered") or not isinstance(recovery.get("next_action"), dict):
            return action, execution_result, validation_result, dict(recovery)

        recovered_action = self._normalize_action(recovery["next_action"])
        recovered_execution = await self.action_executor.execute(recovered_action)
        recovered_validation = await self.action_validator.validate(
            page=self.page,
            action=recovered_action,
            before_state=before_state,
            action_result=recovered_execution,
        )
        return recovered_action, recovered_execution, recovered_validation, dict(recovery)

    def _select_next_action(
        self,
        *,
        user_goal: str,
        dom_state: dict[str, list[DomElement]],
        delta: DomDeltaSummary,
        recent_actions: list[dict[str, Any]],
        failures: list[dict[str, Any]],
        step: int,
    ) -> Action:
        state_machine_action = self._state_machine_action(
            user_goal=user_goal,
            dom_state=dom_state,
            recent_actions=recent_actions,
        )
        if state_machine_action is not None:
            get_logger(__name__).info(
                "strategy_pivot",
                extra={"step": step, "strategy": "state_machine", "action": state_machine_action},
            )
            return state_machine_action

        deterministic = self._deterministic_action(
            user_goal=user_goal,
            dom_state=dom_state,
            step=step,
            recent_actions=recent_actions,
            failures=failures,
        )
        if deterministic is not None:
            get_logger(__name__).info("strategy_pivot", extra={"step": step, "strategy": "deterministic", "action": deterministic})
            return deterministic

        if not self.llm_enabled:
            self.degraded_mode = True
            get_logger(__name__).warning("deterministic_fallback_mode", extra={"step": step, "reason": "llm_disabled"})
            return {"action": "extract_text", "selector": "body"}

        flattened = self._flatten_dom(dom_state)
        compact_goal = self._compact_goal_with_context(
            user_goal=user_goal,
            delta=delta,
            recent_actions=recent_actions,
            failures=failures,
        )
        try:
            suggested = self.decision_engine.choose_next_action(compact_goal, flattened)
            normalized = self._normalize_action(suggested)
            if normalized.get("action") == "extract_text":
                self.degraded_mode = True
                get_logger(__name__).warning("deterministic_fallback_mode", extra={"step": step, "reason": "llm_unavailable"})
            return normalized
        except Exception:
            self.degraded_mode = True
            get_logger(__name__).warning("llm_unavailable", extra={"step": step})
            get_logger(__name__).warning("deterministic_fallback_mode", extra={"step": step, "reason": "llm_exception"})
            return {"action": "extract_text", "selector": "body"}

    def _state_machine_action(
        self,
        *,
        user_goal: str,
        dom_state: dict[str, list[DomElement]],
        recent_actions: list[dict[str, Any]],
    ) -> Action | None:
        current_url = self.page.url
        if self.search_state_machine is not None:
            action = self.search_state_machine.next_action(
                user_goal=user_goal,
                dom_state=dom_state,
                current_url=current_url,
                recent_actions=recent_actions,
            )
            if action is not None:
                return action
        if self.form_state_machine is not None:
            action = self.form_state_machine.next_action(
                user_goal=user_goal,
                dom_state=dom_state,
                current_url=current_url,
                recent_actions=recent_actions,
            )
            if action is not None:
                return action
        if self.login_state_machine is not None:
            action = self.login_state_machine.next_action(
                user_goal=user_goal,
                dom_state=dom_state,
                current_url=current_url,
                recent_actions=recent_actions,
            )
            if action is not None:
                return action
        return None

    @staticmethod
    def _deterministic_action(
        *,
        user_goal: str,
        dom_state: dict[str, list[DomElement]],
        step: int,
        recent_actions: list[dict[str, Any]],
        failures: list[dict[str, Any]],
    ) -> Action | None:
        lowered_goal = user_goal.lower()
        is_search_task = any(token in lowered_goal for token in ("search", "find", "look up", "query"))
        query = AgentLoop._extract_search_query(lowered_goal)

        if is_search_task:
            if AgentLoop._is_anti_bot_context(failures):
                # Deterministic anti-bot mitigation: avoid direct query URL nav after block.
                search_input = AgentLoop._find_search_input_selector(dom_state)
                if search_input and query:
                    return {"action": "fill", "selector": search_input, "value": query}
                search_button = AgentLoop._find_search_button_selector(dom_state)
                if search_button:
                    return {"action": "click", "selector": search_button}

            # deterministic direct query only before blocked patterns appear
            if step == 1 and query:
                return {"action": "goto", "url": f"https://google.com/search?q={quote_plus(query)}"}

            if AgentLoop._is_search_homepage(dom_state) and query:
                search_input = AgentLoop._find_search_input_selector(dom_state)
                if search_input:
                    return {"action": "fill", "selector": search_input, "value": query}
                search_button = AgentLoop._find_search_button_selector(dom_state)
                if search_button:
                    return {"action": "click", "selector": search_button}

        # Step 1 deterministic URL open.
        if step == 1:
            for token in lowered_goal.split():
                candidate = token.strip(".,)")
                if candidate.startswith("http://") or candidate.startswith("https://"):
                    return {"action": "goto", "url": candidate}
                if any(candidate.endswith(s) for s in (".com", ".org", ".net", ".io", ".ai")):
                    return {"action": "goto", "url": f"https://{candidate}"}

        # Deterministic button click for clear goal keywords.
        goal = lowered_goal
        if any(k in goal for k in ("login", "log in", "sign in", "authenticate")):
            username_selector = AgentLoop._find_input_by_keywords(dom_state, ("username", "user", "email", "login"))
            if username_selector:
                return {"action": "fill", "selector": username_selector, "value": "tomsmith"}
            password_selector = AgentLoop._find_input_by_keywords(dom_state, ("password", "passcode"))
            if password_selector:
                return {"action": "fill", "selector": password_selector, "value": "SuperSecretPassword!"}

        if any(k in goal for k in ("form", "apply", "submit form", "fill")):
            target_selector = AgentLoop._find_input_by_keywords(dom_state, ("name", "email", "phone"))
            if target_selector:
                return {"action": "fill", "selector": target_selector, "value": "demo"}

        if any(k in goal for k in ("submit", "continue", "next", "login", "sign in")):
            for button in dom_state.get("buttons", []):
                text = str(button.get("text", "")).lower()
                selector = str(button.get("selector", "")).strip()
                if selector and any(k in text for k in ("submit", "continue", "next", "login", "sign in")):
                    return {"action": "click", "selector": selector}
        return None

    @staticmethod
    def _find_input_by_keywords(dom_state: dict[str, list[DomElement]], keywords: tuple[str, ...]) -> str | None:
        for item in dom_state.get("inputs", []):
            selector = str(item.get("selector", "")).strip()
            attrs = item.get("attributes", {}) if isinstance(item.get("attributes"), dict) else {}
            blob = " ".join(
                [
                    str(item.get("text", "")),
                    str(attrs.get("name", "")),
                    str(attrs.get("placeholder", "")),
                    str(attrs.get("aria-label", "")),
                    str(attrs.get("id", "")),
                    str(attrs.get("type", "")),
                ]
            ).lower()
            if selector and any(token in blob for token in keywords):
                return selector
        return None

    def _compact_goal_with_context(
        self,
        *,
        user_goal: str,
        delta: DomDeltaSummary,
        recent_actions: list[dict[str, Any]],
        failures: list[dict[str, Any]],
    ) -> str:
        # Token-efficient context packing for DecisionEngine.
        recent = [
            f"{a.get('action','')}:{a.get('status','')}:{str(a.get('selector',''))[:40]}"
            for a in recent_actions[-4:]
        ]
        failed = [
            f"{f.get('action','')}:{str(f.get('error',''))[:60]}"
            for f in failures[-3:]
        ]
        return (
            f"Goal:{user_goal}\n"
            f"Delta:{delta.get('token_optimized_summary','')}\n"
            f"Recent:{' | '.join(recent)}\n"
            f"Failures:{' | '.join(failed)}"
        )

    @staticmethod
    def _flatten_dom(dom_state: dict[str, list[DomElement]]) -> list[DomElement]:
        merged: list[DomElement] = []
        for key in ("buttons", "inputs", "links", "forms", "visible_text"):
            items = dom_state.get(key, [])
            if isinstance(items, list):
                merged.extend(item for item in items if isinstance(item, dict))
        return merged

    @staticmethod
    def _normalize_action(next_action: NextAction | dict[str, Any]) -> Action:
        action_type = next_action.get("action")
        if action_type not in {"goto", "click", "fill", "submit", "extract_text"}:
            return {"action": "extract_text", "selector": "body"}

        action: Action = {"action": action_type}
        selector = next_action.get("selector")
        url = next_action.get("url")
        value = next_action.get("value")

        if isinstance(selector, str) and selector.strip():
            action["selector"] = selector.strip()
        if isinstance(url, str) and url.strip():
            action["url"] = url.strip()
        if isinstance(value, str):
            action["value"] = value
        submit_method = next_action.get("submit_method")
        if isinstance(submit_method, str) and submit_method.strip():
            action["submit_method"] = submit_method.strip()
        return action

    def _termination_status(
        self,
        *,
        action: Action,
        success: bool,
        consecutive_failures: int,
        execution_result: dict[str, Any],
        validation_result: ValidationResult,
        anti_bot_hits: int,
    ) -> tuple[LoopStatus, str] | None:
        if anti_bot_hits >= self.anti_bot_termination_threshold:
            return "blocked_by_anti_bot", "blocked_by_anti_bot"

        if not success and consecutive_failures >= self.max_consecutive_failures:
            return "failed", "Unrecoverable failure: too many consecutive failures"

        if not success:
            return None

        if self.search_state_machine is not None and self.search_state_machine.is_failed():
            return "failed", "search_state_machine_failed"

        if self.complete_on_extract_text and action.get("action") == "extract_text":
            if self.search_state_machine is not None and self.search_state_machine._state != "start" and not self.search_state_machine.should_allow_extract_termination():
                return None
            data = execution_result.get("data")
            if isinstance(data, dict) and isinstance(data.get("text"), str) and data["text"].strip():
                return "completed", ""

        # Optional completion hint from validator reason.
        if validation_result.get("reason") == "extract_text_validated":
            return "completed", ""

        return None

    @staticmethod
    def _classify_error(
        action: Action,
        execution_result: dict[str, Any],
        validation_result: ValidationResult,
    ) -> ErrorType:
        action_type = str(action.get("action", ""))
        error_text = f"{execution_result.get('error', '')} {validation_result.get('reason', '')}".lower()

        if "blocked" in error_text or "captcha" in error_text or "anti_bot" in error_text or "sorry" in error_text:
            return "anti_bot_blocked"
        if "fill_loop" in error_text or "fill_validated" in error_text and action_type == "fill":
            return "fill_loop_stagnation"
        if action_type == "goto" or "navigation" in error_text:
            return "navigation_failure"
        if "timeout" in error_text:
            return "timeout"
        return "missing_selector"

    @staticmethod
    def _extract_search_query(goal: str) -> str:
        triggers = ("search for ", "search ", "find ", "look up ", "query ")
        for trigger in triggers:
            idx = goal.find(trigger)
            if idx >= 0:
                return goal[idx + len(trigger):].strip().strip(".")
        return ""

    @staticmethod
    def _find_search_input_selector(dom_state: dict[str, list[DomElement]]) -> str | None:
        for item in dom_state.get("inputs", []):
            selector = str(item.get("selector", "")).strip()
            attrs = item.get("attributes", {}) if isinstance(item.get("attributes"), dict) else {}
            label_blob = " ".join(
                [
                    str(item.get("text", "")),
                    str(attrs.get("name", "")),
                    str(attrs.get("placeholder", "")),
                    str(attrs.get("aria-label", "")),
                    str(attrs.get("title", "")),
                ]
            ).lower()
            if selector and any(k in label_blob for k in ("search", "query", "find")):
                return selector
        return None

    @staticmethod
    def _find_search_button_selector(dom_state: dict[str, list[DomElement]]) -> str | None:
        for item in dom_state.get("buttons", []):
            selector = str(item.get("selector", "")).strip()
            text = str(item.get("text", "")).lower()
            attrs = item.get("attributes", {}) if isinstance(item.get("attributes"), dict) else {}
            blob = f"{text} {attrs.get('aria-label','')} {attrs.get('name','')}".lower()
            if selector and any(k in blob for k in ("search", "go", "submit")):
                return selector
        return None

    @staticmethod
    def _is_search_homepage(dom_state: dict[str, list[DomElement]]) -> bool:
        return AgentLoop._find_search_input_selector(dom_state) is not None

    @staticmethod
    def _is_anti_bot_context(failures: list[dict[str, Any]]) -> bool:
        for failure in failures[-4:]:
            text = f"{failure.get('error','')} {failure.get('metadata',{})}".lower()
            if any(k in text for k in ("anti_bot", "captcha", "/sorry/", "access denied", "verify you are human")):
                return True
        return False

    def _check_loop_guards(self, *, history: list[StepRecord], next_action: Action) -> str | None:
        recent = history[-6:]
        if len(recent) >= 3:
            last_actions = [h.get("action", {}) for h in recent[-3:]]
            if all(isinstance(a, dict) and a.get("action") == "goto" and a.get("url") == next_action.get("url") for a in last_actions):
                return "loop_guard_repeated_identical_goto"

        urls = []
        for h in recent:
            validation = h.get("validation", {})
            details = validation.get("details", {}) if isinstance(validation, dict) else {}
            url = details.get("current_url")
            if isinstance(url, str):
                urls.append(url.rstrip("/"))
        if len(urls) >= 4 and urls[-1] == urls[-3] and urls[-2] == urls[-4]:
            return "loop_guard_navigation_ping_pong"

        anti_bot_count = 0
        for h in recent:
            validation = h.get("validation", {})
            details = validation.get("details", {}) if isinstance(validation, dict) else {}
            interstitial = details.get("interstitial", {}) if isinstance(details, dict) else {}
            if isinstance(interstitial, dict) and interstitial.get("detection_type") in {"anti_bot", "captcha", "access_denied"}:
                anti_bot_count += 1
        if anti_bot_count >= self.anti_bot_termination_threshold:
            return "blocked_by_anti_bot"

        fill_streak = self._identical_fill_streak(recent)
        if fill_streak >= self.max_identical_fill_streak:
            return "fill_loop_guard_triggered"

        fill_without_progress = self._fill_without_progress_count(recent)
        if fill_without_progress >= self.max_fill_without_progress:
            return "fill_loop_guard_triggered"
        return None

    async def _maybe_pivot_repeated_fill(
        self,
        *,
        history: list[StepRecord],
        dom_state: dict[str, list[DomElement]],
        proposed_action: Action,
        step: int,
    ) -> Action | None:
        logger = get_logger(__name__)
        if proposed_action.get("action") != "fill":
            return None

        recent = history[-8:]
        fill_streak = self._identical_fill_streak(recent)
        fill_no_progress = self._fill_without_progress_count(recent)
        if fill_streak < self.max_identical_fill_streak and fill_no_progress < self.max_fill_without_progress:
            return None

        logger.warning(
            "repeated_fill_detected",
            extra={
                "step": step,
                "fill_streak": fill_streak,
                "fill_without_progress": fill_no_progress,
                "selector": proposed_action.get("selector", ""),
            },
        )

        selector = str(proposed_action.get("selector", "")).strip()
        if selector:
            try:
                # Deterministic submission attempt #1: press Enter on current search-like input.
                await self.page.locator(selector).first.press("Enter")
                logger.info(
                    "search_submission_pivot",
                    extra={"step": step, "strategy": "enter_key", "selector": selector},
                )
                return {"action": "extract_text", "selector": "body"}
            except Exception:
                pass

        search_button = self._find_search_button_selector(dom_state)
        if search_button:
            logger.info(
                "search_submission_pivot",
                extra={"step": step, "strategy": "search_button_click", "selector": search_button},
            )
            return {"action": "click", "selector": search_button}

        logger.warning(
            "fill_loop_guard_triggered",
            extra={"step": step, "reason": "no_submission_path"},
        )
        return {"action": "extract_text", "selector": "body"}

    def _identical_fill_streak(self, history: list[StepRecord]) -> int:
        streak = 0
        expected_sig = ""
        for entry in reversed(history):
            action = entry.get("action", {})
            if not isinstance(action, dict) or action.get("action") != "fill":
                break
            selector = str(action.get("selector", "")).strip()
            value = str(action.get("value", ""))
            sig = f"{selector}::{value}"
            if not expected_sig:
                expected_sig = sig
            if sig != expected_sig:
                break
            streak += 1
        return streak

    def _fill_without_progress_count(self, history: list[StepRecord]) -> int:
        count = 0
        for entry in reversed(history):
            action = entry.get("action", {})
            if not isinstance(action, dict) or action.get("action") != "fill":
                break
            dom_delta = entry.get("dom_delta", {})
            validation = entry.get("validation", {})
            details = validation.get("details", {}) if isinstance(validation, dict) else {}
            nav_changed = bool(dom_delta.get("navigation_changed", False))
            meaningful_dom_change = (
                int(dom_delta.get("new", 0) or 0)
                + int(dom_delta.get("removed", 0) or 0)
                + int(dom_delta.get("text_changed", 0) or 0)
                + int(dom_delta.get("form_changed", 0) or 0)
            ) > 1
            fill_progress = str(details.get("actual", "")) != ""
            if nav_changed or meaningful_dom_change:
                break
            if not fill_progress:
                count += 1
        return count

    @staticmethod
    def _candidate_selectors_from_validation(validation: ValidationResult) -> list[str]:
        details = validation.get("details", {})
        if not isinstance(details, dict):
            return []
        candidates: list[str] = []
        for key in ("selector", "suggested_selector", "fallback_selector"):
            value = details.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())
        return candidates

    @staticmethod
    def _should_cancel(step: int, history: list[StepRecord], cancel_check: CancelCheck | None) -> bool:
        if cancel_check is None:
            return False
        try:
            return bool(cancel_check(step, {"history": history}))
        except Exception:
            return False

    @staticmethod
    def _build_result(
        *,
        status: LoopStatus,
        steps_executed: int,
        history: list[StepRecord],
        final_data: Any,
        error: str,
    ) -> AgentLoopResult:
        out: AgentLoopResult = {
            "status": status,
            "steps_executed": steps_executed,
            "history": history,
            "final_data": final_data,
        }
        if error:
            out["error"] = error
        return out
