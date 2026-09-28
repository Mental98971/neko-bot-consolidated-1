"""
Start handler, help, and the functional inline menu system.

Each menu button opens a real command guide for features that exist in
this codebase — not empty placeholders.
"""
from __future__ import annotations

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler

from bot.identity import bot_name
from bot.utils.helpers import mention_html
from bot.core.errors import safe_handler


MAIN_MENU = [
    ["🛡️ Admin", "🤖 AI", "🎭 Personality"],
    ["🎌 Anime", "🎵 Music", "🎧 Live Music"],
    ["🎯 Fun", "🔤 Fonts", "🤗 Reactions"],
    ["🔐 Locks", "📝 Notes", "🔍 Filters"],
    ["👋 Greetings", "⚠️ Warnings", "📊 Stats"],
    ["🎴 Collector", "💰 Economy", "🎮 Games"],
]

PAGE_2 = [
    ["🧹 Purge", "📜 Rules", "ℹ️ Info"],
    ["💤 AFK", "☯️ Karma", "💑 Couples"],
    ["🌐 Translate", "📖 Urban", "🎬 IMDb"],
    ["📰 News", "🌤 Weather", "📲 QR"],
    ["🎤 TTS", "⬇️ Download", "🛠 Tools"],
]

PAGE_3 = [
    ["🌙 Nightmode", "🚫 Anti-Spam", "🛡 Anti-Flood"],
    ["✅ Captcha", "📢 Tag All", "📋 Log Channel"],
    ["🌐 Federation", "🔧 Disable", "⚫ Blacklist"],
    ["🎧 Playlists", "📜 Lyrics", "🎛 Control"],
]

PAGES = {1: MAIN_MENU, 2: PAGE_2, 3: PAGE_3}
TOTAL_PAGES = 3

