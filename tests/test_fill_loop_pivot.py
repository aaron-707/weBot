from __future__ import annotations

import logging
from typing import Generator
from unittest.mock import AsyncMock, MagicMock

import pytest

from webot.workflows.agent_loop import AgentLoop, StepRecord


@pytest.fixture(autouse=True)
def attach_caplog(caplog: pytest.LogCaptureFixture) -> Generator[None, None, None]:
    webot_logger = logging.getLogger("webot")
    webot_logger.addHandler(caplog.handler)
    yield
    webot_logger.removeHandler(caplog.handler)


def _build_mock_agent_loop(**overrides) -> AgentLoop:
    mock_page = MagicMock()
    mock_page.url = "https://example.com/form"

    mock_dom_extractor = MagicMock()
    mock_dom_extractor.extract_all = AsyncMock(return_value={"buttons": [], "inputs": [], "links": []})

    mock_decision_engine = MagicMock()
    mock_decision_engine.choose_next_action = MagicMock(
        return_value={"action": "fill", "selector": "#username", "value": "test_user"}
    )

    mock_action_executor = MagicMock()
    mock_action_executor.execute = AsyncMock(
        return_value={"success": True, "action": "fill", "attempts": 1, "data": None}
    )

    mock_action_validator = MagicMock()
    mock_action_validator.capture_state = AsyncMock(return_value={"url": "https://example.com/form", "dom_count": 5})
    mock_action_validator.validate = AsyncMock(
        return_value={"success": True, "reason": "fill_validated", "details": {}}
    )

    mock_session_memory = MagicMock()
    mock_session_memory.get_recent_actions = MagicMock(return_value=[])
    mock_session_memory.get_failures = MagicMock(return_value=[])
    mock_session_memory.add_action = MagicMock()

    mock_dom_delta = MagicMock()
    mock_dom_delta.compare = MagicMock(
        return_value={
            "navigation_changed": False,
            "new_elements": [],
            "removed_elements": [],
            "changed_text": [],
            "form_state_changes": [],
            "token_optimized_summary": "no changes",
        }
    )

    mock_recovery_engine = MagicMock()

    kwargs = {
        "page": mock_page,
        "dom_extractor": mock_dom_extractor,
        "decision_engine": mock_decision_engine,
        "action_executor": mock_action_executor,
        "action_validator": mock_action_validator,
        "session_memory": mock_session_memory,
        "dom_delta": mock_dom_delta,
        "recovery_engine": mock_recovery_engine,
        "max_steps": 6,
        "max_identical_fill_streak": 3,
        "complete_on_extract_text": True,
        # Disable state machines so custom mock actions drive the loop cleanly
        "search_state_machine": None,
        "form_state_machine": None,
        "login_state_machine": None,
    }
    kwargs.update(overrides)
    loop = AgentLoop(**kwargs)
    # Set state machines to a dummy object that doesn't trigger
    dummy_sm = MagicMock()
    dummy_sm.next_action = MagicMock(return_value=None)
    dummy_sm.is_failed = MagicMock(return_value=False)
    dummy_sm.should_allow_extract_termination = MagicMock(return_value=True)
    loop.search_state_machine = dummy_sm
    loop.form_state_machine = dummy_sm
    loop.login_state_machine = dummy_sm
    return loop


def _make_fill_history(count: int, selector: str = "#username", value: str = "test_user") -> list[StepRecord]:
    history: list[StepRecord] = []
    for i in range(1, count + 1):
        history.append(
            {
                "step": i,
                "action": {"action": "fill", "selector": selector, "value": value},
                "success": True,
                "reason": "fill_validated",
                "error": "",
                "data": None,
                "validation": {"success": True, "reason": "fill_validated", "details": {}},
                "recovery": {},
                "dom_delta": {
                    "navigation_changed": False,
                    "new": 0,
                    "removed": 0,
                    "text_changed": 0,
                    "form_changed": 0,
                },
                "interstitial_type": "",
                "anti_bot_detected": False,
            }
        )
    return history


