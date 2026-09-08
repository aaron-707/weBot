from __future__ import annotations

import logging
from typing import Generator
from unittest.mock import AsyncMock, MagicMock

import pytest

from webot.workflows.action_validator import ActionValidator


@pytest.fixture(autouse=True)
def attach_caplog(caplog: pytest.LogCaptureFixture) -> Generator[None, None, None]:
    webot_logger = logging.getLogger("webot")
    webot_logger.addHandler(caplog.handler)
    yield
    webot_logger.removeHandler(caplog.handler)


@pytest.mark.asyncio
async def test_submit_validation_url_change_triggers_success(caplog: pytest.LogCaptureFixture) -> None:
    """Check 1: URL/hash change triggers validation success."""
    caplog.set_level(logging.INFO, logger="webot")
    validator = ActionValidator()

    mock_page = MagicMock()
    mock_page.wait_for_timeout = AsyncMock()
    mock_page.url = "https://example.com/form#submitted"
    mock_page.evaluate = AsyncMock(return_value=10)

    mock_locator = MagicMock()
    mock_locator.first.is_enabled = AsyncMock(return_value=True)
    mock_page.locator = MagicMock(return_value=mock_locator)

    before_state = {"url": "https://example.com/form", "dom_count": 10}
    result = await validator.validate(
        page=mock_page,
        action={"action": "submit", "selector": "#submit"},
        before_state=before_state,
    )

    assert result["success"] is True
    assert result["details"]["validation_passed"] is True
    assert any("submit_signal_url_changed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_submit_validation_modal_text_match_triggers_success(caplog: pytest.LogCaptureFixture) -> None:
    """Check 2: Modal dialog text match triggers validation success."""
    caplog.set_level(logging.INFO, logger="webot")
    validator = ActionValidator()

    mock_page = MagicMock()
    mock_page.wait_for_timeout = AsyncMock()
    mock_page.url = "https://example.com/form"
    mock_page.evaluate = AsyncMock(return_value=10)

    mock_modal = MagicMock()
    mock_modal.inner_text = AsyncMock(return_value="Thanks for submitting the form")

    async def wait_for_selector_mock(selector: str, state: str = "visible", timeout: int = 2000) -> MagicMock:
        if selector == ".modal":
            return mock_modal
        raise TimeoutError("timeout")

    mock_page.wait_for_selector = AsyncMock(side_effect=wait_for_selector_mock)

    def locator_mock(selector: str) -> MagicMock:
        loc = MagicMock()
        if selector == ".modal":
            loc.first.inner_text = AsyncMock(return_value="Thanks for submitting the form")
        else:
            loc.first.is_enabled = AsyncMock(return_value=True)
        return loc

    mock_page.locator = MagicMock(side_effect=locator_mock)

    before_state = {"url": "https://example.com/form", "dom_count": 10}
    result = await validator.validate(
        page=mock_page,
        action={"action": "submit", "selector": "#submit"},
        before_state=before_state,
    )

    assert result["success"] is True
    assert result["details"]["validation_passed"] is True
    assert any("submit_signal_modal_visible" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_submit_validation_toast_selector_triggers_success(caplog: pytest.LogCaptureFixture) -> None:
    """Check 3: Toast or alert selector visibility triggers validation success."""
    caplog.set_level(logging.INFO, logger="webot")
    validator = ActionValidator()

    mock_page = MagicMock()
    mock_page.wait_for_timeout = AsyncMock()
    mock_page.url = "https://example.com/form"
    mock_page.evaluate = AsyncMock(return_value=10)

    mock_toast = MagicMock()

    async def wait_for_selector_mock(selector: str, state: str = "visible", timeout: int = 2000) -> MagicMock:
        if selector == ".alert-success":
            return mock_toast
        raise TimeoutError("timeout")

    mock_page.wait_for_selector = AsyncMock(side_effect=wait_for_selector_mock)

    mock_locator = MagicMock()
    mock_locator.first.is_enabled = AsyncMock(return_value=True)
    mock_page.locator = MagicMock(return_value=mock_locator)

    before_state = {"url": "https://example.com/form", "dom_count": 10}
    result = await validator.validate(
        page=mock_page,
        action={"action": "submit", "selector": "#submit"},
        before_state=before_state,
    )

    assert result["success"] is True
    assert result["details"]["validation_passed"] is True
    assert any("submit_signal_toast_visible" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_submit_validation_form_hidden_or_disabled_triggers_success(caplog: pytest.LogCaptureFixture) -> None:
    """Check 4: Form inputs disabled or hidden triggers validation success."""
    caplog.set_level(logging.INFO, logger="webot")
    validator = ActionValidator()

    mock_page = MagicMock()
    mock_page.wait_for_timeout = AsyncMock()
    mock_page.url = "https://example.com/form"
    mock_page.evaluate = AsyncMock(return_value=10)
    mock_page.wait_for_selector = AsyncMock(side_effect=TimeoutError("timeout"))

    def locator_mock(selector: str) -> MagicMock:
        loc = MagicMock()
        if selector == 'form:not([style*="display:none"]) input:not([disabled])':
            loc.count = AsyncMock(return_value=0)
        else:
            loc.first.is_enabled = AsyncMock(return_value=True)
        return loc

    mock_page.locator = MagicMock(side_effect=locator_mock)

    before_state = {"url": "https://example.com/form", "dom_count": 10}
    result = await validator.validate(
        page=mock_page,
        action={"action": "submit", "selector": "#submit"},
        before_state=before_state,
    )

    assert result["success"] is True
    assert result["details"]["validation_passed"] is True
    assert any("submit_signal_form_hidden_or_disabled" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_submit_validation_all_miss_returns_false(caplog: pytest.LogCaptureFixture) -> None:
    """When all four signals miss and no URL/DOM change occurred, validation fails."""
    caplog.set_level(logging.WARNING, logger="webot")
    validator = ActionValidator()

    mock_page = MagicMock()
    mock_page.wait_for_timeout = AsyncMock()
    mock_page.url = "https://example.com/form"
    mock_page.evaluate = AsyncMock(return_value=10)
    mock_page.wait_for_selector = AsyncMock(side_effect=TimeoutError("timeout"))

    def locator_mock(selector: str) -> MagicMock:
        loc = MagicMock()
        if selector == 'form:not([style*="display:none"]) input:not([disabled])':
            loc.count = AsyncMock(return_value=3)
        else:
            loc.first.is_enabled = AsyncMock(return_value=True)
        return loc

    mock_page.locator = MagicMock(side_effect=locator_mock)

    before_state = {"url": "https://example.com/form", "dom_count": 10}
    result = await validator.validate(
        page=mock_page,
        action={"action": "submit", "selector": "#submit"},
        before_state=before_state,
    )

    assert result["success"] is False
    assert result["details"]["validation_passed"] is False
    assert result["reason"] == "submit_no_observable_change"
    assert any("no_submit_signal_detected" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_submit_signal_logging_emits_expected_keys(caplog: pytest.LogCaptureFixture) -> None:
    """Assert the exact log keys are emitted at the expected levels."""
    validator = ActionValidator()

    # Test warning log on all miss
    caplog.clear()
    caplog.set_level(logging.INFO, logger="webot")
    page_miss = MagicMock()
    page_miss.wait_for_timeout = AsyncMock()
    page_miss.url = "https://example.com/form"
    page_miss.wait_for_selector = AsyncMock(side_effect=TimeoutError("timeout"))
    loc_miss = MagicMock()
    loc_miss.count = AsyncMock(return_value=1)
    page_miss.locator = MagicMock(return_value=loc_miss)

    res_miss = await validator._check_submit_signals(page_miss, "https://example.com/form")
    assert res_miss is False
    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("no_submit_signal_detected" in r.message for r in warning_records)

    # Test info log on URL change
    caplog.clear()
    page_url = MagicMock()
    page_url.wait_for_timeout = AsyncMock()
    page_url.url = "https://example.com/new_page"
    res_url = await validator._check_submit_signals(page_url, "https://example.com/form")
    assert res_url is True
    info_records = [r for r in caplog.records if r.levelname == "INFO"]
    assert any("submit_signal_url_changed" in r.message for r in info_records)
