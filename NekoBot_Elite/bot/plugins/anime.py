"""
Anime / manga / character lookup — AniList (GraphQL) + MyAnimeList via Jikan.

Primary: AniList. If AniList misses or errors, falls back to Jikan (MAL).
Results show both AniList and MAL links when both are available.
No API keys required for either source.
"""
from __future__ import annotations

import re
from typing import Any, Optional

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler

from bot.utils.helpers import escape_html
from bot.utils.logger import get_logger
from bot.core.errors import safe_handler

logger = get_logger(__name__)

import time as _time
from collections import OrderedDict

# Simple TTL cache for AniList/Jikan responses (reduce rate-limit hits).
_CACHE_TTL = 900.0  # 15 minutes
_CACHE_MAX = 256
_anilist_cache: OrderedDict = OrderedDict()
_jikan_cache: OrderedDict = OrderedDict()


def _cache_get(store: OrderedDict, key: str):
    item = store.get(key)
    if not item:
        return None
    val, exp = item
    if _time.time() > exp:
        store.pop(key, None)
        return None
    store.move_to_end(key)
    return val


def _cache_set(store: OrderedDict, key: str, val) -> None:
    store[key] = (val, _time.time() + _CACHE_TTL)
    store.move_to_end(key)
    while len(store) > _CACHE_MAX:
        store.popitem(last=False)


ANILIST_URL = "https://graphql.anilist.co"
JIKAN_BASE = "https://api.jikan.moe/v4"

