"""Unit tests for GoalEvaluator deterministic confidence floors.

Covers:
- _evaluate_login: post-auth URL (/secure, dashboard, welcome) raises
  completion_confidence to >= 0.85.
- _evaluate_search: extracted_text > 200 chars + "result" key in dom_summary
  raises completion_confidence to >= 0.75.
Neither test touches the LLM fallback or _adjust_with_progress.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from webot.workflows.form_state_machine import FormStateMachine
from webot.workflows.goal_evaluator import GoalEvaluator

_EMPTY_PROGRESS: dict = {
    "progress_score": 0.5,
    "stagnation_score": 0.0,
    "loop_risk_score": 0.0,
    "termination_recommendation": "continue",
    "signals": {},
}

_NO_LLM = GoalEvaluator(llm_client=None)


# -- Login floor tests ---------------------------------------------------------

@pytest.mark.parametrize("url,label", [
    ("https://the-internet.herokuapp.com/secure", "/secure"),
    ("https://app.example.com/dashboard", "dashboard"),
    ("https://app.example.com/welcome", "welcome"),
])
def test_login_post_auth_url_floor(url: str, label: str) -> None:
    """A post-auth URL alone must push confidence to >= 0.85."""
    result = _NO_LLM.evaluate(
        user_goal="Login to the site",
        current_url=url,
        dom_summary={},
        extracted_text="",
        recent_actions=[],
        progress_report=_EMPTY_PROGRESS,
    )
    assert result["completion_confidence"] >= 0.85, (
        f"Expected >= 0.85 for URL with '{label}', got {result['completion_confidence']:.4f}"
    )
    assert result["task_type"] == "login"


def test_login_no_floor_without_post_auth_url() -> None:
    """A plain /login URL must NOT receive the floor boost."""
    result = _NO_LLM.evaluate(
        user_goal="Login to the site",
        current_url="https://example.com/login",
        dom_summary={},
        extracted_text="",
        recent_actions=[],
        progress_report=_EMPTY_PROGRESS,
    )
    assert result["completion_confidence"] < 0.85, (
        f"Expected < 0.85 for /login URL, got {result['completion_confidence']:.4f}"
    )


# -- Search floor tests --------------------------------------------------------

def test_search_result_bucket_and_long_text_floor() -> None:
    """extracted_text > 200 chars + 'result' dom_summary key => confidence >= 0.75."""
    long_text = "Python internship " * 20
    dom_with_results = {"results": [{"title": "Job 1"}, {"title": "Job 2"}]}

    result = _NO_LLM.evaluate(
        user_goal="Search for python internships",
        current_url="https://duckduckgo.com/?q=python+internships",
        dom_summary=dom_with_results,
        extracted_text=long_text,
        recent_actions=[{"action": "fill", "selector": "input", "value": "python internships"}],
        progress_report=_EMPTY_PROGRESS,
    )
    assert result["completion_confidence"] >= 0.75, (
        f"Expected >= 0.75 for result bucket + long text, got {result['completion_confidence']:.4f}"
    )
    assert result["task_type"] == "search"


def test_search_no_floor_without_result_bucket() -> None:
    """Long text with no 'result' DOM key must NOT trigger the search floor."""
    long_text = "Some random page content " * 20
    dom_no_results = {"links": [{"href": "/a"}, {"href": "/b"}]}

    result = _NO_LLM.evaluate(
        user_goal="Search for python internships",
        current_url="https://unknown-host.example/",
        dom_summary=dom_no_results,
        extracted_text=long_text,
        recent_actions=[],
        progress_report=_EMPTY_PROGRESS,
    )
    assert result["completion_confidence"] < 0.75, (
        f"Expected < 0.75 without result bucket, got {result['completion_confidence']:.4f}"
    )


def test_search_no_floor_with_short_text() -> None:
    """Even with a 'result' key, text <= 200 chars must NOT trigger the floor."""
    dom_with_results = {"results": [{"title": "Job 1"}]}

    result = _NO_LLM.evaluate(
        user_goal="Search for python internships",
        current_url="https://unknown-host.example/",
        dom_summary=dom_with_results,
        extracted_text="Short",
        recent_actions=[],
        progress_report=_EMPTY_PROGRESS,
    )
    assert result["completion_confidence"] < 0.75, (
        f"Expected < 0.75 with short text, got {result['completion_confidence']:.4f}"
    )


# -- Form goal word boundary classification tests ------------------------------

@pytest.mark.parametrize("goal", [
    "Review the platform documentation",
    "Perform the automated check",
    "Check the format of the file",
    "Follow the uniform guidelines",
    "Transform the input data",
    "Provide informed consent",
    "Open the information page",
])
def test_non_form_goals_do_not_classify_as_form_fill(goal: str) -> None:
    """Goals containing 'form' as a substring must NOT classify as form_fill."""
    assert GoalEvaluator._infer_task_type(goal) != "form_fill", (
        f"Goal '{goal}' should not infer task_type as form_fill"
    )
    assert not FormStateMachine._is_form_goal(goal), (
        f"Goal '{goal}' should not be detected as form goal by FormStateMachine"
    )


@pytest.mark.parametrize("goal", [
    "fill out the form",
    "submit the application form",
    "Fill the registration form",
    "Please submit form with details",
])
def test_genuine_form_goals_classify_as_form_fill(goal: str) -> None:
    """Genuine form goals with word-bounded 'form' or fill/submit must classify as form_fill."""
    assert GoalEvaluator._infer_task_type(goal) == "form_fill", (
        f"Goal '{goal}' should infer task_type as form_fill"
    )
    assert FormStateMachine._is_form_goal(goal), (
        f"Goal '{goal}' should be detected as form goal by FormStateMachine"
    )

