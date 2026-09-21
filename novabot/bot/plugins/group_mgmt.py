"""
Group Management Suite
Welcome/Goodbye, Rules, Notes, Filters, Locks, Nightmode, CAPTCHA, Log Channel, Reports
"""
from datetime import datetime, time as dt_time
from telegram import Update, ChatPermissions
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters
from bot.core.decorators import admin_only, group_only
from bot.core.database import async_session, Chat, Note
from bot.utils.helpers import mention_html, escape_html
from bot.core.errors import safe_handler
from sqlalchemy import select


# ─── Welcome / Goodbye ───
@admin_only
@group_only
async def setwelcome_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.reply_to_message.text if update.message.reply_to_message else " ".join(context.args)
    if not text:
        await update.message.reply_text("Reply to a message or provide text.\nVariables: {first}, {last}, {username}, {mention}, {chat}")
        return

    async with async_session() as session:
        result = await session.execute(select(Chat).where(Chat.id == update.effective_chat.id))
        chat = result.scalar_one_or_none()
        if not chat:
            chat = Chat(id=update.effective_chat.id, title=update.effective_chat.title, type=update.effective_chat.type)
            session.add(chat)
        chat.welcome_text = text
        await session.commit()

    await update.message.reply_text("✅ Welcome message updated.")


@admin_only
@group_only
async def setgoodbye_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.reply_to_message.text if update.message.reply_to_message else " ".join(context.args)
    if not text:
        await update.message.reply_text("Reply to a message or provide text.")
        return

    async with async_session() as session:
        result = await session.execute(select(Chat).where(Chat.id == update.effective_chat.id))
        chat = result.scalar_one_or_none()
        if not chat:
            chat = Chat(id=update.effective_chat.id, title=update.effective_chat.title, type=update.effective_chat.type)
            session.add(chat)
        chat.goodbye_text = text
        chat.goodbye_enabled = True
        await session.commit()

    await update.message.reply_text("✅ Goodbye message updated.")


async def welcome_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.chat_member:
        return

    member = update.chat_member.new_chat_member
    if member.status not in ("member", "administrator") or update.chat_member.old_chat_member.status != "left":
        return

    chat_id = update.effective_chat.id
    async with async_session() as session:
        result = await session.execute(select(Chat).where(Chat.id == chat_id))
        chat = result.scalar_one_or_none()
        if not chat or not chat.welcome_enabled or not chat.welcome_text:
            return

        user = member.user
        text = chat.welcome_text.format(
            first=escape_html(user.first_name),
            last=escape_html(user.last_name or ""),
            username=user.username or "",
            mention=mention_html(user.id, user.first_name),
            chat=escape_html(update.effective_chat.title)
        )

        await context.bot.send_message(chat_id, text, parse_mode="HTML")


# ─── Rules ───
@admin_only
@group_only
async def setrules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.reply_to_message.text if update.message.reply_to_message else " ".join(context.args)
    if not text:
        await update.message.reply_text("Reply to a message or provide rules text.")
        return

    async with async_session() as session:
        result = await session.execute(select(Chat).where(Chat.id == update.effective_chat.id))
        chat = result.scalar_one_or_none()
        if not chat:
            chat = Chat(id=update.effective_chat.id, title=update.effective_chat.title, type=update.effective_chat.type)
            session.add(chat)
        chat.rules = text
        await session.commit()

    await update.message.reply_text("✅ Rules updated.")


@safe_handler
async def rules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    async with async_session() as session:
        result = await session.execute(select(Chat).where(Chat.id == chat_id))
        chat = result.scalar_one_or_none()

    if chat and chat.rules:
        await update.message.reply_text(f"<b>📜 Rules</b>\n\n{chat.rules}", parse_mode="HTML")
    else:
        await update.message.reply_text("No rules set. Admins can set with /setrules")


# ─── Notes ───
@admin_only
@group_only
async def save_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /save <note_name> (reply to content)")
        return

    name = context.args[0].lower()
    reply = update.message.reply_to_message
    content = reply.text or reply.caption if reply else " ".join(context.args[1:])
    file_id = None
    msg_type = "text"

    if reply:
        if reply.photo:
            file_id = reply.photo[-1].file_id
            msg_type = "photo"
        elif reply.video:
            file_id = reply.video.file_id
            msg_type = "video"
        elif reply.document:
            file_id = reply.document.file_id
            msg_type = "document"
        elif reply.audio:
            file_id = reply.audio.file_id
            msg_type = "audio"

    if not content and not file_id:
        await update.message.reply_text("Reply to a message with content to save.")
        return

    async with async_session() as session:
        note = Note(
            chat_id=update.effective_chat.id,
            name=name,
            content=content,
            file_id=file_id,
            message_type=msg_type,
            created_by=update.effective_user.id
        )
        session.add(note)
        await session.commit()

    await update.message.reply_text(f"✅ Note '<code>{name}</code>' saved!", parse_mode="HTML")


