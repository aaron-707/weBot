from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from webot.workflows.action_executor import ActionExecutor
from webot.workflows.action_validator import ActionValidator
from webot.workflows.coding_state_machine import CodingState, CodingStateMachine
from webot.workflows.task_interpreter import TaskInterpreter


def test_task_interpreter_detects_leetcode_coding_goal():
    prompt = "open leetcode and goto any easy level problem of your choice and solve it then submit the program(use python)"
    ti = TaskInterpreter()

    assert ti.is_coding_goal(prompt) is True
    assert ti.extract_coding_language(prompt) == "python"
    assert ti.extract_coding_difficulty(prompt) == "easy"
    start_url = ti.extract_start_url(prompt)
    assert start_url == "https://leetcode.com/problemset/"


def test_coding_state_machine_navigates_to_leetcode_from_blank():
    prompt = "open leetcode and goto any easy level problem of your choice and solve it then submit the program(use python)"
    csm = CodingStateMachine()
    dom_state = {"inputs": [], "buttons": [], "links": []}

    action = csm.next_action(
        user_goal=prompt,
        dom_state=dom_state,
        current_url="about:blank",
        recent_actions=[],
    )
    assert action is not None
    assert action["action"] == "goto"
    assert "leetcode.com/problemset/" in action["url"]


def test_coding_state_machine_problemset_selection_and_transitions():
    prompt = "open leetcode and solve an easy problem in python and submit"
    csm = CodingStateMachine()
    dom_state = {
        "inputs": [],
        "buttons": [],
        "links": [
            {
                "selector": "#two-sum-link",
                "text": "1. Two Sum",
                "attributes": {"href": "/problems/two-sum/"},
            }
        ],
    }

    # Step 1: On problemset page, selects Two Sum link
    action_select = csm.next_action(
        user_goal=prompt,
        dom_state=dom_state,
        current_url="https://leetcode.com/problemset/",
        recent_actions=[],
    )
    assert action_select is not None
    assert action_select["action"] == "click"
    assert "two-sum" in action_select["selector"]

    # Step 2: On problem page, extracts problem description
    action_extract = csm.next_action(
        user_goal=prompt,
        dom_state=dom_state,
        current_url="https://leetcode.com/problems/two-sum/",
        recent_actions=[],
    )
    assert action_extract is not None
    assert action_extract["action"] == "extract_text"

    # Step 3: Synthesize and inject code
    action_inject = csm.next_action(
        user_goal=prompt,
        dom_state=dom_state,
        current_url="https://leetcode.com/problems/two-sum/",
        recent_actions=[
            {
                "action": "extract_text",
                "data": {"text": "Given an array of integers nums and an integer target, return indices of two numbers..."},
            }
        ],
    )
    assert action_inject is not None
    assert action_inject["action"] == "editor_fill"
    assert "def twoSum" in action_inject["value"]

    # Step 4: Click Submit button
    dom_with_submit = {
        "inputs": [],
        "buttons": [
            {
                "selector": "[data-e2e-locator='console-submit-button']",
                "text": "Submit",
                "attributes": {"data-e2e-locator": "console-submit-button"},
            }
        ],
        "links": [],
    }
    action_submit = csm.next_action(
        user_goal=prompt,
        dom_state=dom_with_submit,
        current_url="https://leetcode.com/problems/two-sum/",
        recent_actions=[],
    )
    assert action_submit is not None
    assert action_submit["action"] == "click"
    assert "submit" in action_submit["selector"]


@pytest.mark.asyncio
async def test_action_executor_and_validator_editor_fill():
    mock_browser = MagicMock()
    mock_browser.set_editor_content = AsyncMock(return_value={"ok": True, "error": None})

    executor = ActionExecutor(browser_controller=mock_browser)
    validator = ActionValidator()

    code = "class Solution:\n    pass\n"
    action = {"action": "editor_fill", "value": code}

    result = await executor.execute(action)
    assert result["success"] is True
    assert result["action"] == "editor_fill"
    mock_browser.set_editor_content.assert_awaited_once_with(code, selector=None)

    val_result = await validator.validate(
        page=MagicMock(),
        action=action,
        action_result=result,
    )
    assert val_result["success"] is True
    assert val_result["reason"] == "editor_fill_validated"
