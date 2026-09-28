"""
SQLAlchemy 2.0 Async ORM — the single database backing every plugin:
group management, moderation, AI memory, the personality layer, font
preferences, and the live-music queue.

Defaults to a local SQLite file (aiosqlite) so the bot runs with zero
external services. Set DATABASE_URL to a postgresql+asyncpg:// URL for
production; connection-pool arguments are only applied for Postgres,
since SQLite doesn't support them.
"""
from __future__ import annotations

from typing import AsyncGenerator

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Float, ForeignKey, Index, Integer,
    JSON, String, Text, UniqueConstraint, event, func,
)
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.pool import NullPool

from bot.config import settings
from bot.utils.logger import get_logger
from bot.utils.retry import with_retry

logger = get_logger(__name__)

_is_sqlite = settings.database_url.startswith("sqlite")

_engine_kwargs: dict = {"echo": settings.debug, "pool_pre_ping": not _is_sqlite}
if _is_sqlite:
    # SQLite allows only one writer at a time; without WAL + a busy
    # timeout, a second concurrent write doesn't queue — it fails
    # immediately with "database is locked" (visible under any real
    # concurrent load, e.g. an active group chat, not just tests).
    # Postgres isn't affected: it handles concurrent writers natively.
    _engine_kwargs["connect_args"] = {"timeout": 15}
    # NullPool: open a fresh connection per checkout instead of pooling
    # (cheap for a local SQLite file, unlike a networked Postgres). This
    # avoids aiosqlite's pooled-connection-tied-to-the-event-loop-that-
    # created-it problem, which otherwise surfaces as a spurious
    # "bound to a different event loop" error any time the engine
    # outlives the loop that first used it (e.g. across test cases, or
    # a reconnect cycle).
    _engine_kwargs["poolclass"] = NullPool
else:
    _engine_kwargs.update(pool_size=20, max_overflow=10)

engine = create_async_engine(settings.database_url, **_engine_kwargs)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

if _is_sqlite:
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()


class TimestampMixin:
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


# ─── Users & Chats ──────────────────────────────────────────────────
class User(Base, TimestampMixin):
    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True)
    username = Column(String(32), index=True)
    first_name = Column(String(64))
    last_name = Column(String(64))
    language = Column(String(5), default="en")
    is_bot = Column(Boolean, default=False)
    warns = Column(Integer, default=0)
    karma = Column(Integer, default=0)
    afk_reason = Column(Text, nullable=True)
    afk_since = Column(DateTime, nullable=True)
    notes = Column(JSON, default=dict)
    # Name/username change history — [{"field", "old", "new", "at"}, ...],
    # newest last. Populated by bot/middleware/name_history.py.
    name_history = Column(JSON, default=list)

    # AI memory (the /ai /chat /code /summarize plugin)
    ai_persona = Column(Text, nullable=True)
    ai_history = Column(JSON, default=list)

    # Font plugin (formerly font_bot_ultimate.py)
    default_font = Column(String(8), nullable=True)
    font_auto_delete = Column(Boolean, default=False)
    font_show_preview = Column(Boolean, default=True)


