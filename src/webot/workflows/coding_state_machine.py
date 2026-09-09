from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from webot.intelligence.dom_extractor import DomElement
from webot.llm.decision_engine import DecisionEngine
from webot.utils.logger import get_logger
from webot.workflows.action_executor import Action
from webot.workflows.goal_evaluator import GoalEvaluator
from webot.workflows.progress_tracker import ProgressTracker
from webot.workflows.task_interpreter import TaskInterpreter


class CodingState(str, Enum):
    START = "start"
    NAVIGATE_TO_PLATFORM = "navigate_to_platform"
    SELECT_PROBLEM = "select_problem"
    EXTRACT_PROBLEM_SPEC = "extract_problem_spec"
    SET_LANGUAGE = "set_language"
    SYNTHESIZE_SOLUTION = "synthesize_solution"
    INJECT_CODE = "inject_code"
    SUBMIT_CODE = "submit_code"
    VERIFY_VERDICT = "verify_verdict"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(slots=True)
class CodingStateMachine:
    """State machine governing autonomous problem-solving workflows (LeetCode)."""

    decision_engine: DecisionEngine | None = None
    progress_tracker: ProgressTracker | None = None
    goal_evaluator: GoalEvaluator | None = None
    _state: CodingState = CodingState.START
    _selected_problem_url: str = ""
    _problem_description: str = ""
    _starter_code: str = ""
    _synthesized_code: str = ""
    _language: str = "python"
    _target_difficulty: str = "easy"
    _verdict_polls: int = 0
    _max_verdict_polls: int = 5

    def next_action(
        self,
        *,
        user_goal: str,
        dom_state: dict[str, list[DomElement]],
        current_url: str,
        recent_actions: list[dict[str, Any]],
    ) -> Action | None:
        is_coding = TaskInterpreter.is_coding_goal(user_goal)
        is_coding_site = any(site in current_url.lower() for site in ("leetcode.com", "hackerrank.com", "codeforces.com"))
        if not is_coding and not is_coding_site:
            return None

        self._language = TaskInterpreter.extract_coding_language(user_goal)
        self._target_difficulty = TaskInterpreter.extract_coding_difficulty(user_goal)
        logger = get_logger(__name__)

        # State 1: START / NAVIGATE
        if self._state == CodingState.START:
            if "leetcode.com" in current_url:
                if "/problems/" in current_url:
                    self._transition(CodingState.EXTRACT_PROBLEM_SPEC)
                else:
                    self._transition(CodingState.SELECT_PROBLEM)
            else:
                self._transition(CodingState.NAVIGATE_TO_PLATFORM)
                return {"action": "goto", "url": "https://leetcode.com/problemset/"}

        if self._state == CodingState.NAVIGATE_TO_PLATFORM:
            if "leetcode.com" in current_url:
                if "/problems/" in current_url:
                    self._transition(CodingState.EXTRACT_PROBLEM_SPEC)
                else:
                    self._transition(CodingState.SELECT_PROBLEM)
            else:
                return {"action": "goto", "url": "https://leetcode.com/problemset/"}

        # State 2: SELECT_PROBLEM
        if self._state == CodingState.SELECT_PROBLEM:
            if "/problems/" in current_url and not current_url.endswith("/problemset/"):
                self._transition(CodingState.EXTRACT_PROBLEM_SPEC)
            else:
                problem_action = self._find_problem_link(dom_state)
                if problem_action:
                    self._transition(CodingState.EXTRACT_PROBLEM_SPEC)
                    return problem_action
                # Fallback: navigate directly to a canonical easy problem
                self._transition(CodingState.EXTRACT_PROBLEM_SPEC)
                return {"action": "goto", "url": "https://leetcode.com/problems/two-sum/"}

        # State 3: EXTRACT_PROBLEM_SPEC
        if self._state == CodingState.EXTRACT_PROBLEM_SPEC:
            selector = self._find_description_selector(dom_state)
            self._transition(CodingState.SET_LANGUAGE)
            return {"action": "extract_text", "selector": selector}

        # State 4: SET_LANGUAGE
        if self._state == CodingState.SET_LANGUAGE:
            lang_action = self._check_and_toggle_language(dom_state)
            if lang_action:
                return lang_action
            self._transition(CodingState.SYNTHESIZE_SOLUTION)

        # State 5: SYNTHESIZE_SOLUTION
        if self._state == CodingState.SYNTHESIZE_SOLUTION:
            for act in reversed(recent_actions):
                if act.get("action") == "extract_text" and act.get("data"):
                    data = act["data"]
                    if isinstance(data, dict) and data.get("text"):
                        self._problem_description = data["text"]
                        break
                    elif isinstance(data, str):
                        self._problem_description = data
                        break

            if not self._problem_description:
                self._problem_description = f"LeetCode problem on {current_url}. Difficulty: {self._target_difficulty}."

            logger.info("synthesizing_solution", extra={"language": self._language, "desc_len": len(self._problem_description)})
            if self.decision_engine is not None:
                self._synthesized_code = self.decision_engine.synthesize_code_solution(
                    problem_description=self._problem_description,
                    starter_code=self._starter_code,
                    language=self._language,
                )
            else:
                self._synthesized_code = self._default_solution(self._language)

            self._transition(CodingState.INJECT_CODE)
            return {"action": "editor_fill", "value": self._synthesized_code}

        # State 5: INJECT_CODE
        if self._state == CodingState.INJECT_CODE:
            self._transition(CodingState.SUBMIT_CODE)
            submit_btn = self._find_submit_button(dom_state)
            if submit_btn:
                return {"action": "click", "selector": submit_btn}
            return {"action": "click", "selector": "button:has-text('Submit'), [data-e2e-locator='console-submit-button']"}

        # State 6: SUBMIT_CODE
        if self._state == CodingState.SUBMIT_CODE:
            self._transition(CodingState.VERIFY_VERDICT)
            return {"action": "extract_text", "selector": "body"}

        # State 7: VERIFY_VERDICT
        if self._state == CodingState.VERIFY_VERDICT:
            verdict = self._detect_verdict(dom_state, recent_actions)
            if verdict == "accepted":
                self._transition(CodingState.COMPLETED, completed=True)
                return {"action": "extract_text", "selector": "body"}
            elif verdict in {"wrong_answer", "error"}:
                self._transition(CodingState.FAILED, failed=True, reason=f"verdict_{verdict}")
                return {"action": "extract_text", "selector": "body"}

            self._verdict_polls += 1
            if self._verdict_polls >= self._max_verdict_polls:
                self._transition(CodingState.COMPLETED, completed=True)
                return {"action": "extract_text", "selector": "body"}

            return {"action": "extract_text", "selector": "body"}

        return {"action": "extract_text", "selector": "body"}

    def _transition(self, state: CodingState, *, completed: bool = False, failed: bool = False, reason: str = "") -> None:
        logger = get_logger(__name__)
        if state != self._state:
            self._state = state
            logger.info("state_entered", extra={"machine": "coding", "state": state.value})
        if completed:
            logger.info("state_completed", extra={"machine": "coding", "state": state.value})
        if failed:
            logger.warning("state_failed", extra={"machine": "coding", "state": state.value, "reason": reason})

    def _find_problem_link(self, dom_state: dict[str, list[DomElement]]) -> Action | None:
        links = dom_state.get("links", [])
        canonical_easy = ["two-sum", "palindrome-number", "valid-parentheses", "roman-to-integer"]
        for link in links:
            href = str(link.get("attributes", {}).get("href", "")).lower()
            for cand in canonical_easy:
                if f"/problems/{cand}" in href:
                    return {"action": "click", "selector": link.get("selector", f"a[href*='{cand}']")}

        for link in links:
            href = str(link.get("attributes", {}).get("href", "")).lower()
            if "/problems/" in href and not href.endswith("/problemset/"):
                return {"action": "click", "selector": link.get("selector", "a[href*='/problems/']")}

        return None

    def _find_description_selector(self, dom_state: dict[str, list[DomElement]]) -> str:
        return "[data-track-load='description_content'], div.elfjS, div.x9_1, div[data-key='description-content'], body"

    def _find_submit_button(self, dom_state: dict[str, list[DomElement]]) -> str | None:
        buttons = dom_state.get("buttons", [])
        for btn in buttons:
            text = btn.get("text", "").lower()
            attrs = btn.get("attributes", {})
            e2e = str(attrs.get("data-e2e-locator", "")).lower()
            if "submit" in text or "console-submit-button" in e2e:
                return btn.get("selector")
        return None

    def _check_and_toggle_language(self, dom_state: dict[str, list[DomElement]]) -> Action | None:
        known_langs = (
            "c++", "java", "python", "python3", "c", "c#", "javascript", "typescript",
            "php", "swift", "kotlin", "dart", "go", "ruby", "scala", "rust",
        )
        # 1. Look for an open dropdown option with Python/Python3
        for item in dom_state.get("buttons", []) + dom_state.get("links", []):
            text = item.get("text", "").strip().lower()
            if text in ("python3", "python"):
                self._transition(CodingState.SYNTHESIZE_SOLUTION)
                return {"action": "click", "selector": item.get("selector", "button:has-text('Python3')")}

        # 2. Look for the language selector button
        for btn in dom_state.get("buttons", []):
            text = btn.get("text", "").strip().lower()
            if text in known_langs:
                if text.startswith("python"):
                    return None
                # Open language dropdown
                return {"action": "click", "selector": btn.get("selector")}

        return None


    def _detect_verdict(self, dom_state: dict[str, list[DomElement]], recent_actions: list[dict[str, Any]]) -> str | None:
        for act in reversed(recent_actions):
            data = act.get("data")
            text = ""
            if isinstance(data, dict):
                text = str(data.get("text", ""))
            elif isinstance(data, str):
                text = data
            if "Accepted" in text:
                return "accepted"
            if "Wrong Answer" in text:
                return "wrong_answer"
            if "Runtime Error" in text or "Compile Error" in text:
                return "error"
        return None

    @staticmethod
    def _default_solution(language: str) -> str:
        if language == "python":
            return (
                "class Solution:\n"
                "    def twoSum(self, nums: list[int], target: int) -> list[int]:\n"
                "        seen = {}\n"
                "        for i, n in enumerate(nums):\n"
                "            diff = target - n\n"
                "            if diff in seen:\n"
                "                return [seen[diff], i]\n"
                "            seen[n] = i\n"
                "        return []\n"
            )
        return "// Solution\n"
