from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict
from urllib.parse import urlparse

from playwright.async_api import Page

from webot.intelligence.interstitial_detector import InterstitialDetector
from webot.utils.logger import get_logger


ActionType = Literal["goto", "click", "fill", "submit", "extract_text"]


class ValidationResult(TypedDict):
    success: bool
    reason: str
    details: dict[str, Any]


@dataclass(slots=True)
class ActionValidator:
    """Validates whether executed browser actions actually succeeded."""

    timeout_ms: int = 5_000
    poll_interval_seconds: float = 0.2
    interstitial_detector: InterstitialDetector = field(default_factory=InterstitialDetector)

    async def validate(
        self,
        *,
        page: Page,
        action: dict[str, Any],
        before_state: dict[str, Any] | None = None,
        action_result: dict[str, Any] | None = None,
    ) -> ValidationResult:
        logger = get_logger(__name__)
        action_type = str(action.get("action", "")).strip()

        try:
            if action_type == "goto":
                result = await self._validate_goto(page=page, action=action, before_state=before_state)
            elif action_type == "click":
                result = await self._validate_click(page=page, action=action, before_state=before_state)
            elif action_type == "fill":
                result = await self._validate_fill(page=page, action=action, before_state=before_state)
            elif action_type == "submit":
                result = await self._validate_submit(page=page, action=action, before_state=before_state)
            elif action_type == "extract_text":
                result = await self._validate_extract_text(action_result=action_result)
            else:
                result = {
                    "success": False,
                    "reason": "unsupported_action",
                    "details": {"action": action_type},
                }

            logger.info("action_validation_completed", extra={"action": action_type, "result": result})
            return result
        except Exception as exc:  # noqa: BLE001
            logger.exception("action_validation_failed", extra={"action": action_type})
            return {
                "success": False,
                "reason": "validation_exception",
                "details": {"error": str(exc), "action": action_type},
            }

    async def _validate_goto(
        self,
        *,
        page: Page,
        action: dict[str, Any],
        before_state: dict[str, Any] | None,
    ) -> ValidationResult:
        expected_url = str(action.get("url", "")).strip()
        previous_url = str((before_state or {}).get("url", "")).strip()

        loaded = await self._wait_page_loaded(page)
        current_url = page.url
        title = await self._page_title(page)
        text_preview = await self._page_text_preview(page)
        interstitial = self.interstitial_detector.detect(url=current_url, title=title, text=text_preview)

        url_changed = self._url_changed(previous_url, current_url)
        url_matches_expected = self._url_matches_expected(expected_url, current_url)

        is_blocked = interstitial["detection_type"] in {"anti_bot", "captcha", "access_denied"}
        success = loaded and url_matches_expected and not is_blocked
        if success:
            reason = "goto_validated"
        elif is_blocked:
            reason = f"goto_blocked_{interstitial['detection_type']}"
        elif loaded and url_changed and not url_matches_expected:
            reason = "goto_partial_redirect"
        else:
            reason = "goto_validation_failed"

        return {
            "success": success,
            "reason": reason,
            "details": {
                "expected_url": expected_url,
                "previous_url": previous_url,
                "current_url": current_url,
                "url_changed": url_changed,
                "url_matches_expected": url_matches_expected,
                "loaded": loaded,
                "interstitial": interstitial,
            },
        }

    async def _validate_click(
        self,
        *,
        page: Page,
        action: dict[str, Any],
        before_state: dict[str, Any] | None,
    ) -> ValidationResult:
        selector = str(action.get("selector", "")).strip()
        previous_url = str((before_state or {}).get("url", "")).strip()
        previous_dom_count = self._safe_int((before_state or {}).get("dom_count"), default=-1)
        previous_modal_count = self._safe_int((before_state or {}).get("modal_count"), default=-1)
        previous_enabled = (before_state or {}).get("element_enabled")

        after = await self.capture_state(page=page, selector=selector if selector else None)
        current_url = str(after.get("url", ""))

        navigation_occurred = self._url_changed(previous_url, current_url)
        dom_changed = (
            previous_dom_count >= 0
            and self._safe_int(after.get("dom_count"), default=previous_dom_count) != previous_dom_count
        )
        modal_changed = (
            previous_modal_count >= 0
            and self._safe_int(after.get("modal_count"), default=previous_modal_count) != previous_modal_count
        )

        button_state_changed = False
        if isinstance(previous_enabled, bool) and isinstance(after.get("element_enabled"), bool):
            button_state_changed = bool(after["element_enabled"]) != previous_enabled

        success = navigation_occurred or dom_changed or modal_changed or button_state_changed
        reason = "click_validated" if success else "click_no_observable_change"

        return {
            "success": success,
            "reason": reason,
            "details": {
                "selector": selector,
                "previous_url": previous_url,
                "current_url": current_url,
                "navigation_occurred": navigation_occurred,
                "dom_changed": dom_changed,
                "modal_changed": modal_changed,
                "button_state_changed": button_state_changed,
                "before": before_state or {},
                "after": after,
            },
        }

    async def _validate_fill(
        self,
        *,
        page: Page,
        action: dict[str, Any],
        before_state: dict[str, Any] | None = None,
    ) -> ValidationResult:
        selector = str(action.get("selector", "")).strip()
        expected = str(action.get("value", ""))

        if not selector:
            return {
                "success": False,
                "reason": "missing_selector",
                "details": {"selector": selector},
            }

        previous_url = str((before_state or {}).get("url", ""))
        current_url = page.url
        if expected and self._url_changed(previous_url, current_url):
            return {
                "success": True,
                "reason": "fill_enter_submitted",
                "details": {
                    "selector": selector,
                    "expected": expected,
                    "actual": "",
                    "previous_url": previous_url,
                    "current_url": current_url,
                },
            }

        actual = await self._read_input_value(page, selector)
        success = actual == expected

        return {
            "success": success,
            "reason": "fill_validated" if success else "fill_value_mismatch",
            "details": {
                "selector": selector,
                "expected": expected,
                "actual": actual,
            },
        }

    async def _validate_extract_text(self, *, action_result: dict[str, Any] | None) -> ValidationResult:
        data = (action_result or {}).get("data")
        text = ""

        if isinstance(data, dict):
            maybe_text = data.get("text")
            if isinstance(maybe_text, str):
                text = maybe_text.strip()

        success = bool(text)
        return {
            "success": success,
            "reason": "extract_text_validated" if success else "empty_extracted_text",
            "details": {"text_length": len(text)},
        }

    async def _validate_submit(
        self,
        *,
        page: Page,
        action: dict[str, Any],
        before_state: dict[str, Any] | None,
    ) -> ValidationResult:
        selector = str(action.get("selector", "")).strip()
        previous_url = str((before_state or {}).get("url", "")).strip()
        after = await self.capture_state(page=page, selector=selector if selector else None)
        current_url = str(after.get("url", ""))
        navigation_occurred = self._url_changed(previous_url, current_url)
        dom_changed = self._safe_int(after.get("dom_count"), default=-1) != self._safe_int((before_state or {}).get("dom_count"), default=-1)
        success = navigation_occurred or dom_changed
        return {
            "success": success,
            "reason": "submit_validated" if success else "submit_no_observable_change",
            "details": {
                "selector": selector,
                "previous_url": previous_url,
                "current_url": current_url,
                "navigation_occurred": navigation_occurred,
                "dom_changed": dom_changed,
            },
        }

    async def capture_state(self, *, page: Page, selector: str | None = None) -> dict[str, Any]:
        """Capture lightweight page state for before/after action validation."""
        state: dict[str, Any] = {
            "url": page.url,
            "dom_count": await self._dom_node_count(page),
            "modal_count": await self._modal_count(page),
        }

        if selector:
            state["selector"] = selector
            state["element_enabled"] = await self._is_element_enabled(page, selector)
        return state

    async def _wait_page_loaded(self, page: Page) -> bool:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=self.timeout_ms)
            await page.wait_for_load_state("load", timeout=self.timeout_ms)
            return True
        except Exception:
            return False

    async def _page_title(self, page: Page) -> str:
        async def _get() -> str:
            return await page.title()

        return await self._with_timeout(_get(), fallback="")

    async def _page_text_preview(self, page: Page, max_chars: int = 2000) -> str:
        async def _get() -> str:
            value = await page.evaluate("() => (document.body?.innerText || '').trim()")
            if not isinstance(value, str):
                return ""
            return value[:max_chars]

        return await self._with_timeout(_get(), fallback="")

    async def _read_input_value(self, page: Page, selector: str) -> str:
        async def _get() -> str:
            locator = page.locator(selector).first
            await locator.wait_for(state="attached", timeout=self.timeout_ms)
            value = await locator.input_value(timeout=self.timeout_ms)
            return value

        return await self._with_timeout(_get(), fallback="")

    async def _dom_node_count(self, page: Page) -> int:
        async def _count() -> int:
            value = await page.evaluate("() => document.querySelectorAll('*').length")
            return self._safe_int(value, default=-1)

        return await self._with_timeout(_count(), fallback=-1)

    async def _modal_count(self, page: Page) -> int:
        js = """
        () => {
            const selectors = [
              '[role="dialog"]',
              '[aria-modal="true"]',
              '.modal',
              '[data-modal]'
            ];
            const set = new Set();
            for (const sel of selectors) {
              for (const el of Array.from(document.querySelectorAll(sel))) {
                const style = window.getComputedStyle(el);
                if (!style) continue;
                if (style.display === 'none' || style.visibility === 'hidden') continue;
                const rect = el.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) continue;
                set.add(el);
              }
            }
            return set.size;
        }
        """

        async def _count() -> int:
            value = await page.evaluate(js)
            return self._safe_int(value, default=-1)

        return await self._with_timeout(_count(), fallback=-1)

    async def _is_element_enabled(self, page: Page, selector: str) -> bool | None:
        async def _enabled() -> bool | None:
            locator = page.locator(selector).first
            if await locator.count() == 0:
                return None
            return await locator.is_enabled()

        return await self._with_timeout(_enabled(), fallback=None)

    async def _with_timeout(self, awaitable: Any, *, fallback: Any) -> Any:
        try:
            timeout_seconds = max(self.timeout_ms / 1000.0, self.poll_interval_seconds)
            return await asyncio.wait_for(awaitable, timeout=timeout_seconds)
        except Exception:
            return fallback

    @staticmethod
    def _url_changed(before: str, after: str) -> bool:
        return bool(before and after and before.rstrip("/") != after.rstrip("/"))

    @staticmethod
    def _url_matches_expected(expected: str, current: str) -> bool:
        if not expected or not current:
            return False

        expected_url = expected if expected.startswith(("http://", "https://")) else f"https://{expected}"
        try:
            expected_parsed = urlparse(expected_url)
            current_parsed = urlparse(current)
        except Exception:
            return False

        if not expected_parsed.netloc or not current_parsed.netloc:
            return False

        expected_host = expected_parsed.netloc.lower()
        current_host = current_parsed.netloc.lower()

        host_match = current_host == expected_host or current_host.endswith(f".{expected_host}")
        path_match = (
            not expected_parsed.path
            or expected_parsed.path == "/"
            or current_parsed.path.startswith(expected_parsed.path)
        )
        return host_match and path_match

    @staticmethod
    def _safe_int(value: Any, *, default: int) -> int:
        try:
            return int(value)
        except Exception:
            return default