class Chat(Base, TimestampMixin):
    __tablename__ = "chats"

    id = Column(BigInteger, primary_key=True)
    title = Column(String(128))
    type = Column(String(20))  # private, group, supergroup, channel
    username = Column(String(32), nullable=True)

    # Settings
    welcome_enabled = Column(Boolean, default=True)
    welcome_text = Column(Text, nullable=True)
    welcome_media = Column(String(512), nullable=True)
    goodbye_enabled = Column(Boolean, default=False)
    goodbye_text = Column(Text, nullable=True)

    # Security
    antiflood_enabled = Column(Boolean, default=True)
    antiflood_limit = Column(Integer, default=5)
    captcha_enabled = Column(Boolean, default=False)
    captcha_mode = Column(String(20), default="button")  # button, math, image
    reactions_enabled = Column(Boolean, default=False)

    # Locks
    lock_url = Column(Boolean, default=False)
    lock_forward = Column(Boolean, default=False)
    lock_photo = Column(Boolean, default=False)
    lock_video = Column(Boolean, default=False)
    lock_sticker = Column(Boolean, default=False)
    lock_gif = Column(Boolean, default=False)
    lock_contact = Column(Boolean, default=False)
    lock_location = Column(Boolean, default=False)

    # Filters
    blacklist_words = Column(JSON, default=list)
    blacklist_action = Column(String(20), default="warn")  # warn, mute, kick, ban

    # Logging
    log_channel = Column(BigInteger, nullable=True)

    # Nightmode
    nightmode_enabled = Column(Boolean, default=False)
    nightmode_start = Column(String(5), default="23:00")
    nightmode_end = Column(String(5), default="06:00")
    nightmode_lock = Column(Boolean, default=True)

    # Federation
    federation_id = Column(String(36), ForeignKey("federations.id"), nullable=True)
    fed_admin = Column(Boolean, default=False)

    # Per-chat disabled commands (from FallenRobot/YaeMiko's /disable)
    disabled_commands = Column(JSON, default=list)

    # Misc
    rules = Column(Text, nullable=True)
    filters = Column(JSON, default=dict)  # trigger -> response
    pinned_message = Column(BigInteger, nullable=True)

    # Personality layer (formerly nanora_bot) — tracked per-conversation,
    # same as the original standalone bot tracked it per chat_id.
    personality_enabled = Column(Boolean, nullable=True)  # None = use settings default
    personality_interactions = Column(Integer, default=0)
    personality_running_gags = Column(JSON, default=list)
    # Free-text persona description parsed by custom_instructions.py
    # (e.g. "be sarcastic and obsessed with pineapple pizza").
    custom_instructions = Column(Text, nullable=True)
    # Named response-pool skin (see data/personality/skins/*.json).
    # None = settings.personality_default_skin.
    personality_skin = Column(String(32), nullable=True)
    # Parsed CustomPersonalityTraits, cached as JSON so it survives a
    # restart without re-parsing custom_instructions every time.
    personality_custom_config = Column(JSON, nullable=True)
    # Per-chat override of settings.personality_user_limit_default.
    personality_user_limit = Column(Integer, nullable=True)
    # Per-chat override of settings.personality_response_delay_default,
    # in seconds.
    personality_response_delay = Column(Float, nullable=True)

    # Auto-moderation
    warn_limit = Column(Integer, nullable=True)  # None = use settings default_warn_limit
    warn_action = Column(String(10), nullable=True)  # None = use settings default_warn_action
    antiraid_enabled = Column(Boolean, default=False)
    antiraid_join_threshold = Column(Integer, nullable=True)
    antiraid_join_window = Column(Integer, nullable=True)
    lockdown_until = Column(DateTime, nullable=True)

    # Bot-wide access control (from YukkiMusicBot's blacklist/private-mode)
    # bot_blacklisted: this specific chat is refused entirely, regardless
    # of PRIVATE_BOT_MODE. private_mode_authorized: when the *global*
    # settings.private_bot_mode is on, only chats with this set to True
    # get served — distinct from bot_blacklisted, which is a deny-list
    # that applies either way.
    bot_blacklisted = Column(Boolean, default=False)
    private_mode_authorized = Column(Boolean, default=False)

    # Play preferences (from YukkiMusicBot's /playmode)
    playmode = Column(String(10), default="direct")  # direct | inline
    play_admin_only = Column(Boolean, default=False)  # only admins may /play
    cleanmode_enabled = Column(Boolean, default=False)
    video_enabled = Column(Boolean, default=False)  # opt-in: /play sends video, not just audio
    # Off by default so existing chats see no behavior change — once on,
    # /skip /stop /pause /resume /shuffle /repeat /effects /volume need
    # a chat admin or an explicit /auth user (see plugins/music_live.py).
    restrict_music_controls = Column(Boolean, default=False)

    # Character-collector game (from anime_collector_bot / anime_catcher_bot)
    # — how many chat messages between ambient spawns. Per-chat since
    # activity level varies enormously between groups.
    collector_spawn_rate = Column(Integer, nullable=True)  # None = settings default


