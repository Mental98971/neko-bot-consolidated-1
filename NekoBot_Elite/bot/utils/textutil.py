"""Shared text safety utilities used across plugins and personality."""
from __future__ import annotations

import re

# Telegram message hard limit
TELEGRAM_MAX_MESSAGE = 4096
TELEGRAM_SAFE_MESSAGE = 4090

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_MULTI_NL = re.compile(r"\n{3,}")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")


def sanitize_plain(text: str, max_len: int = TELEGRAM_SAFE_MESSAGE) -> str:
    """Strip control chars, collapse excess whitespace, clamp length."""
    if not text:
        return ""
    text = _CONTROL.sub("", text)
    text = _MULTI_NL.sub("\n\n", text)
    text = text.strip()
    if len(text) <= max_len:
        return text
    # Prefer sentence break
    cut = text.rfind(".", 0, max_len)
    if cut > max_len // 2:
        return text[: cut + 1]
    space = text.rfind(" ", 0, max_len - 1)
    if space > max_len // 2:
        return text[:space].rstrip() + "…"
    return text[: max_len - 1] + "…"


def safe_html_message(text: str, max_len: int = TELEGRAM_SAFE_MESSAGE) -> str:
    """HTML-escape and sanitize together — see bot.utils.helpers.escape_html
    for the canonical HTML-escaping used across this codebase (this module
    doesn't redefine it, to avoid two slightly different implementations
    living side by side)."""
    from bot.utils.helpers import escape_html
    return escape_html(sanitize_plain(text, max_len=max_len))


def truncate_middle(text: str, max_len: int = 200) -> str:
    if not text or len(text) <= max_len:
        return text or ""
    half = (max_len - 1) // 2
    return text[:half] + "…" + text[-half:]
