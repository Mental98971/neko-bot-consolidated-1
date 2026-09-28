"""
Utilities plugin — fills gaps advertised by /menu.

Implements (no API keys required unless noted):
  /id /info          — user & chat identity
  /tr                — translate text (LibreTranslate public + fallback)
  /weather           — current weather via wttr.in
  /imdb              — title search (OMDb if OMDB_API_KEY set, else Wikipedia)
  /calc              — safe arithmetic calculator
  /tagall            — mention active members (admin)
  /dice /fun         — dice roll + fun hub
  /warns /resetwarns — warn history
  /stats             — alias hub for bot/user stats
"""
from __future__ import annotations

import ast
import logging
import operator
import re
from typing import Any, Optional

from urllib.parse import quote

import aiohttp
from sqlalchemy import delete, desc, func, select
from telegram import ChatPermissions, Update
from telegram.ext import CommandHandler, ContextTypes

from bot.config import settings
from bot.core.database import ChatMember, User, Warn, async_session
from bot.core.decorators import admin_only, group_only
from bot.core.errors import safe_handler
from bot.utils.helpers import escape_html, mention_html

logger = logging.getLogger("bot.plugins.utilities")

# Safe calculator operators only
_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _eval_ast(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval_ast(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.Num):  # py<3.8
        return float(node.n)
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left = _eval_ast(node.left)
        right = _eval_ast(node.right)
        if isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)) and right == 0:
            raise ZeroDivisionError("division by zero")
        if isinstance(node.op, ast.Pow) and (abs(left) > 1e6 or abs(right) > 100):
            raise ValueError("exponent too large")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval_ast(node.operand))
    raise ValueError("unsupported expression")


def _safe_calc(expr: str) -> float:
    expr = expr.strip().replace("^", "**")
    if len(expr) > 120 or not re.fullmatch(r"[0-9+\-*/().%\s^]+", expr.replace("**", "^")):
        # allow ** after replace check using original with limited charset
        if not re.fullmatch(r"[0-9+\-*/().%\s*]+", expr):
            raise ValueError("invalid characters")
    tree = ast.parse(expr, mode="eval")
    return _eval_ast(tree)


# ─── Identity ───────────────────────────────────────────────────────────────

@safe_handler
async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show IDs for user / replied user / chat."""
    user = update.effective_user
    chat = update.effective_chat
    reply = update.message.reply_to_message if update.message else None

    lines = [
        f"👤 <b>You:</b> <code>{user.id}</code>",
        f"💬 <b>Chat:</b> <code>{chat.id}</code> ({chat.type})",
    ]
    if reply and reply.from_user:
        lines.append(
            f"↩️ <b>Replied:</b> <code>{reply.from_user.id}</code> "
            f"({escape_html(reply.from_user.first_name or '')})"
        )
    if reply and reply.forward_from:
        lines.append(f"➡️ <b>Forwarded from:</b> <code>{reply.forward_from.id}</code>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@safe_handler
async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Profile card for yourself or a replied/mentioned user."""
    target = update.effective_user
    if update.message and update.message.reply_to_message and update.message.reply_to_message.from_user:
        target = update.message.reply_to_message.from_user
    elif context.args:
        # try numeric id
        try:
            uid = int(context.args[0])
            async with async_session() as session:
                u = await session.get(User, uid)
                if u:
                    name = u.first_name or u.username or str(uid)
                    await update.message.reply_text(
                        f"ℹ️ <b>{escape_html(name)}</b>\n"
                        f"ID: <code>{uid}</code>\n"
                        f"Username: @{escape_html(u.username or '—')}\n"
                        f"Karma: {u.karma or 0}",
                        parse_mode="HTML",
                    )
                    return
            target_id = uid
            await update.message.reply_text(f"ℹ️ User <code>{target_id}</code> (not in DB yet).", parse_mode="HTML")
            return
        except ValueError:
            pass

    async with async_session() as session:
        u = await session.get(User, target.id)
        karma = u.karma if u else 0
        # per-chat xp if in group
        xp = level = 0
        if update.effective_chat and update.effective_chat.type != "private":
            cm = (
                await session.execute(
                    select(ChatMember).where(
                        ChatMember.chat_id == update.effective_chat.id,
                        ChatMember.user_id == target.id,
                    )
                )
            ).scalar_one_or_none()
            if cm:
                xp, level = cm.xp or 0, cm.level or 0

    uname = f"@{target.username}" if target.username else "—"
    text = (
        f"ℹ️ <b>{escape_html(target.first_name or 'User')}</b>\n"
        f"ID: <code>{target.id}</code>\n"
        f"Username: {escape_html(uname)}\n"
        f"Karma: {karma}\n"
    )
    if update.effective_chat and update.effective_chat.type != "private":
        text += f"Level: {level} · XP: {xp:,}\n"
    await update.message.reply_text(text, parse_mode="HTML")


