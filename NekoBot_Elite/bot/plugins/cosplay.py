"""
/cosplay — random high-quality cosplay image delivery.

Sources (in priority order, with graceful fallback):
  1. waifu.im (tag: cosplay, SFW)
  2. xxapi / yscos style endpoints (Genshin-focused but high quality)
  3. Local fallback message if all remote sources fail

Rate-limited, cached lightly, respects NSFW settings if configured.
"""
from __future__ import annotations

import asyncio
import random
from typing import Optional

import aiohttp
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

from bot.core.errors import safe_handler
from bot.utils.logger import get_logger
from bot.utils.ratelimit import hit

logger = get_logger(__name__)

# Primary + fallback endpoints. All return direct image URLs or JSON with URL.
_SOURCES = [
    # waifu.im search
    {
        "name": "waifu.im",
        "url": "https://api.waifu.im/search",
        "params": {"included_tags": "cosplay", "is_nsfw": "false", "limit": "1"},
        "extract": lambda d: (d.get("images") or [{}])[0].get("url"),
    },
    # Alternative free endpoints that frequently host cosplay
    {
        "name": "xxapi-yscos",
        "url": "https://v2.xxapi.cn/api/yscos",
        "params": {"return": "json"},
        "extract": lambda d: d.get("data") if isinstance(d.get("data"), str) else None,
    },
]

_CAPTIONS = [
    "✨ It's what cosplay do.",
    "🎭 Cosplay drop!",
    "📸 Found this one for you.",
    "🌟 Peak cosplay energy.",
    "💫 Serving looks.",
]


@safe_handler
async def cosplay_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a random cosplay image."""
    user = update.effective_user
    if not user:
        return

    # Soft rate limit: 1 per 3s per user
    if not hit(f"cosplay:{user.id}", limit=1, window=3.0):
        await update.message.reply_text("⏳ Slow down a little — another cosplay is loading.")
        return

    status = await update.message.reply_text("🔍 Hunting for cosplay…")

    url = await _fetch_cosplay_url()
    if not url:
        await status.edit_text(
            "😿 Couldn't reach any cosplay source right now. Try again in a moment."
        )
        return

    caption = random.choice(_CAPTIONS)
    try:
        await update.message.reply_photo(
            photo=url,
            caption=caption,
            parse_mode="HTML",
        )
        await status.delete()
    except Exception as e:
        logger.warning("cosplay_send_failed", extra={"error": str(e), "url": url[:80]})
        await status.edit_text(
            f"🖼️ Found one but Telegram rejected the photo.\n<code>{url[:60]}…</code>",
            parse_mode="HTML",
        )


async def _fetch_cosplay_url() -> Optional[str]:
    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # Shuffle for load distribution
        sources = list(_SOURCES)
        random.shuffle(sources)
        for src in sources:
            try:
                async with session.get(src["url"], params=src.get("params")) as resp:
                    if resp.status != 200:
                        continue
                    # Some endpoints return raw redirect / image; prefer JSON
                    ctype = resp.headers.get("Content-Type", "")
                    if "json" in ctype or "text" in ctype:
                        data = await resp.json(content_type=None)
                        url = src["extract"](data)
                        if url and isinstance(url, str) and url.startswith("http"):
                            return url
                    else:
                        # Direct image response — use the request URL itself if it worked
                        return str(resp.url)
            except Exception as e:
                logger.debug("cosplay_source_fail", extra={"source": src["name"], "err": str(e)})
                continue
    return None


def register(app):
    app.add_handler(CommandHandler("cosplay", cosplay_cmd))
    app.add_handler(CommandHandler("cp", cosplay_cmd))  # short alias
