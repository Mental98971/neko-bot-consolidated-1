"""
Personality / banter plugin v2.3 — all 28 improvements applied.

v2.3 changes:
  - Atomic increment_and_check() replaces race-prone check+increment (fix 1.2).
  - Response delay uses JobQueue instead of blocking asyncio.sleep (fix 1.7).
  - handle_message passes skin_data to subplugins via route() kwargs (fix 1.3).
  - Inline keyboard has Back buttons for navigation (fix 3.3).
  - /dpersonality and nnr_settings>Reset have aligned scope (fix 3.4).
  - /nnr_test previews personality response without sending to chat (feature 4.3).
  - /nnr_help unified command list (feature 4.8).
  - Graceful DB degradation: fallback to stateless mode on DB failure (feature 4.7).
  - hashlib imported at module level (quality 5.1-5.2).
  - _is_enabled_for cached for 30s (fix 2.2).
  - Startup loading via personality.load_persisted_settings() (fix 1.1/3.1).
  - Daily cleanup job for old personality data (feature 4.6).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import random
import time

from cachetools import TTLCache
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    CommandHandler, ContextTypes, MessageHandler, CallbackQueryHandler, filters,
)

from bot.config import settings
from bot.core.database import Chat, async_session
from bot.core.errors import safe_handler
from bot.core.decorators import owner_only
from bot.personality.custom_instructions import instruction_processor
from bot.personality.jokes import jokes_db, Joke
from bot.personality.memory import memory
from bot.personality.personality import personality
from bot.personality.subplugins import (
    AnimePlugin, GamingPlugin, GeneralPlugin, MetaBanterPlugin,
    PluginManager, ProgrammingPlugin,
)
from bot.personality.triggers import triggers
from bot.utils.logger import get_logger

logger = get_logger(__name__)

_cooldowns: TTLCache = TTLCache(
    maxsize=10_000,
    ttl=max(settings.personality_cooldown_seconds * 2, 10),
)

# v2.3: Cache for _is_enabled_for (30s TTL)
_enabled_cache: TTLCache = TTLCache(maxsize=500, ttl=30)

_plugin_manager = PluginManager()
_plugin_manager.register(ProgrammingPlugin())
_plugin_manager.register(AnimePlugin())
_plugin_manager.register(GamingPlugin())
_plugin_manager.register(MetaBanterPlugin())
_plugin_manager.register(GeneralPlugin())


def _on_cooldown(user_id: int, chat_id: int) -> bool:
    now = time.time()
    key = (user_id, chat_id)
    last = _cooldowns.get(key, 0)
    if now - last < settings.personality_cooldown_seconds:
        return True
    _cooldowns[key] = now
    return False


async def _is_enabled_for(chat_id: int, chat_type: str) -> bool:
    # v2.3: Check cache first
    cache_key = (chat_id, chat_type)
    if cache_key in _enabled_cache:
        return _enabled_cache[cache_key]

    default = settings.personality_default_dm if chat_type == "private" else settings.personality_default_group
    try:
        async with async_session() as session:
            row = await session.get(Chat, chat_id)
            if row is not None and row.personality_enabled is not None:
                result = row.personality_enabled
            else:
                result = default
    except Exception as e:
        logger.warning("DB error in _is_enabled_for, using default: %s", e)
        result = default

    _enabled_cache[cache_key] = result
    return result


async def _get_response_delay(chat_id: int) -> float:
    """Get the per-chat response delay, falling back to global default."""
    try:
        async with async_session() as session:
            row = await session.get(Chat, chat_id)
            if row is not None:
                delay = row.personality_response_delay
                if delay is not None:
                    return max(settings.personality_response_delay_min, delay)
    except Exception as e:
        logger.warning("DB error in _get_response_delay, using default: %s", e)
    return settings.personality_response_delay_default


async def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if the user is an admin in the current chat."""
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == "private":
        return True
    member = await context.bot.get_chat_member(chat.id, user.id)
    return member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)


# ==================== /nnr_settings COMMAND =========================

def _settings_keyboard():
    """Build the main settings keyboard."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Custom Instructions", callback_data="nnr_ci")],
        [InlineKeyboardButton("User Limit", callback_data="nnr_ul"),
         InlineKeyboardButton("Response Delay", callback_data="nnr_rd")],
        [InlineKeyboardButton("Skin", callback_data="nnr_skin"),
         InlineKeyboardButton("Sarcasm/Metaphors", callback_data="nnr_cfg")],
        [InlineKeyboardButton("Reset All", callback_data="nnr_reset")],
    ])


def _back_keyboard():
    """v2.3: Back button for sub-pages."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("<< Back", callback_data="nnr_main")],
    ])


def _format_settings_text(chat, chat_settings) -> str:
    """Render the settings-hub body text. Shared by the initial /nnr_settings
    command and the callback's "<< Back" handler so the two can't drift."""
    ci = chat_settings.custom_instructions
    ci_display = (ci[:60] + "...") if ci and len(ci) > 60 else (ci or "Not set")
    return (
        f"Neko Settings - {chat.title or 'this chat'}\n"
        f"{'=' * 35}\n"
        f"Custom Instructions: {ci_display}\n"
        f"Skin: {chat_settings.skin or 'neko'}\n"
        f"User Limit: {chat_settings.user_limit or settings.personality_user_limit_default}\n"
        f"Response Delay: {chat_settings.response_delay if chat_settings.response_delay is not None else settings.personality_response_delay_default}s\n"
        f"{'=' * 35}\n"
        f"Tap a button below to adjust:"
    )


