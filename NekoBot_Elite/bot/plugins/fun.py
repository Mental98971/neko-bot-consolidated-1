"""
Fun & Social Plugin
Couples, karma, AFK, memes, quotes, 8ball, dice, wish, urban dictionary.
"""
import random
from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters
from bot.core.database import async_session, User, ChatMember
from bot.core.errors import safe_handler
from bot.utils.helpers import mention_html, escape_html, random_couple
from sqlalchemy import select, desc, func


def _get_meme_url() -> str:
    subreddits = ["memes", "dankmemes", "me_irl", "ProgrammerHumor"]
    sub = random.choice(subreddits)
    return f"https://meme-api.com/gimme/{sub}"


@safe_handler
async def meme_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.get(_get_meme_url()) as resp:
            if resp.status == 200:
                data = await resp.json()
                await update.message.reply_photo(
                    data["url"],
                    caption=f"😂 <b>{data['title']}</b>\n👍 {data['ups']} | 💬 {data['num_comments']}",
                    parse_mode="HTML"
                )
            else:
                await update.message.reply_text("❌ Couldn't fetch meme.")


@safe_handler
async def couples_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pick two active members from this chat as Couple of the Day.

    Uses ChatMember rows that already accumulate XP / activity so no
    extra tracking command is required. Falls back to a friendly
    message when fewer than 2 members have been seen.
    """
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("📢 Use this in a group!")
        return

    async with async_session() as session:
        result = await session.execute(
            select(ChatMember.user_id, User.first_name, User.username)
            .join(User, User.id == ChatMember.user_id)
            .where(ChatMember.chat_id == chat.id)
            .order_by(desc(ChatMember.xp), desc(ChatMember.personality_msg_count))
            .limit(50)
        )
        rows = result.all()

    if len(rows) < 2:
        await update.message.reply_text(
            "💑 Need at least 2 active members in this chat first.\n"
            "Chat a bit more so the bot can learn who's here, then try again."
        )
        return

    # Deterministic daily pair: same couple for the whole UTC day
    today = datetime.utcnow().strftime("%Y-%m-%d")
    seed = hash((chat.id, today)) % (2**31)
    rng = random.Random(seed)
    pair = rng.sample(rows, 2)
    (uid_a, name_a, user_a), (uid_b, name_b, user_b) = pair
    display_a = name_a or user_a or str(uid_a)
    display_b = name_b or user_b or str(uid_b)

    await update.message.reply_text(
        "💑 <b>Couple of the Day</b>\n\n"
        f"{mention_html(uid_a, display_a)}  ❤️  {mention_html(uid_b, display_b)}\n\n"
        f"<i>Ship locked until midnight UTC · seed {today}</i>",
        parse_mode="HTML",
    )


@safe_handler
async def karma_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = update.message.reply_to_message

    if reply:
        target = reply.from_user
        async with async_session() as session:
            result = await session.execute(select(User).where(User.id == target.id))
            u = result.scalar_one_or_none()
            if not u:
                u = User(id=target.id, username=target.username, first_name=target.first_name)
                session.add(u)
            u.karma += 1
            await session.commit()
        await update.message.reply_text(
            f"☯️ {mention_html(target.id, target.first_name)} gained +1 karma! (Total: {u.karma})",
            parse_mode="HTML"
        )
    else:
        async with async_session() as session:
            result = await session.execute(
                select(User).order_by(desc(User.karma)).limit(10)
            )
            top = result.scalars().all()

        text = "<b>🏆 Karma Leaderboard</b>\n\n"
        for i, u in enumerate(top, 1):
            name = u.first_name or "Unknown"
            text += f"{i}. {mention_html(u.id, name)} — {u.karma}\n"
        await update.message.reply_text(text, parse_mode="HTML")


@safe_handler
async def afk_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    reason = " ".join(context.args) or "AFK"

    async with async_session() as session:
        result = await session.execute(select(User).where(User.id == user.id))
        u = result.scalar_one_or_none()
        if not u:
            u = User(id=user.id, username=user.username, first_name=user.first_name)
            session.add(u)
        u.afk_reason = reason
        u.afk_since = datetime.utcnow()
        await session.commit()

    await update.message.reply_text(
        f"💤 {mention_html(user.id, user.first_name)} is now AFK: <i>{escape_html(reason)}</i>",
        parse_mode="HTML"
    )


@safe_handler
async def afk_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    # Check if anyone mentioned is AFK
    if update.message.entities:
        for entity in update.message.entities:
            if entity.type == "mention":
                username = update.message.text[entity.offset+1:entity.offset+entity.length]
                async with async_session() as session:
                    result = await session.execute(select(User).where(User.username == username))
                    u = result.scalar_one_or_none()
                    if u and u.afk_reason:
                        await update.message.reply_text(
                            f"💤 {username} is AFK: <i>{escape_html(u.afk_reason)}</i>",
                            parse_mode="HTML"
                        )

    # Check reply
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
        async with async_session() as session:
            result = await session.execute(select(User).where(User.id == target.id))
            u = result.scalar_one_or_none()
            if u and u.afk_reason:
                await update.message.reply_text(
                    f"💤 {mention_html(target.id, target.first_name)} is AFK: <i>{escape_html(u.afk_reason)}</i>",
                    parse_mode="HTML"
                )

    # Remove AFK if user sends message
    user = update.effective_user
    async with async_session() as session:
        result = await session.execute(select(User).where(User.id == user.id))
        u = result.scalar_one_or_none()
        if u and u.afk_reason:
            u.afk_reason = None
            u.afk_since = None
            await session.commit()
            await update.message.reply_text(
                f"👋 Welcome back, {mention_html(user.id, user.first_name)}!",
                parse_mode="HTML"
            )


@safe_handler
async def eightball_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answers = [
        "🟢 It is certain.", "🟢 Without a doubt.", "🟡 Ask again later.",
        "🟡 Better not tell you now.", "🔴 Don't count on it.", "🔴 My sources say no.",
        "🟢 Yes definitely.", "🟡 Concentrate and ask again.", "🔴 Very doubtful."
    ]
    await update.message.reply_text(random.choice(answers))


@safe_handler
async def wish_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wish = " ".join(context.args) or "nothing"
    chance = random.randint(1, 100)
    bar = "█" * (chance // 10) + "▒" * (10 - chance // 10)
    await update.message.reply_text(
        f"⭐ <b>Your Wish</b>: <i>{escape_html(wish)}</i>\n"
        f"<b>Likelihood</b>: {bar} {chance}%",
        parse_mode="HTML"
    )


@safe_handler
async def ud_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    term = " ".join(context.args)
    if not term:
        await update.message.reply_text("Usage: /ud <term>")
        return

    import aiohttp
    url = f"https://api.urbandictionary.com/v0/define?term={aiohttp.helpers.quote(term)}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            if data["list"]:
                entry = data["list"][0]
                defi = entry["definition"][:800]
                example = entry.get("example", "")[:400]
                await update.message.reply_text(
                    f"<b>📖 {escape_html(entry['word'])}</b>\n"
                    f"{escape_html(defi)}\n\n"
                    f"<i>Example:</i> {escape_html(example)}\n"
                    f"👍 {entry['thumbs_up']} | 👎 {entry['thumbs_down']}",
                    parse_mode="HTML"
                )
            else:
                await update.message.reply_text("❌ No definition found.")


@safe_handler
async def quote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    quotes = [
        "The only way to do great work is to love what you do. — Steve Jobs",
        "Innovation distinguishes between a leader and a follower. — Steve Jobs",
        "Stay hungry, stay foolish. — Stewart Brand",
        "Code is like humor. When you have to explain it, it's bad. — Cory House",
        "First, solve the problem. Then, write the code. — John Johnson",
    ]
    await update.message.reply_text(f"💬 <i>{random.choice(quotes)}</i>", parse_mode="HTML")


def register(app):
    app.add_handler(CommandHandler("meme", meme_cmd))
    app.add_handler(CommandHandler("couples", couples_cmd))
    app.add_handler(CommandHandler("karma", karma_cmd))
    app.add_handler(CommandHandler("k", karma_cmd))
    app.add_handler(CommandHandler("afk", afk_cmd))
    app.add_handler(CommandHandler("8ball", eightball_cmd))
    app.add_handler(CommandHandler("wish", wish_cmd))
    app.add_handler(CommandHandler("ud", ud_cmd))
    app.add_handler(CommandHandler("quote", quote_cmd))
    # Distinct group so this doesn't collide with group_mgmt.py's lock/filter
    # listeners or the personality plugin's catch-all reply handler — see the
    # comment in group_mgmt.py's register() for the full explanation.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, afk_listener), group=3)
