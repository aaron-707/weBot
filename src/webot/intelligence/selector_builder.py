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

        if value.startswith("get_by_label(") or "[aria-label=" in value:
            return 0.95
        if value.startswith("get_by_role(") or "[role=" in value:
            return 0.92
        if value.startswith("get_by_placeholder(") or "[placeholder=" in value:
            return 0.88
        if (value.startswith("locator(") and "[name=" in value) or "[name=" in value:
            return 0.82
        if (value.startswith("locator(") and "#" in value) or value.startswith("#"):
            return 0.80
        if value.startswith("get_by_text(") or value.startswith("text=") or ":has-text(" in value:
            return 0.74
        if value.startswith("locator("):
            return 0.55
        return 0.40

    def build_fallback_chain(
        self,
        elements: list[dict],
        action_type: str,  # "click" | "fill" | "submit"
    ) -> list[str]:
        """Return up to 3 candidate selector strings ranked by stability.

        Returns [] if no candidates found. Each string is a raw locator without 'page.' prefix.
        """
        if not elements:
            return []

        scored_candidates: list[tuple[str, float]] = []
        for element in elements:
            if not isinstance(element, dict):
                continue

            raw_sel = self._as_str(element.get("selector"))
            if raw_sel:
                cleaned = self._clean_selector(raw_sel)
                if cleaned:
                    scored_candidates.append((cleaned, self.rank_selector_stability(cleaned)))

            candidates = self._build_candidates(element)
            bonus = self._action_type_bonus(element, action_type)
            for sel, score in candidates:
                cleaned = self._clean_selector(sel)
                if cleaned:
                    scored_candidates.append((cleaned, score + bonus))

        if not scored_candidates:
            return []

        best_by_selector: dict[str, float] = {}
        for sel, score in scored_candidates:
            if not sel:
                continue
            if sel not in best_by_selector or score > best_by_selector[sel]:
                best_by_selector[sel] = score

        sorted_items = sorted(best_by_selector.items(), key=lambda item: item[1], reverse=True)
        return [sel for sel, _ in sorted_items[:3]]

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
            selector = f'[aria-label="{self._escape(aria_label)}"]'
            candidates.append((selector, self.rank_selector_stability(selector)))

        effective_role = role or self._infer_role(tag, element)
        if effective_role and aria_label:
            selector = f'[role="{self._escape(effective_role)}"][aria-label="{self._escape(aria_label)}"]'
            candidates.append((selector, self.rank_selector_stability(selector)))

        if effective_role and text:
            selector = f'{effective_role}:has-text("{self._escape(self._short_text(text))}")'
            candidates.append((selector, self.rank_selector_stability(selector)))

        if placeholder:
            selector = f'[placeholder="{self._escape(placeholder)}"]'
            candidates.append((selector, self.rank_selector_stability(selector)))

        if name:
            base_tag = tag if tag else "*"
            selector = f'{base_tag}[name="{self._escape(name)}"]'
            candidates.append((selector, self.rank_selector_stability(selector)))

        if element_id:
            selector = f'#{self._css_escape(element_id)}'
            candidates.append((selector, self.rank_selector_stability(selector)))

        if text:
            selector = f'text="{self._escape(self._short_text(text))}"'
            candidates.append((selector, self.rank_selector_stability(selector)))

        return candidates

    @staticmethod
    def _clean_selector(selector: str) -> str:
        s = selector.strip()
        if s.startswith("page.locator(") and s.endswith(")"):
            inner = s[len("page.locator("):-1].strip()
            if (inner.startswith('"') and inner.endswith('"')) or (inner.startswith("'") and inner.endswith("'")):
                inner = inner[1:-1]
            return inner
        if s.startswith("page."):
            return s[len("page."):].strip()
        return s

    def _action_type_bonus(self, element: dict[str, Any], action_type: str) -> float:
        tag = self._as_str(element.get("tag")).lower()
        role = self._as_str(element.get("role")).lower()
        input_type = self._as_str(element.get("input_type")).lower()

        if action_type == "fill":
            if tag in {"input", "textarea"} and input_type not in {"submit", "button", "reset"}:
                return 0.05
            if role == "textbox":
                return 0.05
        elif action_type in {"click", "submit"}:
            if tag == "button" or input_type in {"submit", "button"}:
                return 0.05
            if tag == "a" or role in {"button", "link"}:
                return 0.03
        return 0.0

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
