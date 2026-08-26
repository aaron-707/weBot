from unittest.mock import MagicMock
import pytest
from webot.workflows.goal_evaluator import GoalEvaluator
from webot.intelligence.interstitial_detector import InterstitialDetector
from webot.workflows.agent_loop import AgentLoop
from webot.workflows.search_state_machine import SearchStateMachine, SearchState

def test_anti_bot_challenge_false_positive():
    # Bug A: False anti-bot detection caused by matching "challenge"
    evaluator = GoalEvaluator()
    detector = InterstitialDetector()
    
    url = "https://duckduckgo.com/?q=python+internships"
    text = "Join our Python Coding Challenge today!"
    
    is_anti_bot_eval = evaluator._looks_like_anti_bot(url=url, text=text)
    detection = detector.detect(url=url, title="Python Jobs", text=text)
    
    # Assert CORRECT behavior: "challenge" in page content should not trigger anti-bot block.
    assert is_anti_bot_eval is False, "GoalEvaluator incorrectly flagged 'challenge' as anti-bot"
    assert detection["detection_type"] == "none", "InterstitialDetector incorrectly flagged 'challenge' as anti-bot"

def test_inverted_stagnation_logic():
    # Bug B: Inverted stagnation logic in _fill_without_progress_count
    # Set up mock history with 3 successful fill actions that had no DOM changes
    history = [
        {
            "step": 1,
            "action": {"action": "fill", "selector": "#firstName", "value": "Demo"},
            "success": True,
            "validation": {"reason": "fill_validated", "details": {"actual": "Demo"}},
            "dom_delta": {"navigation_changed": False, "new": 0, "removed": 0, "text_changed": 0, "form_changed": 0}
        },
        {
            "step": 2,
            "action": {"action": "fill", "selector": "#lastName", "value": "User"},
            "success": True,
            "validation": {"reason": "fill_validated", "details": {"actual": "User"}},
            "dom_delta": {"navigation_changed": False, "new": 0, "removed": 0, "text_changed": 0, "form_changed": 0}
        },
        {
            "step": 3,
            "action": {"action": "fill", "selector": "#userEmail", "value": "demo@example.com"},
            "success": True,
            "validation": {"reason": "fill_validated", "details": {"actual": "demo@example.com"}},
            "dom_delta": {"navigation_changed": False, "new": 0, "removed": 0, "text_changed": 0, "form_changed": 0}
        }
    ]
    
    # Instantiate AgentLoop with mocks
    loop = AgentLoop(
        page=MagicMock(),
        dom_extractor=MagicMock(),
        decision_engine=MagicMock(),
        action_executor=MagicMock(),
        action_validator=MagicMock(),
        session_memory=MagicMock(),
        dom_delta=MagicMock(),
        recovery_engine=MagicMock()
    )
    
    count = loop._fill_without_progress_count(history)
    # Assert CORRECT behavior: 3 successful sequential fills are NOT stagnation.
    assert count == 0, f"Stagnation count should be 0, got {count}"

def test_termination_status_blocked_by_search_machine():
    # Bug D: _termination_status blocking extract_text for non-search workflows
    # Instantiate AgentLoop with mocks
    loop = AgentLoop(
        page=MagicMock(),
        dom_extractor=MagicMock(),
        decision_engine=MagicMock(),
        action_executor=MagicMock(),
        action_validator=MagicMock(),
        session_memory=MagicMock(),
        dom_delta=MagicMock(),
        recovery_engine=MagicMock()
    )
    
    # search_state_machine is initialized to START state by default
    assert loop.search_state_machine is not None
    assert loop.search_state_machine._state == SearchState.START
    
    action = {"action": "extract_text"}
    execution_result = {"success": True, "data": {"text": "some extracted text"}}
    validation_result = {"success": True, "reason": "extract_text_validated"}
    
    termination = loop._termination_status(
        action=action,
        success=True,
        consecutive_failures=0,
        execution_result=execution_result,
        validation_result=validation_result,
        anti_bot_hits=0
    )
    
    # Assert CORRECT behavior: Should terminate with completed because it's not a search workflow.
    assert termination == ("completed", ""), f"Termination status should be ('completed', ''), got {termination}"
