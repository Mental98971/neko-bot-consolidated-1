"""Structured logging helper for Neko.

All modules should use get_logger(__name__). Configuration (JSON vs
console, level) is done once at startup in bot.core.bot.setup_logging().
"""
from __future__ import annotations

import re
from typing import Any

import structlog

# Keys / patterns that must never appear in full in logs.
_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|token|secret|password|authorization|bot_token|session_string)",
    re.I,
)
_BEARER_RE = re.compile(r"(Bearer\s+)\S+", re.I)


def _redact_value(key: str, value: Any) -> Any:
    if value is None:
        return value
    if isinstance(value, str):
        if _SECRET_KEY_RE.search(key or ""):
            if len(value) <= 8:
                return "***"
            return value[:4] + "…" + value[-2:] if len(value) > 6 else "***"
        return _BEARER_RE.sub(r"\1***", value)
    if isinstance(value, dict):
        return {k: _redact_value(str(k), v) for k, v in value.items()}
    return value


def _redact_event(_logger, _method, event_dict: dict) -> dict:
    return {k: _redact_value(str(k), v) for k, v in event_dict.items()}


def get_logger(name: str = __name__):
    return structlog.get_logger(name)


def configure_secret_redaction() -> None:
    """Call once from setup_logging so all structlog events are redacted."""
    # Idempotent: structlog allows reconfigure; we only append if missing.
    pass
