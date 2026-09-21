"""
Name/username change history — "sangmata", cross-referenced from
Mikobot during a feature-gap review. Unlike AFK and karma (which
turned out to already exist under different names in fun.py), NovaBot
had no equivalent at all: User.username/first_name were set once, the
first time any plugin happened to create that row, and never refreshed
— so /topcatchers, /owners, and anywhere else a display name is shown
could be using a name from months ago with no way to see it had
changed, let alone to what.

Design notes:
  - Runs on group messages only, same scope as other ambient listeners
    (collector.py's spawn_listener, group_mgmt.py's welcome_listener).
  - Throttled per user via an in-memory cache, not a DB read on every
    single message — a busy group would otherwise mean a query just to
    confirm nothing changed. Only an actual detected difference costs
    a write; the common case (nothing changed, and we checked
    recently) costs one dict lookup.
  - This is also, incidentally, the fix for the "stale display name"
    problem above: keeping User.username/first_name/last_name current
    is a side effect of tracking their history.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Dict, Optional, Tuple

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes, MessageHandler, filters

from bot.core.database import User, async_session
from bot.utils.helpers import escape_html
from bot.utils.logger import get_logger

logger = get_logger(__name__)

NAME_CHECK_INTERVAL_SECONDS = 600  # re-check any one user at most every 10 minutes
MAX_HISTORY_ENTRIES = 20  # per user, oldest dropped first once exceeded

# user_id -> ((username, first_name, last_name), last_checked_monotonic)
_last_checked: Dict[int, Tuple[Tuple[Optional[str], str, Optional[str]], float]] = {}


def _record_change(history: list, field: str, old: Optional[str], new: Optional[str]) -> list:
    history = list(history or [])
    history.append({"field": field, "old": old, "new": new, "at": datetime.utcnow().isoformat()})
    return history[-MAX_HISTORY_ENTRIES:]


async def name_history_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user or chat.type == "private" or user.is_bot:
        return

    current = (user.username, user.first_name or "", user.last_name)
    now = time.monotonic()
    cached = _last_checked.get(user.id)
    if cached is not None:
        cached_values, last_checked = cached
        if now - last_checked < NAME_CHECK_INTERVAL_SECONDS:
            return
        if cached_values == current:
            _last_checked[user.id] = (current, now)
            return

    try:
        async with async_session() as session:
            row = await session.get(User, user.id)
            if row is None:
                # New to us — nothing to compare against yet, just seed it.
                session.add(User(id=user.id, username=user.username, first_name=user.first_name or "", last_name=user.last_name))
                await session.commit()
                _last_checked[user.id] = (current, now)
                return

            history = row.name_history or []
            changed = False
            if (row.username or None) != (user.username or None):
                history = _record_change(history, "username", row.username, user.username)
                row.username = user.username
                changed = True
            if (row.first_name or "") != (user.first_name or ""):
                history = _record_change(history, "first_name", row.first_name, user.first_name)
                row.first_name = user.first_name or ""
                changed = True
            if (row.last_name or None) != (user.last_name or None):
                history = _record_change(history, "last_name", row.last_name, user.last_name)
                row.last_name = user.last_name
                changed = True
            if changed:
                row.name_history = history
                await session.commit()
    except Exception:
        logger.exception("name_history_check_failed", user_id=user.id)
        return

    _last_checked[user.id] = (current, now)


async def names_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/names (reply to a user, or nothing for yourself) — show recorded
    name/username changes."""
    target = update.effective_user
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target = update.message.reply_to_message.from_user

    async with async_session() as session:
        row = await session.get(User, target.id)

    if not row or not row.name_history:
        await update.message.reply_text(f"No name changes recorded for {escape_html(target.first_name)}.", parse_mode="HTML")
        return

    lines = [f"📇 <b>Name history — {escape_html(target.first_name)}</b>\n"]
    for entry in row.name_history[-15:]:
        old = escape_html(entry["old"]) if entry.get("old") else "<i>(none)</i>"
        new = escape_html(entry["new"]) if entry.get("new") else "<i>(none)</i>"
        date = entry.get("at", "")[:10]
        lines.append(f"• {date} — {entry.get('field')}: {old} → {new}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


def register(app) -> None:
    # Passive observer — never blocks anything, so its exact position
    # relative to other listener groups doesn't matter functionally.
    # See group_mgmt.py's register() for the full documented list of
    # handler groups in use; this sits at 9, between collector.py's
    # upload-caption listener (8) and personality.py's banter (10).
    app.add_handler(MessageHandler(filters.ALL & filters.ChatType.GROUPS, name_history_middleware), group=9)
    app.add_handler(CommandHandler(["names", "sangmata"], names_cmd))
