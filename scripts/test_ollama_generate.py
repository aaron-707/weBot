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

    print("Resolved settings model:", settings.ollama.model)
    print("Resolved settings base_url:", settings.ollama.host)
    print("Resolved config sources:")
    print(json.dumps(info, indent=2, ensure_ascii=True))

    client = OllamaClient(
        base_url=info["base_url"],
        model=info["model"],
        timeout_seconds=float(settings.ollama.timeout_seconds),
        max_retries=2,
        retry_delay_seconds=0.5,
        model_source=info["model_source"],
        base_url_source=info["base_url_source"],
    )

    print("Active client model:", client.model)
    print("Active client base_url:", client.base_url)

    response = client.generate("Reply with exactly: OK")
    print("Model response:")
    print(response)


if __name__ == "__main__":
    main()
