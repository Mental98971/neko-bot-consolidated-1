"""
Start handler, help, and the sleek inline menu system matching the screenshots.
"""
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler
from bot.identity import bot_name
from bot.utils.helpers import mention_html
from bot.core.errors import safe_handler


MAIN_MENU = [
    ["🛡️ Admin", "🤖 AI", "📢 AniQuotes"],
    ["🎌 Anime", "🚫 Anti-Spam", "🔒 Anti-NSFW"],
    ["✅ Approvals", "💾 Backup", "⚫ Blacklist"],
    ["🎛 Control", "💑 Couples", "🏏 Cricket"],
    ["🔧 Disable", "⬇️ Downloader", "🎉 Extra Funs"],
    ["🔍 Filters", "🎯 Fun", "👋 Greetings"],
    ["ℹ️ Info & AFK", "☯️ Karma", "🔐 Locks"],
]

PAGE_2 = [
    ["😂 Memes", "📌 Mentions", "📝 Notes"],
    ["🧹 Purges", "📊 Reporting", "📜 Rules"],
    ["🏷 Stickers", "🏷️ Tagger", "📰 Telegraph"],
    ["🛠 Tools", "⚠️ Warnings", "🎬 IMDb"],
]

PAGE_3 = [
    ["🧹 Cleaner", "👗 Cosplay", "💑 Couple"],
    ["💱 Currency", "⬇️ Downloader", "✨ Extras"],
    ["🔤 Font", "🎲 Fun", "👋 Greetings"],
    ["🆔 ID", "🎭 Imposter", "🚪 Join Request"],
    ["🔐 Locks", "📋 Log Channel", "🎨 Logo"],
]

PAGE_4 = [
    ["🎤 MICS", "⚡ MassActions", "🎵 Music"],
    ["📰 News", "🌙 Nightmode", "🔍 OCR"],
    ["🤵 Personal Assistant", "📱 Pokedex", "📲 QR Code"],
    ["💬 Quotes", "🔎 Reverse Search", "🌸 Seasonal Anime"],
    ["💻 Session", "⚽ Sports", "🏷 Stickers"],
]

PAGE_5 = [
    ["🎤 TTS", "📢 Tag All", "🔤 Tiny"],
    ["🌐 Translation", "⬆️ Uploader", "📖 Urban Dictionary"],
    ["⚠️ Warn", "⭐ Wish", "✍️ Write"],
    ["🚪 Leave Chats", "🛡️ AntiBanAll"],
]

PAGE_6 = [
    ["🔍 Filters", "📸 Instagram Downloader", "🔺 Upscale"],
    ["👮 Admin", "💤 AFK", "🎌 Anime"],
    ["📰 Anime News", "📅 Anime Schedule", "📢 Announcement"],
    ["🚫 AntiChannel", "✅ Approval", "❓ Ask"],
    ["🔨 Ban", "⚫ Blacklist", "🧮 Calculator"],
]


def _build_keyboard(page_data, page_num, total_pages):
    buttons = []
    for row in page_data:
        buttons.append([InlineKeyboardButton(btn, callback_data=f"menu:{btn.split()[-1].lower()}") for btn in row])

    nav = []
    if page_num > 1:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"page:{page_num-1}"))
    nav.append(InlineKeyboardButton("🗑", callback_data="close"))
    if page_num < total_pages:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"page:{page_num+1}"))
    buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="page:1")])
    return InlineKeyboardMarkup(buttons)


PAGES = {1: MAIN_MENU, 2: PAGE_2, 3: PAGE_3, 4: PAGE_4, 5: PAGE_5, 6: PAGE_6}


@safe_handler
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (
        f"<b>👋 Welcome, {mention_html(user.id, user.first_name)}!</b>\n\n"
        f"<i>🚀 {bot_name()}</i> — moderation, AI chat, live voice-chat music, "
        f"a sarcastic personality mode, and Unicode font styling, all in one bot.\n"
        f"Tap a button below to explore features, or send /help for the full command list."
    )
    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=_build_keyboard(MAIN_MENU, 1, 6)
    )