@safe_handler
async def cmd_nnr_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The main settings hub for Neko in group chats."""
    chat = update.effective_chat

    if not await _is_admin(update, context):
        await update.message.reply_text("Admin only.")
        return

    chat_settings = await memory.get_settings(chat.id)
    text = _format_settings_text(chat, chat_settings)
    await update.message.reply_text(text, reply_markup=_settings_keyboard())


@safe_handler
async def cmd_nnr_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard callbacks from /nnr_settings."""
    query = update.callback_query
    await query.answer()
    chat = update.effective_chat
    data = query.data

    if not await _is_admin(update, context):
        await query.edit_message_text("Admin only.")
        return

    # v2.3: Back button returns to main menu
    if data == "nnr_main":
        chat_settings = await memory.get_settings(chat.id)
        text = _format_settings_text(chat, chat_settings)
        await query.edit_message_text(text, reply_markup=_settings_keyboard())
        return

    if data == "nnr_ci":
        await query.edit_message_text(
            "Custom Instructions\n\n"
            "Send a message starting with /nnr_ci followed by your instructions.\n"
            "Examples:\n"
            "  /nnr_ci be cute and supportive, no sarcasm\n"
            "  /nnr_ci speak like a medieval knight\n"
            "  /nnr_ci be serious and professional\n"
            "  /nnr_ci be chaotic and unhinged\n\n"
            "Available presets: " + ", ".join(instruction_processor.available_presets()) + "\n\n"
            "Use /nnr_ci clear to remove custom instructions.",
            reply_markup=_back_keyboard(),
        )

    elif data == "nnr_ul":
        current = (await memory.get_settings(chat.id)).user_limit or settings.personality_user_limit_default
        await query.edit_message_text(
            f"User Limit\n\n"
            f"Current: {current} messages per user\n"
            f"Max: {settings.personality_user_limit_max}\n\n"
            f"Use: /nnr_ul <number> (1-{settings.personality_user_limit_max})\n"
            f"Example: /nnr_ul 50",
            reply_markup=_back_keyboard(),
        )

    elif data == "nnr_rd":
        current_delay = (await memory.get_settings(chat.id)).response_delay
        current_val = current_delay if current_delay is not None else settings.personality_response_delay_default
        await query.edit_message_text(
            f"Response Delay\n\n"
            f"Current: {current_val}s\n"
            f"Min: {settings.personality_response_delay_min}s\n\n"
            f"Use: /nnr_rd <seconds>\n"
            f"Example: /nnr_rd 1.5  (or /nnr_rd 0 for instant)",
            reply_markup=_back_keyboard(),
        )

    elif data == "nnr_skin":
        skins = personality.available_skins()
        current = personality.get_chat_skin(chat.id)
        await query.edit_message_text(
            f"Skin\n\n"
            f"Current: {current}\n"
            f"Available: {', '.join(skins)}\n\n"
            f"Use: /cpersonality skin <name>",
            reply_markup=_back_keyboard(),
        )

    elif data == "nnr_cfg":
        cfg = personality.get_chat_config_dict(chat.id)
        await query.edit_message_text(
            f"Config\n\n"
            f"Sarcasm: {cfg['sarcasm_level']}\n"
            f"Metaphors: {cfg['metaphor_frequency']}\n"
            f"Deadpan: {'on' if cfg['deadpan'] else 'off'}\n"
            f"Max Length: {cfg['max_response_length']}\n\n"
            f"Use: /cpersonality sarcasm 0.3\n"
            f"     /cpersonality metaphors 0.6\n"
            f"     /cpersonality deadpan off\n"
            f"     /cpersonality max_length 600",
            reply_markup=_back_keyboard(),
        )

    elif data == "nnr_reset":
        # v2.3: Aligned with /dpersonality scope (fix 3.4)
        personality.reset_chat_config(chat.id)
        personality.clear_custom_instructions(chat.id)
        await memory.clear_all_settings(chat.id)
        await query.edit_message_text(
            "All settings reset to defaults. Neko is back to her usual self.",
            reply_markup=_back_keyboard(),
        )


# ─── /nnr_ci — Set Custom Instructions ───────────────────────────────

