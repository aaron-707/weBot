from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypedDict

from playwright.async_api import Page

from webot.utils.logger import get_logger


ErrorType = Literal["missing_selector", "navigation_failure", "timeout", "anti_bot_blocked", "fill_loop_stagnation"]


class RecoveryInput(TypedDict, total=False):
    error_type: ErrorType
    failed_action: dict[str, Any]
    error_message: str
    candidate_selectors: list[str]


class RecoveryResult(TypedDict, total=False):
    recovered: bool
    strategy: str
    next_action: dict[str, Any]
    details: str


class LlmLike(Protocol):
    def generate(self, prompt: str, *, model: str | None = None) -> str: ...


@dataclass(slots=True)
class RecoveryEngine:
    max_retries: int = 2
    llm_client: LlmLike | None = None

    async def recover(
        self,
        *,
        page: Page,
        error_type: ErrorType,
        failed_action: dict[str, Any],
        error_message: str = "",
        retry_count: int = 0,
        candidate_selectors: list[str] | None = None,
    ) -> RecoveryResult:
        logger = get_logger(__name__)
        logger.info(
            "recovery_started",
            extra={
                "error_type": error_type,
                "retry_count": retry_count,
                "failed_action": failed_action,
            },
        )

        if error_type == "missing_selector":
            return await self._recover_missing_selector(
                page=page,
                failed_action=failed_action,
                retry_count=retry_count,
                candidate_selectors=candidate_selectors or [],
            )

        if error_type == "navigation_failure":
            return await self._recover_navigation_failure(
                page=page,
                failed_action=failed_action,
                retry_count=retry_count,
            )

        if error_type == "timeout":
            return await self._recover_timeout(
                page=page,
                failed_action=failed_action,
                retry_count=retry_count,
                candidate_selectors=candidate_selectors or [],
            )
        if error_type == "anti_bot_blocked":
            return {
                "recovered": False,
                "strategy": "stop_no_retry",
                "details": "Anti-bot/interstitial detected; avoid retry storm",
            }
        if error_type == "fill_loop_stagnation":
            return {
                "recovered": False,
                "strategy": "stop_no_retry",
                "details": "Repeated fill without progress detected; avoid retry storm",
            }

        return {
            "recovered": False,
            "strategy": "none",
            "details": f"Unsupported error type: {error_type}",
        }

    async def _recover_missing_selector(
        self,
        *,
        page: Page,
        failed_action: dict[str, Any],
        retry_count: int,
        candidate_selectors: list[str],
    ) -> RecoveryResult:
        if retry_count < self.max_retries:
            return {
                "recovered": True,
                "strategy": "retry",
                "next_action": failed_action,
                "details": "Retrying failed action",
            }

        alt_selector = await self._find_working_selector(page, candidate_selectors)
        if alt_selector:
            next_action = dict(failed_action)
            next_action["selector"] = alt_selector
            return {
                "recovered": True,
                "strategy": "alternative_selector_search",
                "next_action": next_action,
                "details": f"Found alternative selector: {alt_selector}",
            }

        llm_action = self._llm_fallback_action("missing_selector", failed_action, candidate_selectors)
        if llm_action is not None:
            return {
                "recovered": True,
                "strategy": "llm_fallback",
                "next_action": llm_action,
                "details": "Using LLM fallback action",
            }

        return {
            "recovered": False,
            "strategy": "none",
            "details": "No recovery path found for missing selector",
        }

    async def _recover_navigation_failure(
        self,
        *,
        page: Page,
        failed_action: dict[str, Any],
        retry_count: int,
    ) -> RecoveryResult:
        if "sorry" in failed_action.get("url", "").lower() or "captcha" in failed_action.get("url", "").lower():
            return {
                "recovered": False,
                "strategy": "stop_no_retry",
                "details": "Navigation target appears blocked by anti-bot challenge",
            }

        if retry_count < self.max_retries:
            return {
                "recovered": True,
                "strategy": "retry",
                "next_action": failed_action,
                "details": "Retrying navigation",
            }

        try:
            await page.reload(wait_until="domcontentloaded")
            return {
                "recovered": True,
                "strategy": "refresh_page",
                "next_action": failed_action,
                "details": "Page refreshed, retry navigation action",
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "recovered": False,
                "strategy": "refresh_page",
                "details": f"Refresh failed: {exc}",
            }

    async def _recover_timeout(
        self,
        *,
        page: Page,
        failed_action: dict[str, Any],
        retry_count: int,
        candidate_selectors: list[str],
    ) -> RecoveryResult:
        if retry_count < self.max_retries:
            return {
                "recovered": True,
                "strategy": "retry",
                "next_action": failed_action,
                "details": "Retrying after timeout",
            }

        try:
            await page.reload(wait_until="domcontentloaded")
        except Exception:
            pass

        alt_selector = await self._find_working_selector(page, candidate_selectors)
        if alt_selector:
            next_action = dict(failed_action)
            next_action["selector"] = alt_selector
            return {
                "recovered": True,
                "strategy": "alternative_selector_search",
                "next_action": next_action,
                "details": f"Timeout recovery with alternative selector: {alt_selector}",
            }

        llm_action = self._llm_fallback_action("timeout", failed_action, candidate_selectors)
        if llm_action is not None:
            return {
                "recovered": True,
                "strategy": "llm_fallback",
                "next_action": llm_action,
                "details": "Using LLM fallback after timeout",
            }

        return {
            "recovered": False,
            "strategy": "none",
            "details": "Timeout recovery failed",
        }

    async def _find_working_selector(self, page: Page, candidates: list[str]) -> str | None:
        for selector in candidates:
            if not isinstance(selector, str) or not selector.strip():
                continue
            try:
                locator = page.locator(selector)
                if await locator.count() > 0 and await locator.first.is_visible():
                    return selector
            except Exception:
                continue
        return None

    def _llm_fallback_action(
        self,
        error_type: ErrorType,
        failed_action: dict[str, Any],
        candidate_selectors: list[str],
    ) -> dict[str, Any] | None:
        if self.llm_client is None:
            return None

        prompt = (
            "Return one JSON action for browser recovery. "
            'Schema: {"action":"goto|click|fill|extract_text","selector"?:string,"value"?:string,"url"?:string}. '
            "No markdown.\n"
            f"error_type:{error_type}\n"
            f"failed_action:{json.dumps(failed_action, ensure_ascii=True, separators=(',', ':'))}\n"
            f"candidates:{json.dumps(candidate_selectors[:15], ensure_ascii=True, separators=(',', ':'))}"
        )

        try:
            raw = self.llm_client.generate(prompt)
            parsed = json.loads(self._strip_fence(raw))
            if not isinstance(parsed, dict):
                return None
            action = parsed.get("action")
            if action not in {"goto", "click", "fill", "extract_text"}:
                return None
            return parsed
        except Exception:
            return None

    @staticmethod
    def _strip_fence(text: str) -> str:
        cleaned = text.strip()
        cleaned = cleaned.removeprefix("```json").removeprefix("```")
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        return cleaned.strip()
