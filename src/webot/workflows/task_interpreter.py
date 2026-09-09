from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar, Literal, TypedDict
from urllib.parse import urlparse


from webot.llm.decision_engine import DecisionEngine, DomElement
from webot.utils.logger import get_logger
from webot.utils.url_safety import is_safe_web_url


ActionType = Literal["goto", "search", "click", "fill", "extract_text"]


class BrowserAction(TypedDict, total=False):
    action: ActionType
    url: str
    query: str
    selector: str
    value: str


@dataclass(slots=True)
class TaskInterpreter:
    """Converts natural language instructions into structured browser actions."""

    decision_engine: DecisionEngine | None = None
    llm_consulted_count: int = 0
    deterministic_interpret_count: int = 0

    def interpret(
        self,
        instruction: str,
        dom_summary: list[DomElement] | None = None,
    ) -> list[BrowserAction]:
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("instruction must be a non-empty string")

        normalized = self._normalize(instruction)
        actions: list[BrowserAction] = []

        goto_action = self._extract_goto_action(normalized)
        if goto_action:
            actions.append(goto_action)

        search_action = self._extract_search_action(normalized)
        if search_action:
            actions.append(search_action)

        if actions:
            self.deterministic_interpret_count += 1
            return actions

        # Deterministic parsing found no supported actions.
        # Fall back to LLM reasoning if DecisionEngine is configured.
        if self.decision_engine is not None:
            logger = get_logger(__name__)
            self.llm_consulted_count += 1
            logger.info("task_interpreter_llm_consulted", extra={"instruction": instruction})
            llm_actions = self.decision_engine.propose_action_sequence(
                user_goal=instruction,
                elements=dom_summary,
            )
            converted: list[BrowserAction] = []
            for act in llm_actions:
                if isinstance(act, dict):
                    action_type = act.get("action")
                    if action_type in {"goto", "search", "click", "fill", "extract_text"}:
                        converted.append(dict(act))  # type: ignore[arg-type]
            if converted:
                return converted

        return []

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"\s+", " ", text.strip())

    def _extract_goto_action(self, instruction: str) -> BrowserAction | None:
        url = self._extract_explicit_url(instruction)
        if url:
            return {"action": "goto", "url": url}

        domain = self._extract_domain_hint(instruction)
        if domain:
            candidate = f"https://{domain}"
            if is_safe_web_url(candidate):
                return {"action": "goto", "url": candidate}

        return None

    @staticmethod
    def _extract_explicit_url(instruction: str) -> str | None:
        match = re.search(r"\bhttps?://[^\s]+", instruction, flags=re.IGNORECASE)
        if not match:
            return None

        candidate = match.group(0).rstrip(".,)")
        if is_safe_web_url(candidate):
            return candidate
        return None

    #: Known-name -> domain lookup. Extend as new target sites are added to
    #: the search/form/login state machines or runtime test suite.
    _NAMED_SITES: ClassVar[dict[str, str]] = {
        "youtube": "youtube.com",
        "reddit": "reddit.com",
        "amazon": "amazon.com",
        "linkedin": "linkedin.com",
        "google": "google.com",
        "github": "github.com",
        "duckduckgo": "duckduckgo.com",
        "demoqa": "demoqa.com/automation-practice-form",
        "the-internet": "the-internet.herokuapp.com/login",
        "herokuapp": "the-internet.herokuapp.com/login",
        "wikipedia": "wikipedia.org",
        "leetcode": "leetcode.com/problemset/",
    }

    def _extract_domain_hint(self, instruction: str) -> str | None:
        patterns = [
            r"\bopen\s+([a-z0-9-]+(?:\.[a-z0-9-]+)+)\b",
            r"\bgo(?:es)?\s+to\s+([a-z0-9-]+(?:\.[a-z0-9-]+)+)\b",
            r"\bnavigate\s+to\s+([a-z0-9-]+(?:\.[a-z0-9-]+)+)\b",
            r"\b(?:log\s*in|sign\s+in|login)\s+to\s+([a-z0-9-]+(?:\.[a-z0-9-]+)+)\b",
            r"\bon\s+([a-z0-9-]+(?:\.[a-z0-9-]+)+)\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, instruction, flags=re.IGNORECASE)
            if match:
                captured = match.group(1).lower()
                # If the regex only captured the bare hostname but _NAMED_SITES
                # has a more-specific value for that host (i.e. includes a path),
                # prefer the full path-aware value so callers land on the correct
                # page rather than the site root.
                for domain in self._NAMED_SITES.values():
                    host = domain.split("/")[0]
                    if host == captured and "/" in domain:
                        return domain
                return captured

        # Fall back to a bare mention of a known site name anywhere in the
        # instruction (no trigger word required) — site names are distinctive
        # enough that this rarely false-positives, and it covers phrasing
        # like "Fill DemoQA practice form fields" with no "open"/"on".
        lowered = instruction.lower()
        for key, domain in self._NAMED_SITES.items():
            if key in lowered:
                return domain

        return None

    def extract_start_url(self, instruction: str) -> str | None:
        """Public helper: best-effort starting URL for a free-text prompt.

        Returns deterministic URL if present in instruction or known site name.
        Falls back to DecisionEngine.infer_start_url if DecisionEngine is provided.
        Returns None when no start URL can be deduced.
        """
        normalized = self._normalize(instruction)
        action = self._extract_goto_action(normalized)
        if action and action.get("url"):
            return action["url"]

        if self.decision_engine is not None:
            logger = get_logger(__name__)
            self.llm_consulted_count += 1
            logger.info("task_interpreter_start_url_llm_consulted", extra={"instruction": instruction})
            inferred = self.decision_engine.infer_start_url(instruction)
            if inferred and is_safe_web_url(inferred):
                return inferred

        return None

    @staticmethod
    def _extract_search_action(instruction: str) -> BrowserAction | None:
        patterns = [
            r"\bsearch\s+for\s+(.+)$",
            r"\bsearch\s+(.+)$",
            r"\blook\s+for\s+(.+)$",
            r"\bfind\s+(.+)$",
        ]

        for pattern in patterns:
            match = re.search(pattern, instruction, flags=re.IGNORECASE)
            if not match:
                continue
            query = match.group(1).strip().rstrip(".")
            if query:
                return {"action": "search", "query": query}

        return None

    @staticmethod
    def is_coding_goal(instruction: str) -> bool:
        lowered = instruction.lower()
        if any(site in lowered for site in ("leetcode", "hackerrank", "codeforces")):
            return True
        if "problem" in lowered and any(token in lowered for token in ("solve", "code", "program", "submit", "choose", "select", "pick", "easy", "medium", "hard")):
            return True
        return ("solve" in lowered) and ("code" in lowered or "program" in lowered or "submit" in lowered)

    @staticmethod
    def extract_coding_language(instruction: str) -> str:
        lowered = instruction.lower()
        for lang in ("python3", "python", "javascript", "typescript", "cpp", "c++", "java", "rust", "go"):
            if lang in lowered:
                return "python" if lang in ("python3", "python") else lang
        return "python"

    @staticmethod
    def extract_coding_difficulty(instruction: str) -> str:
        lowered = instruction.lower()
        for diff in ("easy", "medium", "hard"):
            if diff in lowered:
                return diff
        return "easy"
