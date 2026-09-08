"""verify_llm_path.py -- smoke-test the OllamaClient LLM path.

Usage (from repo root):
    python scripts/verify_llm_path.py

Exits 0 on success, 1 on any failure.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make webot importable when run directly from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from webot.llm.ollama_client import OllamaClient
from webot.utils.logger import get_logger

logger = get_logger(__name__)

_PROBE_PROMPT = 'Return JSON: {"action":"goto","url":"https://example.com"}'
_PROBE_MODEL = "llama3"


def main() -> int:
    client = OllamaClient()

    # -- Step 1: availability check ----------------------------------------
    available = client.is_available(force_refresh=True)
    logger.info("ollama_available", extra={"available": available})
    print(f"[1] is_available(force_refresh=True) -> {available}")

    if not available:
        print("    Ollama is not reachable -- skipping generate() call.")
        print("    RESULT: FAIL (LLM path not exercised)")
        return 1

    # -- Step 2: generate() ------------------------------------------------
    try:
        response = client.generate(_PROBE_PROMPT)
        logger.info("generate_ok", extra={"response_len": len(response)})
        print(f"[2] generate() raw response:\n    {response!r}")
    except Exception as exc:
        logger.error("generate_failed", extra={"error": str(exc)})
        print(f"[2] generate() FAILED: {exc}")
        return 1

    # -- Step 3: test_model_available() ------------------------------------
    found, detail = client.test_model_available(_PROBE_MODEL)
    logger.info("model_probe", extra={"model": _PROBE_MODEL, "found": found, "detail": detail})
    print(f"[3] test_model_available({_PROBE_MODEL!r}) -> found={found}, detail={detail!r}")

    print("\nRESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
