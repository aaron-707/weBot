from pathlib import Path
import sys
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from webot.workflows.task_interpreter import TaskInterpreter


def test_task_interpreter_deterministic_match():
    """Known sites and search/goto patterns must match deterministically without LLM."""
    mock_de = MagicMock()
    interpreter = TaskInterpreter(decision_engine=mock_de)

    actions = interpreter.interpret("search python asyncio on duckduckgo")
    assert len(actions) > 0
    assert interpreter.deterministic_interpret_count == 1
    assert interpreter.llm_consulted_count == 0
    mock_de.propose_action_sequence.assert_not_called()


def test_task_interpreter_novel_goal_llm_fallback():
    """Novel goals not matching regex or _NAMED_SITES should fall back to DecisionEngine."""
    mock_de = MagicMock()
    mock_de.propose_action_sequence.return_value = [
        {"action": "goto", "url": "https://doc.rust-lang.org"},
        {"action": "click", "selector": "a#book-index"},
    ]
    interpreter = TaskInterpreter(decision_engine=mock_de)

    actions = interpreter.interpret("Read the introduction chapter in the rust programming book")
    assert len(actions) == 2
    assert actions[0]["action"] == "goto"
    assert actions[0]["url"] == "https://doc.rust-lang.org"
    assert interpreter.deterministic_interpret_count == 0
    assert interpreter.llm_consulted_count == 1
    mock_de.propose_action_sequence.assert_called_once_with(
        user_goal="Read the introduction chapter in the rust programming book", elements=None
    )


def test_task_interpreter_degraded_mode_no_llm():
    """When decision_engine is None, novel goals return empty actions gracefully."""
    interpreter = TaskInterpreter(decision_engine=None)
    actions = interpreter.interpret("Read the introduction chapter in the rust programming book")
    assert actions == []
    assert interpreter.deterministic_interpret_count == 0
    assert interpreter.llm_consulted_count == 0


def test_task_interpreter_extract_start_url_fallback():
    """extract_start_url should try deterministic first, then fall back to infer_start_url."""
    # Deterministic match
    interpreter = TaskInterpreter()
    assert interpreter.extract_start_url("search cats on duckduckgo") == "https://duckduckgo.com"

    # Novel match with DecisionEngine
    mock_de = MagicMock()
    mock_de.infer_start_url.return_value = "https://news.ycombinator.com"
    interpreter_llm = TaskInterpreter(decision_engine=mock_de)
    url = interpreter_llm.extract_start_url("browse front page tech news on hacker news")
    assert url == "https://news.ycombinator.com"
    mock_de.infer_start_url.assert_called_once_with("browse front page tech news on hacker news")
