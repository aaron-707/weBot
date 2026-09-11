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


def test_form_state_machine_does_not_hijack_coding_goals():
    from webot.workflows.form_state_machine import FormStateMachine

    fsm = FormStateMachine()
    prompt = "open leetcode and goto any easy level problem of your choice and solve it then submit the program(use python)"
    dom_state = {"inputs": [], "buttons": [], "links": []}

    action = fsm.next_action(
        user_goal=prompt,
        dom_state=dom_state,
        current_url="https://leetcode.com/problemset/",
        recent_actions=[],
    )
    assert action is None, "FormStateMachine must NOT hijack a LeetCode problem submission goal"


def test_coding_state_machine_handles_followup_prompts_on_active_domain():
    csm = CodingStateMachine()
    dom_state = {
        "inputs": [],
        "buttons": [],
        "links": [
            {
                "selector": "a.two-sum",
                "text": "Two Sum",
                "attributes": {"href": "/problems/two-sum/"},
            }
        ],
    }

    # Follow-up prompt with informal wording on active LeetCode page
    action = csm.next_action(
        user_goal="choose any problem of you choice",
        dom_state=dom_state,
        current_url="https://leetcode.com/problemset/",
        recent_actions=[],
    )
    assert action is not None
    assert action["action"] == "click"
    assert "two-sum" in action["selector"]


def test_coding_state_machine_language_toggle_with_dropdown_options():
    csm = CodingStateMachine()
    csm._state = CodingState.SET_LANGUAGE
    prompt = "solve two sum in python"

    # Stage 1: C++ button is currently visible. CSM should click it once to open the dropdown
    dom_closed = {
        "buttons": [
            {"selector": "button#lang-picker", "text": "C++", "attributes": {}}
        ],
        "links": [],
    }
    action1 = csm.next_action(
        user_goal=prompt,
        dom_state=dom_closed,
        current_url="https://leetcode.com/problems/two-sum/",
        recent_actions=[],
    )
    assert action1 is not None
    assert action1["action"] == "click"
    assert action1["selector"] == "button#lang-picker"
    assert csm._lang_trigger_clicked is True

    # Stage 2: Dropdown opened, options are visible including Python3
    dom_open = {
        "buttons": [
            {"selector": "button#lang-picker", "text": "C++", "attributes": {}},
            {"selector": "div[role='option']:has-text('Python3')", "text": "Python3", "attributes": {"role": "option"}},
        ],
        "links": [],
    }
    action2 = csm.next_action(
        user_goal=prompt,
        dom_state=dom_open,
        current_url="https://leetcode.com/problems/two-sum/",
        recent_actions=[],
    )
    assert action2 is not None
    assert action2["action"] == "click"
    assert "Python3" in action2["selector"]
    assert csm._state == CodingState.SYNTHESIZE_SOLUTION


def test_coding_state_machine_skips_toggle_if_already_target_language():
    csm = CodingStateMachine()
    csm._state = CodingState.SET_LANGUAGE
    prompt = "solve two sum in python"

    # Language is already Python3
    dom_already_python = {
        "buttons": [
            {"selector": "button#lang-picker", "text": "Python3", "attributes": {}}
        ],
        "links": [],
    }
    action = csm.next_action(
        user_goal=prompt,
        dom_state=dom_already_python,
        current_url="https://leetcode.com/problems/two-sum/",
        recent_actions=[],
    )
    # Transitions directly to SYNTHESIZE_SOLUTION and outputs editor_fill
    assert action is not None
    assert action["action"] == "editor_fill"
    assert csm._state == CodingState.INJECT_CODE


def test_dom_extractor_button_selectors_include_dropdown_and_menu_roles():
    from webot.intelligence.dom_extractor import DomExtractor
    selectors = " ".join(DomExtractor.button_selectors())
    assert "[role='combobox']" in selectors
    assert "[role='option']" in selectors
    assert "[role='menuitem']" in selectors


def test_agent_loop_guards_ping_pong_vs_single_page():
    from webot.workflows.agent_loop import AgentLoop

    loop = AgentLoop(
        page=MagicMock(),
        dom_extractor=MagicMock(),
        decision_engine=MagicMock(),
        action_executor=MagicMock(),
        action_validator=MagicMock(),
        session_memory=MagicMock(),
        dom_delta=MagicMock(),
        recovery_engine=MagicMock(),
    )

    # 4 steps on the SAME URL must NOT trigger navigation ping pong
    same_page_history = [
        {"validation": {"details": {"current_url": "https://leetcode.com/problems/two-sum"}}},
        {"validation": {"details": {"current_url": "https://leetcode.com/problems/two-sum"}}},
        {"validation": {"details": {"current_url": "https://leetcode.com/problems/two-sum"}}},
        {"validation": {"details": {"current_url": "https://leetcode.com/problems/two-sum"}}},
    ]
    guard = loop._check_loop_guards(history=same_page_history, next_action={"action": "click", "selector": "#btn"})
    assert guard != "loop_guard_navigation_ping_pong"

    # Genuine oscillation A -> B -> A -> B MUST trigger navigation ping pong
    ping_pong_history = [
        {"validation": {"details": {"current_url": "https://site.com/a"}}},
        {"validation": {"details": {"current_url": "https://site.com/b"}}},
        {"validation": {"details": {"current_url": "https://site.com/a"}}},
        {"validation": {"details": {"current_url": "https://site.com/b"}}},
    ]
    guard_pp = loop._check_loop_guards(history=ping_pong_history, next_action={"action": "click", "selector": "#btn"})
    assert guard_pp == "loop_guard_navigation_ping_pong"


