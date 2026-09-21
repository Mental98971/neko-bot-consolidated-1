"""
Permission decorators, rate limiters, and chat-type filters.
"""
import functools
import time
from typing import Callable
from telegram import Update
from telegram.ext import ContextTypes
from bot.config import settings


# ─── Permission Checks ───
def admin_only(func: Callable) -> Callable:
    """Restricts a command to the acting chat's admins/creator.

    Private chats have no admin concept to check against. The original
    implementation treated that as "nothing to check, let it through" —
    which meant every admin_only command (ban/mute/kick/warn/filters/
    welcome/notes/locks/captcha/log-channel, 20 commands in total) could
    be invoked by *any* user via a DM to the bot, admin check fully
    bypassed. None of them have a legitimate DM use case, so private
    chats are now denied rather than silently let through.
    """
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        if not chat or chat.type == "private":
            return await update.message.reply_text("🚫 This is a group-admin command — try it in a group you administer.")
        user = update.effective_user
        member = await context.bot.get_chat_member(chat.id, user.id)
        if member.status not in ("creator", "administrator"):
            return await update.message.reply_text("🚫 Admin only.")
        return await func(update, context)
    return wrapper


def owner_only(func: Callable) -> Callable:
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != settings.owner_id:
            return await update.message.reply_text("🚫 Owner only.")
        return await func(update, context)
    return wrapper


def group_only(func: Callable) -> Callable:
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_chat or update.effective_chat.type == "private":
            return await update.message.reply_text("📢 This command works in groups only.")
        return await func(update, context)
    return wrapper


# ─── Rate Limiting (in-memory with Redis fallback pattern) ───
_rate_limits: dict = {}


def rate_limit(max_calls: int = 5, window: int = 60):
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            user_id = update.effective_user.id
            now = time.time()
            key = f"{func.__name__}:{user_id}"

            if key not in _rate_limits:
                _rate_limits[key] = []

            # Clean old entries
            _rate_limits[key] = [t for t in _rate_limits[key] if now - t < window]

            if len(_rate_limits[key]) >= max_calls:
                return await update.message.reply_text(
                    f"⏳ Rate limit exceeded. Try again in {window}s."
                )

            _rate_limits[key].append(now)
            return await func(update, context)
        return wrapper
    return decorator


# ─── Cooldown for groups ───
def chat_cooldown(seconds: int = 3):
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            chat_id = update.effective_chat.id if update.effective_chat else update.effective_user.id
            key = f"cooldown:{func.__name__}:{chat_id}"
            now = time.time()

            if key in _rate_limits and now - _rate_limits[key] < seconds:
                return None  # Silently ignore

            _rate_limits[key] = now
            return await func(update, context)
        return wrapper
    return decorator