@safe_handler
async def get_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return
    name = context.args[0].lower()

    async with async_session() as session:
        result = await session.execute(
            select(Note).where(Note.chat_id == update.effective_chat.id, Note.name == name)
        )
        note = result.scalar_one_or_none()

    if not note:
        return

    if note.message_type == "text":
        await update.message.reply_text(note.content)
    elif note.message_type == "photo":
        await update.message.reply_photo(note.file_id, caption=note.content)
    elif note.message_type == "video":
        await update.message.reply_video(note.file_id, caption=note.content)
    elif note.message_type == "document":
        await update.message.reply_document(note.file_id, caption=note.content)
    elif note.message_type == "audio":
        await update.message.reply_audio(note.file_id, caption=note.content)


@safe_handler
async def notes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        result = await session.execute(
            select(Note).where(Note.chat_id == update.effective_chat.id)
        )
        notes = result.scalars().all()

    if not notes:
        await update.message.reply_text("📝 No notes saved.")
        return

    text = "<b>📝 Saved Notes</b>\n\n"
    for note in notes:
        text += f"• <code>{note.name}</code> ({note.message_type})\n"
    await update.message.reply_text(text, parse_mode="HTML")


# ─── Filters ───
@admin_only
@group_only
async def addfilter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /addfilter <trigger> <response>")
        return

    trigger = context.args[0].lower()
    response = " ".join(context.args[1:])

    async with async_session() as session:
        result = await session.execute(select(Chat).where(Chat.id == update.effective_chat.id))
        chat = result.scalar_one_or_none()
        if not chat:
            chat = Chat(id=update.effective_chat.id, title=update.effective_chat.title, type=update.effective_chat.type)
            session.add(chat)
        if not chat.filters:
            chat.filters = {}
        chat.filters[trigger] = response
        await session.commit()

    await update.message.reply_text(f"✅ Filter '<code>{trigger}</code>' added.", parse_mode="HTML")


async def filter_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.lower()
    async with async_session() as session:
        result = await session.execute(select(Chat).where(Chat.id == update.effective_chat.id))
        chat = result.scalar_one_or_none()
        if not chat or not chat.filters:
            return

        for trigger, response in chat.filters.items():
            if trigger in text:
                await update.message.reply_text(response)
                break


# ─── Locks ───
LOCK_TYPES = {
    "url": "lock_url",
    "forward": "lock_forward",
    "photo": "lock_photo",
    "video": "lock_video",
    "sticker": "lock_sticker",
    "gif": "lock_gif",
    "contact": "lock_contact",
    "location": "lock_location",
}


@admin_only
@group_only
async def lock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        types = ", ".join(LOCK_TYPES.keys())
        await update.message.reply_text(f"Usage: /lock <type>\nTypes: {types}")
        return

    lock_type = context.args[0].lower()
    if lock_type not in LOCK_TYPES:
        await update.message.reply_text(f"Invalid type. Use: {', '.join(LOCK_TYPES.keys())}")
        return

    async with async_session() as session:
        result = await session.execute(select(Chat).where(Chat.id == update.effective_chat.id))
        chat = result.scalar_one_or_none()
        if not chat:
            chat = Chat(id=update.effective_chat.id, title=update.effective_chat.title, type=update.effective_chat.type)
            session.add(chat)
        setattr(chat, LOCK_TYPES[lock_type], True)
        await session.commit()

    await update.message.reply_text(f"🔒 {lock_type.title()} locked.")


@admin_only
@group_only
async def unlock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /unlock <type>")
        return

    lock_type = context.args[0].lower()
    if lock_type not in LOCK_TYPES:
        return

    async with async_session() as session:
        result = await session.execute(select(Chat).where(Chat.id == update.effective_chat.id))
        chat = result.scalar_one_or_none()
        if chat:
            setattr(chat, LOCK_TYPES[lock_type], False)
            await session.commit()

    await update.message.reply_text(f"🔓 {lock_type.title()} unlocked.")


async def lock_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat:
        return

    msg = update.message
    chat_id = update.effective_chat.id

    async with async_session() as session:
        result = await session.execute(select(Chat).where(Chat.id == chat_id))
        chat = result.scalar_one_or_none()
        if not chat:
            return

    # Check admin status
    try:
        member = await update.effective_chat.get_member(msg.from_user.id)
        if member.status in ("administrator", "creator"):
            return
    except Exception:
        pass

    checks = [
        (chat.lock_url and (msg.entities and any(e.type in ("url", "text_link") for e in msg.entities))),
        (chat.lock_forward and msg.forward_date),
        (chat.lock_photo and msg.photo),
        (chat.lock_video and msg.video),
        (chat.lock_sticker and msg.sticker),
        (chat.lock_gif and msg.animation),
        (chat.lock_contact and msg.contact),
        (chat.lock_location and (msg.location or msg.venue)),
    ]

    if any(checks):
        try:
            await msg.delete()
            await context.bot.send_message(
                chat_id,
                f"🚫 {mention_html(msg.from_user.id, msg.from_user.first_name)}, this content is locked.",
                parse_mode="HTML"
            )
        except Exception:
            pass


