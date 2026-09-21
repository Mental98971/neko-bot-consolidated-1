"""
Advanced AI Plugin supporting OpenAI, Anthropic, and Google Gemini.
Features: chat, summarize, imagine (DALL-E), code assistant, persona memory.
"""
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes, CommandHandler
from bot.config import settings
from bot.identity import bot_name
from bot.core.database import async_session, AIConversation, User
from bot.utils.helpers import escape_html
from bot.utils.ratelimit import hit
from bot.core.errors import safe_handler
from sqlalchemy import select, desc
import openai

# Initialize clients conditionally — only providers with a key set do anything.
_openai_client = None
if settings.openai_api_key:
    _openai_client = openai.AsyncOpenAI(api_key=settings.openai_api_key)

# Groq and OpenRouter expose OpenAI-compatible chat APIs.
_groq_client = None
if settings.groq_api_key:
    _groq_client = openai.AsyncOpenAI(
        api_key=settings.groq_api_key,
        base_url="https://api.groq.com/openai/v1",
    )

_openrouter_client = None
if settings.openrouter_api_key:
    _openrouter_client = openai.AsyncOpenAI(
        api_key=settings.openrouter_api_key,
        base_url="https://openrouter.ai/api/v1",
    )

_anthropic_client = None
if settings.anthropic_api_key:
    import anthropic
    _anthropic_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

_gemini_configured = False
_gemini_key = settings.gemini_api_key or settings.google_api_key
if _gemini_key:
    import google.generativeai as genai
    genai.configure(api_key=_gemini_key)
    _gemini_configured = True


def _provider_order(model: str) -> list[str]:
    """Return providers in preferred order, allowing model-specific routing."""
    m = (model or "").lower()
    if "groq" in m and _groq_client:
        preferred = ["groq"]
    elif "openrouter" in m and _openrouter_client:
        preferred = ["openrouter"]
    elif "gemini" in m and _gemini_configured:
        preferred = ["google"]
    elif "claude" in m and _anthropic_client:
        preferred = ["anthropic"]
    elif "gpt" in m and _openai_client:
        preferred = ["openai"]
    else:
        # User-requested free-first routing.
        preferred = ["google", "groq", "openrouter", "openai", "anthropic"]
    return [p for p in preferred if {
        "google": _gemini_configured, "groq": _groq_client,
        "openrouter": _openrouter_client, "openai": _openai_client,
        "anthropic": _anthropic_client,
    }.get(p, False)]


async def _dispatch_provider(provider: str, model: str, system_prompt: str, turns: list[dict]) -> str:
    """Call a single AI provider and return its text response.

    Raises on failure — callers own the fallback chain (catch, record the
    error, try the next provider) so this stays a pure single-provider
    call with no retry/fallback logic of its own.
    """
    if provider == "google":
        import google.generativeai as genai
        transcript = "\n".join(f"{t['role']}: {t['content']}" for t in turns)
        gemini_model = genai.GenerativeModel(
            settings.gemini_model, system_instruction=system_prompt
        )
        resp = await gemini_model.generate_content_async(transcript)
        return resp.text

    if provider == "groq":
        resp = await _groq_client.chat.completions.create(
            model=settings.groq_model,
            messages=[{"role": "system", "content": system_prompt}, *turns],
            max_tokens=800, temperature=0.7,
        )
        return resp.choices[0].message.content

    if provider == "openrouter":
        resp = await _openrouter_client.chat.completions.create(
            model=settings.openrouter_model,
            messages=[{"role": "system", "content": system_prompt}, *turns],
            max_tokens=800, temperature=0.7,
        )
        return resp.choices[0].message.content

    if provider == "openai":
        resp = await _openai_client.chat.completions.create(
            model=model if "gpt" in model else "gpt-4o-mini",
            messages=[{"role": "system", "content": system_prompt}, *turns],
            max_tokens=800, temperature=0.7,
        )
        return resp.choices[0].message.content

    if provider == "anthropic":
        resp = await _anthropic_client.messages.create(
            model=model if "claude" in model else "claude-3-5-sonnet-latest",
            max_tokens=800, system=system_prompt, messages=turns,
        )
        return resp.content[0].text

    raise ValueError(f"Unknown provider: {provider}")


async def _get_ai_response(user_id: int, text: str, model: str = None) -> str:
    model = model or settings.default_ai_model

    async with async_session() as session:
        result = await session.execute(
            select(AIConversation)
            .where(AIConversation.user_id == user_id)
            .order_by(desc(AIConversation.id))
            .limit(10)
        )
        history = result.scalars().all()
        history.reverse()

    system_prompt = f"You are {bot_name()} AI, a helpful, witty, and concise assistant."
    async with async_session() as session:
        user_row = await session.get(User, user_id)
        if user_row and user_row.ai_persona:
            system_prompt = user_row.ai_persona

    turns = [{"role": h.role, "content": h.content} for h in history]
    turns.append({"role": "user", "content": text})

    errors = []
    for provider in _provider_order(model):
        try:
            return await _dispatch_provider(provider, model, system_prompt, turns)
        except Exception as exc:
            errors.append(f"{provider}: {exc}")
            continue

    if errors:
        raise RuntimeError("All configured AI providers failed. " + " | ".join(errors[-3:]))
    return "🤖 AI is not configured. Set GEMINI_API_KEY, GROQ_API_KEY, or OPENROUTER_API_KEY."