@safe_handler
async def cmd_nnr_ci(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set or clear custom instructions for this chat."""
    if not await _is_admin(update, context):
        await update.message.reply_text("Admin only.")
        return

    chat_id = update.effective_chat.id

    if not context.args:
        chat_settings = await memory.get_settings(chat_id)
        ci = chat_settings.custom_instructions
        text = (
            "Custom Instructions\n\n"
            f"Current: {ci or 'Not set'}\n\n"
            "Usage:\n"
            "  /nnr_ci be cute and supportive, no sarcasm\n"
            "  /nnr_ci speak like a medieval knight\n"
            "  /nnr_ci be serious and professional\n\n"
            "Available presets: " + ", ".join(instruction_processor.available_presets()) + "\n\n"
            "  /nnr_ci clear  - remove custom instructions"
        )
        await update.message.reply_text(text)
        return

    arg_text = " ".join(context.args)

    if arg_text.lower() == "clear":
        personality.clear_custom_instructions(chat_id)
        await memory.clear_custom_instructions(chat_id)
        await update.message.reply_text("Custom instructions cleared. Back to my usual charming self.")
        return

    # Parse and apply
    traits = personality.set_custom_instructions(chat_id, arg_text)
    await memory.set_custom_instructions(chat_id, arg_text)

    # Report what was detected
    if traits.tone:
        await update.message.reply_text(
            f"Custom instructions set. Detected tone: {traits.tone}.\n"
            f"Sarcasm: {traits.sarcasm_level}, Deadpan: {traits.deadpan}, "
            f"Metaphors: {traits.metaphor_frequency}.\n"
            f"I'll try to follow these. No promises though. I'm testing this too."
        )
    else:
        await update.message.reply_text(
            "Custom instructions set. I'll do my best to follow them.\n"
            "This is experimental - I might not perfectly match what you expect."
        )


# ─── /nnr_ul — Set User Limit ────────────────────────────────────────

@safe_handler
async def cmd_nnr_ul(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set the per-user message limit for this chat."""
    if not await _is_admin(update, context):
        await update.message.reply_text("Admin only.")
        return

    if not context.args:
        current = (await memory.get_settings(update.effective_chat.id)).user_limit
        current_val = current or settings.personality_user_limit_default
        await update.message.reply_text(
            f"User Limit: {current_val}\n"
            f"Usage: /nnr_ul <number> (1-{settings.personality_user_limit_max})"
        )
        return

    try:
        val = int(context.args[0])
        val = max(1, min(settings.personality_user_limit_max, val))
    except ValueError:
        await update.message.reply_text(f"Must be a number between 1 and {settings.personality_user_limit_max}.")
        return

    await memory.set_user_limit(update.effective_chat.id, val)
    await update.message.reply_text(
        f"User limit set to {val}. I'll 'get tired' of someone after {val} messages. "
        f"{'Generous.' if val > 50 else 'Reasonable.' if val > 20 else 'Strict. I like it.'}"
    )


# ─── /nnr_rd — Set Response Delay ────────────────────────────────────

@safe_handler
async def cmd_nnr_rd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set the response delay for this chat."""
    if not await _is_admin(update, context):
        await update.message.reply_text("Admin only.")
        return

    if not context.args:
        current = (await memory.get_settings(update.effective_chat.id)).response_delay
        current_val = current if current is not None else settings.personality_response_delay_default
        await update.message.reply_text(
            f"Response Delay: {current_val}s\n"
            f"Usage: /nnr_rd <seconds> (min {settings.personality_response_delay_min})"
        )
        return

    try:
        val = float(context.args[0])
        val = max(settings.personality_response_delay_min, val)
    except ValueError:
        await update.message.reply_text(f"Must be a number (min {settings.personality_response_delay_min}).")
        return

    await memory.set_response_delay(update.effective_chat.id, val)
    await update.message.reply_text(
        f"Response delay set to {val}s. "
        f"{'I will be zippy.' if val < 1 else 'I will take my time.' if val > 5 else 'Balanced pace.'}"
    )


# ==================== /nnr_test — Preview (v2.3) ====================

@safe_handler
async def cmd_nnr_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """v2.3: Preview what Neko would say without sending to the chat."""
    if not await _is_admin(update, context):
        await update.message.reply_text("Admin only.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /nnr_test <message>\nExample: /nnr_test hello there")
        return

    test_message = " ".join(context.args)
    chat_id = update.effective_chat.id

    trigger_matches = triggers.analyze(test_message)
    intents = [m.intent for m in trigger_matches]

    # Get skin data for subplugins (fix 1.3)
    skin_data = personality._get_skin_for_chat(chat_id)

    plugin_result = await _plugin_manager.route(
        test_message, chat_id, [], intents=intents, skin_data=skin_data
    )

    if plugin_result and plugin_result.response:
        response = await personality.rewrite(chat_id, plugin_result.response, intents, test_message)
    else:
        response = await personality.generate_direct(chat_id, intents, test_message)

    await update.message.reply_text(
        f"Preview for: \"{test_message}\"\n"
        f"Intents: {intents or 'none'}\n"
        f"Skin: {personality.get_chat_skin(chat_id)}\n"
        f"CI: {'yes' if personality.has_custom_instructions(chat_id) else 'no'}\n"
        f"{'-' * 30}\n"
        f"{response}"
    )


# ==================== /nnr_help — Unified Help (v2.3) ===============

@safe_handler
async def cmd_nnr_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """v2.3: Unified command list."""
    lines = [
        "Neko Commands:\n",
        "/nnr_settings - Settings hub (admin)",
        "/nnr_ci [text] - Custom instructions (admin)",
        "/nnr_ul [n] - User message limit (admin)",
        "/nnr_rd [s] - Response delay (admin)",
        "/nnr_test [msg] - Preview personality response (admin)",
        "/cpersonality [key] [val] - Fine-tune personality (admin)",
        "/dpersonality - Reset to defaults (admin)",
        "/personality on|off - Toggle personality (admin)",
        "/personalitystatus - Show stats",
        "/joke [category] - Get a joke",
        "/mystats - Your stats",
        "/addjoke <cat> | <text> - Add joke (owner)",
        "/nnr_help - This message",
    ]
    await update.message.reply_text("\n".join(lines))


# ==================== EXISTING COMMANDS (v2.0/v2.1) ==================

@safe_handler
async def cmd_joke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await memory.save_message(chat_id, "user", "/joke", intent="joke_command")
    category = context.args[0].lower() if context.args else None
    recent_ids = await memory.get_recent_joke_ids(chat_id, limit=15)
    joke = jokes_db.get_random(category, exclude_ids=recent_ids)
    if joke:
        await memory.record_callback(chat_id, joke.id, "forced_joke")
        response = f"Fine. Here's your joke.\n\n{joke.text}\n\nHappy now?"
    else:
        response = "I don't have jokes about that. I'm not a circus."
    await update.message.reply_text(response)
    await memory.save_message(chat_id, "bot", response, intent="joke")


@safe_handler
async def cmd_mystats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    profile = await memory.get_user_profile(chat_id)
    if profile:
        text = (
            f"Your stats? Fine.\n\n"
            f"Interactions: {profile['interaction_count']}\n"
            f"Running gags: {len(profile.get('running_gags', []))}\n\n"
            f"Satisfied? I didn't think so."
        )
    else:
        text = "No stats. You barely exist to me yet."
    await update.message.reply_text(text)


@safe_handler
async def cmd_personality_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not await _is_admin(update, context):
        await update.message.reply_text("Admin only.")
        return
    arg = context.args[0].lower() if context.args else ""
    if arg not in ("on", "off"):
        await update.message.reply_text("Usage: /personality on  or  /personality off")
        return
    async with async_session() as session:
        row = await session.get(Chat, chat.id)
        if row is None:
            row = Chat(id=chat.id, type=chat.type)
            session.add(row)
        row.personality_enabled = arg == "on"
        await session.commit()
    _enabled_cache.pop((chat.id, chat.type), None)
    await update.message.reply_text(f"Personality mode is now {'ON' if arg == 'on' else 'OFF'} here.")


@safe_handler
async def cmd_cpersonality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Customize the personality for this chat."""
    if not await _is_admin(update, context):
        await update.message.reply_text("Admin only.")
        return
    chat = update.effective_chat
    if not context.args:
        skins = personality.available_skins()
        current_skin = personality.get_chat_skin(chat.id)
        cfg = personality.get_chat_config_dict(chat.id)
        ci_traits = personality.get_custom_instructions_traits(chat.id)
        ci_info = f"Custom Instructions: {ci_traits.tone or 'none'}" if ci_traits else "Custom Instructions: none"
        lines = [
            "Customize the personality:\n",
            "  /cpersonality skin <name>       - switch skin",
            "  /cpersonality sarcasm <0.0-1.0> - set sarcasm level",
            "  /cpersonality metaphors <0.0-1.0> - set metaphor frequency",
            "  /cpersonality deadpan <on|off>  - toggle deadpan filter",
            "  /cpersonality max_length <num>   - set max response length\n",
            f"Available skins: {', '.join(skins)}",
            f"Current skin: {current_skin}",
            f"{ci_info}",
            f"Config: sarcasm={cfg['sarcasm_level']}, metaphors={cfg['metaphor_frequency']}, deadpan={cfg['deadpan']}, max_len={cfg['max_response_length']}",
        ]
        await update.message.reply_text("\n".join(lines))
        return

    setting = context.args[0].lower()

    if setting == "skin":
        if len(context.args) < 2:
            await update.message.reply_text(f"Available skins: {', '.join(personality.available_skins())}")
            return
        skin_name = context.args[1].lower()
        if personality.set_chat_skin(chat.id, skin_name):
            await memory.set_skin(chat.id, skin_name)
            await update.message.reply_text(f"Personality skin set to '{skin_name}'. I feel different already.")
        else:
            await update.message.reply_text(f"Skin '{skin_name}' not found. Available: {', '.join(personality.available_skins())}")
        return

    if setting == "sarcasm":
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /cpersonality sarcasm <0.0-1.0>")
            return
        try:
            val = max(0.0, min(1.0, float(context.args[1])))
        except ValueError:
            await update.message.reply_text("Must be a number between 0.0 and 1.0.")
            return
        personality.set_chat_config(chat.id, sarcasm_level=val)
        await memory.set_custom_config(chat.id, personality.get_chat_config_dict(chat.id))
        await update.message.reply_text(f"Sarcasm level set to {val}.")
        return

    if setting == "metaphors":
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /cpersonality metaphors <0.0-1.0>")
            return
        try:
            val = max(0.0, min(1.0, float(context.args[1])))
        except ValueError:
            await update.message.reply_text("Must be a number between 0.0 and 1.0.")
            return
        personality.set_chat_config(chat.id, metaphor_frequency=val)
        await memory.set_custom_config(chat.id, personality.get_chat_config_dict(chat.id))
        await update.message.reply_text(f"Metaphor frequency set to {val}.")
        return

    if setting == "deadpan":
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /cpersonality deadpan <on|off>")
            return
        val = context.args[1].lower() == "on"
        personality.set_chat_config(chat.id, deadpan=val)
        await memory.set_custom_config(chat.id, personality.get_chat_config_dict(chat.id))
        await update.message.reply_text(f"Deadpan filter {'enabled' if val else 'disabled'}.")
        return

    if setting == "max_length":
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /cpersonality max_length <num>")
            return
        try:
            val = max(50, min(4096, int(context.args[1])))
        except ValueError:
            await update.message.reply_text("Must be a number (50-4096).")
            return
        personality.set_chat_config(chat.id, max_response_length=val)
        await memory.set_custom_config(chat.id, personality.get_chat_config_dict(chat.id))
        await update.message.reply_text(f"Max response length set to {val}.")
        return

    await update.message.reply_text("Unknown setting. Use /cpersonality for help.")


@safe_handler
async def cmd_dpersonality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset personality to default settings for this chat."""
    if not await _is_admin(update, context):
        await update.message.reply_text("Admin only.")
        return
    chat_id = update.effective_chat.id
    # v2.3: Aligned with nnr_settings>Reset scope (fix 3.4)
    personality.reset_chat_config(chat_id)
    personality.clear_custom_instructions(chat_id)
    await memory.clear_all_settings(chat_id)
    await update.message.reply_text("Personality reset to default. Back to my charming self.")


@safe_handler
async def cmd_personality_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    stats = await memory.get_stats(chat_id)
    if not stats:
        await update.message.reply_text("No personality data yet.")
        return
    cfg = personality.get_chat_config_dict(chat_id)
    ci = stats.get("custom_instructions")
    ci_display = (ci[:40] + "...") if ci and len(ci) > 40 else (ci or "none")
    lines = [
        f"Personality: {'ON' if stats['enabled'] else 'OFF'}",
        f"Skin: {stats['skin']}",
        f"Custom Instructions: {ci_display}",
        f"User Limit: {stats.get('user_limit') or settings.personality_user_limit_default}",
        f"Response Delay: {stats.get('response_delay') if stats.get('response_delay') is not None else settings.personality_response_delay_default}s",
        f"Config: sarcasm={cfg['sarcasm_level']}, metaphors={cfg['metaphor_frequency']}, deadpan={cfg['deadpan']}, max_len={cfg['max_response_length']}",
        f"Interactions: {stats['interactions']}",
        f"Jokes told: {stats['jokes_told']}",
        f"Running gags: {stats['running_gags']}",
        f"Messages tracked: {stats['messages']}",
    ]
    await update.message.reply_text("\n".join(lines))


@safe_handler
async def cmd_addjoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not settings.is_admin_id(user.id):
        await update.message.reply_text("Admin only.")
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /addjoke <category> | <joke text>\n"
            "Categories: " + ", ".join(jokes_db.get_categories())
        )
        return
    text = " ".join(context.args)
    if "|" not in text:
        await update.message.reply_text("Format: /addjoke <category> | <joke text>")
        return
    parts = text.split("|", 1)
    category = parts[0].strip().lower()
    joke_text = parts[1].strip()
    if not category or not joke_text:
        await update.message.reply_text("Both category and joke text are required.")
        return
    joke_id = f"user_{hashlib.md5(joke_text.encode(), usedforsecurity=False).hexdigest()[:8]}"
    if jokes_db.has_id(joke_id):
        await update.message.reply_text("That joke already exists.")
        return
    joke = Joke(id=joke_id, category=category, text=joke_text)
    if jokes_db.add(joke):
        await update.message.reply_text(f"Joke added (id: {joke_id}, category: {category}). My suffering grows.")
    else:
        await update.message.reply_text("Failed to add joke.")


# ==================== MESSAGE PIPELINE ====================

@safe_handler
def _message_addresses_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True if the user mentioned the bot or replied to one of the bot's messages.

    Used so group chats can talk to Neko via @mention / reply even when
    ambient personality is off for that chat. /ai is the general assistant;
    personality is /chat + mention + reply.
    """
    msg = update.effective_message
    if not msg:
        return False
    # Reply to the bot
    if msg.reply_to_message and msg.reply_to_message.from_user:
        if msg.reply_to_message.from_user.id == context.bot.id:
            return True
    # @username mention or text_mention of the bot
    entities = msg.entities or []
    text = msg.text or ""
    bot_username = (context.bot.username or "").lower()
    for ent in entities:
        if ent.type == "mention" and bot_username:
            mention = text[ent.offset : ent.offset + ent.length].lstrip("@").lower()
            if mention == bot_username:
                return True
        if ent.type == "text_mention" and ent.user and ent.user.id == context.bot.id:
            return True
    return False


def _strip_bot_mention(text: str, bot_username: str) -> str:
    """Remove a leading @botusername so the personality sees the actual question."""
    if not text or not bot_username:
        return text or ""
    import re
    return re.sub(
        rf"^@{re.escape(bot_username)}\b[:,]?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip() or text


@safe_handler
async def _run_personality_pipeline(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    force: bool = False,
    skip_cooldown: bool = False,
) -> None:
    """Shared personality generate + send path.

    force=True: used by /chat — always runs when the feature is globally
    enabled (ignores per-chat ambient toggle). Ambient handle_message
    still respects per-chat enable + mention/reply gates.
    """
    if not settings.enable_personality or not update.message:
        return
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user or not text:
        return

    if not skip_cooldown and _on_cooldown(user.id, chat.id):
        return

    if not force:
        enabled = await _is_enabled_for(chat.id, chat.type)
        addressed = _message_addresses_bot(update, context)
        # Ambient: enabled chats always; groups may also fire on @mention/reply
        # even when ambient personality is off for that chat.
        if not enabled and not addressed:
            return
        if not enabled and addressed:
            # Mention/reply path — treat as intentional Neko chat
            pass

    try:
        within_limit = await memory.increment_and_check(chat.id, user.id)
    except Exception as e:
        logger.error("DB error in increment_and_check: %s — falling back to allow", e)
        within_limit = True

    if not within_limit:
        if random.random() < 0.05:
            tired_responses = [
                "I'm tired. Give me a break.",
                "You talk a lot. I need coffee.",
                "My patience has a cooldown and you exceeded it.",
                "...can we pick this up later? I'm buffering.",
            ]
            await update.message.reply_text(random.choice(tired_responses))
        return

    try:
        await memory.update_profile(chat.id, username=user.username, chat_type=chat.type)
    except Exception as e:
        logger.warning("DB error in update_profile, continuing: %s", e)

    trigger_matches = triggers.analyze(text)
    intents = [m.intent for m in trigger_matches]
    primary_intent = trigger_matches[0] if trigger_matches else None

    try:
        await memory.save_message(
            chat.id, "user", text,
            intent=primary_intent.intent if primary_intent else None,
        )
    except Exception as e:
        logger.warning("DB error in save_message, continuing: %s", e)

    try:
        skin_data = personality._get_skin_for_chat(chat.id)
        plugin_result = await _plugin_manager.route(
            text, chat.id, [], intents=intents, skin_data=skin_data
        )
        if plugin_result and plugin_result.response:
            response = await personality.rewrite(chat.id, plugin_result.response, intents, text)
        else:
            response = await personality.generate_direct(chat.id, intents, text)
    except Exception as e:
        logger.error("personality_generate_failed: %s", e, exc_info=True)
        return

    if not response or not str(response).strip():
        return

    delay = 0.0 if force else await _get_response_delay(chat.id)
    if delay > 0 and hasattr(context, "job_queue") and context.job_queue:
        chat_id = chat.id
        reply_to_id = update.message.message_id

        async def _delayed_send(ctx: ContextTypes.DEFAULT_TYPE):
            try:
                await ctx.bot.send_message(
                    chat_id=chat_id, text=response,
                    reply_to_message_id=reply_to_id,
                )
            except Exception as e:
                logger.error("Delayed send failed: %s", e)

        context.job_queue.run_once(_delayed_send, when=delay)
    else:
        await update.message.reply_text(response)

    try:
        await memory.save_message(
            chat.id, "bot", response,
            intent=primary_intent.intent if primary_intent else "general",
        )
    except Exception as e:
        logger.warning("DB error in save_message (bot), continuing: %s", e)

    if primary_intent and random.random() < 0.1:
        try:
            await memory.update_profile(chat.id, add_gag=primary_intent.intent)
        except Exception as e:
            logger.warning("DB error in update_profile (gag), continuing: %s", e)

    logger.debug(
        "personality_fired",
        chat_id=chat.id, user_id=user.id,
        intent=primary_intent.intent if primary_intent else None,
        skin=personality.get_chat_skin(chat.id),
        custom_instructions=personality.has_custom_instructions(chat.id),
        response_length=len(response),
        force=force,
    )


@safe_handler
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ambient personality: DMs when enabled, groups when enabled or @mention/reply."""
    if not settings.enable_personality or not update.message or not update.message.text:
        return
    text = update.message.text
    bot_username = context.bot.username or ""
    if _message_addresses_bot(update, context):
        text = _strip_bot_mention(text, bot_username)
    await _run_personality_pipeline(update, context, text, force=False)


@safe_handler
async def cmd_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Explicit personality chat — Neko/Nanora voice (not the generic /ai assistant).

    Usage: /chat <message>  or  reply to a message with /chat
    """
    if not settings.enable_personality:
        await update.message.reply_text("Personality is currently disabled.")
        return
    text = " ".join(context.args) if context.args else None
    if not text and update.message.reply_to_message:
        text = update.message.reply_to_message.text or update.message.reply_to_message.caption
    if not text:
        await update.message.reply_text(
            "Usage: /chat <message> or reply to a message with /chat\n"
            "(For the general AI assistant, use /ai.)"
        )
        return
    await _run_personality_pipeline(
        update, context, text, force=True, skip_cooldown=True
    )


# ==================== v2.3: Startup + Cleanup Jobs ====================

async def on_startup(app):
    """v2.3+: Called on bot startup. Loads persisted settings, few-shot bank, and schedules cleanup."""
    try:
        await personality.load_persisted_settings()
    except Exception as e:
        logger.error("Failed to load persisted settings: %s", e)
    try:
        from bot.personality.examples import example_bank
        example_bank.load()
        logger.info("Few-shot bank ready", count=example_bank.count)
    except Exception as e:
        logger.warning("Few-shot bank load skipped: %s", e)

    # Schedule daily cleanup job
    if hasattr(app, 'job_queue') and app.job_queue:
        async def _daily_cleanup(ctx: ContextTypes.DEFAULT_TYPE):
            try:
                deleted = await memory.cleanup_old_data(days=settings.personality_cleanup_days)
                logger.info("Daily cleanup: deleted %d rows", deleted)
            except Exception as e:
                logger.error("Daily cleanup failed: %s", e)

        app.job_queue.run_daily(_daily_cleanup, time=dt.time(hour=3, minute=0))
        logger.info("Scheduled daily personality data cleanup at 03:00")



# ═══════════════════════════════════════════════════════════════════
# SUPEROWNER persona management — skins, presets, response data
# OWNER_ID only (superowner). Supports single-line and multi-line
# (reply-to-message) bulk data injection.
# ═══════════════════════════════════════════════════════════════════

_PERSONA_ADMIN_HELP = """\
<b>👑 Persona admin</b> (superowner only)

<b>List / inspect</b>
/plist — list all skins + presets
/pshow skin|preset &lt;name&gt; — show JSON summary
/preload — reload skins &amp; presets from disk

<b>Skins</b>
/padd skin &lt;name&gt; [description…] — create minimal skin
/pdel skin &lt;name&gt; — delete (neko/nanora protected)
/pmerge skin &lt;source&gt; &lt;target&gt; — merge source → target
/pset skin &lt;name&gt; &lt;field&gt; &lt;value&gt; — set scalar field
  fields: description, persona_context, sarcasm_level, …

<b>Presets</b>
/padd preset &lt;name&gt; [description…]
/pdel preset &lt;name&gt;
/pmerge preset &lt;source&gt; &lt;target&gt;
/pset preset &lt;name&gt; &lt;field&gt; &lt;value&gt;

<b>Response data</b>
/pdata &lt;skin&gt; &lt;category&gt; &lt;line…&gt; — append one direct_response line
/pdata &lt;skin&gt; &lt;category&gt; — reply with multiple lines (one per line)
/pmeta &lt;skin&gt; &lt;category&gt; &lt;metaphor…&gt; — append metaphor
/pjson skin|preset &lt;name&gt; — reply with full JSON body to replace

Categories for /pdata: general, greeting, thanks, confusion, …
"""


def _personality_layer():
    from bot.personality.personality import personality
    return personality


def _instruction_processor():
    from bot.personality.custom_instructions import instruction_processor
    return instruction_processor


def _parse_float(s: str):
    try:
        return float(s)
    except ValueError:
        return s


@owner_only
@safe_handler
async def cmd_plist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    layer = _personality_layer()
    proc = _instruction_processor()
    skins = layer.available_skins()
    presets = proc.available_presets()
    text = (
        f"<b>🎭 Skins ({len(skins)})</b>\n<code>"
        + ", ".join(skins)
        + f"</code>\n\n<b>🎛️ Presets ({len(presets)})</b>\n<code>"
        + ", ".join(presets)
        + "</code>"
    )
    # Telegram 4096 limit — chunk if needed
    if len(text) > 4000:
        await update.message.reply_text(
            f"<b>🎭 Skins ({len(skins)})</b>\n<code>" + ", ".join(skins) + "</code>",
            parse_mode="HTML",
        )
        await update.message.reply_text(
            f"<b>🎛️ Presets ({len(presets)})</b>\n<code>" + ", ".join(presets) + "</code>",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(text, parse_mode="HTML")


@owner_only
@safe_handler
async def cmd_pshow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if len(args) < 2 or args[0] not in ("skin", "preset"):
        await update.message.reply_text("Usage: /pshow skin|preset <name>")
        return
    kind, name = args[0], args[1].lower()
    if kind == "skin":
        data = _personality_layer()._skin_loader.get_raw(name)
    else:
        data = _instruction_processor().get_preset(name)
    if not data:
        await update.message.reply_text(f"❌ {kind} <code>{name}</code> not found.", parse_mode="HTML")
        return
    import json as _json
    blob = _json.dumps(data, indent=2, ensure_ascii=False)
    if len(blob) > 3500:
        blob = blob[:3500] + "\n… (truncated)"
    await update.message.reply_text(f"<b>{kind} {name}</b>\n<pre>{blob}</pre>", parse_mode="HTML")


@owner_only
@safe_handler
async def cmd_preload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    layer = _personality_layer()
    layer.reload_skins()
    layer.reload_presets()
    _instruction_processor().reload_presets()
    await update.message.reply_text(
        f"✅ Reloaded. Skins: {len(layer.available_skins())} · "
        f"Presets: {len(_instruction_processor().available_presets())}"
    )


@owner_only
@safe_handler
async def cmd_padd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if len(args) < 2 or args[0] not in ("skin", "preset"):
        await update.message.reply_text("Usage: /padd skin|preset <name> [description…]")
        return
    kind, name = args[0], args[1].lower()
    desc = " ".join(args[2:]) or f"Custom {kind} '{name}'"
    if kind == "skin":
        data = {
            "name": name,
            "description": desc,
            "default_config": {
                "sarcasm_level": 0.5, "metaphor_frequency": 0.4, "exaggeration_level": 0.3,
                "deadpan": False, "programmer_humor": 0.2, "anime_references": 0.2,
                "max_response_length": 400,
            },
            "metaphors": {"general": []},
            "direct_responses": {"general": ["Okay."], "greeting": ["Hi."]},
            "persona_context": desc,
        }
        _personality_layer()._skin_loader.save(name, data, overwrite=False)
        # matching preset for keyword detection
        _instruction_processor().save_preset(name, {
            "keywords": [name],
            "sarcasm_level": 0.5, "metaphor_frequency": 0.4, "exaggeration_level": 0.3,
            "deadpan": False, "deadpan_safe": False, "programmer_humor": 0.2,
            "anime_references": 0.2, "max_response_length": 400,
            "persona_context": desc, "themed_openers": ["Okay."], "themed_closers": [],
            "themed_prefixes": [], "themed_metaphors": {},
        }, overwrite=True)
    else:
        _instruction_processor().save_preset(name, {
            "keywords": [name],
            "sarcasm_level": 0.5, "metaphor_frequency": 0.4, "exaggeration_level": 0.3,
            "deadpan": False, "deadpan_safe": False, "programmer_humor": 0.2,
            "anime_references": 0.2, "max_response_length": 400,
            "persona_context": desc, "themed_openers": ["Okay."], "themed_closers": [],
            "themed_prefixes": [], "themed_metaphors": {},
        }, overwrite=False)
    await update.message.reply_text(f"✅ Created {kind} <code>{name}</code>", parse_mode="HTML")


@owner_only
@safe_handler
async def cmd_pdel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if len(args) < 2 or args[0] not in ("skin", "preset"):
        await update.message.reply_text("Usage: /pdel skin|preset <name>")
        return
    kind, name = args[0], args[1].lower()
    try:
        if kind == "skin":
            ok = _personality_layer()._skin_loader.delete(name)
        else:
            ok = _instruction_processor().delete_preset(name)
    except PermissionError as e:
        await update.message.reply_text(f"🚫 {e}")
        return
    await update.message.reply_text("✅ Deleted." if ok else "❌ Not found.")


@owner_only
@safe_handler
async def cmd_pmerge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if len(args) < 3 or args[0] not in ("skin", "preset"):
        await update.message.reply_text("Usage: /pmerge skin|preset <source> <target>")
        return
    kind, src, tgt = args[0], args[1].lower(), args[2].lower()
    try:
        if kind == "skin":
            _personality_layer()._skin_loader.merge(src, tgt)
        else:
            _instruction_processor().merge_presets(src, tgt)
    except FileNotFoundError as e:
        await update.message.reply_text(f"❌ Missing: {e}")
        return
    await update.message.reply_text(f"✅ Merged {kind} <code>{src}</code> → <code>{tgt}</code>", parse_mode="HTML")


@owner_only
@safe_handler
async def cmd_pset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if len(args) < 4 or args[0] not in ("skin", "preset"):
        await update.message.reply_text("Usage: /pset skin|preset <name> <field> <value…>")
        return
    kind, name, field = args[0], args[1].lower(), args[2]
    value: object = " ".join(args[3:])
    # numeric fields
    if field in {
        "sarcasm_level", "metaphor_frequency", "exaggeration_level",
        "programmer_humor", "anime_references", "max_response_length",
    }:
        try:
            value = float(value) if field != "max_response_length" else int(float(str(value)))
        except ValueError:
            await update.message.reply_text("❌ Expected a number for that field.")
            return
    if field in {"deadpan", "deadpan_safe"}:
        value = str(value).lower() in {"1", "true", "yes", "on"}

    if kind == "skin":
        data = _personality_layer()._skin_loader.get_raw(name)
        if not data:
            await update.message.reply_text("❌ Skin not found.")
            return
        if field in data.get("default_config", {}):
            data.setdefault("default_config", {})[field] = value
        else:
            data[field] = value
        _personality_layer()._skin_loader.save(name, data, overwrite=True)
    else:
        data = _instruction_processor().get_preset(name)
        if not data:
            await update.message.reply_text("❌ Preset not found.")
            return
        data[field] = value
        _instruction_processor().save_preset(name, data, overwrite=True)
    await update.message.reply_text(f"✅ Set {kind} <code>{name}</code>.{field}", parse_mode="HTML")


@owner_only
@safe_handler
async def cmd_pdata(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Append direct_response lines to a skin. Bulk via reply body."""
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage:\n/pdata <skin> <category> <line…>\n"
            "Or reply to a multi-line message with: /pdata <skin> <category>"
        )
        return
    name, category = args[0].lower(), args[1].lower()
    lines = []
    if len(args) > 2:
        lines.append(" ".join(args[2:]))
    if update.message.reply_to_message and update.message.reply_to_message.text:
        lines.extend(
            ln.strip() for ln in update.message.reply_to_message.text.splitlines() if ln.strip()
        )
    if not lines:
        await update.message.reply_text("❌ Provide a line or reply to a multi-line message.")
        return
    loader = _personality_layer()._skin_loader
    data = loader.get_raw(name)
    if not data:
        await update.message.reply_text("❌ Skin not found.")
        return
    dr = data.setdefault("direct_responses", {})
    bucket = dr.setdefault(category, [])
    for ln in lines:
        if ln not in bucket:
            bucket.append(ln)
    loader.save(name, data, overwrite=True)
    await update.message.reply_text(
        f"✅ Added {len(lines)} line(s) to <code>{name}</code>.direct_responses.{category} "
        f"(now {len(bucket)})",
        parse_mode="HTML",
    )


@owner_only
@safe_handler
async def cmd_pmeta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if len(args) < 3:
        await update.message.reply_text("Usage: /pmeta <skin> <category> <metaphor…>")
        return
    name, category = args[0].lower(), args[1].lower()
    metaphor = " ".join(args[2:])
    loader = _personality_layer()._skin_loader
    data = loader.get_raw(name)
    if not data:
        await update.message.reply_text("❌ Skin not found.")
        return
    metas = data.setdefault("metaphors", {})
    bucket = metas.setdefault(category, [])
    if metaphor not in bucket:
        bucket.append(metaphor)
    loader.save(name, data, overwrite=True)
    await update.message.reply_text(f"✅ Metaphor added to <code>{name}</code>.{category}", parse_mode="HTML")


@owner_only
@safe_handler
async def cmd_pjson(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Replace entire skin/preset JSON from a replied message body."""
    args = context.args or []
    if len(args) < 2 or args[0] not in ("skin", "preset"):
        await update.message.reply_text(
            "Usage: reply to a JSON message with /pjson skin|preset <name>"
        )
        return
    kind, name = args[0], args[1].lower()
    body = update.message.reply_to_message.text if update.message.reply_to_message else None
    if not body:
        await update.message.reply_text("❌ Reply to a message that contains JSON.")
        return
    import json as _json
    try:
        data = _json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("root must be an object")
    except Exception as e:
        await update.message.reply_text(f"❌ Invalid JSON: {e}")
        return
    if kind == "skin":
        data.setdefault("name", name)
        _personality_layer()._skin_loader.save(name, data, overwrite=True)
    else:
        _instruction_processor().save_preset(name, data, overwrite=True)
    await update.message.reply_text(f"✅ Replaced {kind} <code>{name}</code> from JSON.", parse_mode="HTML")


@owner_only
@safe_handler
async def cmd_padmin_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(_PERSONA_ADMIN_HELP, parse_mode="HTML")


def register(app):
    """Register all personality handlers with the Telegram app."""
    if not settings.enable_personality:
        return

    # v2.3: Main settings hub
    app.add_handler(CommandHandler("nnr_settings", cmd_nnr_settings))
    app.add_handler(CallbackQueryHandler(cmd_nnr_settings_callback, pattern="^nnr_"))

    # v2.2: Direct setting commands
    app.add_handler(CommandHandler("nnr_ci", cmd_nnr_ci))
    app.add_handler(CommandHandler("nnr_ul", cmd_nnr_ul))
    app.add_handler(CommandHandler("nnr_rd", cmd_nnr_rd))

    # v2.3: New commands
    app.add_handler(CommandHandler("nnr_test", cmd_nnr_test))
    app.add_handler(CommandHandler("nnr_help", cmd_nnr_help))

    # v2.0/v2.1 commands
    app.add_handler(CommandHandler("joke", cmd_joke))
    app.add_handler(CommandHandler("mystats", cmd_mystats))
    app.add_handler(CommandHandler("personality", cmd_personality_toggle))
    app.add_handler(CommandHandler("cpersonality", cmd_cpersonality))
    app.add_handler(CommandHandler("dpersonality", cmd_dpersonality))
    app.add_handler(CommandHandler("personalitystatus", cmd_personality_status))
    app.add_handler(CommandHandler("addjoke", cmd_addjoke))

    # /chat → Neko personality (distinct from /ai general assistant)
    app.add_handler(CommandHandler("chat", cmd_chat))

    # Superowner persona management
    app.add_handler(CommandHandler("plist", cmd_plist))
    app.add_handler(CommandHandler("pshow", cmd_pshow))
    app.add_handler(CommandHandler("preload", cmd_preload))
    app.add_handler(CommandHandler("padd", cmd_padd))
    app.add_handler(CommandHandler("pdel", cmd_pdel))
    app.add_handler(CommandHandler("pmerge", cmd_pmerge))
    app.add_handler(CommandHandler("pset", cmd_pset))
    app.add_handler(CommandHandler("pdata", cmd_pdata))
    app.add_handler(CommandHandler("pmeta", cmd_pmeta))
    app.add_handler(CommandHandler("pjson", cmd_pjson))
    app.add_handler(CommandHandler(["padmin", "personaadmin"], cmd_padmin_help))

    # Ambient personality: DMs + groups (mention/reply or chat-enabled)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
