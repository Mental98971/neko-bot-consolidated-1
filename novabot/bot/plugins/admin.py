"""
Complete Admin & Moderation Plugin
Ban, Mute, Kick, Warn, TMute, TBan, Unban, Unmute, Purge, Pin, Unpin, Slowmode
"""
import asyncio
from datetime import datetime, timedelta
from telegram import Update, ChatPermissions
from telegram.ext import ContextTypes, CommandHandler
from bot.core.decorators import admin_only, group_only
from bot.config import settings
from bot.utils.helpers import mention_html, time_parser, format_duration
from bot.core.database import async_session, Warn, Ban, Chat, TempAction
from bot.core.errors import safe_handler
from sqlalchemy import select, func


@admin_only
@group_only
@safe_handler
async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    reply = update.message.reply_to_message
    args = context.args

    target = reply.from_user if reply else None
    duration = None
    reason = "No reason provided"

    if not target and args:
        # Try to parse user ID or username
        try:
            target_id = int(args[0])
            target = await context.bot.get_chat_member(chat.id, target_id)
            target = target.user
            args = args[1:]
        except (ValueError, IndexError):
            await update.message.reply_text("Usage: /ban <@user/id> [time] [reason]")
            return

    if not target:
        await update.message.reply_text("Reply to a user or specify ID.")
        return

    # Parse time and reason
    if args:
        if time_parser(args[0]):
            duration = time_parser(args[0])
            reason = " ".join(args[1:]) or reason
        else:
            reason = " ".join(args)

    until = datetime.utcnow() + timedelta(seconds=duration) if duration else None

    try:
        await context.bot.ban_chat_member(
            chat.id, target.id,
            until_date=int(until.timestamp()) if until else None
        )
        dur_text = f" for {format_duration(duration)}" if duration else " permanently"
        await update.message.reply_text(
            f"🔨 <b>Banned</b> {mention_html(target.id, target.first_name)}{dur_text}.\n"
            f"<b>Reason:</b> {reason}",
            parse_mode="HTML"
        )
        if duration:
            async with async_session() as session:
                action = TempAction(chat_id=chat.id, user_id=target.id, action="ban", reason=reason, expires_at=until)
                session.add(action)
                await session.commit()
                action_id = action.id
            from bot.plugins.automod import schedule_temp_action_notice
            schedule_temp_action_notice(context.application, action_id, duration)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}")


@admin_only
@group_only
@safe_handler
async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    reply = update.message.reply_to_message
    if reply:
        target = reply.from_user
    elif context.args:
        try:
            target = await context.bot.get_chat(chat.id).get_member(int(context.args[0]))
            target = target.user
        except Exception:
            await update.message.reply_text("Usage: /unban <user_id> or reply")
            return
    else:
        await update.message.reply_text("Reply to a user or provide ID.")
        return

    await context.bot.unban_chat_member(chat.id, target.id)
    await update.message.reply_text(
        f"✅ {mention_html(target.id, target.first_name)} unbanned.",
        parse_mode="HTML"
    )


@admin_only
@group_only
@safe_handler
async def mute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    reply = update.message.reply_to_message
    if not reply:
        await update.message.reply_text("Reply to a user to mute.")
        return

    target = reply.from_user
    duration = None
    reason = "No reason"

    if context.args:
        if time_parser(context.args[0]):
            duration = time_parser(context.args[0])
            reason = " ".join(context.args[1:]) or reason
        else:
            reason = " ".join(context.args)

    perms = ChatPermissions(can_send_messages=False)
    until = datetime.utcnow() + timedelta(seconds=duration) if duration else None

    await context.bot.restrict_chat_member(
        chat.id, target.id, perms,
        until_date=int(until.timestamp()) if until else None
    )
    dur_text = f" for {format_duration(duration)}" if duration else ""
    await update.message.reply_text(
        f"🔇 <b>Muted</b> {mention_html(target.id, target.first_name)}{dur_text}.\n"
        f"<b>Reason:</b> {reason}",
        parse_mode="HTML"
    )
    if duration:
        async with async_session() as session:
            action = TempAction(chat_id=chat.id, user_id=target.id, action="mute", reason=reason, expires_at=until)
            session.add(action)
            await session.commit()
            action_id = action.id
        from bot.plugins.automod import schedule_temp_action_notice
        schedule_temp_action_notice(context.application, action_id, duration)


