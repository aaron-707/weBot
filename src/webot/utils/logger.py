from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


_DEFAULT_LOGGER_NAME = "webot"
_DEFAULT_LOG_LEVEL = "INFO"
_DEFAULT_LOG_FILE = "logs/webot.log"


class JsonFormatter(logging.Formatter):
    """JSON formatter for structured log output."""

    def format(self, record: logging.LogRecord) -> str:
        log_payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        if record.exc_info:
            log_payload["exception"] = self.formatException(record.exc_info)

        if record.stack_info:
            log_payload["stack"] = self.formatStack(record.stack_info)

        standard_attrs = {
            "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
            "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
            "created", "msecs", "relativeCreated", "thread", "threadName",
            "processName", "process", "message", "asctime", "taskName",
        }
        for key, val in record.__dict__.items():
            if key not in standard_attrs and not key.startswith("_"):
                try:
                    json.dumps(val)
                    log_payload[key] = val
                except (TypeError, OverflowError):
                    log_payload[key] = str(val)

        return json.dumps(log_payload, ensure_ascii=True)


def setup_logging(
    *,
    logger_name: str = _DEFAULT_LOGGER_NAME,
    level: str = _DEFAULT_LOG_LEVEL,
    log_file: str | Path = _DEFAULT_LOG_FILE,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 3,
) -> logging.Logger:
    """Configure and return a reusable structured logger.

    Safe to call multiple times; handlers are only added once per logger name.
    """
    logger = logging.getLogger(logger_name)
    logger.setLevel(_normalize_level(level))
    logger.propagate = False

    if logger.handlers:
        return logger

    json_formatter = JsonFormatter()

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logger.level)
    console_handler.setFormatter(json_formatter)

    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(
        filename=log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(logger.level)
    file_handler.setFormatter(json_formatter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child logger that shares the root project logging config."""
    root_logger = setup_logging()
    if not name:
        return root_logger
    return root_logger.getChild(name)


def _normalize_level(level: str) -> int:
    level_name = str(level).upper()
    level_value = logging.getLevelNamesMapping().get(level_name)
    if isinstance(level_value, int):
        return level_value
    return logging.INFO
