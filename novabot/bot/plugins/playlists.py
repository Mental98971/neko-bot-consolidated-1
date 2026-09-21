"""
Music Playlists & Lyrics Lookup — new plugin, not present in any of the
original four projects.

Playlists save/restore into the same queue system music_live.py uses.
/lyrics intentionally returns a link rather than lyric text: Genius's
API is designed that way on purpose (their lyrics pages are licensed
content, and the API only exposes metadata + a URL to view them) — this
follows the same rule this project applies to its own output, that
copyrighted text gets linked to, not reproduced.
"""
from __future__ import annotations

import httpx
from sqlalchemy import select
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot.config import settings
from bot.core.database import Playlist, PlaylistTrack, async_session
from bot.models.track import Track
from bot.services.queue_manager import queue_manager
from bot.utils.helpers import escape_html
from bot.core.errors import safe_handler

GENIUS_SEARCH_URL = "https://api.genius.com/search"


@safe_handler
async def playlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "<code>/playlist save &lt;name&gt;</code> — save the current queue\n"
            "<code>/playlist load &lt;name&gt;</code> — queue a saved playlist\n"
            "<code>/playlist list</code> — your saved playlists\n"
            "<code>/playlist delete &lt;name&gt;</code>",
            parse_mode="HTML",
        )
        return

    sub = context.args[0].lower()
    name = " ".join(context.args[1:]).strip()
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if sub == "list":
        async with async_session() as session:
            result = await session.execute(select(Playlist).where(Playlist.owner_id == user_id))
            playlists = result.scalars().all()
        if not playlists:
            await update.message.reply_text("📭 You have no saved playlists. /playlist save <name> to make one.")
            return
        lines = "\n".join(f"• {escape_html(p.name)}" for p in playlists)
        await update.message.reply_text(f"🎼 <b>Your playlists</b>\n{lines}", parse_mode="HTML")
        return

    if not name:
        await update.message.reply_text(f"Usage: /playlist {sub} <name>")
        return

    if sub == "save":
        if update.effective_chat.type == "private":
            await update.message.reply_text("🎧 The live queue only exists in groups — nothing to save from a DM.")
            return
        queue = await queue_manager.get_queue(chat_id)
        if queue.is_empty:
            await update.message.reply_text("📭 The queue is empty — nothing to save.")
            return

        async with async_session() as session:
            result = await session.execute(
                select(Playlist).where(Playlist.owner_id == user_id, Playlist.name == name)
            )
            playlist = result.scalar_one_or_none()
            if playlist:
                await session.execute(
                    PlaylistTrack.__table__.delete().where(PlaylistTrack.playlist_id == playlist.id)
                )
            else:
                playlist = Playlist(owner_id=user_id, name=name)
                session.add(playlist)
                await session.flush()

            for i, entry in enumerate(queue.entries):
                session.add(PlaylistTrack(playlist_id=playlist.id, position=i, track_data=entry.track.model_dump(mode="json")))
            await session.commit()

        await update.message.reply_text(f"💾 Saved {len(queue.entries)} tracks to playlist <b>{escape_html(name)}</b>.", parse_mode="HTML")
        return

    if sub == "load":
        if update.effective_chat.type == "private":
            await update.message.reply_text("🎧 The live queue only exists in groups — try this in a group.")
            return

        async with async_session() as session:
            result = await session.execute(
                select(Playlist).where(Playlist.owner_id == user_id, Playlist.name == name)
            )
            playlist = result.scalar_one_or_none()
            if not playlist:
                await update.message.reply_text(f"❌ No playlist named '{escape_html(name)}'. See /playlist list")
                return
            track_result = await session.execute(
                select(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist.id).order_by(PlaylistTrack.position)
            )
            rows = track_result.scalars().all()

        for row in rows:
            track = Track.model_validate(row.track_data)
            await queue_manager.add_track(chat_id, track)

        import bot.plugins.music_live as music_live
        started = await music_live.start_playback_if_idle(chat_id)
        suffix = f"\n▶️ Now playing: <b>{escape_html(started.display_name)}</b>" if started else ""
        if not music_live.is_configured():
            suffix = "\n<i>(Live music isn't configured — queued for when it is, or use /music to download tracks individually.)</i>"

        await update.message.reply_text(f"📥 Queued {len(rows)} tracks from <b>{escape_html(name)}</b>.{suffix}", parse_mode="HTML")
        return

    if sub == "delete":
        async with async_session() as session:
            result = await session.execute(
                select(Playlist).where(Playlist.owner_id == user_id, Playlist.name == name)
            )
            playlist = result.scalar_one_or_none()
            if not playlist:
                await update.message.reply_text(f"❌ No playlist named '{escape_html(name)}'.")
                return
            await session.delete(playlist)
            await session.commit()
        await update.message.reply_text(f"🗑 Deleted playlist <b>{escape_html(name)}</b>.", parse_mode="HTML")
        return

    await update.message.reply_text("Unknown subcommand. Try /playlist with no arguments for usage.")


@safe_handler
async def lyrics_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not settings.genius_api_key:
        await update.message.reply_text("❌ Lyrics lookup needs GENIUS_API_KEY (free at genius.com/api-clients).")
        return

    query = " ".join(context.args)
    if not query:
        queue = await queue_manager.get_queue(update.effective_chat.id)
        track = queue.current_track
        if not track:
            await update.message.reply_text("Usage: /lyrics <song> — or play something first and I'll use that.")
            return
        query = f"{track.title} {track.artist}"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                GENIUS_SEARCH_URL,
                params={"q": query},
                headers={"Authorization": f"Bearer {settings.genius_api_key}"},
            )
            resp.raise_for_status()
            hits = resp.json().get("response", {}).get("hits", [])
    except Exception as e:
        await update.message.reply_text(f"❌ Lyrics search failed: {e}")
        return

    if not hits:
        await update.message.reply_text("❌ No results found.")
        return

    result = hits[0]["result"]
    title = result.get("title", "Unknown")
    artist = result.get("primary_artist", {}).get("name", "Unknown")
    url = result.get("url", "")

    # Deliberately not scraping/reproducing the lyric text here — Genius's
    # API doesn't provide it (their lyrics pages are licensed content) and
    # this project doesn't reproduce copyrighted text it fetches on a
    # user's behalf; the link goes straight to the legitimate source.
    await update.message.reply_text(
        f"🎵 <b>{escape_html(title)}</b>\nby {escape_html(artist)}\n\n📖 <a href=\"{url}\">View lyrics on Genius</a>",
        parse_mode="HTML",
    )


def register(app):
    if not settings.enable_playlists:
        return
    app.add_handler(CommandHandler("playlist", playlist_cmd))
    app.add_handler(CommandHandler("lyrics", lyrics_cmd))