MENU_PANELS: dict[str, tuple[str, str]] = {
    "admin": (
        "🛡️ Admin & moderation",
        "/ban [time] · /tban [time] · /mute [time] · /tmute [time]\n"
        "/unban /unmute /kick /warn /warns /resetwarns\n"
        "/purge /del /slowmode /pin /unpin\n"
        "/zombies /rmzombies /unbanall\n"
        "<i>Time: 30m 2h 1d — reply to target user</i>",
    ),
    "ai": (
        "🤖 AI assistant",
        "<b>/ai</b> &lt;question&gt; — general assistant\n"
        "/imagine /summarize /code /see /transcribe /persona\n"
        "<i>For Neko personality banter use /chat (not /ai).</i>",
    ),
    "personality": (
        "🎭 Neko personality",
        "<b>/chat</b> &lt;message&gt; — talk to Neko\n"
        "Or <b>@mention</b> / <b>reply</b> to the bot\n"
        "/personality on|off · /joke · /nnr_settings · /nnr_help\n"
        "Ambient banter is on by default in DMs.",
    ),
    "anime": (
        "🎌 Anime",
        "/anime &lt;title&gt; · /manga &lt;title&gt; · /character &lt;name&gt;\n"
        "/seasonal — currently airing (MyAnimeList)\n"
        "<i>Sources: AniList + MyAnimeList (Jikan)</i>",
    ),
    "music": (
        "🎵 Music files",
        "/music &lt;query&gt; — download &amp; send as audio file\n"
        "/tts &lt;text&gt; · /insta · /qr\n"
        "<i>For voice-chat streaming see Live Music.</i>",
    ),
    "live": (
        "🎧 Live voice-chat music",
        "<b>Requires:</b> active voice chat + MUSIC_API_ID/HASH in .env\n"
        "Bot account must be able to join the call (usually admin).\n\n"
        "/play &lt;song or URL&gt; · /skip · /pause · /resume · /stop\n"
        "/queue · /nowplaying · /volume · /shuffle · /repeat\n"
        "/loop · /seek · /seekback · /effects · /playlist · /lyrics\n"
        "/auth · /toptracks · /resetqueue · /channelplay · /videomode",
    ),
    "fun": (
        "🎯 Fun",
        "/fun · /meme · /cosplay · /quote · /wish · /couples · /dice\n"
        "/8ball · /joke · /karma · /coinflip · /rps · /slots",
    ),
    "fonts": (
        "🔤 Fonts",
        "/fonts · /f1 … /f18 · /flip · /fontfx\n"
        "/stinky /bubbles /underline /rays /strike /frozen\n"
        "/random /mix /reverse",
    ),
    "reactions": (
        "🤗 Reactions",
        "/reactions on|off\n"
        "Then: /hug /pat /kiss /cuddle /slap /poke /tickle /bonk …\n"
        "Reply to someone or @mention them as the target.",
    ),
    "locks": (
        "🔐 Locks",
        "/lock &lt;type&gt; · /unlock &lt;type&gt;\n"
        "Types: url forward photo video sticker gif contact location",
    ),
    "notes": (
        "📝 Notes",
        "/notes — list · /save &lt;name&gt; &lt;content&gt; · /get &lt;name&gt;",
    ),
    "filters": (
        "🔍 Filters",
        "/addfilter &lt;trigger&gt; &lt;reply&gt; — add a keyword auto-reply\n"
        "Auto-replies fire on matching messages in the group.",
    ),
    "greetings": (
        "👋 Greetings",
        "/setwelcome &lt;text&gt; · /setgoodbye &lt;text&gt;\n"
        "Use {user} / {chat} placeholders. Fires on join/leave.",
    ),
    "warnings": (
        "⚠️ Warnings",
        "/warn (reply) · /warns · /resetwarns (admin, reply)\n"
        "Auto-action when limit is reached (ban/kick/mute).",
    ),
    "stats": (
        "📊 Stats",
        "/stats · /mystats · /botstats · /globalstats\n"
        "/level · /leaderboard · /balance · /topcatchers · /cstats",
    ),
    "collector": (
        "🎴 Character collector",
        "Characters spawn in groups — /grab to catch\n"
        "/collection · /characters · /listcharacters · /cview · /crandom\n"
        "/fav · /smelt · /myprofile · /cstats · /cprivacy · /topcatchers\n"
        "/trade · /gift — full list: /chelp",
    ),
    "economy": (
        "💰 Economy",
        "/daily · /balance · /pay · /shop · /buy · /inventory\n"
        "/level · /leaderboard — XP from chatting in groups",
    ),
    "games": (
        "🎮 Games",
        "/trivia · /tictactoe · /poll · /bet (where enabled)",
    ),
    "purge": (
        "🧹 Purge",
        "/purge — delete from replied message to here\n"
        "/del — delete the replied message",
    ),
    "rules": (
        "📜 Rules",
        "/rules · /setrules",
    ),
    "info": (
        "ℹ️ Info",
        "/id · /info [reply|id] · /names (name history)",
    ),
    "afk": (
        "💤 AFK",
        "/afk [reason] — mark yourself away",
    ),
    "karma": (
        "☯️ Karma",
        "Reply with +1 / -1 or /karma",
    ),
    "couples": (
        "💑 Couples",
        "/couples — couple of the day in this chat",
    ),
    "translate": (
        "🌐 Translation",
        "/tr &lt;lang&gt; &lt;text&gt; — e.g. /tr es Hello\n"
        "/tr &lt;text&gt; → English · reply + /tr &lt;lang&gt;",
    ),
    "urban": (
        "📖 Urban Dictionary",
        "/ud &lt;term&gt;",
    ),
    "imdb": (
        "🎬 IMDb / titles",
        "/imdb &lt;title&gt; — movie &amp; series lookup",
    ),
    "news": (
        "📰 News",
        "/news — latest headlines (when configured)\n"
        "<i>Also: anime seasonal via /seasonal</i>",
    ),
    "weather": (
        "🌤 Weather",
        "/weather &lt;city&gt; — live conditions (wttr.in)",
    ),
    "qr": (
        "📲 QR",
        "/qr &lt;text&gt;",
    ),
    "tts": (
        "🎤 TTS",
        "/tts &lt;text&gt;",
    ),
    "download": (
        "⬇️ Download",
        "/music &lt;query&gt; · /yt · /insta — media as files\n"
        "<i>Not the same as /play (live VC streaming).</i>",
    ),
    "tools": (
        "🛠 Tools",
        "/id · /info · /tr · /ud · /weather · /qr\n"
        "/calc · /imdb · /tagall · /names · /speedtest",
    ),
    "nightmode": (
        "🌙 Nightmode",
        "/nightmode — toggle restricted hours (admin)",
    ),
    "anti-spam": (
        "🚫 Anti-Spam",
        "Automatic heuristic spam scoring in groups. Admins are exempt.",
    ),
    "anti-flood": (
        "🛡 Anti-Flood",
        "Automatic flood detection in groups (warn / temp ban).",
    ),
    "captcha": (
        "✅ Captcha",
        "/captcha — configure join captcha (admin)",
    ),
    "all": (
        "📢 Tag all",
        "/tagall [message] · /all · /mentionall\n"
        "Mentions active members (admin only).",
    ),
    "channel": (
        "📋 Log channel",
        "/log — set a log channel for moderation events",
    ),
    "federation": (
        "🌐 Federation",
        "/newfed /joinfed /leavefed /fedban /unfedban /fedinfo /fpromote",
    ),
    "disable": (
        "🔧 Disable commands",
        "/disable &lt;cmd&gt; · /enable &lt;cmd&gt; · /disabled",
    ),
    "blacklist": (
        "⚫ Blacklist",
        "/blacklistchat /authorize (sudo) — chat access control",
    ),
    "playlists": (
        "🎧 Playlists",
        "/playlist save|load|list — save queues for later",
    ),
    "lyrics": (
        "📜 Lyrics",
        "/lyrics &lt;song&gt;",
    ),
    "control": (
        "🎛 Control panel",
        "You're already in /menu — use the pages or /settings.",
    ),
}


