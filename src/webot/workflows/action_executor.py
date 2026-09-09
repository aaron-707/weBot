from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypedDict

import structlog
from structlog.typing import FilteringBoundLogger


ActionType = Literal["goto", "click", "fill", "submit", "extract_text", "editor_fill"]


class Action(TypedDict, total=False):
    action: ActionType
    selector: str
    url: str
    value: str
    submit_method: str


class ActionResult(TypedDict, total=False):
    success: bool
    action: str
    attempts: int
    data: Any
    error: str


class BrowserControllerProtocol(Protocol):
    async def goto(self, url: str) -> None: ...

    async def click(self, selector: str) -> None: ...

    async def fill(self, selector: str, value: str) -> None: ...

    async def extract_text(self, selector: str) -> str: ...

    async def set_editor_content(self, code: str, selector: str | None = None) -> dict[str, bool | str | None]: ...


@dataclass(slots=True)
class ActionExecutor:
    """Validates and executes structured browser actions with retries."""

    browser_controller: BrowserControllerProtocol
    max_retries: int = 2
    retry_delay_seconds: float = 0.5
    _logger: FilteringBoundLogger = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._logger = structlog.get_logger(__name__)

    async def execute(self, action: Action) -> ActionResult:
        """Validate and execute a single action dictionary."""
        validation_error = self._validate_action(action)
        if validation_error:
            self._logger.warning(
                "action_validation_failed",
                action=action,
                error=validation_error,
            )
            return {
                "success": False,
                "action": str(action.get("action", "")),
                "attempts": 0,
                "error": validation_error,
            }

        action_name = action["action"]
        attempts = 0
        last_error: str = ""

        while attempts <= self.max_retries:
            attempts += 1
            try:
                self._logger.info(
                    "action_execution_started",
                    action=action_name,
                    attempt=attempts,
                )

                data = await self._execute_action(action)

                self._logger.info(
                    "action_execution_succeeded",
                    action=action_name,
                    attempt=attempts,
                )
                return {
                    "success": True,
                    "action": action_name,
                    "attempts": attempts,
                    "data": data,
                }
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                safe_error = last_error.encode("ascii", errors="replace").decode("ascii")
                try:
                    self._logger.exception(
                        "action_execution_failed",
                        action=action_name,
                        attempt=attempts,
                        max_retries=self.max_retries,
                        error=safe_error,
                    )
                except UnicodeEncodeError:
                    self._logger.warning(
                        "action_execution_failed",
                        action=action_name,
                        attempt=attempts,
                        max_retries=self.max_retries,
                        error=safe_error,
                    )

                if attempts > self.max_retries:
                    break

                if self.retry_delay_seconds > 0:
                    await asyncio.sleep(self.retry_delay_seconds)

        return {
            "success": False,
            "action": action_name,
            "attempts": attempts,
            "error": last_error or "Action execution failed",
        }

    async def _execute_action(self, action: Action) -> Any:
        action_name = action["action"]

        if action_name == "goto":
            await self.browser_controller.goto(action["url"])
            return {"url": action["url"]}

        if action_name == "click":
            await self.browser_controller.click(action["selector"])
            page = getattr(self.browser_controller, "page", None)
            if page is not None and any(k in action["selector"].lower() for k in ("search", "submit")):
                try:
                    await page.wait_for_timeout(1000)
                except Exception:
                    pass
            return {"selector": action["selector"]}

        if action_name == "fill":
            fill_value = action["value"]
            await self.browser_controller.fill(action["selector"], fill_value)
            return {
                "selector": action["selector"],
                "value": fill_value,
            }

        if action_name == "submit":
            selector = action["selector"]
            method = str(action.get("submit_method", "enter")).lower()
            page = getattr(self.browser_controller, "page", None)
            if page is None:
                raise ValueError("Submit action requires browser controller page access")
            if method == "click":
                await self.browser_controller.click(selector)
            else:
                await page.locator(selector).first.press("Enter", timeout=1500)
            try:
                await page.wait_for_timeout(1000)
            except Exception:
                pass
            return {"selector": selector, "method": method}

        if action_name == "extract_text":
            text = await self.browser_controller.extract_text(action["selector"])
            return {
                "selector": action["selector"],
                "text": text,
            }

        if action_name == "editor_fill":
            code = action["value"]
            selector = action.get("selector")
            res = await self.browser_controller.set_editor_content(code, selector=selector)
            if isinstance(res, dict) and not res.get("ok"):
                raise RuntimeError(res.get("error") or "Failed to set editor content")
            return {
                "selector": selector or "editor",
                "value_length": len(code),
            }

        # Defensive fallback even though validation should prevent this.
        raise ValueError(f"Unsupported action: {action_name}")

    def _validate_action(self, action: Action) -> str | None:
        if not isinstance(action, dict):
            return "Action must be a dictionary"

        action_name = action.get("action")
        if action_name not in {"goto", "click", "fill", "submit", "extract_text", "editor_fill"}:
            return "Invalid or missing action type"

        if action_name == "goto":
            return self._require_non_empty_string(action, "url")

        if action_name in {"click", "extract_text", "submit"}:
            return self._require_non_empty_string(action, "selector")

        if action_name == "fill":
            selector_error = self._require_non_empty_string(action, "selector")
            if selector_error:
                return selector_error
            return self._require_non_empty_string(action, "value")

        if action_name == "editor_fill":
            return self._require_non_empty_string(action, "value")

        return "Invalid action"

    @staticmethod
    def _require_non_empty_string(action: Action, key: str) -> str | None:
        value = action.get(key)
        if not isinstance(value, str) or not value.strip():
            return f"Missing or invalid '{key}'"
        return None
