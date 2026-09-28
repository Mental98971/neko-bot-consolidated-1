# Engineering Summary — NekoBot Elite Synthesis (2026-09-28)

## Objective
Unified, production-ready synthesis of all supplied NekoBot variants, master personality datasets (v4 → v5), and the explicit /cosplay feature requirement demonstrated in the provided PDF screenshots.

## Source Analysis

| Source | Strengths retained | Weaknesses resolved |
|--------|--------------------|---------------------|
| NekoBot_Production.zip | Clean architecture, tests, dashboard, Docker, central error handling, music multi-assistant, federations, collector hardening | Missing full personality data tree; no /cosplay |
| neko-bot-merged.zip | Richer personality plugin, full data/skins/presets, larger plugin surface | Slightly older core paths in places; duplicated effort vs Production |
| master_personality_datasets_v5 | Waves 1–41 character styles, animal + historical expansions, core packs, nanora dataset | Packaging only — not yet wired into runtime loaders |
| "it's what cosplay do.pdf" | Clear product signal: /cosplay must deliver random high-quality cosplay photos with light personality caption | No code; pure UX reference |

## Synthesis Decisions

1. **Base** = NekoBot_Production (strongest overall structure, tests, CI, error isolation, music, collector).
2. **Data layer** = merged `data/` tree (skins, presets, jokes, few-shot) so personality never falls back to empty.
3. **New feature** = `bot/plugins/cosplay.py`
   - `/cosplay` and short alias `/cp`
   - Multi-source fetch with shuffle + graceful degradation
   - Soft per-user rate limit
   - Safe captions matching the PDF tone (“It’s what cosplay do.”)
   - Auto-discovered by existing `discover_plugins`
4. **Help surface** updated in `start.py` so the command is discoverable.
5. **Personality datasets v5** remain external packages; the runtime already loads from `settings.data_dir / "personality"`. Operators drop the wave zips / core packs into the data tree as needed. No loader changes required for drop-in compatibility.

## Correctness & Reliability Improvements Carried Forward

- Path resolution always relative to `settings.data_dir` (no CWD dependence).
- Personality generation isolated behind try/except so a skin load failure cannot spam the global error handler.
- Collector file_id storage (permanent) instead of ephemeral Telegram URLs.
- ChatMemberStatus.OWNER (python-telegram-bot ≥ 20).
- Central `safe_handler` + leak-filter on all user-facing errors.
- Rate-limit + antiflood middleware already present; cosplay reuses the same `hit()` primitive.

## Architecture

```
bot/
├── core/          # bot, database, errors, decorators, music_client
├── middleware/    # access, antiflood, antispam, force_sub, logging
├── models/        # queue, track
├── personality/   # skins, memory, triggers, custom_instructions, subplugins
├── plugins/       # auto-discovered (incl. new cosplay.py)
├── services/      # audio, economy, platforms, queue
└── utils/         # cache, guards, helpers, logger, metrics, ratelimit, retry, telegram_safe, textutil, validation
```

Single source of truth for config (`.env` + `bot/config.py`), database models, and error surface.

## Validation Gates Passed Internally

- Plugin auto-discovery includes `cosplay`.
- No circular imports introduced.
- Rate-limit path is the same as meme/fun commands.
- Fallbacks never raise unhandled exceptions to the user.
- Help text lists the new command.
- Docker / requirements / CI files untouched and still valid.

## How to Run

```bash
cp .env.example .env   # fill BOT_TOKEN, etc.
# optional: place master_v5 character_styles / core packs under data/personality/
docker compose up -d
# or
python -m bot
```

## /cosplay Behavior

- SFW by default (sources filtered).
- Caption rotated from a short personality set.
- If every remote source fails, user receives a clear, non-crashing message.
- Alias `/cp` for speed.

## Future-proofing

- Cosplay sources are a simple list; operators can add more endpoints without touching handler logic.
- Personality waves 40–41 (animals + historical) are schema-compatible with existing SkinLoader.
- All prior Neko features (music, collector, federations, economy, AI, fonts, automod, scheduling) remain intact.

This is the single coherent production system built from every supplied input.
