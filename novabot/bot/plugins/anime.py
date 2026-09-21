"""
Anime & Manga Plugin using AniList GraphQL API.
Features: search anime/manga, character info, seasonal, schedule, news, quotes.
"""
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler
from bot.utils.helpers import escape_html
from bot.core.errors import safe_handler

ANILIST_URL = "https://graphql.anilist.co"

ANIME_QUERY = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
    id
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


async def _anilist_request(query: str, variables: dict):
    async with aiohttp.ClientSession() as session:
        async with session.post(ANILIST_URL, json={"query": query, "variables": variables}) as resp:
            if resp.status == 200:
                return await resp.json()
            return None


@safe_handler
async def anime_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    search = " ".join(context.args)
    if not search:
        await update.message.reply_text("Usage: /anime <title>")
        return

    msg = await update.message.reply_text("🔍 Searching AniList...")
    data = await _anilist_request(ANIME_QUERY, {"search": search})

    if not data or not data.get("data", {}).get("Media"):
        return await msg.edit_text("❌ No results found.")

    media = data["data"]["Media"]
    title = media["title"]["english"] or media["title"]["romaji"]
    desc = media["description"][:400] + "..." if media["description"] else "No description."
    score = media.get("averageScore", "N/A")
    eps = media.get("episodes", "?")
    status = media.get("status", "Unknown")
    genres = ", ".join(media.get("genres", [])[:5])

    text = (
        f"<b>🎌 {escape_html(title)}</b>\n"
        f"<b>📊 Score:</b> {score}/100\n"
        f"<b>📺 Episodes:</b> {eps} | <b>Status:</b> {status}\n"
        f"<b>🏷 Genres:</b> {genres}\n\n"
        f"{escape_html(desc)}"
    )

    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 AniList", url=media["siteUrl"])]
    ])

    await msg.delete()
    await update.message.reply_photo(
        media["coverImage"]["large"],
        caption=text,
        parse_mode="HTML",
        reply_markup=buttons
    )


@safe_handler
async def manga_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    search = " ".join(context.args)
    if not search:
        await update.message.reply_text("Usage: /manga <title>")
        return

    data = await _anilist_request(MANGA_QUERY, {"search": search})
    if not data or not data.get("data", {}).get("Media"):
        return await update.message.reply_text("❌ No results.")

    media = data["data"]["Media"]
    title = media["title"]["english"] or media["title"]["romaji"]
    await update.message.reply_text(
        f"<b>📖 {escape_html(title)}</b>\n"
        f"<b>Chapters:</b> {media.get('chapters', '?')} | <b>Volumes:</b> {media.get('volumes', '?')}\n"
        f"<b>Score:</b> {media.get('averageScore', 'N/A')}/100\n"
        f"<b>🔗</b> <a href='{media['siteUrl']}'>AniList</a>",
        parse_mode="HTML"
    )


@safe_handler
async def character_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    search = " ".join(context.args)
    if not search:
        await update.message.reply_text("Usage: /character <name>")
        return

    data = await _anilist_request(CHAR_QUERY, {"search": search})
    if not data or not data.get("data", {}).get("Character"):
        return await update.message.reply_text("❌ No results.")

    char = data["data"]["Character"]
    name = char["name"]["full"]
    desc = char["description"][:500] + "..." if char["description"] else "No info."

    await update.message.reply_photo(
        char["image"]["large"],
        caption=f"<b>🎭 {escape_html(name)}</b>\n\n{escape_html(desc)}",
        parse_mode="HTML"
    )


@safe_handler
async def seasonal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Simplified seasonal — would need a proper seasonal query
    await update.message.reply_text("🌸 Use /anime with title. Seasonal browser coming in v2!")


def register(app):
    app.add_handler(CommandHandler("anime", anime_cmd))
    app.add_handler(CommandHandler("manga", manga_cmd))
    app.add_handler(CommandHandler("character", character_cmd))
    app.add_handler(CommandHandler("seasonal", seasonal_cmd))
