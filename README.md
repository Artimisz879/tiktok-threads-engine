# tiktok-threads-engine

Autonomous content & conversion engine: takes a **TikTok affiliate product link**, analyzes it, waits for the right moment, writes a Threads post, self-reviews it, publishes, and tracks performance — all driven by a Telegram bot you control.

Built for affiliate marketers who want hands-off, trend-aware posting on [Threads](https://www.threads.net) with real safety rails (dry-run mode, rate limits, quiet hours, affiliate disclosure, a critic pass before anything goes live).

## How it works

```
 TikTok product link (via Telegram)
        │
        ▼
 ┌─────────────┐   ┌──────────────┐   ┌──────────────┐
 │  Product    │──▶│  Intelligence│──▶│   Smart Queue │
 │  Resolver   │   │  (LLM: 3 calls)│  │  (deterministic)│
 └─────────────┘   └──────────────┘   └──────┬───────┘
                                              │ trend ≥ threshold
                                              │ or max wait elapsed
                                              ▼
 ┌─────────────┐   ┌──────────────┐   ┌──────────────┐
 │  Analytics  │◀──│   Publisher  │◀──│  Critic      │
 │  (1/6/24/72h)│  │  (Threads API)│   │  (LLM gate)  │
 └─────────────┘   └──────────────┘   └──────────────┘
```

- **Product resolution** — TikTok Shop links are resolved via the public metadata endpoint (or the official TikTok Shop API if you have access).
- **Intelligence** — an LLM analyzes the product, matches it against live trends (RSS feeds by default, optional web-search provider), and drafts content candidates.
- **Smart queue** — a deterministic opportunity engine scores each product against trends. Strong match → post now. No match → wait (up to a configurable window) and fall back to an evergreen angle.
- **Critic gate** — a second LLM pass reviews the candidate (claims, tone, disclosure, safety) before publishing.
- **Publishing** — Threads Graph API with built-in rate limiting, quiet hours, similarity dedup, and affiliate disclosure. `DRY_RUN=true` runs the entire pipeline without publishing anything.
- **Analytics** — checkpoints at 1/6/24/72h pull reach/engagement back into the DB and report via Telegram.
- **Restart-safe** — all state lives in SQLite; a restart simply resumes pending work.

The LLM budget per full product cycle is **4 calls** (analyst, trend match, candidates, critic). All scoring, gating, and scheduling is deterministic and never touches the LLM.

## Features

- Telegram-first control: send a product link, get approvals, drafts, and reports
- Instant mode (post now) and smart mode (wait for trend opportunity)
- Dry-run mode for safe end-to-end testing
- Anti-spam rails: max posts/day, min hours between posts, quiet hours, similarity threshold
- Strategy memory: past performance feeds back into future content choices
- Mock LLM provider for fully offline development
- Local admin/debug API (bound to 127.0.0.1, token-protected)

## Requirements

- Python **3.12+**
- A Telegram bot token ([@BotFather](https://t.me/BotFather))
- An OpenAI-compatible LLM endpoint (OpenRouter, OpenAI, or set `LLM_PROVIDER=mock` for offline dev)
- Threads app credentials (Meta for Developers) for real publishing

## Quick start

```bash
# 1. Clone
git clone https://github.com/<you>/tiktok-threads-engine.git
cd tiktok-threads-engine

# 2. Create a virtualenv and install
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/macOS
pip install -e ".[dev]"

# 3. Configure
cp .env.example .env          # Windows: copy .env.example .env
# edit .env — at minimum fill in:
#   TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_ID
#   LLM_PROVIDER / LLM_API_KEY  (or LLM_PROVIDER=mock)

# 4. (Optional) Get Threads access token
python -m scripts.threads_login

# 5. Run
python -m app.main
```

Keep `DRY_RUN=true` (the default) until you trust the output, then flip `AUTO_PUBLISH=true` to go live.

## Project structure

```
app/
├── main.py              # entrypoint: DB + pipeline + Telegram + scheduler + admin API
├── pipeline.py          # orchestrator (state machine driver)
├── state_machine.py     # product lifecycle states
├── config.py            # pydantic-settings (.env)
├── database.py          # SQLAlchemy / SQLite
├── models.py            # ORM models
├── schemas.py           # pydantic schemas
├── intelligence/        # LLM: product analyst, trend matcher, strategist, critic
├── opportunity/         # deterministic opportunity scoring engine
├── trends/              # trend providers (RSS, optional search API) + scoring
├── product/             # product resolution (TikTok Shop metadata / API)
├── publishing/          # Threads client + publisher (rate limits, dry-run)
├── analytics/           # post-publish analytics checkpoints
├── learning/            # strategy memory (feedback loop)
├── llm/                 # LLM client + mock provider
├── telegram/            # bot (commands, approvals, notifications)
├── scheduler/           # APScheduler jobs
├── security/            # prompt-injection & URL validation
└── api/                 # local admin/debug API
scripts/
├── threads_login.py     # interactive Threads token flow
└── demo.py              # demo run
tests/                   # 115 tests (pytest, respx for HTTP mocking)
```

## Testing

```bash
pytest
```

The test suite runs fully offline (mock LLM provider, `respx` for HTTP).

## Configuration

All settings live in `.env` (see [.env.example](.env.example) for the full annotated list). Highlights:

| Setting | Default | Purpose |
|---|---|---|
| `DRY_RUN` | `true` | Run everything, publish nothing |
| `AUTO_PUBLISH` | `false` | Master switch for real publishing |
| `OPPORTUNITY_THRESHOLD` | `75` | Trend score needed to post in smart mode |
| `SMART_MAX_WAIT_HOURS` | `24` | Max wait before falling back to evergreen |
| `MAX_POSTS_PER_DAY` | `4` | Hard posting cap |
| `MIN_HOURS_BETWEEN_POSTS` | `6` | Minimum spacing |
| `QUIET_HOURS` | `23:00-08:00` | No posting window |
| `CONTENT_LANGUAGE` | `rojak` | Output language style |

## Safety notes

- This project automates posting to a real social platform. Use it in line with the [Threads Terms of Service](https://about.fb.com/terms/) and applicable platform automation policies.
- The admin API binds to localhost only and requires `ADMIN_TOKEN`.
- Never commit `.env`. The `.gitignore` covers it, but double-check before your first push.

## License

[MIT](LICENSE)
