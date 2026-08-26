from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, TypedDict


SelectorKind = Literal["label", "role", "placeholder", "name", "id", "text", "css"]


class ElementLike(TypedDict, total=False):
    tag: str
    text: str
    role: str
    aria_label: str
    placeholder: str
    name: str
    id: str
    input_type: str


@dataclass(slots=True)
class SelectorBuilder:
    """Builds stable Playwright-compatible selectors for DOM elements."""

    def build_selector(self, element: dict[str, Any]) -> str:
        candidates = self._build_candidates(element)
        if not candidates:
            return ""

        best = max(candidates, key=lambda item: item[1])
        return best[0]

    def rank_selector_stability(self, selector: str) -> float:
        value = (selector or "").strip()
        if not value:
            return 0.0

        if value.startswith("get_by_label("):
            return 0.95
        if value.startswith("get_by_role("):
            return 0.92
        if value.startswith("get_by_placeholder("):
            return 0.88
        if value.startswith("locator(") and "[name=" in value:
            return 0.82
        if value.startswith("locator(") and "#" in value:
            return 0.80
        if value.startswith("get_by_text("):
            return 0.74
        if value.startswith("locator("):
            return 0.55
        return 0.40

    def _build_candidates(self, element: dict[str, Any]) -> list[tuple[str, float]]:
        tag = self._as_str(element.get("tag")).lower()
        role = self._as_str(element.get("role")).lower()
        aria_label = self._as_str(element.get("aria_label"))
        placeholder = self._as_str(element.get("placeholder"))
        name = self._as_str(element.get("name"))
        element_id = self._as_str(element.get("id"))
        text = self._as_str(element.get("text"))

        candidates: list[tuple[str, float]] = []

        if aria_label:
            selector = f'page.get_by_label("{self._escape(aria_label)}", exact=True)'
            candidates.append((selector, self.rank_selector_stability(selector)))

        effective_role = role or self._infer_role(tag, element)
        if effective_role and aria_label:
            selector = (
                f'page.get_by_role("{self._escape(effective_role)}", '
                f'name="{self._escape(aria_label)}", exact=True)'
            )
            candidates.append((selector, self.rank_selector_stability(selector)))

        if effective_role and text:
            selector = (
                f'page.get_by_role("{self._escape(effective_role)}", '
                f'name="{self._escape(self._short_text(text))}", exact=True)'
            )
            candidates.append((selector, self.rank_selector_stability(selector)))

        if placeholder:
            selector = f'page.get_by_placeholder("{self._escape(placeholder)}", exact=True)'
            candidates.append((selector, self.rank_selector_stability(selector)))

        if name:
            base_tag = tag if tag else "*"
            selector = f'page.locator("{base_tag}[name=\\"{self._escape(name)}\\"]")'
            candidates.append((selector, self.rank_selector_stability(selector)))

        if element_id:
            selector = f'page.locator("#{self._css_escape(element_id)}")'
            candidates.append((selector, self.rank_selector_stability(selector)))

        if text:
            selector = f'page.get_by_text("{self._escape(self._short_text(text))}", exact=True)'
            candidates.append((selector, self.rank_selector_stability(selector)))

        return candidates

    @staticmethod
    def _infer_role(tag: str, element: dict[str, Any]) -> str:
        if tag == "button":
            return "button"
        if tag == "a":
            return "link"
        if tag == "input":
            input_type = str(element.get("input_type", "")).lower()
            if input_type in {"submit", "button", "reset"}:
                return "button"
            return "textbox"
        if tag == "textarea":
            return "textbox"
        if tag == "select":
            return "combobox"
        return ""

    @staticmethod
    def _short_text(value: str, max_len: int = 80) -> str:
        compact = re.sub(r"\s+", " ", value).strip()
        if len(compact) <= max_len:
            return compact
        return compact[: max_len - 1].rstrip() + "..."

    @staticmethod
    def _escape(value: str) -> str:
        return value.replace('\\', '\\\\').replace('"', '\\"')

    @staticmethod
    def _css_escape(value: str) -> str:
        return re.sub(r"([^a-zA-Z0-9_-])", r"\\\1", value)

    @staticmethod
    def _as_str(value: Any) -> str:
        return value.strip() if isinstance(value, str) else ""