async def generate_response(
    system_prompt: str,
    user_message: str,
    base_response: Optional[str] = None,
    model: Optional[str] = None,
) -> Optional[str]:
    """Rewrite `base_response` in the voice described by `system_prompt`,
    using the same provider fallback chain as /ai.

    This is what powers the personality layer's LLM-backed persona
    rewriting (see bot/personality/personality.py:_try_llm_rewrite) — it
    has no conversation history or memory of its own; it's a one-shot
    "restate this in that voice" call.

    Returns None if no AI provider is configured at all, so callers can
    silently fall back to a non-LLM path. Raises if providers ARE
    configured but every one of them failed, so a real outage is still
    visible to a caller that wants to log it — personality.py's caller
    already catches this and falls back too.
    """
    model = model or settings.default_ai_model
    providers = _provider_order(model)
    if not providers:
        return None

    rewrite_system_prompt = (
        f"{system_prompt}\n\n"
        "You are rewriting a chatbot's reply in the persona described above. "
        "Preserve the factual content and intent of the original reply, keep "
        "it concise (2-3 sentences max), and respond with ONLY the rewritten "
        "reply — no preamble, no quotation marks, no explanation."
    )
    turns = [{
        "role": "user",
        "content": (
            f"User message: {user_message}\n\n"
            f"Original reply to rewrite: {base_response or ''}"
        ),
    }]

    errors = []
    for provider in providers:
        try:
            response = await _dispatch_provider(provider, model, rewrite_system_prompt, turns)
            if response and response.strip():
                return response
            errors.append(f"{provider}: empty response")
        except Exception as exc:
            errors.append(f"{provider}: {exc}")
            continue

    raise RuntimeError("All configured AI providers failed. " + " | ".join(errors[-3:]))


async def _check_ai_rate_limit(update: Update, user) -> bool:
    """Shared per-user hourly cap across every AI command (chat, imagine,
    summarize, code) — they all hit paid provider APIs, so the limit is
    tracked jointly rather than separately per command. Returns whether
    the caller is within budget; sends the "try again later" reply
    itself when not, so call sites can just `if not ok: return`."""
    if not user:
        return True
    lim = int(getattr(settings, "ai_rate_limit_per_hour", 20) or 20)
    ok, n = await hit(f"ai:{user.id}", limit=lim, window_seconds=3600)
    if not ok:
        await update.effective_message.reply_text(
            f"AI rate limit reached ({n}/{lim} per hour). Try again later."
        )
    return ok


@safe_handler
async def ai_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await _check_ai_rate_limit(update, user):
        return
    text = " ".join(context.args) or (update.message.reply_to_message.text if update.message.reply_to_message else None)
    if not text:
        await update.message.reply_text("Usage: /ai <question> or reply to a message")
        return

    msg = await update.message.reply_text("🧠 Thinking...")
    try:
        response = await _get_ai_response(user.id, text)
        # Store conversation
        async with async_session() as session:
            session.add(AIConversation(user_id=user.id, role="user", content=text, model=settings.default_ai_model))
            session.add(AIConversation(user_id=user.id, role="assistant", content=response, model=settings.default_ai_model))
            await session.commit()

        await msg.edit_text(f"<b>🤖 {bot_name()} AI</b>\n{escape_html(response)}", parse_mode="HTML")
    except Exception as e:
        await msg.edit_text(f"❌ AI Error: {e}")


@safe_handler
async def ai_imagine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _check_ai_rate_limit(update, update.effective_user):
        return
    if not _openai_client:
        await update.message.reply_text("❌ OpenAI not configured.")
        return
    prompt = " ".join(context.args)
    if not prompt:
        await update.message.reply_text("Usage: /imagine <description>")
        return

    msg = await update.message.reply_text("🎨 Generating image...")
    try:
        resp = await _openai_client.images.generate(
            model="dall-e-3", prompt=prompt, n=1, size="1024x1024"
        )
        url = resp.data[0].url
        await msg.delete()
        await update.message.reply_photo(url, caption=f"🎨 <b>Prompt:</b> <i>{escape_html(prompt)}</i>", parse_mode="HTML")
    except Exception as e:
        await msg.edit_text(f"❌ {e}")


