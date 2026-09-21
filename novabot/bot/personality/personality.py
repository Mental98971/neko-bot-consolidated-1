"""The Personality Layer v2.3 — with startup loading, anti-repetition,
LLM integration hook, and deadpan-safe presets.

v2.3 changes:
  - Per-chat dicts use LRUCache instead of unbounded dicts (fix 2.4).
  - SkinLoader.reload() clears cache (fix 3.5).
  - persona_context is now used in the rewrite pipeline — either via LLM
    when an AI provider is configured, or as a tone filter (fix 1.4).
  - Anti-repetition: tracks recently sent responses per chat (feature 4.5).
  - Deadpan filter respects deadpan_safe preset flag (quality 5.5).
  - load_persisted_settings() loads all settings from DB on startup.

v2.2 features (carried forward):
  - Custom instructions with themed openers/closers/prefixes.
  - Japanese romaji detection and response.
  - Math expression evaluation.
  - Meta-banter response pools.
  - Multiple skins with per-chat switching.
  - Mood/energy state per chat.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from cachetools import LRUCache

from bot.config import settings
from bot.personality.custom_instructions import (
    CustomInstructionProcessor,
    InstructionTraits,
)
from bot.personality.jokes import jokes_db
from bot.personality.memory import memory
from bot.personality.examples import example_bank
from bot.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class PersonalityConfig:
    sarcasm_level: float = 0.8
    metaphor_frequency: float = 0.4
    exaggeration_level: float = 0.5
    deadpan: bool = True
    programmer_humor: float = 0.7
    anime_references: float = 0.4
    max_response_length: int = 400


@dataclass
class PersonalityState:
    energy: float = 1.0
    sarcasm_boost: float = 0.0
    last_interaction: Optional[datetime] = None
    interaction_streak: int = 0


class SkinLoader:
    def __init__(self, skins_dir: str | Path = "data/personality/skins") -> None:
        self._skins_dir = Path(skins_dir)
        self._cache: Dict[str, dict] = {}

    def load(self, name: str) -> dict:
        if name in self._cache:
            return self._cache[name]
        path = self._skins_dir / f"{name}.json"
        if not path.exists():
            logger.warning("Skin '%s' not found, falling back to neko", name)
            name = "neko"
            path = self._skins_dir / "neko.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self._cache[name] = data
        return data

    def reload(self, name: str | None = None) -> None:
        """v2.3: Clear cache so skins are re-read from disk on next access."""
        if name:
            self._cache.pop(name, None)
        else:
            self._cache.clear()
        logger.info("Skin cache reloaded%s", f" ({name})" if name else "")

    def available_skins(self) -> List[str]:
        if not self._skins_dir.exists():
            return ["neko"]
        return sorted(p.stem for p in self._skins_dir.glob("*.json"))


class PersonalityLayer:
    """The rewrite engine — v2.3 with anti-repetition and LLM hook."""

    _RE_MULTI_BANG = re.compile(r"!{2,}")
    _RE_MULTI_QMARK = re.compile(r"\?{2,}")
    _RE_EMOJI = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")
    _RE_LAUGHTER = re.compile(r"\b(haha|lol|lmao|rofl|kek)\b", re.IGNORECASE)
    _RE_MULTISPACE = re.compile(r"  +")

    _JP_KEYWORDS: Dict[str, str] = {
        "gomen": "gomen", "gomenasai": "gomenasai", "sumimasen": "sumimasen",
        "arigato": "arigato", "arigatou": "arigato", "daijoubu": "daijoubu",
        "genki": "daijoubu", "ganbatte": "ganbatte", "ganbare": "ganbatte",
        "gambare": "ganbatte", "ohayo": "ohayo", "ohayou": "ohayo",
        "konnichiwa": "konnichiwa", "konbanwa": "konbanwa",
        "sayonara": "sayonara", "senpai": "senpai", "baka": "baka",
        "nani": "nani", "kawaii": "kawaii", "sugoi": "sugoi",
        "urusai": "urusai", "yamete": "yamete", "yamate": "yamete",
        "matte": "matte", "chotto": "chotto", "nya": "nya", "nyaa": "nya",
        "neko": "neko", "nyan": "nya", "itadakimasu": "itadakimasu",
        "nande": "nande",
    }
    _RE_JP = re.compile(r"\b(" + "|".join(_JP_KEYWORDS.keys()) + r")\b", re.IGNORECASE)
    _RE_MATH = re.compile(r"(\d+)\s*([\+\-\*\/x\u00d7])\s*(\d+)")

    def __init__(self, config: PersonalityConfig | None = None) -> None:
        self.config = config or PersonalityConfig()
        self._skin_loader = SkinLoader()
        self._skin_name = "neko"
        self._skin_data: dict = self._skin_loader.load("neko")
        # v2.3: LRU caches instead of unbounded dicts
        self._per_chat_states: LRUCache = LRUCache(maxsize=500)
        self._per_chat_configs: LRUCache = LRUCache(maxsize=500)
        self._per_chat_skins: LRUCache = LRUCache(maxsize=500)
        self._per_chat_traits: LRUCache = LRUCache(maxsize=500)
        self._instruction_processor = CustomInstructionProcessor()
        # v2.3: Anti-repetition tracking
        self._recent_responses: LRUCache = LRUCache(maxsize=500)

    # ─── Skin Management ───

    @property
    def skin_name(self) -> str:
        return self._skin_name

    def set_skin(self, name: str) -> bool:
        try:
            self._skin_data = self._skin_loader.load(name)
            self._skin_name = name
            return True
        except Exception as e:
            logger.error("Failed to load skin '%s': %s", name, e)
            return False

    def set_chat_skin(self, chat_id: int, skin_name: str) -> bool:
        try:
            self._skin_loader.load(skin_name)
            self._per_chat_skins[chat_id] = skin_name
            return True
        except Exception as e:
            logger.error("Failed to set chat skin: %s", e)
            return False

    def get_chat_skin(self, chat_id: int) -> str:
        return self._per_chat_skins.get(chat_id, self._skin_name)

    def clear_chat_skin(self, chat_id: int) -> None:
        self._per_chat_skins.pop(chat_id, None)

    def available_skins(self) -> List[str]:
        return self._skin_loader.available_skins()

    def reload_skins(self) -> None:
        """v2.3: Reload all skins from disk."""
        self._skin_loader.reload()

    def _get_skin_for_chat(self, chat_id: int) -> dict:
        skin_name = self._per_chat_skins.get(chat_id, self._skin_name)
        return self._skin_loader.load(skin_name)

    # ─── Per-Chat Config ───

    def set_chat_config(self, chat_id: int, **kwargs) -> PersonalityConfig:
        current = self._per_chat_configs.get(chat_id, self.config)
        new_config = PersonalityConfig(
            sarcasm_level=kwargs.get("sarcasm_level", current.sarcasm_level),
            metaphor_frequency=kwargs.get("metaphor_frequency", current.metaphor_frequency),
            exaggeration_level=kwargs.get("exaggeration_level", current.exaggeration_level),
            deadpan=kwargs.get("deadpan", current.deadpan),
            programmer_humor=kwargs.get("programmer_humor", current.programmer_humor),
            anime_references=kwargs.get("anime_references", current.anime_references),
            max_response_length=kwargs.get("max_response_length", current.max_response_length),
        )
        self._per_chat_configs[chat_id] = new_config
        return new_config

    def get_chat_config(self, chat_id: int) -> PersonalityConfig:
        # If custom instructions are active, they override the config
        traits = self._per_chat_traits.get(chat_id)
        if traits and traits.tone:
            base = self._per_chat_configs.get(chat_id, self.config)
            return self._instruction_processor.apply_to_config(traits, base)
        return self._per_chat_configs.get(chat_id, self.config)

    def reset_chat_config(self, chat_id: int) -> None:
        self._per_chat_configs.pop(chat_id, None)
        self.clear_chat_skin(chat_id)
        self._per_chat_traits.pop(chat_id, None)
        self._recent_responses.pop(chat_id, None)

    def get_chat_config_dict(self, chat_id: int) -> Dict[str, Any]:
        cfg = self.get_chat_config(chat_id)
        return {
            "sarcasm_level": cfg.sarcasm_level,
            "metaphor_frequency": cfg.metaphor_frequency,
            "exaggeration_level": cfg.exaggeration_level,
            "deadpan": cfg.deadpan,
            "programmer_humor": cfg.programmer_humor,
            "anime_references": cfg.anime_references,
            "max_response_length": cfg.max_response_length,
        }

    # ─── Custom Instructions ───

    def set_custom_instructions(self, chat_id: int, instructions: str) -> InstructionTraits:
        """Parse and apply custom instructions for a chat."""
        traits = self._instruction_processor.parse(instructions)
        self._per_chat_traits[chat_id] = traits
        logger.info("Chat %d custom instructions set: tone=%s", chat_id, traits.tone or "custom")
        return traits

    def clear_custom_instructions(self, chat_id: int) -> None:
        self._per_chat_traits.pop(chat_id, None)
        logger.info("Chat %d custom instructions cleared", chat_id)

    def get_custom_instructions_traits(self, chat_id: int) -> Optional[InstructionTraits]:
        return self._per_chat_traits.get(chat_id)

    def has_custom_instructions(self, chat_id: int) -> bool:
        traits = self._per_chat_traits.get(chat_id)
        return traits is not None and bool(traits.persona_context)

    def available_presets(self) -> List[str]:
        return self._instruction_processor.available_presets()

    def reload_presets(self) -> None:
        """v2.3: Reload trait presets from JSON files."""
        self._instruction_processor.reload_presets()

    # ─── v2.3: Startup Loading ───

    async def load_persisted_settings(self) -> int:
        """Load all persisted personality settings from the database.

        Delegates to memory.load_persisted_settings().
        Called on bot startup to restore custom instructions, skins,
        and configs that were set before the last restart.
        """
        return await memory.load_persisted_settings()

    # ─── Mood / Energy State ───

    def _get_state(self, chat_id: int) -> PersonalityState:
        state = self._per_chat_states.get(chat_id)
        if state is None:
            state = PersonalityState()
            self._per_chat_states[chat_id] = state
        return state

    def _update_mood(self, chat_id: int, was_sarcastic: bool) -> None:
        state = self._get_state(chat_id)
        now = datetime.utcnow()
        if state.last_interaction:
            elapsed = now - state.last_interaction
            if elapsed > timedelta(minutes=30):
                state.energy = min(1.0, state.energy + 0.2)
                state.interaction_streak = 0
            state.sarcasm_boost = max(0.0, state.sarcasm_boost - 0.05)
        state.last_interaction = now
        state.interaction_streak += 1
        if was_sarcastic:
            state.sarcasm_boost = min(0.3, state.sarcasm_boost + 0.1)
        if state.interaction_streak > 10:
            state.energy = max(0.2, state.energy - 0.05)

    def _mood_adjusted_sarcasm(self, chat_id: int, base: float) -> float:
        state = self._get_state(chat_id)
        energy_factor = 0.7 + 0.3 * state.energy
        return min(1.0, (base + state.sarcasm_boost) * energy_factor)

    # ─── Japanese Detection ───

    def _detect_japanese(self, text: str) -> Optional[str]:
        match = self._RE_JP.search(text)
        if match:
            return self._JP_KEYWORDS.get(match.group().lower())
        return None

    def _get_japanese_response(self, skin: dict, key: str) -> Optional[str]:
        return skin.get("japanese_responses", {}).get(key)

    # ─── Math Detection ───

    def _try_math(self, text: str) -> Optional[str]:
        match = self._RE_MATH.search(text)
        if not match:
            return None
        try:
            a, op, b = int(match.group(1)), match.group(2), int(match.group(3))
            if op == "+": r = a + b
            elif op == "-": r = a - b
            elif op in ("*", "x", "\u00d7"): r = a * b
            elif op == "/":
                if b == 0: return f"{a}/{b}? Dividing by zero. Bold. The universe doesn't like that. Neither do I."
                r = a / b
            else: return None
            return str(r)
        except Exception:
            return None

    # ─── v2.3: LLM Integration Hook ───

    async def _try_llm_rewrite(
        self,
        traits: InstructionTraits,
        base_response: str,
        user_message: str,
        skin: Optional[dict] = None,
    ) -> Optional[str]:
        """If LLM-backed persona rewriting is enabled and configured, use
        persona_context as a system prompt to rewrite the response in the
        persona's voice.

        The system prompt is the active skin's own identity (its `name`
        and `description` from data/personality/skins/*.json — previously
        unused fields) followed by the chat's custom instructions. This
        keeps the LLM anchored to whichever skin is actually active for
        this chat instead of drifting into a generic assistant voice when
        custom instructions are minimal, and it means a chat running the
        "cheerful" skin gets a cheerful grounding, not a hardcoded
        sarcastic one, if a different skin is ever added.

        Gated behind settings.personality_llm_enabled (off by default):
        this makes a real external API call on every message in a chat
        that has custom instructions set, so it's an explicit admin
        opt-in even when a provider key already happens to be configured
        for /ai. Bounded by settings.personality_llm_timeout_seconds —
        python-telegram-bot processes updates sequentially by default, so
        an unbounded call here would stall the whole bot, not just this
        chat.

        Returns None if the feature is disabled, no provider is
        available, or the call fails/times out — callers fall back to
        the classic rewrite pipeline in that case.
        """
        if not settings.personality_llm_enabled:
            return None
        if not traits or not traits.persona_context:
            return None

        skin = skin or {}
        grounding = f"You are {skin.get('name', 'Neko')}. {skin.get('description', '')}".strip()
        # Inject curated few-shot examples (from real conversation dataset)
        # to lock tone. Bounded so we never blow the context window.
        fewshot_block = ""
        if getattr(settings, "personality_fewshot_enabled", True):
            try:
                example_bank.load()
                limit = int(getattr(settings, "personality_fewshot_max", 3) or 3)
                skin_name = skin.get("name") or "neko"
                relevant = example_bank.get_relevant(
                    user_message or "", limit=limit, personality=str(skin_name).lower()
                )
                fewshot_block = example_bank.format_for_prompt(
                    relevant, max_chars=900, speaker=str(skin_name).title()
                )
            except Exception as fe:
                logger.debug("Few-shot injection skipped: %s", fe)
        parts = [p for p in (grounding, traits.persona_context, fewshot_block) if p]
        system_prompt = "\n\n".join(parts)

        try:
            # Try to import the AI plugin (optional dependency). Provider
            # availability itself is generate_response's job — checking
            # for specific keys here would just drift out of sync with
            # whatever providers bot/plugins/ai.py actually supports.
            from bot.plugins.ai import generate_response
            response = await asyncio.wait_for(
                generate_response(
                    system_prompt=system_prompt,
                    user_message=user_message,
                    base_response=base_response,
                ),
                timeout=settings.personality_llm_timeout_seconds,
            )
            if response and response.strip():
                return response.strip()
        except ImportError:
            logger.debug("AI plugin not available — persona_context will be unused")
        except asyncio.TimeoutError:
            logger.warning(
                "LLM persona rewrite timed out after %ss", settings.personality_llm_timeout_seconds
            )
        except Exception as e:
            logger.warning("LLM rewrite failed: %s", e)

        return None

    # ─── Rewrite Helpers ───

    def _pick_metaphor(self, skin: dict, topic: str, traits: Optional[InstructionTraits] = None) -> str:
        if traits and traits.themed_metaphors:
            themed = traits.themed_metaphors.get(topic) or traits.themed_metaphors.get("life")
            if themed:
                return random.choice(themed)
        pool = skin.get("metaphors", {}).get(topic, skin.get("metaphors", {}).get("life", [""]))
        return random.choice(pool) if pool else ""

    def _inject_sarcasm(self, text: str, prefixes: List[str], intensity: float = 0.5) -> str:
        if not text or random.random() > intensity:
            return text
        if any(w in text.lower() for w in ["yeah", "sure", "wow", "great", "good luck"]):
            return text
        prefix = random.choice(prefixes) if prefixes else ""
        if not prefix:
            return text
        return prefix + text[0].lower() + text[1:]

    def _shorten(self, text: str, max_len: int) -> str:
        """Trim to max_len, always respecting Telegram's 4096 hard limit
        regardless of what max_len a skin/config passes in."""
        if not text:
            return text
        hard_cap = 4090
        max_len = min(max_len or hard_cap, hard_cap)
        if len(text) <= max_len:
            return text
        break_at = text.rfind(".", 0, max_len)
        if break_at > max_len // 2:
            return text[: break_at + 1]
        # No good sentence break — prefer breaking on a space near the end
        # over cutting mid-word.
        space_at = text.rfind(" ", 0, max_len - 3)
        if space_at > max_len // 2:
            return text[:space_at].rstrip() + "..."
        truncated = text[: max_len - 3]
        if truncated and "\ud800" <= truncated[-1] <= "\udfff":
            truncated = truncated[:-1]
        return truncated + "..."

    def _deadpan(self, text: str) -> str:
        text = self._RE_MULTI_BANG.sub(".", text)
        text = self._RE_MULTI_QMARK.sub("?", text)
        text = self._RE_EMOJI.sub("", text)
        text = self._RE_LAUGHTER.sub("", text)
        text = self._RE_MULTISPACE.sub(" ", text)
        return text.strip()

    async def _get_callback(self, chat_id: int, context: str) -> Optional[str]:
        profile = await memory.get_user_profile(chat_id)
        if not profile:
            return None
        gags = profile.get("running_gags", [])
        if not gags:
            return None
        for gag in reversed(gags[-3:]):
            if gag in context.lower() or random.random() < 0.3:
                return gag
        return None

    def _select_joke_category(self, intents: List[str]) -> Optional[str]:
        mapping = {
            "coding": "programming", "python": "programming", "git": "programming",
            "docker": "programming", "database": "programming", "frontend": "programming",
            "coffee": "coffee", "sleep": "coffee",
            "anime": "anime", "anime_greeting": "anime",
            "gaming": "gaming", "skill_issue": "gaming",
            "linux": "linux", "windows": "linux", "mac": "linux",
            "meme": "internet", "sad": "existential", "angry": "existential",
            "introvert_reveal": "existential", "emotion_challenge": "existential",
            "sticker_activity": "general", "math": "programming",
        }
        for intent in intents:
            if intent in mapping:
                return mapping[intent]
        return "general"

    # ─── Get themed content (custom instructions override skin) ───

    def _get_openers(self, skin: dict, traits: Optional[InstructionTraits]) -> List[str]:
        if traits and traits.themed_openers:
            return traits.themed_openers
        return skin.get("openers", [])

    def _get_closers(self, skin: dict, traits: Optional[InstructionTraits]) -> List[str]:
        if traits and traits.themed_closers:
            return traits.themed_closers
        return skin.get("closers", [])

    def _get_prefixes(self, skin: dict, traits: Optional[InstructionTraits]) -> List[str]:
        if traits and traits.themed_prefixes:
            return traits.themed_prefixes
        return skin.get("sarcastic_prefixes", [])

    # ─── v2.3: Anti-repetition ───

    def _check_and_record_response(self, chat_id: int, response: str) -> str:
        """Check if the response was recently sent to this chat.

        If it was, try to modify it slightly. Returns the (possibly modified) response.
        """
        if not response or not response.strip():
            return response or "..."

        recent = self._recent_responses.get(chat_id, [])
        # Comparison is casefold+whitespace-normalized to catch near-duplicates
        # (same wording, different punctuation/case), not just exact repeats.
        norm = " ".join(response.casefold().split())
        recent_norm = {" ".join(r.casefold().split()) for r in recent}
        if norm in recent_norm or response in recent:
            # Try removing the opener to vary it
            if "\n" in response:
                parts = response.split("\n", 1)
                if len(parts) > 1 and parts[1] not in recent:
                    response = parts[1]
            # If still repeated, prepend a variant
            if response in recent:
                variants = ["", "Anyway. ", "Right. ", "Look. "]
                for v in variants:
                    candidate = v + response
                    if candidate not in recent:
                        response = candidate
                        break
        recent.append(response)
        self._recent_responses[chat_id] = recent[-20:]
        return response

    # ─── Main Rewrite Pipeline ───

    async def rewrite(
        self,
        chat_id: int,
        base_response: str,
        intents: List[str],
        user_message: str,
        include_joke: bool = True,
    ) -> str:
        if not settings.enable_personality:
            return base_response

        skin = self._get_skin_for_chat(chat_id)
        traits = self._per_chat_traits.get(chat_id)
        config = self.get_chat_config(chat_id)

        # v2.3: Try LLM rewrite first when persona_context is available
        if traits and traits.persona_context:
            llm_result = await self._try_llm_rewrite(traits, base_response, user_message, skin)
            if llm_result:
                result = self._shorten(llm_result, config.max_response_length)
                self._update_mood(chat_id, was_sarcastic=False)
                result = self._check_and_record_response(chat_id, result)
                out = (result or "").strip()
                return out if out else "..."

        result = base_response
        joke_category = self._select_joke_category(intents)
        was_sarcastic = False

        # Metaphor injection
        if random.random() < config.metaphor_frequency and joke_category:
            metaphor = self._pick_metaphor(skin, joke_category, traits)
            if metaphor:
                if random.random() < 0.5:
                    result = f"That's {metaphor}. {result}"
                else:
                    result = f"{result} It's basically {metaphor}."

        # Sarcasm injection
        adjusted_sarcasm = self._mood_adjusted_sarcasm(chat_id, config.sarcasm_level)
        if random.random() < settings.personality_sarcasm_probability:
            prefixes = self._get_prefixes(skin, traits)
            new_result = self._inject_sarcasm(result, prefixes, adjusted_sarcasm)
            if new_result != result:
                was_sarcastic = True
            result = new_result

        # Joke injection (only roll the DB queries if the random check passes)
        if include_joke and random.random() < 0.3:
            can_joke = await memory.can_tell_joke(chat_id, min_interval=settings.personality_joke_min_interval)
            if can_joke:
                recent_ids = await memory.get_recent_joke_ids(chat_id, limit=15)
                joke = jokes_db.get_random(joke_category, exclude_ids=recent_ids)
                if joke:
                    await memory.record_callback(chat_id, joke.id, user_message[:50])
                    result = f"{result}\n\n{joke.text}"

        # Conversation-aware callback
        callback = await self._get_callback(chat_id, user_message)
        if callback and random.random() < 0.2:
            ctx = await memory.get_context(chat_id, limit=5)
            if ctx:
                recent_text = " ".join(m.content for m in ctx[-3:])
                if callback.lower() in recent_text.lower() or random.random() < 0.15:
                    result = (
                        f"{result}\n\n"
                        f"(Still thinking about that whole '{callback}' situation. "
                        f"Not judging. Much.)"
                    )

        # Opener
        openers = self._get_openers(skin, traits)
        if random.random() < 0.4 and openers:
            opener = random.choice(openers)
            if opener:
                result = f"{opener} {result}"

        # Closer
        closers = self._get_closers(skin, traits)
        if random.random() < 0.3 and closers:
            closer = random.choice(closers)
            if closer:
                result = f"{result}\n\n{closer}"

        # v2.3: Deadpan filter respects deadpan_safe preset flag
        if config.deadpan:
            if not (traits and not traits.deadpan_safe and traits.themed_closers):
                result = self._deadpan(result)

        result = self._shorten(result, config.max_response_length)
        self._update_mood(chat_id, was_sarcastic)

        # v2.3: Anti-repetition check
        result = self._check_and_record_response(chat_id, result)

        logger.debug(
            "personality_rewrite",
            chat_id=chat_id, skin=self.get_chat_skin(chat_id),
            custom_instructions=bool(traits and traits.tone),
            sarcastic=was_sarcastic, response_len=len(result),
        )
        out = (result or "").strip()
        return out if out else "..."

    # ─── Direct Generation ───

    async def generate_direct(
        self, chat_id: int, intents: List[str], user_message: str,
    ) -> str:
        skin = self._get_skin_for_chat(chat_id)
        responses = skin.get("direct_responses", {})

        # Priority 1: Japanese romaji
        jp_key = self._detect_japanese(user_message)
        if jp_key:
            jp_response = self._get_japanese_response(skin, jp_key)
            if jp_response:
                config = self.get_chat_config(chat_id)
                result = jp_response
                openers = self._get_openers(skin, self._per_chat_traits.get(chat_id))
                if random.random() < 0.3 and openers:
                    opener = random.choice(openers)
                    if opener:
                        result = f"{opener} {result}"
                result = self._shorten(result, config.max_response_length)
                self._update_mood(chat_id, was_sarcastic=False)
                result = self._check_and_record_response(chat_id, result)
                out = (result or "").strip()
                return out if out else "..."

        # Priority 2: Math
        math_result = self._try_math(user_message)
        if math_result and "math" in intents:
            pool = responses.get("math", [
                f"The answer is {math_result}. You're welcome. I accept payment in coffee.",
                f"{math_result}. Unless you're trying to flex basic math as a meme, in which case the obvious choice is the wrong one.",
            ])
            base = random.choice(pool) if pool else math_result
            return await self.rewrite(chat_id, base, intents, user_message, include_joke=True)

        # Priority 3: Intent-based
        primary = intents[0] if intents else "general"
        pool = responses.get(primary, responses.get("general", [
            "Yeah, I don't know what to do with that.",
            "Interesting. Not really, but I'm polite.",
            "My neural networks are buffering. Try again with more caffeine.",
        ]))
        base = random.choice(pool)
        return await self.rewrite(chat_id, base, intents, user_message, include_joke=True)


personality = PersonalityLayer()
