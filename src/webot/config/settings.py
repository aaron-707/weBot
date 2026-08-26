from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE_PATH = PROJECT_ROOT / ".env"


class BrowserSettings(BaseModel):
    headless: bool = Field(default=True)
    timeout_ms: int = Field(default=30_000, ge=1_000)
    navigation_timeout_ms: int = Field(default=30_000, ge=1_000)
    user_agent: str | None = Field(default=None)


class OllamaSettings(BaseModel):
    host: str = Field(default="http://localhost:11434")
    model: str = Field(default="llama3")
    timeout_seconds: int = Field(default=60, ge=1)


class LoggingSettings(BaseModel):
    level: str = Field(default="INFO")
    logger_name: str = Field(default="webot")
    log_file: str = Field(default="logs/webot.log")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE_PATH),
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)


def _apply_flat_env_overrides(settings_obj: Settings) -> Settings:
    """Support flat env keys like OLLAMA_MODEL in addition to nested keys."""
    env_file_values = dotenv_values(ENV_FILE_PATH) if ENV_FILE_PATH.exists() else {}

    env_model = os.getenv("OLLAMA_MODEL") or str(env_file_values.get("OLLAMA_MODEL") or "")
    env_base_url = (
        os.getenv("OLLAMA_BASE_URL")
        or os.getenv("OLLAMA_HOST")
        or str(env_file_values.get("OLLAMA_BASE_URL") or env_file_values.get("OLLAMA_HOST") or "")
    )
    env_timeout = os.getenv("OLLAMA_TIMEOUT_SECONDS") or str(env_file_values.get("OLLAMA_TIMEOUT_SECONDS") or "")

    ollama = settings_obj.ollama.model_copy()
    if env_model:
        ollama.model = env_model
    if env_base_url:
        ollama.host = env_base_url
    if env_timeout and env_timeout.isdigit():
        ollama.timeout_seconds = int(env_timeout)

    settings_obj.ollama = ollama
    return settings_obj


settings = _apply_flat_env_overrides(Settings())


def resolve_ollama_config_sources(settings_obj: Settings) -> dict[str, str]:
    """Resolve active Ollama values and indicate whether source is env or default."""
    env_file_values = dotenv_values(ENV_FILE_PATH) if ENV_FILE_PATH.exists() else {}

    env_model = (
        os.getenv("OLLAMA_MODEL")
        or os.getenv("OLLAMA__MODEL")
        or str(env_file_values.get("OLLAMA_MODEL") or env_file_values.get("OLLAMA__MODEL") or "")
    )
    env_base_url = (
        os.getenv("OLLAMA_BASE_URL")
        or os.getenv("OLLAMA_HOST")
        or os.getenv("OLLAMA__HOST")
        or str(
            env_file_values.get("OLLAMA_BASE_URL")
            or env_file_values.get("OLLAMA_HOST")
            or env_file_values.get("OLLAMA__HOST")
            or ""
        )
    )

    resolved_model = env_model or settings_obj.ollama.model
    resolved_base_url = env_base_url or settings_obj.ollama.host

    return {
        "model": resolved_model,
        "model_source": "env" if env_model else "default",
        "base_url": resolved_base_url,
        "base_url_source": "env" if env_base_url else "default",
        "env_file_path": str(ENV_FILE_PATH.resolve()),
    }