@safe_handler
async def ai_summarize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _check_ai_rate_limit(update, update.effective_user):
        return
    reply = update.message.reply_to_message
    if not reply or not reply.text:
        await update.message.reply_text("Reply to a long text to summarize.")
        return

    msg = await update.message.reply_text("📝 Summarizing...")
    prompt = f"Summarize the following text concisely:\n\n{reply.text[:3000]}"
    try:
        response = await _get_ai_response(update.effective_user.id, prompt, model="gpt-4o-mini")
        await msg.edit_text(f"<b>📝 Summary</b>\n{escape_html(response)}", parse_mode="HTML")
    except Exception as e:
        await msg.edit_text(f"❌ {e}")


@safe_handler
async def ai_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _check_ai_rate_limit(update, update.effective_user):
        return
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usage: /code <programming question>")
        return
    prompt = f"You are an expert programmer. Provide clean, commented code with explanation:\n\n{text}"
    msg = await update.message.reply_text("💻 Coding...")
    try:
        response = await _get_ai_response(update.effective_user.id, prompt)
        await msg.edit_text(f"<b>💻 Code Assistant</b>\n<pre>{escape_html(response)}</pre>", parse_mode="HTML")
    except Exception as e:
        await msg.edit_text(f"❌ {e}")


@safe_handler
async def persona_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args)
    async with async_session() as session:
        user_row = await session.get(User, update.effective_user.id)
        if user_row is None:
            user_row = User(id=update.effective_user.id)
            session.add(user_row)
        if not text or text.lower() == "reset":
            user_row.ai_persona = None
            await session.commit()
            await update.message.reply_text("🎭 Persona reset to the default assistant.")
            return
        user_row.ai_persona = text
        await session.commit()
    await update.message.reply_text(
        f"🎭 Persona set. /ai and /chat will now respond as:\n<i>{escape_html(text)}</i>\n\n"
        f"(<code>/persona reset</code> to go back to default)",
        parse_mode="HTML",
    )


@safe_handler
async def see_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Vision — reply to a photo (or attach one directly) with /see [question]."""
    import base64

    provider = _provider_order("")[0] if _provider_order("") else "none"
    if provider == "none":
        await update.message.reply_text("❌ AI is not configured. Set GEMINI_API_KEY, GROQ_API_KEY, or OPENROUTER_API_KEY.")
        return

    photo = None
    if update.message.photo:
        photo = update.message.photo[-1]
    elif update.message.reply_to_message and update.message.reply_to_message.photo:
        photo = update.message.reply_to_message.photo[-1]
    if not photo:
        await update.message.reply_text("Reply to a photo (or attach one) with /see [optional question]")
        return

    question = " ".join(context.args) or "Describe this image in detail."
    msg = await update.message.reply_text("👁️ Looking...")

    try:
        file = await context.bot.get_file(photo.file_id)
        raw = bytes(await file.download_as_bytearray())
        b64 = base64.b64encode(raw).decode()

        if provider == "openai":
            resp = await _openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ],
                }],
                max_tokens=600,
            )
            answer = resp.choices[0].message.content
        elif provider == "anthropic":
            resp = await _anthropic_client.messages.create(
                model="claude-3-5-sonnet-latest",
                max_tokens=600,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                        {"type": "text", "text": question},
                    ],
                }],
            )
            answer = resp.content[0].text
        else:  # google
            import google.generativeai as genai
            model = genai.GenerativeModel("gemini-1.5-flash")
            resp = await model.generate_content_async([question, {"mime_type": "image/jpeg", "data": raw}])
            answer = resp.text

        await msg.edit_text(f"<b>👁️ Vision</b>\n{escape_html(answer)}", parse_mode="HTML")
    except Exception as e:
        await msg.edit_text(f"❌ {e}")


@safe_handler
async def transcribe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Voice-note transcription via OpenAI Whisper — reply to a voice/audio message."""
    if not _openai_client:
        await update.message.reply_text("❌ Transcription needs OPENAI_API_KEY.")
        return

    reply = update.message.reply_to_message
    media = None
    if reply:
        media = reply.voice or reply.audio
    if not media:
        await update.message.reply_text("Reply to a voice message or audio file with /transcribe")
        return

    msg = await update.message.reply_text("🎙️ Transcribing...")
    try:
        file = await context.bot.get_file(media.file_id)
        raw = bytes(await file.download_as_bytearray())
        transcript = await _openai_client.audio.transcriptions.create(
            model="whisper-1", file=("audio.ogg", raw),
        )
        await msg.edit_text(f"<b>🎙️ Transcript</b>\n{escape_html(transcript.text)}", parse_mode="HTML")
    except Exception as e:
        await msg.edit_text(f"❌ {e}")


def register(app):
    app.add_handler(CommandHandler("ai", ai_chat))
    app.add_handler(CommandHandler("chat", ai_chat))
    app.add_handler(CommandHandler("imagine", ai_imagine))
    app.add_handler(CommandHandler("summarize", ai_summarize))
    app.add_handler(CommandHandler("code", ai_code))
    app.add_handler(CommandHandler("persona", persona_cmd))
    app.add_handler(CommandHandler("see", see_cmd))
    app.add_handler(CommandHandler("transcribe", transcribe_cmd))