@admin_only
@group_only
@safe_handler
async def unmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    reply = update.message.reply_to_message
    target = reply.from_user if reply else None
    if not target:
        await update.message.reply_text("Reply to a muted user.")
        return

    perms = ChatPermissions(
        can_send_messages=True, can_send_polls=True,
        can_send_other_messages=True, can_add_web_page_previews=True,
        can_change_info=True, can_invite_users=True, can_pin_messages=True,
    )
    await context.bot.restrict_chat_member(chat.id, target.id, perms)
    await update.message.reply_text(
        f"🔊 {mention_html(target.id, target.first_name)} can speak again.",
        parse_mode="HTML"
    )


@admin_only
@group_only
@safe_handler
async def kick_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    reply = update.message.reply_to_message
    if not reply:
        await update.message.reply_text("Reply to a user to kick.")
        return

    target = reply.from_user
    await context.bot.ban_chat_member(chat.id, target.id)
    await context.bot.unban_chat_member(chat.id, target.id)
    await update.message.reply_text(
        f"👢 {mention_html(target.id, target.first_name)} has been kicked.",
        parse_mode="HTML"
    )


@admin_only
@group_only
@safe_handler
async def warn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    reply = update.message.reply_to_message
    if not reply:
        await update.message.reply_text("Reply to a user to warn.")
        return

    target = reply.from_user
    reason = " ".join(context.args) or "No reason"

    async with async_session() as session:
        warn = Warn(chat_id=chat.id, user_id=target.id, reason=reason, warned_by=update.effective_user.id)
        session.add(warn)
        await session.commit()

        # Count warns
        result = await session.execute(
            select(func.count()).where(Warn.chat_id == chat.id, Warn.user_id == target.id)
        )
        count = result.scalar()

        chat_row = await session.get(Chat, chat.id)
        limit = (chat_row.warn_limit if chat_row and chat_row.warn_limit else None) or settings.default_warn_limit
        action = (chat_row.warn_action if chat_row and chat_row.warn_action else None) or settings.default_warn_action
        await session.commit()

    text = (
        f"⚠️ <b>Warned</b> {mention_html(target.id, target.first_name)}\n"
        f"<b>Reason:</b> {reason}\n"
        f"<b>Count:</b> {count}/{limit}"
    )

    if count >= limit:
        try:
            if action == "ban":
                await context.bot.ban_chat_member(chat.id, target.id)
                text += "\n🔨 <b>Auto-banned for reaching the warn limit!</b>"
            elif action == "kick":
                await context.bot.ban_chat_member(chat.id, target.id)
                await context.bot.unban_chat_member(chat.id, target.id)
                text += "\n👢 <b>Auto-kicked for reaching the warn limit!</b>"
            else:  # mute
                await context.bot.restrict_chat_member(chat.id, target.id, ChatPermissions(can_send_messages=False))
                text += "\n🔇 <b>Auto-muted for reaching the warn limit!</b> (/unmute to reverse)"
        except Exception as e:
            text += f"\n❌ Auto-action failed: {e}"

    await update.message.reply_text(text, parse_mode="HTML")


@admin_only
@group_only
@safe_handler
async def purge_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    reply = update.message.reply_to_message
    if not reply:
        await update.message.reply_text("Reply to the oldest message to purge from.")
        return

    count = 0
    msg_id = update.message.message_id
    start_id = reply.message_id

    await update.message.delete()

    for m_id in range(start_id, msg_id):
        try:
            await context.bot.delete_message(chat.id, m_id)
            count += 1
            if count % 100 == 0:
                await asyncio.sleep(1)
        except Exception:
            pass

    confirm = await context.bot.send_message(chat.id, f"🧹 Purged {count} messages.")
    await asyncio.sleep(5)
    await confirm.delete()


@admin_only
@group_only
@safe_handler
async def pin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = update.message.reply_to_message
    if not reply:
        await update.message.reply_text("Reply to a message to pin.")
        return
    await context.bot.pin_chat_message(update.effective_chat.id, reply.message_id)
    await update.message.reply_text("📌 Pinned!")


@admin_only
@group_only
@safe_handler
async def unpin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.unpin_all_chat_messages(update.effective_chat.id)
    await update.message.reply_text("📌 Unpinned all messages.")


@admin_only
@group_only
@safe_handler
async def slowmode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not context.args:
        await update.message.reply_text("Usage: /slowmode <seconds> (0 to disable)")
        return
    try:
        seconds = int(context.args[0])
        await context.bot.set_chat_slow_mode_delay(chat.id, seconds)
        if seconds == 0:
            await update.message.reply_text("🐇 Slow mode disabled.")
        else:
            await update.message.reply_text(f"🐢 Slow mode set to {seconds}s.")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


