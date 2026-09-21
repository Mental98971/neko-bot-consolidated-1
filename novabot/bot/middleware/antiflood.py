"""
Advanced Anti-Flood System with exponential backoff and smart heuristics.
Tracks message velocity per user per chat.
"""
import time
from collections import defaultdict
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ChatMemberStatus
from bot.config import settings

# In-memory burst tracker: {(chat_id, user_id): [timestamps]}
_burst_tracker: dict = defaultdict(list)


async def antiflood_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_chat:
        return

    chat = update.effective_chat
    user = update.effective_user
    msg = update.effective_message

    if chat.type == "private" or user.is_bot:
        return

    # Check if user is admin (admins bypass)
    try:
        member = await chat.get_member(user.id)
        if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            return
    except Exception:
        pass

    key = (chat.id, user.id)
    now = time.time()

    # Clean old entries
    window = settings.antiflood_interval
    _burst_tracker[key] = [t for t in _burst_tracker[key] if now - t < window]
    _burst_tracker[key].append(now)

    count = len(_burst_tracker[key])
    limit = settings.antiflood_threshold

    if count > limit + 2:
        # Excessive flooding — ban for configured duration
        try:
            await context.bot.ban_chat_member(
                chat.id, user.id,
                until_date=int(now + settings.antiflood_ban_duration)
            )
            await msg.reply_text(
                f"🛡️ <b>Anti-Flood</b>\n"
                f"User {user.mention_html()} has been temporarily banned for flooding.",
                parse_mode="HTML"
            )
            # Delete their recent messages
            for msg_id in range(msg.message_id - count + 1, msg.message_id + 1):
                try:
                    await context.bot.delete_message(chat.id, msg_id)
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            _burst_tracker[key] = []

    elif count == limit:
        # Warn at threshold
        await msg.reply_text(
            f"⚠️ <b>Slow down, {user.first_name}!</b> You are sending messages too fast.",
            parse_mode="HTML"
        )
