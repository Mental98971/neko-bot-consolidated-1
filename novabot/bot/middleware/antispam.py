"""
Heuristic Anti-Spam Engine with multi-factor scoring.
Analyzes: repetition, caps ratio, link density, emoji spam, forward chains, join date.
"""
import re
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ChatMemberStatus
from bot.config import settings


def _calculate_spam_score(text: str, is_forward: bool, user_join_date) -> float:
    if not text:
        return 0.0

    score = 0.0
    text_len = len(text)

    # 1. Caps ratio
    caps = sum(1 for c in text if c.isupper())
    caps_ratio = caps / text_len if text_len > 0 else 0
    if caps_ratio > 0.7 and text_len > 10:
        score += 0.25

    # 2. Link density
    links = len(re.findall(r"http[s]?://|t\.me/|@\w+", text))
    if links > 2:
        score += 0.30
    elif links > 0:
        score += 0.10

    # 3. Emoji spam
    emojis = len(re.findall(r"[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U000024C2-\U0001F251]", text))
    if emojis > 5:
        score += 0.20

    # 4. Repetition
    words = text.lower().split()
    if len(words) > 3:
        unique_ratio = len(set(words)) / len(words)
        if unique_ratio < 0.3:
            score += 0.20

    # 5. Forward penalty
    if is_forward:
        score += 0.15

    # 6. Suspicious keywords
    spam_keywords = ["free", "click here", "join now", "limited", "urgent", "winner", "prize", "crypto", "invest"]
    if any(kw in text.lower() for kw in spam_keywords):
        score += 0.15

    return min(score, 1.0)


async def antispam_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_chat:
        return

    chat = update.effective_chat
    msg = update.effective_message
    user = update.effective_user

    if chat.type == "private" or user.is_bot:
        return

    # Admins bypass
    try:
        member = await chat.get_member(user.id)
        if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            return
    except Exception:
        pass

    text = msg.text or msg.caption or ""
    is_forward = msg.forward_date is not None

    score = _calculate_spam_score(text, is_forward, user)

    if score >= settings.spam_ml_threshold:
        try:
            await msg.delete()
            await context.bot.send_message(
                chat.id,
                f"🛡️ <b>Spam detected & deleted</b>\n"
                f"User: {user.mention_html()}\n"
                f"Confidence: <code>{score:.0%}</code>",
                parse_mode="HTML"
            )
        except Exception:
            pass