@safe_handler
async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("page:"):
        page = int(data.split(":")[1])
        page_data = PAGES.get(page, MAIN_MENU)
        await query.edit_message_text(
            f"<b>📋 {bot_name()} Control Panel</b> — Page {page}/6",
            parse_mode="HTML",
            reply_markup=_build_keyboard(page_data, page, 6)
        )
    elif data == "close":
        await query.delete_message()
    else:
        module = data.split(":")[1]
        await query.edit_message_text(
            f"<b>⚙️ {module.title()} Settings</b>\n\n"
            f"Feature panel for <code>{module}</code>.\n"
            f"Use /{module} or related commands to configure.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="page:1")]
            ])
        )


@safe_handler
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>📚 " + bot_name() + " Help</b>\n\n"
        "<b>👮 Moderation:</b> /ban /mute /kick /warn /purge /tmute /tban\n"
        "<b>🛡️ Security:</b> /antispam /antiflood /captcha /locks /nightmode\n"
        "<b>📝 Management:</b> /notes /filters /welcome /rules /setwelcome /setrules\n"
        "<b>🤖 AI:</b> /ai /chat /imagine /summarize\n"
        "<b>🎌 Anime:</b> /anime /manga /character /schedule /news\n"
        "<b>🎵 Media:</b> /music /yt /insta /tiktok /tts\n"
        "<b>🎯 Fun:</b> /fun /couples /meme /quote /wish\n"
        "<b>🎴 Collector:</b> /grab (catch a spawned character), /collection /characters "
        "/fav /myprofile /topcatchers /trade /gift — see /chelp for the full list\n"
        "<b>🔧 Utilities:</b> /id /info /tr /ud /weather /qr /calc /imdb\n"
        "<b>📊 Stats:</b> /stats /mystats /botstats /top /leaderboard\n"
        "<b>🌐 Fed:</b> /newfed /joinfed /leavefed /fedban /unfedban /fedinfo /fpromote /fedadmins\n"
        "<b>🛠 More admin:</b> /disable /enable /disabled, /zombies /rmzombies (deleted accounts), /unbanall, /names (name history)\n"
        "<b>🎧 Live Music:</b> /play (YT/Spotify/Apple/SoundCloud/Resso links or search) "
        "/skip /pause /resume /stop /queue /nowplaying /volume /shuffle /repeat /effects "
        "/loop /seek /seekback /playlist /lyrics /channelplay /videomode /toptracks "
        "/resetqueue <i>(voice chat)</i>\n"
        "<b>🛡 Ops (sudo):</b> /globalstats /activevc /blacklistchat /authorize "
        "/maintenance /privatemode /cleanmode /autoend /speedtest /gban /block\n"
        "<b>🎭 Personality:</b> /personality on|off, /joke — a sarcastic banter "
        "mode that replies to plain messages (on by default in DMs)\n"
        "<b>🔤 Fonts:</b> /f1–/f18, /flip, /fontfx (+shortcuts: /stinky /bubbles "
        "/underline /rays /strike /frozen...), /random, /mix, /reverse, /fonts\n\n"
        "<b>🤗 Reactions:</b> /reactions on|off, then /hug /pat /kiss /cuddle /slap "
        "/poke /tickle /bonk and 22 more anime-style GIF reactions — target someone "
        "by replying or @mentioning them\n\n"
        "<i>Tap /menu for the inline control panel, or /settings for quick toggles.</i>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


@safe_handler
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick-glance settings panel: personality mode for this chat, plus
    pointers to the other per-feature settings commands."""
    from bot.config import settings as cfg
    from bot.core.database import async_session, Chat

    chat = update.effective_chat
    personality_state = "default"
    if chat.type != "private":
        async with async_session() as session:
            row = await session.get(Chat, chat.id)
            if row and row.personality_enabled is not None:
                personality_state = "on" if row.personality_enabled else "off"
    else:
        personality_state = "on (default in DMs)" if cfg.personality_default_dm else "off"

    text = (
        "<b>⚙️ " + bot_name() + " Settings</b>\n\n"
        f"<b>🎭 Personality mode:</b> <code>{personality_state}</code>\n"
        f"Toggle with /personality on or /personality off (group admins only).\n\n"
        "<b>🔤 Font preferences:</b> see /fontsettings\n"
        "<b>🎧 Music volume:</b> see /volume\n"
        "<b>🛡️ Moderation options:</b> /locks, /filters, /captcha, /nightmode\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")


def register(app):
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("menu", start_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^(page:|menu:|close)"))
