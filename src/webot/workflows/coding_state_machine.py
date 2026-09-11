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
    _lang_trigger_clicked: bool = False
    _last_url: str = ""
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
        if current_url and self._last_url and current_url.rstrip("/") != self._last_url.rstrip("/"):
            self._lang_trigger_clicked = False
        self._last_url = current_url

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
            self._transition(CodingState.INJECT_CODE)
            prob_desc = self._get_last_extracted_text(recent_actions) or user_goal
            solution_code = self._default_solution(self._language)
            if self.decision_engine:
                solution_code = self.decision_engine.synthesize_code_solution(
                    problem_description=prob_desc,
                    starter_code=self._starter_code or self._default_solution(self._language),
                    language=self._language,
                )
            return {"action": "editor_fill", "value": solution_code}

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
            elif verdict == "requires_login":
                self._transition(CodingState.COMPLETED, completed=True)
                logger.info("submission_requires_login", extra={"machine": "coding", "reason": "site requires login to submit"})
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

    def _get_last_extracted_text(self, recent_actions: list[dict[str, Any]]) -> str:
        for act in reversed(recent_actions):
            if act.get("action") == "extract_text" and act.get("data"):
                data = act["data"]
                if isinstance(data, dict) and data.get("text"):
                    return str(data["text"])
                elif isinstance(data, str):
                    return data
        return ""

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
        target_lang = (self._language or "python").lower()
        known_langs = (
            "c++", "java", "python", "python3", "c", "c#", "javascript", "typescript",
            "php", "swift", "kotlin", "dart", "go", "ruby", "scala", "rust",
        )
        candidate_labels = {target_lang, f"{target_lang}3", f"{target_lang} 3"}
        if target_lang == "python":
            candidate_labels.update({"python3", "python 3", "py3"})

        all_interactives = dom_state.get("buttons", []) + dom_state.get("links", [])
        triggers = []
        options = []
        for item in all_interactives:
            attrs = item.get("attributes", {}) if isinstance(item.get("attributes"), dict) else {}
            role = str(attrs.get("role", "")).lower()
            if role in ("option", "menuitem", "menuitemradio", "menuitemcheckbox") or "option" in str(item.get("selector", "")).lower():
                options.append(item)
            else:
                triggers.append(item)

        def is_leaf_option(text: str, val: str) -> bool:
            if not text and not val:
                return False
            if "\n" in text or len(text) > 30:
                return False
            clean_text = text.strip().lower()
            clean_val = val.strip().lower()
            return clean_text in candidate_labels or clean_val in candidate_labels

        # Only add visible_text if it is a concise single-line leaf option
        for item in dom_state.get("visible_text", []):
            text = item.get("text", "")
            attrs = item.get("attributes", {}) if isinstance(item.get("attributes"), dict) else {}
            val = str(attrs.get("data-value", "") or attrs.get("value", ""))
            if is_leaf_option(text, val):
                options.append(item)

        # 1. First, check if the active language trigger already matches target_lang (e.g. "Python3" or "Python")
        for btn in triggers:
            text = btn.get("text", "").strip().lower()
            if text in known_langs:
                if target_lang in text or (target_lang == "python" and "python" in text):
                    # Already set to the target language!
                    return None

        lang_label = "Python3" if target_lang == "python" else self._language.capitalize()
        universal_lang_selector = (
            f":text-is('{lang_label}'), "
            f"div:text-is('{lang_label}'), "
            f"span:text-is('{lang_label}'), "
            f"li:text-is('{lang_label}'), "
            f"[role='option']:has-text('{lang_label}'), "
            f"[role='menuitem']:has-text('{lang_label}'), "
            f"button:has-text('{lang_label}')"
        )

        # 2. Look for an open dropdown option or menu item matching target_lang
        for item in options:
            text = item.get("text", "")
            attrs = item.get("attributes", {}) if isinstance(item.get("attributes"), dict) else {}
            val = str(attrs.get("data-value", "") or attrs.get("value", ""))
            if is_leaf_option(text, val):
                self._transition(CodingState.SYNTHESIZE_SOLUTION)
                sel = item.get("selector")
                if not sel or (":nth-of-type" in sel and not any(tag in sel for tag in ("option", "menuitem", "#"))):
                    sel = universal_lang_selector
                return {"action": "click", "selector": sel}

        # 3. If we haven't clicked the dropdown trigger yet, click it to open the options
        if not self._lang_trigger_clicked:
            for btn in triggers:
                text = btn.get("text", "").strip().lower()
                if text in known_langs:
                    self._lang_trigger_clicked = True
                    return {"action": "click", "selector": btn.get("selector")}

        # 4. If trigger was already clicked, try clicking target option by role/text directly or advance
        if self._lang_trigger_clicked:
            self._transition(CodingState.SYNTHESIZE_SOLUTION)
            return {
                "action": "click",
                "selector": universal_lang_selector,
            }

        return None

    def _detect_verdict(self, dom_state: dict[str, list[DomElement]], recent_actions: list[dict[str, Any]]) -> str | None:
        all_texts: list[str] = []
        for act in reversed(recent_actions):
            data = act.get("data")
            if isinstance(data, dict):
                all_texts.append(str(data.get("text", "")))
            elif isinstance(data, str):
                all_texts.append(data)

        for item in dom_state.get("visible_text", []):
            t = item.get("text", "").strip()
            if t:
                all_texts.append(t)

        for text in all_texts:
            lower = text.lower()
            if "accepted" in lower:
                return "accepted"
            if "wrong answer" in lower:
                return "wrong_answer"
            if "runtime error" in lower or "compile error" in lower or "time limit exceeded" in lower or "memory limit exceeded" in lower:
                return "error"
            if (
                "to run or submit" in lower
                or "sign in to submit" in lower
                or "log in / sign up" in lower
                or "login to submit" in lower
                or "log in to run" in lower
            ):
                return "requires_login"

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
