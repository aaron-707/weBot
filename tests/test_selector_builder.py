from __future__ import annotations

import logging
from typing import Generator
from unittest.mock import MagicMock

import pytest

from webot.intelligence.selector_builder import SelectorBuilder
from webot.utils.logger import get_logger
from webot.workflows.recovery_engine import RecoveryEngine


@pytest.fixture(autouse=True)
def attach_caplog(caplog: pytest.LogCaptureFixture) -> Generator[None, None, None]:
    engine_logger = get_logger("webot.workflows.recovery_engine")
    engine_logger.setLevel(logging.DEBUG)
    engine_logger.addHandler(caplog.handler)
    webot_logger = logging.getLogger("webot")
    webot_logger.setLevel(logging.DEBUG)
    webot_logger.addHandler(caplog.handler)
    yield
    engine_logger.removeHandler(caplog.handler)
    webot_logger.removeHandler(caplog.handler)


def test_build_fallback_chain_returns_raw_strings() -> None:
    """build_fallback_chain must return raw selector strings without 'page.' prefix."""
    builder = SelectorBuilder()
    elements = [
        {"tag": "button", "id": "submit-btn", "name": "submit", "aria_label": "Submit Form"},
        {"tag": "input", "placeholder": "Enter username", "name": "username"},
    ]

    results = builder.build_fallback_chain(elements, action_type="click")
    assert len(results) > 0
    for sel in results:
        assert not sel.startswith("page.")
        assert "page.locator(" not in sel
        assert "page." not in sel


def test_build_fallback_chain_returns_at_most_3_results() -> None:
    """build_fallback_chain must return at most 3 candidate selector strings."""
    builder = SelectorBuilder()
    elements = [
        {"tag": "button", "id": "btn1", "name": "submit1", "aria_label": "Submit 1", "text": "Save"},
        {"tag": "input", "id": "inp1", "name": "user", "placeholder": "Username"},
        {"tag": "a", "id": "link1", "aria_label": "Next page", "text": "Next"},
    ]

    results = builder.build_fallback_chain(elements, action_type="click")
    assert len(results) <= 3


def test_build_fallback_chain_returns_empty_for_empty_elements() -> None:
    """build_fallback_chain returns [] when given empty elements list."""
    builder = SelectorBuilder()
    assert builder.build_fallback_chain([], action_type="click") == []
    assert builder.build_fallback_chain([], action_type="fill") == []
    assert builder.build_fallback_chain([], action_type="submit") == []


@pytest.mark.asyncio
async def test_recovery_engine_uses_fallback_selector_key_when_selector_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Recovery engine uses fallback_selector key when selector fails and elements exist."""
    caplog.set_level(logging.DEBUG, logger="webot")
    recovery_engine = RecoveryEngine()
    mock_page = MagicMock()

    elements = [{"tag": "button", "id": "alt-submit", "name": "submit_btn"}]
    result = await recovery_engine.recover(
        page=mock_page,
        error_type="missing_selector",
        failed_action={"action": "click", "selector": "#primary-submit"},
        elements=elements,
    )

    assert result["recovered"] is True
    assert "fallback_selector" in result
    assert result["next_action"]["selector"] == result["fallback_selector"]
    assert not result["fallback_selector"].startswith("page.")

    # Verify debug log for trying fallback selector
    assert any("recovery_trying_fallback_selector" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_recovery_engine_selector_failure_error_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Recovery engine also handles selector_failure error_type with fallback_selector."""
    recovery_engine = RecoveryEngine()
    mock_page = MagicMock()

    elements = [{"tag": "input", "id": "search-input", "name": "q"}]
    result = await recovery_engine.recover(
        page=mock_page,
        error_type="selector_failure",
        failed_action={"action": "fill", "selector": "#missing", "value": "test"},
        elements=elements,
    )

    assert result["recovered"] is True
    assert "fallback_selector" in result
    assert result["next_action"]["selector"] == result["fallback_selector"]


@pytest.mark.asyncio
async def test_recovery_engine_no_fallback_selectors_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When fallback chain is empty, warning is logged and recovery proceeds as before."""
    caplog.set_level(logging.WARNING, logger="webot")
    recovery_engine = RecoveryEngine()
    mock_page = MagicMock()

    result = await recovery_engine.recover(
        page=mock_page,
        error_type="missing_selector",
        failed_action={"action": "click", "selector": "#nonexistent"},
        elements=[],
    )

    assert "fallback_selector" not in result
    assert any("recovery_no_fallback_selectors" in r.message for r in caplog.records)
