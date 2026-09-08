from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar, Literal, TypedDict
from urllib.parse import urlparse


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

    def interpret(self, instruction: str) -> list[BrowserAction]:
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

        return actions

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"\s+", " ", text.strip())

    def _extract_goto_action(self, instruction: str) -> BrowserAction | None:
        url = self._extract_explicit_url(instruction)
        if url:
            return {"action": "goto", "url": url}

        domain = self._extract_domain_hint(instruction)
        if domain:
            return {"action": "goto", "url": f"https://{domain}"}

        return None

    @staticmethod
    def _extract_explicit_url(instruction: str) -> str | None:
        match = re.search(r"\bhttps?://[^\s]+", instruction, flags=re.IGNORECASE)
        if not match:
            return None

        candidate = match.group(0).rstrip(".,)")
        parsed = urlparse(candidate)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return candidate
        return None

    #: Known-name -> domain lookup. Extend as new target sites are added to
    #: the search/form/login state machines or runtime test suite.
    _NAMED_SITES: ClassVar[dict[str, str]] = {
        "linkedin": "linkedin.com",
        "google": "google.com",
        "github": "github.com",
        "duckduckgo": "duckduckgo.com",
        "demoqa": "demoqa.com/automation-practice-form",
        "the-internet": "the-internet.herokuapp.com/login",
        "herokuapp": "the-internet.herokuapp.com/login",
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

        Returns None when no explicit URL or recognizable site name is
        present in the instruction — callers should treat that as
        "ask the user for a starting point" rather than guessing.
        """
        normalized = self._normalize(instruction)
        action = self._extract_goto_action(normalized)
        return action["url"] if action else None

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