@pytest.mark.asyncio
async def test_attempt_fill_loop_pivot_clicks_first_visible_selector(caplog: pytest.LogCaptureFixture) -> None:
    """_attempt_fill_loop_pivot finds the first visible submit selector, clicks it, and resets streak."""
    caplog.set_level(logging.INFO, logger="webot")
    loop = _build_mock_agent_loop()

    clicked_selectors: list[str] = []

    def mock_locator(selector: str) -> MagicMock:
        loc = MagicMock()
        # button[type=submit] is not visible, but input[type=submit] is visible
        if selector == "input[type=submit]":
            loc.first.is_visible = AsyncMock(return_value=True)

            async def click_mock(*args, **kwargs):
                clicked_selectors.append(selector)

            loc.first.click = AsyncMock(side_effect=click_mock)
        else:
            loc.first.is_visible = AsyncMock(return_value=False)
            loc.first.click = AsyncMock()
        return loc

    loop.page.locator = MagicMock(side_effect=mock_locator)

    history = _make_fill_history(3)
    assert loop._identical_fill_streak(history) == 3

    pivoted = await loop._attempt_fill_loop_pivot()

    assert pivoted is True
    assert clicked_selectors == ["input[type=submit]"]
    assert loop._identical_fill_streak(history) == 0
    assert any(
        r.message == "fill_loop_pivot_submit_clicked" and getattr(r, "selector", None) == "input[type=submit]"
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_attempt_fill_loop_pivot_returns_false_when_none_visible() -> None:
    """_attempt_fill_loop_pivot returns False when no submit selector is visible."""
    loop = _build_mock_agent_loop()

    def mock_locator(selector: str) -> MagicMock:
        loc = MagicMock()
        loc.first.is_visible = AsyncMock(return_value=False)
        loc.first.click = AsyncMock()
        return loc

    loop.page.locator = MagicMock(side_effect=mock_locator)

    pivoted = await loop._attempt_fill_loop_pivot()
    assert pivoted is False


@pytest.mark.asyncio
async def test_fill_loop_pivot_submit_found_continues_loop(caplog: pytest.LogCaptureFixture) -> None:
    """When identical fill streak reaches 3 and submit is found: clicks submit, resets streak, continues loop."""
    caplog.set_level(logging.INFO, logger="webot")
    loop = _build_mock_agent_loop(max_steps=6)

    # Actions decided by decision engine:
    # Steps 1-3: fill same field (building streak to 3 in history)
    # Step 4: fill proposed -> streak check hits threshold -> triggers pivot -> clicks submit button -> continues!
    # Step 5: extract_text -> terminates loop successfully
    actions = [
        {"action": "fill", "selector": "#username", "value": "test_user"},
        {"action": "fill", "selector": "#username", "value": "test_user"},
        {"action": "fill", "selector": "#username", "value": "test_user"},
        {"action": "fill", "selector": "#username", "value": "test_user"},
        {"action": "extract_text", "selector": "body"},
    ]
    action_idx = 0

    def decide_mock(*args, **kwargs):
        nonlocal action_idx
        act = actions[min(action_idx, len(actions) - 1)]
        action_idx += 1
        return act

    loop.decision_engine.choose_next_action = MagicMock(side_effect=decide_mock)

    submit_loc = MagicMock()
    submit_loc.first.is_visible = AsyncMock(return_value=True)
    submit_loc.first.click = AsyncMock()

    def mock_locator(selector: str) -> MagicMock:
        if selector == "button[type=submit]":
            return submit_loc
        loc = MagicMock()
        loc.first.is_visible = AsyncMock(return_value=False)
        return loc

    loop.page.locator = MagicMock(side_effect=mock_locator)

    async def execute_mock(action: dict) -> dict:
        if action.get("action") == "extract_text":
            return {"success": True, "action": "extract_text", "attempts": 1, "data": {"text": "Results found"}}
        return {"success": True, "action": "fill", "attempts": 1, "data": None}

    loop.action_executor.execute = AsyncMock(side_effect=execute_mock)

    res = await loop.run("test goal")

    # Verify submit button was clicked
    submit_loc.first.click.assert_awaited()
    # Verify fill_loop_pivot_submit_clicked was logged
    assert any(
        r.message == "fill_loop_pivot_submit_clicked" and getattr(r, "selector", None) == "button[type=submit]"
        for r in caplog.records
    )
    # Verify the loop continued and completed (did not terminate with failed / loop_guard_triggered)
    assert res.get("status") == "completed"
    assert res.get("error") is None or res.get("error") == ""


@pytest.mark.asyncio
async def test_fill_loop_pivot_exhausted_terminates_with_fallback(caplog: pytest.LogCaptureFixture) -> None:
    """When identical fill streak reaches 3 and no submit button is found: executes extract fallback and terminates."""
    caplog.set_level(logging.INFO, logger="webot")
    loop = _build_mock_agent_loop(max_steps=5)

    def mock_locator(selector: str) -> MagicMock:
        loc = MagicMock()
        loc.first.is_visible = AsyncMock(return_value=False)
        loc.first.click = AsyncMock()
        return loc

    loop.page.locator = MagicMock(side_effect=mock_locator)

    executed_actions: list[dict] = []

    async def execute_mock(action: dict) -> dict:
        executed_actions.append(action)
        if action.get("action") == "extract_text":
            return {"success": True, "action": "extract_text", "attempts": 1, "data": "Captured body text"}
        return {"success": True, "action": "fill", "attempts": 1, "data": None}

    loop.action_executor.execute = AsyncMock(side_effect=execute_mock)

    loop.decision_engine.choose_next_action = MagicMock(
        return_value={"action": "fill", "selector": "#username", "value": "test_user"}
    )

    res = await loop.run("test goal")

    # Verify termination reason is fill_loop_exhausted_pivots
    assert res.get("status") == "failed"
    assert res.get("error") == "fill_loop_exhausted_pivots"
    # Verify fallback extraction occurred
    assert any(a.get("action") == "extract_text" and a.get("selector") == "body" for a in executed_actions)
    assert res.get("final_data") == "Captured body text"
    # Verify info log fill_loop_pivot_extract_fallback
    assert any(r.message == "fill_loop_pivot_extract_fallback" for r in caplog.records)


def test_pivot_only_triggers_after_streak_threshold_not_before() -> None:
    """Pivot is not attempted when streak is 1 or 2; only when streak hits 3."""
    loop = _build_mock_agent_loop()

    # Streak = 1
    hist_1 = _make_fill_history(1)
    guard_1 = loop._check_loop_guards(history=hist_1, next_action={"action": "fill"})
    assert guard_1 is None

    # Streak = 2
    hist_2 = _make_fill_history(2)
    guard_2 = loop._check_loop_guards(history=hist_2, next_action={"action": "fill"})
    assert guard_2 is None

    # Streak = 3 (threshold reached)
    hist_3 = _make_fill_history(3)
    guard_3 = loop._check_loop_guards(history=hist_3, next_action={"action": "fill"})
    assert guard_3 == "fill_loop_guard_triggered"