def _callback_key(label: str) -> str:
    return label.split()[-1].lower()


def _build_keyboard(page_data, page_num: int, total_pages: int) -> InlineKeyboardMarkup:
    buttons = []
    for row in page_data:
        buttons.append([
            InlineKeyboardButton(btn, callback_data=f"menu:{_callback_key(btn)}")
            for btn in row
        ])
    nav = []
    if page_num > 1:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"page:{page_num - 1}"))
    nav.append(InlineKeyboardButton("🗑", callback_data="close"))
    if page_num < total_pages:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"page:{page_num + 1}"))
    buttons.append(nav)
    return InlineKeyboardMarkup(buttons)


def _panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Menu", callback_data="page:1")],
        [InlineKeyboardButton("🗑 Close", callback_data="close")],
    ])


@safe_handler
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (
        f"<b>👋 Welcome, {mention_html(user.id, user.first_name)}!</b>\n\n"
        f"<i>🚀 {bot_name()}</i> — moderation, live voice-chat music, "
        f"Unicode fonts, and more — all in one bot.\n"
        f"<b>/ai</b> = general assistant · <b>/chat</b> (or @mention / reply) = Neko personality\n"
        f"Tap a button below, or send /help for the full command list."
    )
    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=_build_keyboard(MAIN_MENU, 1, TOTAL_PAGES),
    )


@safe_handler
async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data.startswith("page:"):
        page = int(data.split(":")[1])
        page_data = PAGES.get(page, MAIN_MENU)
        await query.edit_message_text(
            f"<b>📋 {bot_name()} Control Panel</b> — Page {page}/{TOTAL_PAGES}\n"
            f"<i>Tap a category for real commands that work in this bot.</i>",
            parse_mode="HTML",
            reply_markup=_build_keyboard(page_data, page, TOTAL_PAGES),
        )
        return

    if data == "close":
        try:
            await query.delete_message()
        except Exception:
            pass
        return

    if data.startswith("menu:"):
        key = data.split(":", 1)[1]
        panel = MENU_PANELS.get(key)
        if panel:
            title, body = panel
            await query.edit_message_text(
                f"<b>{title}</b>\n\n{body}",
                parse_mode="HTML",
                reply_markup=_panel_keyboard(),
            )
        else:
            await query.edit_message_text(
                f"<b>📋 {key.title()}</b>\n\n"
                f"No dedicated panel for this item. Try /help for the full command list.",
                parse_mode="HTML",
                reply_markup=_panel_keyboard(),
            )