class ChatMember(Base, TimestampMixin):
    __tablename__ = "chat_members"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"))
    user_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    role = Column(String(20), default="member")  # member, admin, owner
    custom_title = Column(String(128), nullable=True)
    until_date = Column(DateTime, nullable=True)

    # Economy & leveling — per-chat by design (a user's level/coins in one
    # group shouldn't leak into another the way a global column would).
    xp = Column(Integer, default=0)
    level = Column(Integer, default=0)
    coins = Column(Integer, default=100)
    last_xp_at = Column(DateTime, nullable=True)
    last_daily_at = Column(DateTime, nullable=True)

    # Auth users (from YukkiMusicBot) — non-admins an admin has explicitly
    # allowed to control playback (skip/pause/etc.) without full admin
    # rights. Orthogonal to `role`, since an auth user is still a member.
    is_auth_user = Column(Boolean, default=False)

    # Personality layer — messages received within the current reset
    # window, for the per-user reply limit (see memory.py:increment_and_check).
    personality_msg_count = Column(Integer, default=0)
    personality_msg_count_reset_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("chat_id", "user_id", name="uq_chat_user"),
        Index("idx_chatmember_chat_xp", "chat_id", "xp"),
    )


# ─── Moderation ─────────────────────────────────────────────────────
class Warn(Base, TimestampMixin):
    __tablename__ = "warns"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"))
    user_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    reason = Column(Text)
    warned_by = Column(BigInteger)

    __table_args__ = (Index("idx_warns_chat_user", "chat_id", "user_id"),)


class Ban(Base, TimestampMixin):
    __tablename__ = "bans"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"))
    user_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    reason = Column(Text)
    banned_by = Column(BigInteger)
    until = Column(DateTime, nullable=True)
    is_fed_ban = Column(Boolean, default=False)


class Mute(Base, TimestampMixin):
    __tablename__ = "mutes"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"))
    user_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    until = Column(DateTime)
    reason = Column(Text)


class Report(Base, TimestampMixin):
    __tablename__ = "reports"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger)
    message_id = Column(BigInteger)
    reporter_id = Column(BigInteger)
    reported_user_id = Column(BigInteger)
    reason = Column(Text)
    resolved = Column(Boolean, default=False)


# ─── Federations ────────────────────────────────────────────────────
class Federation(Base, TimestampMixin):
    __tablename__ = "federations"

    id = Column(String(36), primary_key=True)
    name = Column(String(64))
    owner_id = Column(BigInteger, ForeignKey("users.id"))
    admins = Column(JSON, default=list)
    log_channel = Column(BigInteger, nullable=True)
    bans = relationship("FedBan", back_populates="federation", cascade="all, delete-orphan")


class FedBan(Base, TimestampMixin):
    __tablename__ = "fed_bans"

    id = Column(Integer, primary_key=True)
    federation_id = Column(String(36), ForeignKey("federations.id", ondelete="CASCADE"))
    user_id = Column(BigInteger)
    reason = Column(Text)
    banned_by = Column(BigInteger)

    federation = relationship("Federation", back_populates="bans")


# ─── Anti-Spam & Analytics ──────────────────────────────────────────
class SpamLog(Base, TimestampMixin):
    __tablename__ = "spam_logs"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger)
    user_id = Column(BigInteger)
    message_text = Column(Text, nullable=True)
    confidence = Column(Float)
    action_taken = Column(String(20))
    features = Column(JSON, default=dict)


class MessageStat(Base, TimestampMixin):
    __tablename__ = "message_stats"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, index=True)
    user_id = Column(BigInteger, index=True)
    date = Column(DateTime, server_default=func.now())
    count = Column(Integer, default=1)

    __table_args__ = (Index("idx_stats_chat_date", "chat_id", "date"),)


# ─── Notes & Tags ───────────────────────────────────────────────────
class Note(Base, TimestampMixin):
    __tablename__ = "notes"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"))
    name = Column(String(64), index=True)
    content = Column(Text)
    file_id = Column(String(512), nullable=True)
    message_type = Column(String(20), default="text")  # text, photo, video, audio, document
    created_by = Column(BigInteger)


