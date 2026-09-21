"""Central error handling for Neko.

Provides typed exceptions, a safe handler decorator, and consistent
user-facing error replies that never leak internal names or stack traces.
"""
from __future__ import annotations

import functools
import traceback
from typing import Any, Callable, TypeVar

from telegram import Update
from telegram.ext import ContextTypes

from bot.identity import bot_name
from bot.utils.logger import get_logger

logger = get_logger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


class NekoError(Exception):
    """Base application error. Safe to surface a generic message."""

    user_message: str = "Something went sideways. Try again in a moment."

    def __init__(self, message: str | None = None, *, user_message: str | None = None):
        super().__init__(message or self.user_message)
        if user_message:
            self.user_message = user_message


class PermissionError(NekoError):
    user_message = "You don't have permission to do that."


class ValidationError(NekoError):
    user_message = "That input didn't look right. Check the format and try again."


class RateLimitError(NekoError):
    user_message = "Slow down a bit. You're hitting the rate limit."


class ServiceUnavailableError(NekoError):
    user_message = "A backend service is temporarily unavailable. Give it a minute."


class ConfigError(NekoError):
    user_message = "Bot configuration problem. The owner has been notified."


def _format_user_error(exc: BaseException) -> str:
    name = bot_name()
    if isinstance(exc, NekoError):
        return f"{exc.user_message}"
    # Never leak internals
    return f"Something went sideways inside {name}. The error is logged; try again shortly."


async def reply_safe(update: Update, text: str, **kwargs: Any) -> None:
    """Best-effort reply that never raises to the caller."""
    try:
        if update.effective_message:
            await update.effective_message.reply_text(text, **kwargs)
    except Exception as e:
        logger.warning("Failed to send error reply: %s", e)


def safe_handler(func: F) -> F:
    """Decorator for telegram handlers. Catches all exceptions, logs with
    context, and sends a branded non-leaking reply to the user.
    """

    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args: Any, **kwargs: Any):
        chat_id = None
        user_id = None
        try:
            chat_id = update.effective_chat.id if update.effective_chat else None
            user_id = update.effective_user.id if update.effective_user else None
            return await func(update, context, *args, **kwargs)
        except NekoError as e:
            logger.warning(
                "Handled NekoError in %s | chat=%s user=%s | %s",
                func.__name__, chat_id, user_id, e,
            )
            await reply_safe(update, _format_user_error(e))
        except Exception as e:
            logger.error(
                "Unhandled error in %s | chat=%s user=%s | %s\n%s",
                func.__name__, chat_id, user_id, e, traceback.format_exc(),
            )
            await reply_safe(update, _format_user_error(e))
        return None

    return wrapper  # type: ignore[return-value]