@safe_handler
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>📚 " + bot_name() + " Help</b>\n\n"
        "<b>👮 Moderation:</b> /ban /mute /kick /warn /purge /tmute /tban\n"
        "<b>🛡️ Security:</b> antiflood &amp; antispam (automatic), /locks /nightmode /captcha\n"
        "<b>📝 Management:</b> /notes /filters /welcome /rules /setwelcome /setrules\n"
        "<b>🤖 AI assistant:</b> /ai /imagine /summarize /code /see /transcribe /persona\n"
        "<b>🎭 Neko personality:</b> /chat — or @mention / reply to the bot "
        "(ambient banter on by default in DMs; /personality on|off in groups)\n"
        "<b>🎌 Anime:</b> /anime /manga /character /seasonal "
        "<i>(AniList + MyAnimeList)</i>\n"
        "<b>🎵 Media files:</b> /music /yt /insta /tts /qr\n"
        "<b>🎧 Live VC music:</b> /play /skip /pause /resume /stop /queue /nowplaying "
        "/volume /shuffle /repeat /effects /loop /seek /playlist /lyrics "
        "<i>(needs MUSIC_API_ID/HASH + active voice chat)</i>\n"
        "<b>🎯 Fun:</b> /fun /couples /meme /cosplay /quote /wish /joke\n"
        "<b>🎴 Collector:</b> /grab /collection /characters "
        "/fav /myprofile /topcatchers /trade /gift — see /chelp\n"
        "<b>💰 Economy:</b> /daily /balance /pay /shop /buy /level /leaderboard\n"
        "<b>🔧 Utilities:</b> /id /info /names /tr /ud /weather /qr /calc /imdb /tagall /dice\n"
        "<b>📊 Stats:</b> /stats /mystats /botstats /top /leaderboard\n"
        "<b>🌐 Fed:</b> /newfed /joinfed /leavefed /fedban /unfedban /fedinfo /fpromote\n"
        "<b>🛠 More admin:</b> /disable /enable /disabled /zombies /rmzombies /unbanall /names\n"
        "<b>🛡 Ops (sudo):</b> /globalstats /activevc /blacklistchat /authorize "
        "/maintenance /privatemode /cleanmode /autoend /gban /block\n"
        "<b>🎭 Personality extras:</b> /personality on|off, /joke, /nnr_settings, /nnr_help\n"
        "<b>🔤 Fonts:</b> /f1–/f18, /flip, /fontfx, /fonts\n"
        "<b>🤗 Reactions:</b> /reactions on|off, /hug /pat /kiss /cuddle /slap …\n\n"
        "<i>Tap /menu for the control panel, or /settings for quick toggles.</i>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


@safe_handler
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from bot.config import settings as cfg
    from bot.core.database import async_session, Chat

    chat = update.effective_chat
    personality_state = "default"
    if chat.type != "private":
        async with async_session() as session:
            row = await session.get(Chat, chat.id)
            if row is not None and row.personality_enabled is not None:
                personality_state = "on" if row.personality_enabled else "off"
    else:
        personality_state = "on (DM default)" if cfg.personality_default_dm else "off (DM default)"

    live = "configured" if cfg.live_music_configured else "not configured (set MUSIC_API_ID/HASH)"
    text = (
        f"<b>⚙️ Settings — {bot_name()}</b>\n\n"
        f"Personality here: <b>{personality_state}</b> — /personality on|off\n"
        f"Live music: <b>{live}</b>\n"
        f"AI: /ai · Personality chat: /chat\n"
        f"More: /nnr_settings · /menu · /help"
    )
    await update.message.reply_text(text, parse_mode="HTML")


def register(app):
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("menu", start_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^(page:|menu:|close)"))
