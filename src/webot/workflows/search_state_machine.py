from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from time import perf_counter
from typing import Any

from webot.intelligence.dom_extractor import DomElement
from webot.workflows.action_executor import Action


class SearchState(str, Enum):
    START = "start"
    OPEN_PROVIDER = "open_provider"
    DETECT_SEARCH_BOX = "detect_search_box"
    FILL_QUERY = "fill_query"
    SUBMIT_ENTER = "submit_enter"
    SUBMIT_BUTTON = "submit_button"
    DETECT_RESULTS_PAGE = "detect_results_page"
    EXTRACT_RESULTS = "extract_results"
    VALIDATE_RESULTS = "validate_results"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(slots=True)
class SearchStateMachine:
    progress_tracker: Any | None = None
    goal_evaluator: Any | None = None
    _state: SearchState = SearchState.START
    _query: str = ""
    _search_selector: str = ""
    _provider_index: int = 0
    _provider_started_at: float = 0.0
    _provider_metrics: list[dict[str, Any]] = field(default_factory=list)
    _attempted_submit_button: bool = False
    _target_provider: str = ""

    def next_action(
        self,
        *,
        user_goal: str,
        dom_state: dict[str, list[DomElement]],
        current_url: str,
        recent_actions: list[dict[str, Any]],
    ) -> Action | None:
        if not self._is_search_goal(user_goal):
            return None
        self._query = self._query or self._extract_query(user_goal) or "query"

        if not self._target_provider:
            if self._is_wikipedia_context(current_url=current_url, user_goal=user_goal):
                self._target_provider = "wikipedia"
            else:
                self._target_provider = "duckduckgo"

        if self._state == SearchState.START:
            self._provider_started_at = perf_counter()
            self._transition(SearchState.OPEN_PROVIDER)
        if self._state == SearchState.OPEN_PROVIDER:
            self._transition(SearchState.DETECT_SEARCH_BOX, completed=True)
            return {"action": "goto", "url": self._provider()["url"]}
        if self._state == SearchState.DETECT_SEARCH_BOX:
            if self._target_provider == "wikipedia":
                selector = "#searchInput"
            else:
                selector = self._find_search_input(dom_state)
            if not selector:
                return self._rotate_or_fail("search_box_not_found")
            self._search_selector = selector
            self._transition(SearchState.FILL_QUERY, completed=True)
        if self._state == SearchState.FILL_QUERY:
            self._transition(SearchState.SUBMIT_ENTER, completed=True)
            return {"action": "fill", "selector": self._search_selector, "value": self._query}
        if self._state == SearchState.SUBMIT_ENTER:
            self._transition(SearchState.DETECT_RESULTS_PAGE, completed=True)
            return {"action": "submit", "selector": self._search_selector, "submit_method": "enter"}
        if self._state == SearchState.SUBMIT_BUTTON:
            button = self._find_submit_button(dom_state)
            if not button:
                return self._rotate_or_fail("submit_button_not_found")
            self._attempted_submit_button = True
            self._transition(SearchState.DETECT_RESULTS_PAGE, completed=True)
            return {"action": "submit", "selector": button, "submit_method": "click"}
        if self._state == SearchState.DETECT_RESULTS_PAGE:
            if self._looks_like_anti_bot(current_url):
                return self._rotate_or_fail("anti_bot_detected")
            if self._looks_like_results_page(dom_state, current_url):
                self._transition(SearchState.EXTRACT_RESULTS, completed=True)
            elif not self._attempted_submit_button:
                self._transition(SearchState.SUBMIT_BUTTON, failed=True, reason="enter_submit_not_effective")
                return {"action": "submit", "selector": self._search_selector, "submit_method": "click"}
            else:
                return self._rotate_or_fail("results_page_not_detected")
        if self._state == SearchState.EXTRACT_RESULTS:
            self._transition(SearchState.VALIDATE_RESULTS, completed=True)
            extract_selector = "#mw-content-text" if self._target_provider == "wikipedia" else "body"
            return {"action": "extract_text", "selector": extract_selector}
        if self._state == SearchState.VALIDATE_RESULTS:
            if self._results_valid(current_url, recent_actions):
                self._record_metric(success=True, confidence=0.95, reason="validated")
                self._transition(SearchState.COMPLETED, completed=True)
                extract_selector = "#mw-content-text" if self._target_provider == "wikipedia" else "body"
                return {"action": "extract_text", "selector": extract_selector}
            return self._rotate_or_fail("search_validation_failed")
        extract_selector = "#mw-content-text" if self._target_provider == "wikipedia" else "body"
        return {"action": "extract_text", "selector": extract_selector}

    def should_allow_extract_termination(self) -> bool:
        return self._state == SearchState.COMPLETED

    def is_failed(self) -> bool:
        return self._state == SearchState.FAILED

    def provider_metrics(self) -> list[dict[str, Any]]:
        return list(self._provider_metrics)

    def _rotate_or_fail(self, reason: str) -> Action:
        self._record_metric(success=False, confidence=0.0, reason=reason)
        if self._provider_index + 1 < len(self._providers()):
            self._provider_index += 1
            self._search_selector = ""
            self._attempted_submit_button = False
            self._provider_started_at = perf_counter()
            self._transition(SearchState.OPEN_PROVIDER, failed=True, reason=reason)
            return {"action": "goto", "url": self._provider()["url"]}
        self._transition(SearchState.FAILED, failed=True, reason=reason)
        return {"action": "extract_text", "selector": "body"}

    def _record_metric(self, *, success: bool, confidence: float, reason: str) -> None:
        metric = {
            "provider": self._provider()["name"],
            "success": success,
            "completion_confidence": round(confidence, 4),
            "anti_bot_detections": 1 if "anti_bot" in reason else 0,
            "retries": 0,
            "duration_seconds": round(max(perf_counter() - self._provider_started_at, 0.0), 3),
            "reason": reason,
        }
        self._provider_metrics.append(metric)

    def _transition(self, state: SearchState, *, completed: bool = False, failed: bool = False, reason: str = "") -> None:
        from webot.utils.logger import get_logger

        logger = get_logger(__name__)
        if state != self._state:
            self._state = state
            logger.info("state_entered", extra={"machine": "search", "state": state.value, "provider": self._provider()["name"]})
        if completed:
            logger.info("state_completed", extra={"machine": "search", "state": state.value, "provider": self._provider()["name"]})
        if failed:
            logger.warning("state_failed", extra={"machine": "search", "state": state.value, "provider": self._provider()["name"], "reason": reason})

    @staticmethod
    def _is_search_goal(goal: str) -> bool:
        lowered = goal.lower()
        return any(token in lowered for token in ("search", "find", "look up", "query"))

    # Matches trailing site-reference clauses like " on duckduckgo", " using google.com",
    # " via bing" — a bare word or domain-like token after the preposition.
    _SITE_REF_RE = re.compile(
        r"\s+(?:on|using|via)\s+[A-Za-z0-9](?:[A-Za-z0-9\-]*\.?[A-Za-z0-9]+)*\s*$",
        re.IGNORECASE,
    )

    @staticmethod
    def _extract_query(goal: str) -> str:
        lowered = goal.lower()
        for trigger in ("search for ", "search ", "find ", "look up ", "query "):
            idx = lowered.find(trigger)
            if idx >= 0:
                raw = goal[idx + len(trigger):].strip().strip(".")
                return SearchStateMachine._SITE_REF_RE.sub("", raw).strip()
        return ""

    @staticmethod
    def _find_search_input(dom_state: dict[str, list[DomElement]]) -> str:
        for item in dom_state.get("inputs", []):
            selector = str(item.get("selector", "")).strip()
            attrs = item.get("attributes", {}) if isinstance(item.get("attributes"), dict) else {}
            blob = f"{attrs.get('name','')} {attrs.get('placeholder','')} {attrs.get('aria-label','')} {attrs.get('id','')}".lower()
            if selector and any(token in blob for token in ("search", "query", "q")):
                return selector
        return ""

    @staticmethod
    def _find_submit_button(dom_state: dict[str, list[DomElement]]) -> str:
        for item in dom_state.get("buttons", []):
            selector = str(item.get("selector", "")).strip()
            text = str(item.get("text", "")).lower()
            attrs = item.get("attributes", {}) if isinstance(item.get("attributes"), dict) else {}
            blob = f"{text} {attrs.get('aria-label','')} {attrs.get('name','')}".lower()
            if selector and any(token in blob for token in ("search", "go", "submit")):
                return selector
        return ""

    @staticmethod
    def _is_wikipedia_context(*, current_url: str, user_goal: str) -> bool:
        combined = f"{user_goal} {current_url}".lower()
        return "wikipedia" in combined or "wiki" in current_url.lower()

    @staticmethod
    def _looks_like_results_page(dom_state: dict[str, list[DomElement]], current_url: str) -> bool:
        lowered = current_url.lower()
        return len(dom_state.get("links", [])) >= 5 or "q=" in lowered or "/search" in lowered or "wiki" in lowered

    @staticmethod
    def _looks_like_anti_bot(current_url: str) -> bool:
        lowered = current_url.lower()
        return any(token in lowered for token in ("/sorry/", "captcha", "challenge", "verify you are human", "access denied"))

    @staticmethod
    def _results_valid(current_url: str, recent_actions: list[dict[str, Any]]) -> bool:
        extracted = any(item.get("action") == "extract_text" and item.get("status") == "success" for item in recent_actions[-5:])
        lowered = current_url.lower()
        return extracted and ("search" in lowered or "q=" in lowered or "wiki" in lowered or "wikipedia" in lowered)

    def _providers(self) -> list[dict[str, str]]:
        if self._target_provider == "wikipedia":
            return [
                {"name": "wikipedia", "url": "https://www.wikipedia.org"},
            ]
        return [
            {"name": "duckduckgo", "url": "https://duckduckgo.com"},
            {"name": "bing", "url": "https://www.bing.com"},
        ]

    def _provider(self) -> dict[str, str]:
        providers = self._providers()
        idx = min(max(self._provider_index, 0), len(providers) - 1)
        return providers[idx]
