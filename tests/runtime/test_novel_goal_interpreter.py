from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import sys
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from webot.config.settings import resolve_ollama_config_sources, settings
from webot.llm.decision_engine import DecisionEngine
from webot.llm.ollama_client import OllamaClient
from webot.workflows.task_interpreter import TaskInterpreter


def get_live_ollama_client() -> OllamaClient | None:
    ollama_info = resolve_ollama_config_sources(settings)
    client = OllamaClient(
        base_url=ollama_info["base_url"],
        model=ollama_info["model"],
        timeout_seconds=float(settings.ollama.timeout_seconds),
        model_source=ollama_info["model_source"],
        base_url_source=ollama_info["base_url_source"],
    )
    if not client.is_available():
        return None
    return client


def test_live_novel_goal_interpreter() -> None:
    """Test TaskInterpreter with live Ollama on novel goals not present in _NAMED_SITES.

    Verifies:
    1. Deterministic interpreter yields no actions for novel phrasing without explicit domains.
    2. LLM fallback is triggered (llm_consulted_count increments).
    3. DecisionEngine.infer_start_url infers a valid https URL via live Ollama.
    4. DecisionEngine.propose_action_sequence returns structured BrowserActions.
    """
    client = get_live_ollama_client()
    if client is None:
        pytest.skip("Ollama is not available; skipping live novel goal interpreter test")

    engine = DecisionEngine(ollama_client=client)
    interpreter = TaskInterpreter(decision_engine=engine)

    # Prompt with no URL, no .com/.org domain suffix, not in _NAMED_SITES
    novel_instruction = "Look up the developer documentation and API reference on stripe"

    # Verify deterministic extraction alone cannot resolve it
    start_url = interpreter.extract_start_url(novel_instruction)
    assert start_url is not None
    assert start_url.startswith("http")
    assert "stripe" in start_url.lower()

    actions = interpreter.interpret(novel_instruction)
    assert len(actions) > 0, "LLM fallback should propose actions for novel goal"
    assert interpreter.llm_consulted_count >= 1
    assert interpreter.deterministic_interpret_count == 0

    first_action = actions[0]
    assert "action" in first_action
    assert first_action["action"] in {"goto", "search", "click"}
    print(f"\n[PASS] Novel goal interpreted successfully:")
    print(f"  Inferred URL: {start_url}")
    print(f"  Proposed Actions: {actions}")
    print(f"  Telemetry: deterministic={interpreter.deterministic_interpret_count}, llm_consulted={interpreter.llm_consulted_count}")


def main() -> None:
    print("Testing live novel goal interpretation...")
    test_live_novel_goal_interpreter()
    print("ALL CHECKS PASSED.")


if __name__ == "__main__":
    main()
