from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from webot.workflows.agent_loop import AgentLoop
from webot.workflows.search_state_machine import SearchStateMachine
from webot.workflows.task_interpreter import TaskInterpreter


def test_search_state_machine_rejects_external_site_session():
    sm = SearchStateMachine()
    dom_state = {"inputs": [], "buttons": [], "links": []}

    # Case 1: Browser is on YouTube
    action = sm.next_action(
        user_goal="open youtube and search for the most populat nfs video",
        dom_state=dom_state,
        current_url="https://www.youtube.com",
        recent_actions=[],
    )
    assert action is None, "SearchStateMachine must not hijack an active YouTube session"

    # Case 2: Browser is on GitHub
    action = sm.next_action(
        user_goal="go to github.com and search for webot",
        dom_state=dom_state,
        current_url="https://github.com",
        recent_actions=[],
    )
    assert action is None, "SearchStateMachine must not hijack an active GitHub session"


def test_search_state_machine_rejects_external_site_goal_even_if_blank_url():
    sm = SearchStateMachine()
    dom_state = {"inputs": [], "buttons": [], "links": []}

    # Case: Blank URL but goal targets an external platform
    action = sm.next_action(
        user_goal="open youtube and search for the most populat nfs video",
        dom_state=dom_state,
        current_url="about:blank",
        recent_actions=[],
    )
    assert action is None, "SearchStateMachine must not claim goals targeting external platforms"


def test_search_state_machine_accepts_supported_providers():
    sm_ddg = SearchStateMachine()
    dom_state = {"inputs": [], "buttons": [], "links": []}

    # DuckDuckGo on DDG page
    action_ddg = sm_ddg.next_action(
        user_goal="search for Python internships",
        dom_state=dom_state,
        current_url="https://duckduckgo.com",
        recent_actions=[],
    )
    assert action_ddg is not None
    assert action_ddg.get("action") == "goto"
    assert "duckduckgo.com" in str(action_ddg.get("url"))

    # Wikipedia on Wikipedia page
    sm_wiki = SearchStateMachine()
    action_wiki = sm_wiki.next_action(
        user_goal="search wikipedia for Artificial Intelligence",
        dom_state=dom_state,
        current_url="https://www.wikipedia.org",
        recent_actions=[],
    )
    assert action_wiki is not None
    assert action_wiki.get("action") == "goto"
    assert "wikipedia.org" in str(action_wiki.get("url"))

    # Generic search on blank page
    sm_blank = SearchStateMachine()
    action_blank = sm_blank.next_action(
        user_goal="search for best laptops",
        dom_state=dom_state,
        current_url="about:blank",
        recent_actions=[],
    )
    assert action_blank is not None
    assert action_blank.get("action") == "goto"


def test_agent_loop_deterministic_action_does_not_redirect_to_google_on_external_site():
    dom_state = {
        "inputs": [
            {
                "selector": "#search-input",
                "attributes": {"name": "search_query", "placeholder": "Search"},
            }
        ],
        "buttons": [],
        "links": [],
    }

    action = AgentLoop._deterministic_action(
        user_goal="open youtube and search for the most populat nfs video",
        dom_state=dom_state,
        step=1,
        recent_actions=[],
        failures=[],
        current_url="https://www.youtube.com",
    )

    # Must NOT be a goto action to google.com
    assert action is not None
    assert action.get("action") == "fill"
    assert action.get("selector") == "#search-input"
    assert "nfs video" in action.get("value", "")


def test_agent_loop_deterministic_action_submits_after_fill():
    search_input = "#search-input"
    dom_state = {
        "inputs": [
            {
                "selector": search_input,
                "attributes": {"name": "search_query", "placeholder": "Search"},
            }
        ],
        "buttons": [
            {
                "selector": "#search-icon-legacy",
                "text": "",
                "attributes": {"aria-label": "Search", "id": "search-icon-legacy"},
            }
        ],
        "links": [],
    }

    # Step 2: previous action was fill on the same search input
    action = AgentLoop._deterministic_action(
        user_goal="open youtube and search for the most populat nfs video",
        dom_state=dom_state,
        step=2,
        recent_actions=[{"action": "fill", "selector": search_input, "value": "nfs video"}],
        failures=[],
        current_url="https://www.youtube.com",
    )

    # Must submit (click search button or enter) rather than emitting another fill
    assert action is not None
    assert action.get("action") in {"click", "submit"}


def test_task_interpreter_resolves_youtube_start_url():
    ti = TaskInterpreter()
    url = ti.extract_start_url("open youtube and search for the most populat nfs video")
    assert url == "https://youtube.com"
