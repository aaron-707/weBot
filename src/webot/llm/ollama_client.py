from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import requests


@dataclass(slots=True)
class OllamaClient:
    """Minimal client for Ollama REST API interactions."""

    base_url: str = "http://localhost:11434"
    model: str = ""
    timeout_seconds: float = 30.0
    max_retries: int = 2
    retry_delay_seconds: float = 0.5
    model_source: str = "unspecified"
    base_url_source: str = "unspecified"
    availability_cache_ttl_seconds: float = 15.0
    failure_cooldown_seconds: float = 20.0
    fast_fail_timeout_seconds: float = 1.5
    _logger: logging.Logger = field(init=False, repr=False)
    _last_health_check_at: float = field(default=0.0, init=False, repr=False)
    _cached_available: bool | None = field(default=None, init=False, repr=False)
    _cooldown_until: float = field(default=0.0, init=False, repr=False)
    _last_health_error: str = field(default="", init=False, repr=False)

    def __post_init__(self) -> None:
        self._logger = logging.getLogger(__name__)
        self._logger.info(
            "ollama_client_initialized base_url=%s model=%s base_url_source=%s model_source=%s",
            self.base_url,
            self.model or "<unset>",
            self.base_url_source,
            self.model_source,
        )

    def generate(self, prompt: str, *, model: str | None = None) -> str:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Prompt must be a non-empty string")
        resolved_model = (model or self.model).strip()
        if not resolved_model:
            raise ValueError("Ollama model is not configured")
        if not self.is_available():
            raise RuntimeError("ollama_unavailable")

        payload: dict[str, Any] = {
            "model": resolved_model,
            "prompt": prompt,
            "stream": False,
        }
        self._logger.info(
            "ollama_generate_model_trace constructor_model=%s self_model=%s override_model=%s payload_model=%s",
            self.model or "<unset>",
            self.model or "<unset>",
            model if model is not None else "<none>",
            payload["model"],
        )

        data = self._request_json("POST", "/generate", json_payload=payload)
        result = data.get("response") if isinstance(data, dict) else None
        if not isinstance(result, str):
            raise RuntimeError("Invalid Ollama response payload: missing 'response' string")
        return result

    def is_available(self, *, force_refresh: bool = False) -> bool:
        now = time.monotonic()
        if not force_refresh:
            if self._cached_available is False and now < self._cooldown_until:
                return False
            if (
                self._cached_available is not None
                and (now - self._last_health_check_at) <= max(self.availability_cache_ttl_seconds, 0.0)
            ):
                return self._cached_available

        try:
            self._request_json(
                "GET",
                "/tags",
                timeout_override_seconds=max(min(self.fast_fail_timeout_seconds, self.timeout_seconds), 0.2),
                retries_override=0,
            )
            self._cached_available = True
            self._last_health_error = ""
            self._cooldown_until = 0.0
            self._last_health_check_at = now
            return True
        except Exception as exc:  # noqa: BLE001
            self._cached_available = False
            self._last_health_error = str(exc)
            self._cooldown_until = now + max(self.failure_cooldown_seconds, 0.0)
            self._last_health_check_at = now
            self._logger.warning("ollama_unavailable", extra={"error": self._last_health_error})
            return False

    def test_connection(self) -> tuple[bool, str]:
        """Check server reachability using the documented list-models endpoint."""
        if self.is_available(force_refresh=True):
            return True, "ok"
        return False, self._last_health_error or "unavailable"

    def test_model_available(self, model_name: str) -> tuple[bool, str]:
        """Check whether a model exists in local Ollama model list."""
        if not isinstance(model_name, str) or not model_name.strip():
            return False, "model_name must be a non-empty string"

        target = model_name.strip().lower()
        try:
            data = self._request_json("GET", "/tags")
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list):
            return False, "Invalid /api/tags response: missing 'models' list"

        for item in models:
            if not isinstance(item, dict):
                continue
            candidates = [item.get("name"), item.get("model")]
            for candidate in candidates:
                if isinstance(candidate, str) and candidate.lower() == target:
                    return True, "available"

        return False, f"Model not found: {model_name}"

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_payload: dict[str, Any] | None = None,
        timeout_override_seconds: float | None = None,
        retries_override: int | None = None,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        url = self._build_api_url(path)
        retry_budget = self.max_retries if retries_override is None else max(retries_override, 0)
        timeout_seconds = self.timeout_seconds if timeout_override_seconds is None else timeout_override_seconds

        for attempt in range(1, retry_budget + 2):
            try:
                response = requests.request(
                    method=method,
                    url=url,
                    json=json_payload,
                    timeout=timeout_seconds,
                )

                if response.status_code < 200 or response.status_code >= 300:
                    body_preview = response.text[:1000]
                    self._logger.error(
                        "ollama_http_error status=%s url=%s body=%s",
                        response.status_code,
                        url,
                        body_preview,
                    )
                    raise RuntimeError(
                        f"Ollama HTTP {response.status_code} at {url}. Response body: {body_preview}"
                    )

                data = response.json()
                if not isinstance(data, dict):
                    raise RuntimeError(f"Invalid Ollama JSON response type: {type(data).__name__}")
                return data
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                last_error = exc
                if attempt > retry_budget:
                    break
                if self.retry_delay_seconds > 0:
                    time.sleep(self.retry_delay_seconds)

        assert last_error is not None
        raise RuntimeError(f"Ollama request failed after retries: {last_error}") from last_error

    def _build_api_url(self, path: str) -> str:
        normalized_path = path if path.startswith("/") else f"/{path}"

        raw = self.base_url.strip()
        parsed = urlsplit(raw)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(f"Invalid base_url: {self.base_url}")

        base_path = parsed.path.rstrip("/")
        if base_path.endswith("/api"):
            api_path = base_path
        elif base_path:
            api_path = f"{base_path}/api"
        else:
            api_path = "/api"

        return f"{parsed.scheme}://{parsed.netloc}{api_path}{normalized_path}"
