"""
Neko — Unified Telegram Bot
Moderation + AI chat + live voice-chat music + personality mode + fonts.

Entry point. Supports both polling and webhook modes.
"""
import sys

from telegram import Update

from bot.config import settings
from bot.core.bot import create_application

if __name__ == "__main__":
    if sys.platform != "win32":
        try:
            import uvloop
            uvloop.install()
        except ImportError:
            pass  # uvloop is a Linux/macOS performance optimization, not required

    app = create_application()

    if settings.webhook_url:
        app.run_webhook(
            listen="0.0.0.0",
            port=settings.webhook_port,
            webhook_url=settings.webhook_url,
        )
    else:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
