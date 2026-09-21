"""
Media & Download Plugin
YouTube audio, TTS, Instagram/TikTok downloader, QR codes, Upscale placeholder.
"""
import os
import asyncio
import aiohttp
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler
from bot.identity import bot_name
from bot.utils.helpers import escape_html
from bot.core.errors import safe_handler
import yt_dlp

DOWNLOAD_DIR = "data/downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


@safe_handler
async def music_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("Usage: /music <song name or YouTube URL>")
        return

    msg = await update.message.reply_text("🎵 Searching & downloading...")

    # Search if not URL
    url = query if query.startswith("http") else f"ytsearch1:{query}"

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": f"{DOWNLOAD_DIR}/%(id)s.%(ext)s",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "quiet": True,
        "noplaylist": True,
    }

    try:
        loop = asyncio.get_event_loop()
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=True))
            if "entries" in info:
                info = info["entries"][0]
            file_path = ydl.prepare_filename(info).replace(".webm", ".mp3").replace(".m4a", ".mp3")

        title = info.get("title", "Unknown")
        duration = info.get("duration", 0)
        thumb = info.get("thumbnail")

        await msg.delete()
        with open(file_path, "rb") as audio:
            await update.message.reply_audio(
                audio,
                title=title,
                duration=duration,
                performer=bot_name(),
                thumbnail=thumb,
                caption=f"🎵 <b>{escape_html(title)}</b>",
                parse_mode="HTML"
            )
        os.remove(file_path)
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")


@safe_handler
async def tts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usage: /tts <text>")
        return

    # Using Google Translate TTS (gTTS) or similar
    # Here we simulate with a placeholder since gTTS isn't in requirements
    # In production, use gTTS or pyttsx3
    await update.message.reply_text(f"🔊 TTS: <i>{escape_html(text[:200])}</i>\n(Integrate gTTS for full audio output)", parse_mode="HTML")


@safe_handler
async def insta_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = context.args[0] if context.args else None
    if not url or "instagram.com" not in url:
        await update.message.reply_text("Usage: /insta <Instagram URL>")
        return

    await update.message.reply_text("📸 Instagram downloader requires yt-dlp or instaloader integration.\nUse: /yt <url> for generic download.")


@safe_handler
async def qr_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usage: /qr <text or URL>")
        return

    # Use a public QR API
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={aiohttp.helpers.quote(text)}"
    await update.message.reply_photo(qr_url, caption=f"📲 QR for: <code>{escape_html(text[:100])}</code>", parse_mode="HTML")


@safe_handler
async def yt_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = context.args[0] if context.args else None
    if not url:
        await update.message.reply_text("Usage: /yt <YouTube URL>")
        return
    await music_cmd(update, context)  # Reuse music logic


def register(app):
    app.add_handler(CommandHandler("music", music_cmd))
    app.add_handler(CommandHandler("tts", tts_cmd))
    app.add_handler(CommandHandler("insta", insta_cmd))
    app.add_handler(CommandHandler("qr", qr_cmd))
    app.add_handler(CommandHandler("yt", yt_cmd))
