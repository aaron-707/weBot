from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, TypedDict
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

    @staticmethod
    def _extract_domain_hint(instruction: str) -> str | None:
        patterns = [
            r"\bopen\s+([a-z0-9-]+(?:\.[a-z0-9-]+)+)\b",
            r"\bgo to\s+([a-z0-9-]+(?:\.[a-z0-9-]+)+)\b",
            r"\bnavigate to\s+([a-z0-9-]+(?:\.[a-z0-9-]+)+)\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, instruction, flags=re.IGNORECASE)
            if match:
                return match.group(1).lower()

        named_sites: dict[str, str] = {
            "linkedin": "linkedin.com",
            "google": "google.com",
            "github": "github.com",
        }
        lowered = instruction.lower()
        for key, domain in named_sites.items():
            if key in lowered and any(word in lowered for word in ["open", "go to", "navigate"]):
                return domain

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