def test_coding_state_machine_ignores_multiline_container_in_language_selection():
    csm = CodingStateMachine()
    csm._state = CodingState.SET_LANGUAGE
    csm._lang_trigger_clicked = True
    prompt = "open leetcode and solve two sum in python and submit"

    # DOM state contains a multiline container text that includes Python3 and Ruby
    dom_state = {
        "buttons": [
            {"selector": "button#lang-picker", "text": "C++", "attributes": {}}
        ],
        "links": [],
        "visible_text": [
            {
                "selector": "html > body > div:nth-of-type(8)",
                "text": "C++\nJava\nPython\nPython3\nRuby\nGo\nRust",
                "attributes": {},
            }
        ],
    }

    action = csm.next_action(
        user_goal=prompt,
        dom_state=dom_state,
        current_url="https://leetcode.com/problems/two-sum/",
        recent_actions=[],
    )

    assert action is not None
    assert action["action"] == "click"
    # Must NOT click the container div
    assert action["selector"] != "html > body > div:nth-of-type(8)"
    # Must target Python3 leaf selector
    assert "Python3" in action["selector"]


def test_coding_state_machine_detects_login_required_verdict():
    csm = CodingStateMachine()
    csm._state = CodingState.VERIFY_VERDICT
    prompt = "open leetcode and solve two sum in python and submit"

    dom_state = {
        "buttons": [],
        "links": [],
        "visible_text": [
            {
                "selector": "div.login-prompt",
                "text": "You need to log in / sign up to run or submit",
                "attributes": {},
            }
        ],
    }

    action = csm.next_action(
        user_goal=prompt,
        dom_state=dom_state,
        current_url="https://leetcode.com/problems/two-sum/",
        recent_actions=[],
    )

    assert action is not None
    assert csm._state == CodingState.COMPLETED


def test_decision_engine_python_prompt_includes_class_solution():
    from webot.llm.decision_engine import DecisionEngine

    mock_client = MagicMock()
    mock_client.is_available.return_value = True
    mock_client.generate.return_value = "class Solution:\n    def twoSum(self, nums, target):\n        return []"

    de = DecisionEngine(ollama_client=mock_client)
    res = de.synthesize_code_solution(
        problem_description="Given an array of integers nums and an integer target...",
        starter_code="",
        language="python",
    )

    assert "class Solution:" in res
    mock_client.generate.assert_called_once()
    called_prompt = mock_client.generate.call_args[0][0]
    assert "class Solution:" in called_prompt


def test_deterministic_action_skips_goto_when_already_on_url():
    """Verify _deterministic_action does NOT emit a goto when the URL in the goal
    matches the current page URL."""
    from webot.workflows.agent_loop import AgentLoop

    dom_state = {"inputs": [], "buttons": [], "links": [], "visible_text": []}

    # Case 1: explicit URL in goal matches current_url — should return None
    action = AgentLoop._deterministic_action(
        user_goal="i want you to go to https://flappybird.io/ and play the game",
        dom_state=dom_state,
        step=1,
        recent_actions=[],
        failures=[],
        current_url="https://flappybird.io/",
    )
    assert action is None or action.get("action") != "goto", (
        f"Should NOT emit goto to current URL, got: {action}"
    )

    # Case 2: bare domain in goal matches current_url — should return None
    action2 = AgentLoop._deterministic_action(
        user_goal="go to flappybird.io and play",
        dom_state=dom_state,
        step=1,
        recent_actions=[],
        failures=[],
        current_url="https://flappybird.io/",
    )
    assert action2 is None or action2.get("action") != "goto", (
        f"Should NOT emit goto to current URL, got: {action2}"
    )

    # Case 3: different URL — should return goto
    action3 = AgentLoop._deterministic_action(
        user_goal="go to https://flappybird.io/ and play",
        dom_state=dom_state,
        step=1,
        recent_actions=[],
        failures=[],
        current_url="https://example.com/",
    )
    assert action3 is not None
    assert action3["action"] == "goto"
    assert "flappybird.io" in action3["url"]


def test_llm_suggested_goto_to_current_url_is_suppressed():
    """Verify _select_next_action suppresses LLM-suggested goto to the current URL."""
    from webot.workflows.agent_loop import AgentLoop

    mock_page = MagicMock()
    mock_page.url = "https://flappybird.io/"

    mock_decision_engine = MagicMock()
    mock_decision_engine.choose_next_action.return_value = {
        "action": "goto",
        "url": "https://flappybird.io/",
    }

    loop = AgentLoop(
        page=mock_page,
        dom_extractor=MagicMock(),
        decision_engine=mock_decision_engine,
        action_executor=MagicMock(),
        action_validator=MagicMock(),
        session_memory=MagicMock(),
        dom_delta=MagicMock(),
        recovery_engine=MagicMock(),
        llm_enabled=True,
    )

    dom_state = {"inputs": [], "buttons": [], "links": [], "visible_text": []}
    delta = {
        "navigation_changed": False,
        "new_elements": [],
        "removed_elements": [],
        "changed_text": [],
        "form_state_changes": [],
    }

    action = loop._select_next_action(
        user_goal="play flappy bird",
        dom_state=dom_state,
        delta=delta,
        recent_actions=[],
        failures=[],
        step=2,
    )

    # Should be extract_text, not goto
    assert action["action"] == "extract_text", (
        f"LLM goto to current URL should be suppressed, got: {action}"
    )