# ─── Translate ──────────────────────────────────────────────────────────────

@safe_handler
async def tr_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /tr <lang> <text>     — translate to lang (e.g. /tr es Hello)
    /tr <text>            — translate to English
    Reply + /tr <lang>    — translate replied message
    """
    text = None
    dest = "en"
    args = list(context.args or [])

    if update.message.reply_to_message and update.message.reply_to_message.text:
        text = update.message.reply_to_message.text
        if args:
            dest = args[0].lower()[:8]
    elif len(args) >= 2 and len(args[0]) <= 5 and args[0].isalpha():
        dest = args[0].lower()
        text = " ".join(args[1:])
    elif args:
        text = " ".join(args)
    else:
        await update.message.reply_text(
            "Usage:\n"
            "/tr &lt;lang&gt; &lt;text&gt;\n"
            "/tr &lt;text&gt;  (→ English)\n"
            "Or reply to a message with /tr &lt;lang&gt;",
            parse_mode="HTML",
        )
        return

    if not text or len(text) > 2500:
        await update.message.reply_text("Text missing or too long (max 2500 chars).")
        return

    translated = await _translate(text, dest)
    if not translated:
        await update.message.reply_text("❌ Translation failed. Try again later.")
        return
    await update.message.reply_text(
        f"🌐 <b>{escape_html(dest)}</b>\n{escape_html(translated)}",
        parse_mode="HTML",
    )


async def _translate(text: str, dest: str) -> Optional[str]:
    # 1) LibreTranslate public instances
    endpoints = [
        "https://libretranslate.com/translate",
        "https://translate.argosopentech.com/translate",
    ]
    payload = {"q": text, "source": "auto", "target": dest, "format": "text"}
    async with aiohttp.ClientSession() as session:
        for url in endpoints:
            try:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if isinstance(data, dict) and data.get("translatedText"):
                            return data["translatedText"]
            except Exception:
                logger.debug("translate endpoint failed: %s", url, exc_info=True)
        # 2) MyMemory free fallback
        try:
            mm = (
                "https://api.mymemory.translated.net/get"
                f"?q={quote(text[:500])}&langpair=|{dest}"
            )
            async with session.get(mm, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    t = (data.get("responseData") or {}).get("translatedText")
                    if t:
                        return t
        except Exception:
            logger.debug("mymemory failed", exc_info=True)
    return None


# ─── Weather ────────────────────────────────────────────────────────────────

@safe_handler
async def weather_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /weather <city> — current conditions via wttr.in (no API key). """
    if not context.args:
        await update.message.reply_text("Usage: /weather &lt;city&gt;", parse_mode="HTML")
        return
    city = " ".join(context.args).strip()
    url = f"https://wttr.in/{quote(city)}?format=j1"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=12),
                headers={"User-Agent": "NekoBot/1.0"},
            ) as resp:
                if resp.status != 200:
                    await update.message.reply_text("❌ Could not fetch weather.")
                    return
                data = await resp.json()
        cur = data["current_condition"][0]
        area = data.get("nearest_area", [{}])[0]
        place = area.get("areaName", [{}])[0].get("value", city)
        country = area.get("country", [{}])[0].get("value", "")
        desc = cur.get("weatherDesc", [{}])[0].get("value", "")
        temp_c = cur.get("temp_C", "?")
        feels = cur.get("FeelsLikeC", "?")
        humidity = cur.get("humidity", "?")
        wind = cur.get("windspeedKmph", "?")
        await update.message.reply_text(
            f"🌤 <b>{escape_html(place)}</b>, {escape_html(country)}\n"
            f"{escape_html(desc)}\n"
            f"🌡 {temp_c}°C (feels {feels}°C)\n"
            f"💧 Humidity {humidity}% · 💨 Wind {wind} km/h",
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("weather_failed")
        await update.message.reply_text("❌ Weather lookup failed.")


# ─── IMDb / title search ────────────────────────────────────────────────────

@safe_handler
async def imdb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /imdb <title> — movie/series lookup. Uses OMDb if OMDB_API_KEY set. """
    if not context.args:
        await update.message.reply_text("Usage: /imdb &lt;title&gt;", parse_mode="HTML")
        return
    title = " ".join(context.args).strip()
    key = getattr(settings, "omdb_api_key", None) or getattr(settings, "OMDB_API_KEY", None)
    # also check env-style on settings dict if present
    if not key and hasattr(settings, "model_dump"):
        key = settings.model_dump().get("omdb_api_key")

    async with aiohttp.ClientSession() as session:
        if key:
            url = f"https://www.omdbapi.com/?t={quote(title)}&apikey={key}"
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                    data = await resp.json()
                if data.get("Response") == "True":
                    await update.message.reply_text(
                        f"🎬 <b>{escape_html(data.get('Title',''))}</b> ({data.get('Year','')})\n"
                        f"{escape_html(data.get('Rated','') or '')} · {escape_html(data.get('Runtime','') or '')}\n"
                        f"⭐ IMDb {data.get('imdbRating','—')} · {escape_html(data.get('Genre','') or '')}\n\n"
                        f"{escape_html(data.get('Plot','') or '')}\n\n"
                        f"<i>{escape_html(data.get('Director','') or '')}</i>",
                        parse_mode="HTML",
                    )
                    return
            except Exception:
                logger.exception("omdb_failed")

        # Wikipedia opensearch fallback
        try:
            api = (
                "https://en.wikipedia.org/w/api.php?action=opensearch&limit=1&namespace=0"
                f"&format=json&search={quote(title)}"
            )
            async with session.get(api, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
            if len(data) >= 4 and data[1]:
                await update.message.reply_text(
                    f"🎬 <b>{escape_html(data[1][0])}</b>\n"
                    f"{escape_html(data[2][0] if data[2] else '')}\n"
                    f"{data[3][0] if data[3] else ''}",
                    parse_mode="HTML",
                    disable_web_page_preview=False,
                )
                return
        except Exception:
            logger.exception("wiki_imdb_fallback_failed")

    await update.message.reply_text("❌ No results found.")


# ─── Calculator ─────────────────────────────────────────────────────────────

@safe_handler
async def calc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /calc &lt;expression&gt;\nExample: /calc (3+5)*2", parse_mode="HTML")
        return
    expr = " ".join(context.args)
    try:
        result = _safe_calc(expr)
        if result == int(result):
            result = int(result)
        await update.message.reply_text(f"🧮 <code>{escape_html(expr)}</code> = <b>{result}</b>", parse_mode="HTML")
    except ZeroDivisionError:
        await update.message.reply_text("❌ Division by zero.")
    except Exception as e:
        await update.message.reply_text(f"❌ Invalid expression: {escape_html(str(e))}", parse_mode="HTML")


# ─── Tag all ────────────────────────────────────────────────────────────────

@admin_only
@group_only
@safe_handler
async def tagall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mention active members of this chat (from ChatMember activity)."""
    chat = update.effective_chat
    note = " ".join(context.args) if context.args else "👋 Attention everyone!"

    async with async_session() as session:
        rows = (
            await session.execute(
                select(ChatMember.user_id, User.first_name, User.username)
                .join(User, User.id == ChatMember.user_id)
                .where(ChatMember.chat_id == chat.id)
                .order_by(desc(ChatMember.xp))
                .limit(40)
            )
        ).all()

    if not rows:
        await update.message.reply_text(
            "No active members tracked yet — chat a bit first so the bot learns who's here."
        )
        return

    mentions = []
    for uid, name, username in rows:
        label = name or username or str(uid)
        mentions.append(mention_html(uid, label))

    # Telegram message limit — chunk if needed
    header = f"📢 {escape_html(note)}\n\n"
    chunk = header
    messages = []
    for m in mentions:
        if len(chunk) + len(m) + 2 > 3500:
            messages.append(chunk)
            chunk = header
        chunk += m + " "
    if chunk.strip():
        messages.append(chunk)

    for msg in messages[:3]:  # hard cap 3 messages
        await update.message.reply_text(msg, parse_mode="HTML")


# ─── Dice / fun hub ─────────────────────────────────────────────────────────

@safe_handler
async def dice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_dice(emoji="🎲")


@safe_handler
async def fun_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎯 <b>Fun commands</b>\n\n"
        "/meme · /quote · /wish · /couples · /dice\n"
        "/8ball · /joke · /karma · /afk\n"
        "/coinflip · /rps · /slots · /tictactoe · /trivia",
        parse_mode="HTML",
    )