@admin_only
@group_only
@safe_handler
async def del_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = update.message.reply_to_message
    if reply:
        await reply.delete()
        await update.message.delete()


# ─── Zombies (from FallenRobot/YaeMiko) ───
# Enumerating every member of a group isn't something the plain Bot API
# can do at all (Telegram only exposes that through MTProto, for
# privacy/scale reasons) — this reuses the same MTProto assistant
# connection already set up for live music (bot/core/music_client.py)
# rather than needing a separate userbot just for this one feature. If
# no assistant is configured, it says so rather than pretending to work.
async def _get_deleted_accounts(chat_id: int) -> list[int]:
    from bot.core.music_client import get_assistant_pool

    pool = get_assistant_pool()
    if not pool.pairs:
        return []
    pyrogram_client = pool.pairs[0][0].pyrogram
    deleted = []
    async for member in pyrogram_client.get_chat_members(chat_id):
        if getattr(member.user, "is_deleted", False):
            deleted.append(member.user.id)
    return deleted


@admin_only
@group_only
@safe_handler
async def zombies_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from bot.config import settings as cfg

    if not cfg.live_music_configured:
        await update.message.reply_text(
            "❌ This needs MUSIC_API_ID/MUSIC_API_HASH configured — enumerating every "
            "group member isn't something the plain Bot API can do; it needs the same "
            "MTProto connection live music uses."
        )
        return
    msg = await update.message.reply_text("🔍 Scanning for deleted accounts...")
    try:
        deleted = await _get_deleted_accounts(update.effective_chat.id)
    except Exception as e:
        await msg.edit_text(f"❌ Scan failed: {e}")
        return
    if not deleted:
        await msg.edit_text("✅ No deleted accounts found.")
        return
    await msg.edit_text(f"🧟 Found {len(deleted)} deleted account(s). /rmzombies to remove them.")


@admin_only
@group_only
@safe_handler
async def rmzombies_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from bot.config import settings as cfg

    if not cfg.live_music_configured:
        await update.message.reply_text("❌ This needs MUSIC_API_ID/MUSIC_API_HASH configured — see /zombies.")
        return
    msg = await update.message.reply_text("🔍 Scanning and removing deleted accounts...")
    try:
        deleted = await _get_deleted_accounts(update.effective_chat.id)
    except Exception as e:
        await msg.edit_text(f"❌ Scan failed: {e}")
        return
    removed = 0
    for user_id in deleted:
        try:
            await context.bot.ban_chat_member(update.effective_chat.id, user_id)
            await context.bot.unban_chat_member(update.effective_chat.id, user_id, only_if_banned=True)
            removed += 1
        except Exception:
            pass
    await msg.edit_text(f"🧟 Removed {removed}/{len(deleted)} deleted account(s).")


@admin_only
@group_only
@safe_handler
async def unbanall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lifts every ban this bot itself issued in this chat (via
    Ban/TempAction rows) — doesn't affect bans placed some other way."""
    async with async_session() as session:
        user_ids = (await session.execute(
            select(Ban.user_id).where(Ban.chat_id == update.effective_chat.id).distinct()
        )).scalars().all()

    if not user_ids:
        await update.message.reply_text("No bans recorded for this chat.")
        return
    msg = await update.message.reply_text(f"🔓 Unbanning {len(user_ids)} user(s)...")
    unbanned = 0
    for user_id in user_ids:
        try:
            await context.bot.unban_chat_member(update.effective_chat.id, user_id, only_if_banned=True)
            unbanned += 1
        except Exception:
            pass
    await msg.edit_text(f"🔓 Unbanned {unbanned}/{len(user_ids)} user(s).")


def register(app):
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("mute", mute_cmd))
    app.add_handler(CommandHandler("unmute", unmute_cmd))
    app.add_handler(CommandHandler("kick", kick_cmd))
    app.add_handler(CommandHandler("warn", warn_cmd))
    app.add_handler(CommandHandler("purge", purge_cmd))
    app.add_handler(CommandHandler("pin", pin_cmd))
    app.add_handler(CommandHandler("unpin", unpin_cmd))
    app.add_handler(CommandHandler("slowmode", slowmode_cmd))
    app.add_handler(CommandHandler("del", del_cmd))
    app.add_handler(CommandHandler("zombies", zombies_cmd))
    app.add_handler(CommandHandler("rmzombies", rmzombies_cmd))
    app.add_handler(CommandHandler("unbanall", unbanall_cmd))