ANIME_QUERY = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
    id
    idMal
    title { romaji english native }
    description
    episodes
    status
    averageScore
    genres
    coverImage { large }
    siteUrl
    nextAiringEpisode { episode timeUntilAiring }
  }
}
"""

MANGA_QUERY = """
query ($search: String) {
  Media(search: $search, type: MANGA) {
    id
    idMal
    title { romaji english native }
    description
    chapters
    volumes
    status
    averageScore
    genres
    coverImage { large }
    siteUrl
  }
}
"""

CHAR_QUERY = """
query ($search: String) {
  Character(search: $search) {
    id
    name { full native }
    description
    image { large }
    siteUrl
    media { nodes { title { romaji } type } }
  }
}
"""


def _strip_html(text: Optional[str]) -> str:
    if not text:
        return ""
    return re.sub(r"<[^>]+>", "", text)


async def _anilist_request(query: str, variables: dict) -> Optional[dict]:
    cache_key = f"al:{hash((query, tuple(sorted((variables or {}).items()))))}"
    hit = _cache_get(_anilist_cache, cache_key)
    if hit is not None:
        return hit
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                ANILIST_URL,
                json={"query": query, "variables": variables},
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    _cache_set(_anilist_cache, cache_key, data)
                    return data
                logger.warning("anilist_http", status=resp.status)
                return None
    except Exception as e:
        logger.warning("anilist_request_failed", error=str(e))
        return None


async def _jikan_get(path: str, params: Optional[dict] = None) -> Optional[dict]:
    """Jikan v4 — unofficial MAL API, no key required."""
    cache_key = f"jk:{path}:{tuple(sorted((params or {}).items()))}"
    hit = _cache_get(_jikan_cache, cache_key)
    if hit is not None:
        return hit
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{JIKAN_BASE}{path}",
                params=params or {},
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    _cache_set(_jikan_cache, cache_key, data)
                    return data
                logger.warning("jikan_http", status=resp.status, path=path)
                return None
    except Exception as e:
        logger.warning("jikan_request_failed", error=str(e), path=path)
        return None


def _mal_anime_url(mal_id: Optional[int]) -> Optional[str]:
    if mal_id:
        return f"https://myanimelist.net/anime/{mal_id}"
    return None


def _mal_manga_url(mal_id: Optional[int]) -> Optional[str]:
    if mal_id:
        return f"https://myanimelist.net/manga/{mal_id}"
    return None


def _mal_character_url(mal_id: Optional[int]) -> Optional[str]:
    if mal_id:
        return f"https://myanimelist.net/character/{mal_id}"
    return None


async def _search_anime_anilist(search: str) -> Optional[dict]:
    data = await _anilist_request(ANIME_QUERY, {"search": search})
    if not data:
        return None
    media = data.get("data", {}).get("Media")
    return media


async def _search_anime_mal(search: str) -> Optional[dict]:
    data = await _jikan_get("/anime", {"q": search, "limit": 1})
    if not data or not data.get("data"):
        return None
    item = data["data"][0]
    # Normalize toward AniList-shaped fields for shared formatting.
    return {
        "id": None,
        "idMal": item.get("mal_id"),
        "title": {
            "english": item.get("title_english") or item.get("title"),
            "romaji": item.get("title"),
            "native": (item.get("title_japanese") or None),
        },
        "description": item.get("synopsis"),
        "episodes": item.get("episodes"),
        "status": item.get("status"),
        "averageScore": int(item["score"] * 10) if item.get("score") else None,
        "genres": [g["name"] for g in (item.get("genres") or [])],
        "coverImage": {"large": (item.get("images") or {}).get("jpg", {}).get("large_image_url")},
        "siteUrl": None,
        "source": "mal",
    }


async def _search_manga_anilist(search: str) -> Optional[dict]:
    data = await _anilist_request(MANGA_QUERY, {"search": search})
    if not data:
        return None
    return data.get("data", {}).get("Media")


async def _search_manga_mal(search: str) -> Optional[dict]:
    data = await _jikan_get("/manga", {"q": search, "limit": 1})
    if not data or not data.get("data"):
        return None
    item = data["data"][0]
    return {
        "id": None,
        "idMal": item.get("mal_id"),
        "title": {
            "english": item.get("title_english") or item.get("title"),
            "romaji": item.get("title"),
            "native": item.get("title_japanese"),
        },
        "description": item.get("synopsis"),
        "chapters": item.get("chapters"),
        "volumes": item.get("volumes"),
        "status": item.get("status"),
        "averageScore": int(item["score"] * 10) if item.get("score") else None,
        "genres": [g["name"] for g in (item.get("genres") or [])],
        "coverImage": {"large": (item.get("images") or {}).get("jpg", {}).get("large_image_url")},
        "siteUrl": None,
        "source": "mal",
    }


async def _search_character_anilist(search: str) -> Optional[dict]:
    data = await _anilist_request(CHAR_QUERY, {"search": search})
    if not data:
        return None
    return data.get("data", {}).get("Character")


async def _search_character_mal(search: str) -> Optional[dict]:
    data = await _jikan_get("/characters", {"q": search, "limit": 1})
    if not data or not data.get("data"):
        return None
    item = data["data"][0]
    return {
        "id": None,
        "idMal": item.get("mal_id"),
        "name": {"full": item.get("name"), "native": item.get("name_kanji")},
        "description": item.get("about"),
        "image": {"large": (item.get("images") or {}).get("jpg", {}).get("image_url")},
        "siteUrl": None,
        "source": "mal",
    }


def _title_from_media(media: dict) -> str:
    t = media.get("title") or {}
    return t.get("english") or t.get("romaji") or t.get("native") or "Unknown"


def _link_buttons(
    *,
    anilist_url: Optional[str] = None,
    mal_url: Optional[str] = None,
) -> Optional[InlineKeyboardMarkup]:
    row = []
    if anilist_url:
        row.append(InlineKeyboardButton("🔗 AniList", url=anilist_url))
    if mal_url:
        row.append(InlineKeyboardButton("🔗 MyAnimeList", url=mal_url))
    if not row:
        return None
    return InlineKeyboardMarkup([row])


@safe_handler
async def anime_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    search = " ".join(context.args)
    if not search:
        await update.message.reply_text("Usage: /anime <title>")
        return

    msg = await update.message.reply_text("🔍 Searching AniList + MyAnimeList...")
    media = await _search_anime_anilist(search)
    from_mal_only = False
    if not media:
        media = await _search_anime_mal(search)
        from_mal_only = True

    if not media:
        return await msg.edit_text("❌ No results found on AniList or MyAnimeList.")

    title = _title_from_media(media)
    desc_raw = _strip_html(media.get("description") or "")
    desc = (desc_raw[:400] + "...") if len(desc_raw) > 400 else (desc_raw or "No description.")
    score = media.get("averageScore", "N/A")
    eps = media.get("episodes", "?")
    status = media.get("status", "Unknown")
    genres = ", ".join((media.get("genres") or [])[:5])

    text = (
        f"<b>🎌 {escape_html(title)}</b>\n"
        f"<b>📊 Score:</b> {score}/100\n"
        f"<b>📺 Episodes:</b> {eps} | <b>Status:</b> {escape_html(str(status))}\n"
        f"<b>🏷 Genres:</b> {escape_html(genres)}\n\n"
        f"{escape_html(desc)}"
    )

    anilist_url = None if from_mal_only else media.get("siteUrl")
    mal_id = media.get("idMal")
    # If AniList had no idMal, try a quick MAL lookup by title for the link.
    if not mal_id and not from_mal_only:
        mal_hit = await _search_anime_mal(title)
        if mal_hit:
            mal_id = mal_hit.get("idMal")
    buttons = _link_buttons(anilist_url=anilist_url, mal_url=_mal_anime_url(mal_id))

    cover = (media.get("coverImage") or {}).get("large")
    await msg.delete()
    if cover:
        await update.message.reply_photo(
            cover, caption=text, parse_mode="HTML", reply_markup=buttons
        )
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=buttons)


@safe_handler
async def manga_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    search = " ".join(context.args)
    if not search:
        await update.message.reply_text("Usage: /manga <title>")
        return

    media = await _search_manga_anilist(search)
    from_mal_only = False
    if not media:
        media = await _search_manga_mal(search)
        from_mal_only = True

    if not media:
        return await update.message.reply_text("❌ No results on AniList or MyAnimeList.")

    title = _title_from_media(media)
    anilist_url = None if from_mal_only else media.get("siteUrl")
    mal_id = media.get("idMal")
    if not mal_id and not from_mal_only:
        mal_hit = await _search_manga_mal(title)
        if mal_hit:
            mal_id = mal_hit.get("idMal")
    buttons = _link_buttons(anilist_url=anilist_url, mal_url=_mal_manga_url(mal_id))

    await update.message.reply_text(
        f"<b>📖 {escape_html(title)}</b>\n"
        f"<b>Chapters:</b> {media.get('chapters', '?')} | <b>Volumes:</b> {media.get('volumes', '?')}\n"
        f"<b>Score:</b> {media.get('averageScore', 'N/A')}/100",
        parse_mode="HTML",
        reply_markup=buttons,
    )


@safe_handler
async def character_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    search = " ".join(context.args)
    if not search:
        await update.message.reply_text("Usage: /character <name>")
        return

    char = await _search_character_anilist(search)
    from_mal_only = False
    if not char:
        char = await _search_character_mal(search)
        from_mal_only = True

    if not char:
        return await update.message.reply_text("❌ No results on AniList or MyAnimeList.")

    name = (char.get("name") or {}).get("full") or search
    desc_raw = _strip_html(char.get("description") or "")
    desc = (desc_raw[:500] + "...") if len(desc_raw) > 500 else (desc_raw or "No info.")

    anilist_url = None if from_mal_only else char.get("siteUrl")
    mal_id = char.get("idMal")
    buttons = _link_buttons(
        anilist_url=anilist_url,
        mal_url=_mal_character_url(mal_id),
    )

    image = (char.get("image") or {}).get("large")
    caption = f"<b>🎭 {escape_html(name)}</b>\n\n{escape_html(desc)}"
    if image:
        await update.message.reply_photo(
            image, caption=caption, parse_mode="HTML", reply_markup=buttons
        )
    else:
        await update.message.reply_text(caption, parse_mode="HTML", reply_markup=buttons)


@safe_handler
async def seasonal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Current season via Jikan (MAL) — free, no key."""
    data = await _jikan_get("/seasons/now", {"limit": 8})
    if not data or not data.get("data"):
        await update.message.reply_text(
            "🌸 Seasonal list is unavailable right now. Try /anime <title>."
        )
        return

    lines = ["<b>🌸 Currently airing (MyAnimeList)</b>\n"]
    buttons_rows = []
    for item in data["data"][:8]:
        title = item.get("title_english") or item.get("title") or "?"
        score = item.get("score") or "—"
        mal_id = item.get("mal_id")
        lines.append(f"• <b>{escape_html(title)}</b> — score {score}")
        if mal_id:
            buttons_rows.append([
                InlineKeyboardButton(
                    f"MAL: {title[:32]}",
                    url=f"https://myanimelist.net/anime/{mal_id}",
                )
            ])

    kb = InlineKeyboardMarkup(buttons_rows[:6]) if buttons_rows else None
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)


def register(app):
    app.add_handler(CommandHandler("anime", anime_cmd))
    app.add_handler(CommandHandler("manga", manga_cmd))
    app.add_handler(CommandHandler("character", character_cmd))
    app.add_handler(CommandHandler("seasonal", seasonal_cmd))