# ─── AI Conversations (the LLM plugin's own history) ───────────────
class AIConversation(Base, TimestampMixin):
    __tablename__ = "ai_conversations"

    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, index=True)
    chat_id = Column(BigInteger, nullable=True)
    role = Column(String(20))  # user, assistant, system
    content = Column(Text)
    tokens = Column(Integer, default=0)
    model = Column(String(32))


# ─── Personality layer (formerly nanora_bot's own SQLite file) ─────
# Kept separate from AIConversation above: this backs the trigger/banter
# pipeline (intent tracking, running gags, joke callbacks), which is a
# different concern from the LLM plugin's conversation history.
class PersonalityMessage(Base, TimestampMixin):
    __tablename__ = "personality_messages"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, index=True)
    role = Column(String(10))  # user, bot
    content = Column(Text)
    intent = Column(String(32), nullable=True)


class PersonalityCallback(Base, TimestampMixin):
    __tablename__ = "personality_callbacks"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, index=True)
    joke_id = Column(String(32))
    context = Column(Text, nullable=True)

    __table_args__ = (
        # Speeds up "has this joke already been told in this chat
        # recently" lookups, which filter on all three columns together.
        Index("idx_pcb_chat_joke_time", "chat_id", "joke_id", "created_at"),
    )


# ─── Live music queue (formerly harmony's Mongo/Redis-backed queues) ─
# One row per chat; `data` is the JSON dump of a bot.models.queue.Queue.
# Replaces harmony's hard dependency on a separate MongoDB instance.
class MusicQueueState(Base, TimestampMixin):
    __tablename__ = "music_queues"

    chat_id = Column(BigInteger, primary_key=True)
    data = Column(JSON, default=dict)


# ─── Economy & Leveling ──────────────────────────────────────────────
class ShopItem(Base, TimestampMixin):
    __tablename__ = "shop_items"

    id = Column(Integer, primary_key=True)
    key = Column(String(32), unique=True)
    name = Column(String(64))
    description = Column(Text)
    price = Column(Integer)
    emoji = Column(String(8), default="🎁")


class UserInventory(Base, TimestampMixin):
    __tablename__ = "user_inventory"

    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    item_key = Column(String(32))
    quantity = Column(Integer, default=1)

    __table_args__ = (UniqueConstraint("user_id", "item_key", name="uq_user_item"),)


# ─── Reminders & Scheduling ─────────────────────────────────────────
class Reminder(Base, TimestampMixin):
    __tablename__ = "reminders"

    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, index=True)
    chat_id = Column(BigInteger)
    text = Column(Text)
    remind_at = Column(DateTime, index=True)
    fired = Column(Boolean, default=False)


class ScheduledMessage(Base, TimestampMixin):
    __tablename__ = "scheduled_messages"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, index=True)
    text = Column(Text)
    send_at = Column(DateTime, index=True)
    created_by = Column(BigInteger)
    sent = Column(Boolean, default=False)


# ─── Music Playlists ────────────────────────────────────────────────
class Playlist(Base, TimestampMixin):
    __tablename__ = "playlists"

    id = Column(Integer, primary_key=True)
    owner_id = Column(BigInteger, index=True)
    name = Column(String(64))

    tracks = relationship(
        "PlaylistTrack", back_populates="playlist",
        cascade="all, delete-orphan", order_by="PlaylistTrack.position",
    )

    __table_args__ = (UniqueConstraint("owner_id", "name", name="uq_owner_playlist_name"),)


class PlaylistTrack(Base, TimestampMixin):
    __tablename__ = "playlist_tracks"

    id = Column(Integer, primary_key=True)
    playlist_id = Column(Integer, ForeignKey("playlists.id", ondelete="CASCADE"))
    position = Column(Integer, default=0)
    track_data = Column(JSON)  # a bot.models.track.Track, JSON-dumped

    playlist = relationship("Playlist", back_populates="tracks")