# ─── Nightmode ───
@admin_only
@group_only
async def nightmode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        result = await session.execute(select(Chat).where(Chat.id == update.effective_chat.id))
        chat = result.scalar_one_or_none()
        if not chat:
            chat = Chat(id=update.effective_chat.id, title=update.effective_chat.title, type=update.effective_chat.type)
            session.add(chat)
        chat.nightmode_enabled = not chat.nightmode_enabled
        await session.commit()
        status = "enabled 🌙" if chat.nightmode_enabled else "disabled ☀️"

    await update.message.reply_text(f"Nightmode {status}")


async def nightmode_job(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.utcnow().time()
    async with async_session() as session:
        result = await session.execute(select(Chat).where(Chat.nightmode_enabled == True))
        chats = result.scalars().all()

        for chat in chats:
            try:
                start = dt_time(*map(int, chat.nightmode_start.split(":")))
                end = dt_time(*map(int, chat.nightmode_end.split(":")))

                is_night = False
                if start < end:
                    is_night = start <= now < end
                else:
                    is_night = now >= start or now < end

                if is_night and chat.nightmode_lock:
                    perms = ChatPermissions(can_send_messages=False)
                    await context.bot.set_chat_permissions(chat.id, perms)
                else:
                    perms = ChatPermissions(
                        can_send_messages=True, can_send_polls=True,
                        can_send_other_messages=True, can_add_web_page_previews=True,
                        can_change_info=True, can_invite_users=True, can_pin_messages=True,
                    )
                    await context.bot.set_chat_permissions(chat.id, perms)
            except Exception:
                pass


# ─── CAPTCHA ───
@admin_only
@group_only
async def captcha_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        result = await session.execute(select(Chat).where(Chat.id == update.effective_chat.id))
        chat = result.scalar_one_or_none()
        if not chat:
            chat = Chat(id=update.effective_chat.id, title=update.effective_chat.title, type=update.effective_chat.type)
            session.add(chat)
        chat.captcha_enabled = not chat.captcha_enabled
        await session.commit()
        status = "enabled ✅" if chat.captcha_enabled else "disabled ❌"

    await update.message.reply_text(f"CAPTCHA {status}")


# ─── Log Channel ───
@admin_only
@group_only
async def log_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /log <channel_id or @channel>")
        return

    channel = context.args[0]
    try:
        channel_id = int(channel)
    except ValueError:
        # Resolve username
        chat = await context.bot.get_chat(channel)
        channel_id = chat.id

    async with async_session() as session:
        result = await session.execute(select(Chat).where(Chat.id == update.effective_chat.id))
        chat = result.scalar_one_or_none()
        if not chat:
            chat = Chat(id=update.effective_chat.id, title=update.effective_chat.title, type=update.effective_chat.type)
            session.add(chat)
        chat.log_channel = channel_id
        await session.commit()

    await update.message.reply_text(f"✅ Log channel set to {channel_id}")


# ─── Report ───
@safe_handler
async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = update.message.reply_to_message
    if not reply:
        await update.message.reply_text("Reply to a message to report it.")
        return

    chat = update.effective_chat
    reporter = update.effective_user
    reported = reply.from_user

    async with async_session() as session:
        result = await session.execute(select(Chat).where(Chat.id == chat.id))
        chat_db = result.scalar_one_or_none()
        log_channel = chat_db.log_channel if chat_db else None

    text = (
        f"🚨 <b>Report</b>\n"
        f"<b>Chat:</b> {escape_html(chat.title)} ({chat.id})\n"
        f"<b>Reporter:</b> {mention_html(reporter.id, reporter.first_name)}\n"
        f"<b>Reported:</b> {mention_html(reported.id, reported.first_name)}\n"
        f"<b>Message:</b> {reply.text[:500] if reply.text else '[Media]'}"
    )

    if log_channel:
        await context.bot.send_message(log_channel, text, parse_mode="HTML")
        await update.message.reply_text("✅ Report sent to admins.")
    else:
        await update.message.reply_text("❌ No log channel configured. Use /log")


# ─── Disable / Enable (from FallenRobot/YaeMiko) ───
# The actual gate is in bot/middleware/access_control.py, which checks
# Chat.disabled_commands early (before the disabled command's own
# handler would even run) and lets this chat's own admins through
# regardless — /disable is for quieting down regular members, not
# locking admins out of a command they chose to turn off. These three
# commands just manage that list.
@admin_only
@group_only
async def disable_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /disable <command name, without the />")
        return
    cmd_name = context.args[0].lstrip("/").lower()
    async with async_session() as session:
        chat = await session.get(Chat, update.effective_chat.id)
        if chat is None:
            chat = Chat(id=update.effective_chat.id, type=update.effective_chat.type)
            session.add(chat)
        disabled = list(chat.disabled_commands or [])
        if cmd_name in disabled:
            await update.message.reply_text(f"/{cmd_name} is already disabled here.")
            return
        disabled.append(cmd_name)
        chat.disabled_commands = disabled
        await session.commit()
    await update.message.reply_text(f"🔇 /{cmd_name} is now disabled for non-admins in this chat.")


@admin_only
@group_only
async def enable_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /enable <command name>")
        return
    cmd_name = context.args[0].lstrip("/").lower()
    async with async_session() as session:
        chat = await session.get(Chat, update.effective_chat.id)
        if not chat or cmd_name not in (chat.disabled_commands or []):
            await update.message.reply_text(f"/{cmd_name} isn't disabled here.")
            return
        chat.disabled_commands = [c for c in chat.disabled_commands if c != cmd_name]
        await session.commit()
    await update.message.reply_text(f"🔊 /{cmd_name} is enabled again.")


@group_only
async def disabled_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_session() as session:
        chat = await session.get(Chat, update.effective_chat.id)
        disabled = chat.disabled_commands if chat else []
    if not disabled:
        await update.message.reply_text("No commands are disabled here.")
        return
    await update.message.reply_text("🔇 Disabled here: " + ", ".join(f"/{c}" for c in disabled))


def register(app):
    app.add_handler(CommandHandler("setwelcome", setwelcome_cmd))
    app.add_handler(CommandHandler("setgoodbye", setgoodbye_cmd))
    app.add_handler(CommandHandler("setrules", setrules_cmd))
    app.add_handler(CommandHandler("rules", rules_cmd))
    app.add_handler(CommandHandler("save", save_cmd))
    app.add_handler(CommandHandler("get", get_cmd))
    app.add_handler(CommandHandler("notes", notes_cmd))
    app.add_handler(CommandHandler("addfilter", addfilter_cmd))
    app.add_handler(CommandHandler("lock", lock_cmd))
    app.add_handler(CommandHandler("unlock", unlock_cmd))
    app.add_handler(CommandHandler("nightmode", nightmode_cmd))
    app.add_handler(CommandHandler("captcha", captcha_cmd))
    app.add_handler(CommandHandler("log", log_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("disable", disable_cmd))
    app.add_handler(CommandHandler("enable", enable_cmd))
    app.add_handler(CommandHandler("disabled", disabled_cmd))

    # Listeners
    # NOTE: these listeners have overlapping filters (TEXT and ALL both
    # match plain text messages). python-telegram-bot only invokes the FIRST
    # matching handler within a given group, so registering catch-all
    # handlers at the same default group=0 would silently drop all but one
    # of them — this is what originally made this file's own filter_listener
    # dead code (see README). Every catch-all text/service-message handler
    # in NovaBot now gets its own group. Full map, so future additions
    # don't reintroduce the collision:
    #   -3  logging_middleware      (core/bot.py, all updates)
    #   -2  antiflood_middleware    (core/bot.py, groups only)
    #   -1  antispam_middleware     (core/bot.py, groups only)
    #    0  raid_join_listener      (automod.py, NEW_CHAT_MEMBERS)
    #    0  every CommandHandler    (mutually exclusive with the above —
    #                                 commands vs. plain text never overlap)
    #    1  lock_listener           (this file, ALL messages in groups)
    #    2  filter_listener         (this file, TEXT & ~COMMAND)
    #    3  afk_listener            (fun.py, TEXT & ~COMMAND)
    #    4  welcome_listener        (this file, NEW_CHAT_MEMBERS)
    #    5  fonts.handle_message    (fonts.py, TEXT & ~COMMAND — auto-style)
    #    6  economy.xp_listener     (economy.py, TEXT & ~COMMAND)
    #    7  collector.spawn_listener (collector.py, TEXT & ~COMMAND)
    #    8  collector.upload_cmd caption path (collector.py, PHOTO|VIDEO & CAPTION)
    #    9  name_history.name_history_middleware (name_history.py, ALL & GROUPS)
    #   10  personality.handle_message (personality.py, TEXT & ~COMMAND — banter)
    app.add_handler(MessageHandler(filters.ALL & filters.ChatType.GROUPS, lock_listener), group=1)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, filter_listener), group=2)
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_listener), group=4)

    # Jobs
    job_queue = app.job_queue
    if job_queue:
        job_queue.run_repeating(nightmode_job, interval=60, first=10)
