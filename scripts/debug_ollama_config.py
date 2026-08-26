from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from webot.config.settings import resolve_ollama_config_sources, settings
from webot.llm.ollama_client import OllamaClient


def main() -> None:
    info = resolve_ollama_config_sources(settings)

    client = OllamaClient(
        base_url=info["base_url"],
        model=info["model"],
        timeout_seconds=float(settings.ollama.timeout_seconds),
        model_source=info["model_source"],
        base_url_source=info["base_url_source"],
    )

    connected, connection_message = client.test_connection()
    available, availability_message = client.test_model_available(info["model"])

    output = {
        "resolved_model": info["model"],
        "resolved_base_url": info["base_url"],
        "env_file_path": info["env_file_path"],
        "model_source": info["model_source"],
        "base_url_source": info["base_url_source"],
        "connection_ok": connected,
        "connection_message": connection_message,
        "model_available": available,
        "model_available_message": availability_message,
    }
    print(json.dumps(output, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