# ─── Warns ──────────────────────────────────────────────────────────────────

@group_only
@safe_handler
async def warns_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show warn count for replied user or yourself."""
    chat = update.effective_chat
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target = update.message.reply_to_message.from_user
    else:
        target = update.effective_user

    async with async_session() as session:
        count = (
            await session.execute(
                select(func.count()).where(Warn.chat_id == chat.id, Warn.user_id == target.id)
            )
        ).scalar() or 0
        recent = (
            await session.execute(
                select(Warn)
                .where(Warn.chat_id == chat.id, Warn.user_id == target.id)
                .order_by(desc(Warn.id))
                .limit(5)
            )
        ).scalars().all()

    lines = [
        f"⚠️ Warns for {mention_html(target.id, target.first_name or str(target.id))}: "
        f"<b>{count}</b>\n"
    ]
    for w in recent:
        lines.append(f"• {escape_html(w.reason or 'no reason')}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@admin_only
@group_only
@safe_handler
async def resetwarns_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        await update.message.reply_text("Reply to a user to reset their warns.")
        return
    target = update.message.reply_to_message.from_user
    async with async_session() as session:
        await session.execute(
            delete(Warn).where(
                Warn.chat_id == update.effective_chat.id,
                Warn.user_id == target.id,
            )
        )
        await session.commit()
    await update.message.reply_text(
        f"✅ Warns cleared for {mention_html(target.id, target.first_name or str(target.id))}.",
        parse_mode="HTML",
    )


@safe_handler
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hub pointing at existing stats commands."""
    await update.message.reply_text(
        "📊 <b>Stats</b>\n\n"
        "/mystats · /botstats · /globalstats\n"
        "/level · /leaderboard · /balance · /topcatchers\n"
        "/cstats — collector-wide",
        parse_mode="HTML",
    )


def register(app):
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(CommandHandler("info", info_cmd))
    app.add_handler(CommandHandler(["tr", "translate"], tr_cmd))
    app.add_handler(CommandHandler("weather", weather_cmd))
    app.add_handler(CommandHandler("imdb", imdb_cmd))
    app.add_handler(CommandHandler(["calc", "calculate"], calc_cmd))
    app.add_handler(CommandHandler(["tagall", "all", "mentionall"], tagall_cmd))
    app.add_handler(CommandHandler("dice", dice_cmd))
    app.add_handler(CommandHandler("fun", fun_cmd))
    app.add_handler(CommandHandler("warns", warns_cmd))
    app.add_handler(CommandHandler(["resetwarns", "resetwarn"], resetwarns_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