# ─── Precise temp-mute/temp-ban expiry ──────────────────────────────
# Telegram's own `until_date` on restrict/ban calls handles the actual
# enforcement, but doesn't give us a queryable "what's currently muted"
# list or a way to react precisely when something expires. This table
# plus a JobQueue entry (re-armed on startup, see core/bot.py) covers
# both.
class TempAction(Base, TimestampMixin):
    __tablename__ = "temp_actions"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, index=True)
    user_id = Column(BigInteger, index=True)
    action = Column(String(10))  # mute, ban
    reason = Column(Text, nullable=True)
    expires_at = Column(DateTime, index=True)
    reversed = Column(Boolean, default=False)

    __table_args__ = (Index("idx_tempaction_lookup", "chat_id", "action", "reversed", "expires_at"),)


# ─── Dashboard heartbeat ─────────────────────────────────────────────
# The dashboard is a separate process (see dashboard/) with no direct
# access into the bot's memory, so the bot periodically writes a status
# snapshot here for it to read. See bot/plugins/automod.py's job and
# bot/core/bot.py's post_init.
class BotStats(Base, TimestampMixin):
    __tablename__ = "bot_stats"

    id = Column(Integer, primary_key=True)
    started_at = Column(DateTime, nullable=True)
    last_heartbeat = Column(DateTime, nullable=True)
    total_chats = Column(Integer, default=0)
    total_users = Column(Integer, default=0)
    live_music_active_chats = Column(Integer, default=0)
    extra = Column(JSON, default=dict)


# Runtime-toggleable global flags (from YukkiMusicBot's /maintenance and
# PRIVATE_BOT_MODE) — single row, id=1. Distinct from bot/config.py's
# Settings: those are read once from the environment at startup, these
# are meant to be flipped live by a sudoer command and persist across a
# restart without touching .env.
class BotConfig(Base, TimestampMixin):
    __tablename__ = "bot_config"

    id = Column(Integer, primary_key=True)
    maintenance_mode = Column(Boolean, default=False)
    private_bot_mode = Column(Boolean, default=False)
    # Auto-leave a voice chat once no real participants are left (checked
    # via PyTgCalls.get_participants — distinct from
    # ASSISTANT_AUTO_LEAVE_SECONDS, which is about no *playback* activity
    # rather than whether anyone is actually listening). From AnonXMusic's
    # /autoend.
    autoend_enabled = Column(Boolean, default=False)


async def get_bot_config(session) -> "BotConfig":
    row = await session.get(BotConfig, 1)
    if row is None:
        row = BotConfig(id=1)
        session.add(row)
        await session.flush()
    return row


async def _auto_migrate() -> None:
    """Add any columns or indexes present on the ORM models but missing
    from an existing database (e.g. upgrading from an earlier version of
    NovaBot) without requiring a full migration tool. Only ever ADDS —
    never drops or alters anything — so it's safe to run on every
    startup. Brand-new tables are handled by create_all() before this
    runs; this covers changes to tables that already existed."""
    from sqlalchemy import inspect as sa_inspect, text

    def _existing_columns(sync_conn) -> dict[str, set[str]]:
        inspector = sa_inspect(sync_conn)
        return {
            name: {col["name"] for col in inspector.get_columns(name)}
            for name in inspector.get_table_names()
        }

    def _existing_index_names(sync_conn) -> dict[str, set[str]]:
        inspector = sa_inspect(sync_conn)
        return {
            name: {idx["name"] for idx in inspector.get_indexes(name)}
            for name in inspector.get_table_names()
        }

    async with engine.begin() as conn:
        existing_cols = await conn.run_sync(_existing_columns)

        for table in Base.metadata.sorted_tables:
            if table.name not in existing_cols:
                continue  # brand-new table — create_all already made it
            have = existing_cols[table.name]
            for column in table.columns:
                if column.name in have:
                    continue
                try:
                    ddl_type = column.type.compile(dialect=engine.dialect)
                    await conn.execute(
                        text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl_type}')
                    )
                    logger.info("auto_migrated_column", table=table.name, column=column.name)
                except Exception as e:
                    logger.error(
                        "auto_migrate_column_failed", table=table.name, column=column.name, error=str(e)
                    )

        existing_idx = await conn.run_sync(_existing_index_names)
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_idx:
                continue
            have = existing_idx[table.name]
            for index in table.indexes:
                if index.name in have:
                    continue
                try:
                    await conn.run_sync(lambda sync_conn, idx=index: idx.create(sync_conn))
                    logger.info("auto_migrated_index", table=table.name, index=index.name)
                except Exception as e:
                    logger.error(
                        "auto_migrate_index_failed", table=table.name, index=index.name, error=str(e)
                    )


