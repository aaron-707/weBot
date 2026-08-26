from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict


class DomElement(TypedDict, total=False):
    type: str
    text: str
    selector: str
    visible: bool
    attributes: dict[str, str]


class DomSnapshot(TypedDict, total=False):
    buttons: list[DomElement]
    inputs: list[DomElement]
    links: list[DomElement]
    forms: list[DomElement]
    visible_text: list[DomElement]


class DomDeltaSummary(TypedDict):
    navigation_changed: bool
    previous_url: str
    current_url: str
    new_elements: list[dict[str, str]]
    removed_elements: list[dict[str, str]]
    changed_text: list[dict[str, str]]
    form_state_changes: list[dict[str, str]]
    token_optimized_summary: str


@dataclass(slots=True)
class DomDelta:
    """Computes compact DOM deltas for efficient agent reasoning."""

    max_items_per_bucket: int = 20

    def compare(
        self,
        *,
        previous: DomSnapshot,
        current: DomSnapshot,
        previous_url: str = "",
        current_url: str = "",
    ) -> DomDeltaSummary:
        prev_map = self._index_elements(previous)
        curr_map = self._index_elements(current)

        prev_keys = set(prev_map.keys())
        curr_keys = set(curr_map.keys())

        new_keys = curr_keys - prev_keys
        removed_keys = prev_keys - curr_keys
        common_keys = prev_keys & curr_keys

        new_elements = self._compact_elements([curr_map[k] for k in new_keys])
        removed_elements = self._compact_elements([prev_map[k] for k in removed_keys])

        changed_text: list[dict[str, str]] = []
        form_state_changes: list[dict[str, str]] = []

        for key in common_keys:
            prev_el = prev_map[key]
            curr_el = curr_map[key]

            prev_text = self._txt(prev_el.get("text"))
            curr_text = self._txt(curr_el.get("text"))
            if prev_text != curr_text:
                changed_text.append(
                    {
                        "selector": self._txt(curr_el.get("selector")) or self._txt(prev_el.get("selector")),
                        "type": self._txt(curr_el.get("type")) or self._txt(prev_el.get("type")),
                        "before": prev_text,
                        "after": curr_text,
                    }
                )

            if self._is_form_element(prev_el) or self._is_form_element(curr_el):
                prev_state = self._form_state(prev_el)
                curr_state = self._form_state(curr_el)
                if prev_state != curr_state:
                    form_state_changes.append(
                        {
                            "selector": self._txt(curr_el.get("selector")) or self._txt(prev_el.get("selector")),
                            "before": prev_state,
                            "after": curr_state,
                        }
                    )

        navigation_changed = self._normalize_url(previous_url) != self._normalize_url(current_url)

        summary: DomDeltaSummary = {
            "navigation_changed": navigation_changed,
            "previous_url": previous_url,
            "current_url": current_url,
            "new_elements": self._limit(new_elements),
            "removed_elements": self._limit(removed_elements),
            "changed_text": self._limit(changed_text),
            "form_state_changes": self._limit(form_state_changes),
            "token_optimized_summary": "",
        }
        summary["token_optimized_summary"] = self._build_compact_summary(summary)
        return summary

    def _index_elements(self, snapshot: DomSnapshot) -> dict[str, DomElement]:
        indexed: dict[str, DomElement] = {}
        for key in ("buttons", "inputs", "links", "forms", "visible_text"):
            for el in snapshot.get(key, []) or []:
                if not isinstance(el, dict):
                    continue
                element = el
                idx = self._element_key(element)
                if idx:
                    indexed[idx] = element
        return indexed

    def _element_key(self, element: DomElement) -> str:
        selector = self._txt(element.get("selector"))
        el_type = self._txt(element.get("type"))
        if selector:
            return f"{el_type}:{selector}"

        attrs = element.get("attributes") if isinstance(element.get("attributes"), dict) else {}
        attr_id = self._txt(attrs.get("id"))
        attr_name = self._txt(attrs.get("name"))
        attr_placeholder = self._txt(attrs.get("placeholder"))
        if attr_id or attr_name or attr_placeholder:
            return f"{el_type}:{attr_id}|{attr_name}|{attr_placeholder}"

        text = self._txt(element.get("text"))
        return f"{el_type}:text:{text[:80]}"

    def _compact_elements(self, elements: list[DomElement]) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for el in elements:
            out.append(
                {
                    "type": self._txt(el.get("type")),
                    "selector": self._txt(el.get("selector")),
                    "text": self._txt(el.get("text"))[:120],
                }
            )
        return out

    @staticmethod
    def _txt(value: Any) -> str:
        return value.strip() if isinstance(value, str) else ""

    @staticmethod
    def _is_form_element(el: DomElement) -> bool:
        return str(el.get("type", "")) in {"input", "form"}

    def _form_state(self, el: DomElement) -> str:
        attrs = el.get("attributes") if isinstance(el.get("attributes"), dict) else {}
        relevant = {
            "value": self._txt(attrs.get("value")),
            "checked": self._txt(attrs.get("checked")),
            "selected": self._txt(attrs.get("selected")),
            "disabled": self._txt(attrs.get("disabled")),
        }
        return "|".join(f"{k}={v}" for k, v in relevant.items())

    def _limit(self, items: list[dict[str, str]]) -> list[dict[str, str]]:
        return items[: self.max_items_per_bucket]

    @staticmethod
    def _normalize_url(url: str) -> str:
        return (url or "").strip().rstrip("/")

    def _build_compact_summary(self, delta: DomDeltaSummary) -> str:
        parts = [
            f"nav_changed={str(delta['navigation_changed']).lower()}",
            f"new={len(delta['new_elements'])}",
            f"removed={len(delta['removed_elements'])}",
            f"text_changed={len(delta['changed_text'])}",
            f"form_changed={len(delta['form_state_changes'])}",
        ]

        # Include concise previews for fast LLM grounding.
        top_new = delta["new_elements"][:3]
        if top_new:
            preview = "; ".join(
                f"{x.get('type','')}:{x.get('selector','') or x.get('text','')[:30]}" for x in top_new
            )
            parts.append(f"new_preview={preview}")

        top_changed = delta["changed_text"][:2]
        if top_changed:
            preview = "; ".join(
                f"{x.get('selector','')}:{x.get('after','')[:30]}" for x in top_changed
            )
            parts.append(f"text_preview={preview}")

        return " | ".join(parts)
