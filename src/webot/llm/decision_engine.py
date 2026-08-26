from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from webot.llm.ollama_client import OllamaClient


ActionType = Literal["goto", "click", "fill", "extract_text"]


class NextAction(TypedDict, total=False):
    action: ActionType
    selector: str
    value: str
    url: str


class DomElement(TypedDict, total=False):
    type: str
    text: str
    selector: str
    visible: bool
    attributes: dict[str, str]


@dataclass(slots=True)
class DecisionEngine:
    """Maps user goals + extracted DOM to the next structured browser action."""

    ollama_client: OllamaClient

    def choose_next_action(self, user_goal: str, elements: list[DomElement]) -> NextAction:
        if not isinstance(user_goal, str) or not user_goal.strip():
            raise ValueError("user_goal must be a non-empty string")
        logger = logging.getLogger(__name__)

        if not self.ollama_client.is_available():
            logger.warning("llm_unavailable")
            return self._deterministic_fallback(user_goal, elements)

        try:
            prompt = self._build_prompt(user_goal=user_goal.strip(), elements=elements)
            raw = self.ollama_client.generate(prompt)
            parsed = self._parse_json_action(raw)
            if parsed is not None:
                return parsed
        except Exception:
            logger.warning("llm_unavailable")
            return self._deterministic_fallback(user_goal, elements)

        return self._deterministic_fallback(user_goal, elements)

    def _build_prompt(self, *, user_goal: str, elements: list[DomElement]) -> str:
        compact_elements = [self._compact_element(item) for item in elements[:40]]
        elements_json = json.dumps(compact_elements, ensure_ascii=True, separators=(",", ":"))

        return (
            "Return exactly one JSON object for next browser action. "
            'Schema: {"action":"click|fill|goto|extract_text","selector"?:string,"value"?:string,"url"?:string}. '
            "No markdown. No explanation. Prefer deterministic UI actions. "
            f"Goal:{user_goal}\n"
            f"DOM:{elements_json}"
        )

    @staticmethod
    def _compact_element(element: DomElement) -> dict[str, Any]:
        return {
            "type": str(element.get("type", "")),
            "text": str(element.get("text", ""))[:120],
            "selector": str(element.get("selector", ""))[:200],
            "visible": bool(element.get("visible", False)),
        }

    def _parse_json_action(self, text: str) -> NextAction | None:
        if not isinstance(text, str) or not text.strip():
            return None

        payload = text.strip()
        if payload.startswith("```"):
            payload = self._strip_code_fence(payload)

        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            return None

        if not isinstance(obj, dict):
            return None

        return self._validate_action_dict(obj)

    @staticmethod
    def _strip_code_fence(payload: str) -> str:
        cleaned = payload.strip()
        cleaned = cleaned.removeprefix("```json").removeprefix("```")
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        return cleaned.strip()

    def _validate_action_dict(self, obj: dict[str, Any]) -> NextAction | None:
        action = obj.get("action")
        if action not in {"goto", "click", "fill", "extract_text"}:
            return None

        if action == "goto":
            url = obj.get("url")
            if not isinstance(url, str) or not url.strip():
                return None
            return {"action": "goto", "url": url.strip()}

        if action == "click":
            selector = obj.get("selector")
            if not isinstance(selector, str) or not selector.strip():
                return None
            return {"action": "click", "selector": selector.strip()}

        if action == "fill":
            selector = obj.get("selector")
            value = obj.get("value")
            if not isinstance(selector, str) or not selector.strip():
                return None
            if not isinstance(value, str):
                return None
            return {
                "action": "fill",
                "selector": selector.strip(),
                "value": value,
            }

        selector = obj.get("selector")
        if not isinstance(selector, str) or not selector.strip():
            return None
        return {"action": "extract_text", "selector": selector.strip()}

    @staticmethod
    def _deterministic_fallback(user_goal: str, elements: list[DomElement]) -> NextAction:
        goal = user_goal.lower()
        if any(token in goal for token in ("search", "find", "look up", "query")):
            selector = DecisionEngine._find_selector(elements, kind="input", keywords=("search", "query", "find"))
            if selector:
                return {"action": "fill", "selector": selector, "value": DecisionEngine._extract_goal_tail(goal)}
            click_selector = DecisionEngine._find_selector(elements, kind="button", keywords=("search", "go", "submit"))
            if click_selector:
                return {"action": "click", "selector": click_selector}

        if any(token in goal for token in ("login", "log in", "sign in")):
            click_selector = DecisionEngine._find_selector(elements, kind="button", keywords=("login", "sign in", "submit"))
            if click_selector:
                return {"action": "click", "selector": click_selector}

        if any(token in goal for token in ("open ", "go to ", "navigate ")):
            for token in goal.split():
                candidate = token.strip(".,)")
                if candidate.startswith("http://") or candidate.startswith("https://"):
                    return {"action": "goto", "url": candidate}
                if any(candidate.endswith(suffix) for suffix in (".com", ".org", ".net", ".io", ".ai")):
                    return {"action": "goto", "url": f"https://{candidate}"}

        if any(token in goal for token in ("form", "fill", "submit")):
            input_selector = DecisionEngine._find_selector(elements, kind="input", keywords=("name", "email", "phone"))
            if input_selector:
                return {"action": "fill", "selector": input_selector, "value": "demo"}
            submit_selector = DecisionEngine._find_selector(elements, kind="button", keywords=("submit", "continue", "next"))
            if submit_selector:
                return {"action": "click", "selector": submit_selector}

        for item in elements:
            if item.get("type") == "button" and item.get("visible") and item.get("selector"):
                return {"action": "click", "selector": str(item["selector"])}
        return {"action": "extract_text", "selector": "body"}

    @staticmethod
    def _find_selector(elements: list[DomElement], *, kind: str, keywords: tuple[str, ...]) -> str | None:
        for item in elements:
            if str(item.get("type", "")).lower() != kind:
                continue
            selector = str(item.get("selector", "")).strip()
            text = str(item.get("text", "")).lower()
            attrs = item.get("attributes", {}) if isinstance(item.get("attributes"), dict) else {}
            blob = f"{text} {attrs.get('name', '')} {attrs.get('placeholder', '')} {attrs.get('aria-label', '')}".lower()
            if selector and any(token in blob for token in keywords):
                return selector
        return None

    @staticmethod
    def _extract_goal_tail(goal: str) -> str:
        for trigger in ("search for ", "search ", "find ", "look up ", "query "):
            idx = goal.find(trigger)
            if idx >= 0:
                value = goal[idx + len(trigger):].strip().strip(".")
                return value or "query"
        return "query"