# ─── Bot-wide user moderation (from AnonXMusic) ─────────────────────
# Two deliberately distinct mechanisms:
#   GlobalBan (/gban)  — actually removes the user from every chat
#                          NovaBot moderates. A real, visible action.
#   BlockedUser (/block) — NovaBot just stops responding to them
#                          everywhere; no effect on their chat
#                          memberships. Lighter-weight, reversible,
#                          no collateral notification to the group.
class GlobalBan(Base, TimestampMixin):
    __tablename__ = "global_bans"

    user_id = Column(BigInteger, primary_key=True)
    reason = Column(Text, nullable=True)
    banned_by = Column(BigInteger)


class BlockedUser(Base, TimestampMixin):
    __tablename__ = "blocked_users"

    user_id = Column(BigInteger, primary_key=True)
    reason = Column(Text, nullable=True)
    blocked_by = Column(BigInteger)


# ─── Track play counts (from AnonXMusic's /toptracks) ───────────────
class TrackPlay(Base, TimestampMixin):
    __tablename__ = "track_plays"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, index=True)
    user_id = Column(BigInteger, nullable=True)
    title = Column(String(256))
    artist = Column(String(128), nullable=True)
    source_url = Column(String(512), nullable=True)

    __table_args__ = (Index("idx_trackplay_chat_title", "chat_id", "title"),)


# ─── Character-collector game (from anime_collector_bot family +
# WAIFU-HUSBANDO-CATCHER/shivu) ──────────────────────────────────────
# Characters are shared bot-wide (uploaded once, catchable in every
# chat), same scope as CollectorModerator below — this is a bot-wide
# content-curation role, distinct from being an admin of any one chat.
class Character(Base, TimestampMixin):
    __tablename__ = "characters"

    id = Column(Integer, primary_key=True)
    name = Column(String(128), index=True)
    anime = Column(String(128))
    rarity = Column(String(16), default="common")  # common, uncommon, rare, epic, legendary, divine
    image_url = Column(String(512))
    added_by = Column(BigInteger, nullable=True)


class UserCharacter(Base, TimestampMixin):
    """One row per character a user has caught. A user can catch the
    same Character more than once (duplicates are common in this genre —
    tradeable, and some designs let you feed dupes back for currency),
    so this is NOT unique on (user_id, character_id)."""
    __tablename__ = "user_characters"

    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, index=True)
    character_id = Column(Integer, ForeignKey("characters.id", ondelete="CASCADE"))
    is_favorite = Column(Boolean, default=False)

    __table_args__ = (Index("idx_userchar_user_char", "user_id", "character_id"),)


class CollectorModerator(Base, TimestampMixin):
    __tablename__ = "collector_moderators"

    user_id = Column(BigInteger, primary_key=True)
    added_by = Column(BigInteger)


class Giveaway(Base, TimestampMixin):
    __tablename__ = "giveaways"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, index=True)
    prize_coins = Column(Integer, default=0)
    character_id = Column(Integer, ForeignKey("characters.id"), nullable=True)
    created_by = Column(BigInteger)
    ends_at = Column(DateTime)
    claimed_by = Column(BigInteger, nullable=True)
    ended = Column(Boolean, default=False)


async def init_db() -> None:
    """Create tables and run migrations, retrying the initial connection.

    In container deployments the DB service can still be coming up when
    the bot starts (no compose healthcheck ordering guarantee) — retry
    with backoff instead of crashing on the very first attempt.
    """
    @with_retry(
        attempts=5, min_wait=1.0, max_wait=15.0,
        exceptions=(OperationalError, ConnectionError, TimeoutError, OSError),
    )
    async def _connect_and_create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    await _connect_and_create()
    await _auto_migrate()


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session
