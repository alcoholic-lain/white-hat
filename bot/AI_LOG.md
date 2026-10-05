
# Agent Harness Development Log - Session Handover

## Project Summary
Multi-tier Agent Harness: Rust backend service + Python Discord bot + LM Studio.
- **Backend**: Rust, Axum 0.7 HTTP API, Tokio, SQLite/sqlx (memory crate), reqwest 0.11
- **Bot**: Python (uv project), discord.py 2.x, httpx
- **LLM**: LM Studio OpenAI-compatible API (`gemma-4-31b-it-heretic-i1`)
- **Status**: M1.3 + M1.4 done. Discord chat works end to end with streaming. Harness still lacks the spec's sessions/SSE/auth (bot has a fallback for this).
- **Dev machine**: Windows / PowerShell. Repo root `white-hat/` contains `harness/` (Rust workspace) and `bot/` (Python) side by side.

---

## Completed Milestones

### M0: Project Setup & Workspace Structure
- Rust workspace at `harness/`, 5 crates (core, api, lms, llm-lmstudio, memory)
- GitHub repo "white-hat" (`https://github.com/dante.alcoholic/white-hat`)

### M1.1: Core API Server Scaffold
- Axum server on 127.0.0.1:8787, `/status` returns `{"status":"ok","version":"0.1.0"}`

### M1.2: Model Management & LLM Client Stubs
- `lms` crate: `LmsManager` stubs (list_models, list_loaded) - still stubs
- `llm-lmstudio` crate: `LmStudioClient`
- `core` crate: empty `types.rs` / `traits.rs` stubs

### M1.3: SQLite Memory Layer
- `memory` crate: tables `sessions`, `messages`, `runs`; CRUD: create/get session, add/get messages, create/get run, update_run_status
- NOT yet wired into the API (no route uses it)

### M1.3+: LM Studio Integration
- `GET /models`, `POST /chat` (non-streaming; needs max_tokens >= 200 for non-empty output)

### M1.4: Discord Bot (Python, uv)  -- DONE, verified manually by user
- `bot/` uv project. Run: `cd bot; uv sync; Copy-Item .env.example .env; uv run harness-bot`
- Verified in real Discord by the user: bot logs in, harness health check with backoff retry, `/status` works, DM chat works, `@white-hat` mention in a channel opens a thread and replies, follow-ups in the thread work.
- Bot token + allowlist ID come from the Discord Developer Portal. **Message Content Intent must be on** (Bot tab).
- Behaviour: mention -> thread (falls back to inline reply if no thread permission, or `USE_THREADS=false`); replies in a bot-owned thread need no mention; DMs get their own session; bots (incl. itself) ignored; unauthorised users get no response at all.
- `<think>...</think>` stripped (streaming-safe, tags split across chunks handled; unclosed think with no other output is shown instead of swallowed). `SHOW_THINKING=true` disables.
- Output is split into <=2000-char chunks with code fences kept balanced.
- State: `bot_state.db` (SQLite) maps conversation key (`dm:`, `thread:`, `chan:`) -> session id and holds local history for fallback mode. Sessions survive bot restarts.

### M1.4+: Streaming (Rust + bot)
- **Rust** (`llm-lmstudio/src/lib.rs`): real `chat_stream()` - calls LM Studio with `stream:true`, reassembles SSE lines from raw bytes (UTF-8 safe), returns an `mpsc::Receiver<Result<String,String>>`; dropping the receiver aborts the upstream request (cancellation).
- **Rust** (`api/src/main.rs`): new `POST /chat/stream` -> SSE events `{"type":"token","text":..}`, then `{"type":"final","text":<full>}` or `{"type":"error","code":"lm_unreachable"|"lm_error","message":..}`; keep-alive comments enabled.
- Verified by compiling the user's sources in a scratch workspace (rustc 1.91) and running against a fake LM Studio that splits events into 7-byte pieces (emoji and non-ASCII survive). NOT yet verified against the real model by me; the user confirmed edit-mode streaming ran but felt laggy.
- **Bot**: fallback path tries `/chat/stream` first; a 404 is remembered (probe once) and it falls back to `/chat`. Stream error events raise typed errors and nothing is stored in history.
- Bot `chat()` now treats a 200 response with an `error` body as an error (the Rust `/chat` returns 200 + `{"error":...}` on failures).

### M1.4++: Stream modes, access control (all unit-tested; not yet user-verified live)
- **STREAM_MODE=lines (default)**: Discord "typing..." indicator + each finished line/paragraph sent as its own message. Fenced code blocks are held and sent whole; unclosed fence is closed at finish; lines arriving faster than `LINE_INTERVAL` (0.8s) are batched; a background ticker releases held lines. Reason: Discord's edit rate limit (~5/5s) made edit mode laggy and unfixable. **STREAM_MODE=edit** keeps the old single-message edit mode (~1 edit/1.2s).
- **Access**: `ALLOWED_USER_IDS` (trusted), `ADMIN_USER_IDS` (privileged; empty = same as allowlist). `ALLOW_ALL_USERS=true` opens chat to everyone; `ALLOWED_GUILD_IDS` limits open access to listed servers (also blocks strangers' DMs). Non-trusted users get `USER_COOLDOWN` (5s, adds a hourglass reaction) and share a global queue (`MAX_CONCURRENT`=1; waiting users see a temporary "Waiting for the model..." message).
- **Admin**: `/status` is admin-only ("Admin only." ephemeral otherwise). Admins are never rate-limited. Planned: gate P2 tools (shell/files) on the same admin check at the harness.
- **Mentions**: default `allowed_mentions=none`, so model-written `@everyone` never pings (user observed this and asked to enable it). `ALLOW_MENTIONS=true` allows users/roles/@everyone/@here pings; Discord still requires the bot to have "Mention Everyone" per server. Replies never ping the author.
- Multi-server: works with one process; conversations keyed by unique thread/channel/DM IDs. Remove `DISCORD_GUILD_ID` so slash commands register globally. Recommend turning off "Public Bot" in the Developer Portal.

---

## Current File Structure

```
white-hat/
├── harness/                         (Rust workspace; Cargo.toml, AI_LOG.md)
│   └── crates/
│       ├── core/        lib.rs, types.rs (stub), traits.rs (stub)
│       ├── api/         src/main.rs  -> /status, /models, /chat, /chat/stream
│       ├── lms/         src/lib.rs   (stubs)
│       ├── llm-lmstudio/ src/lib.rs  -> chat(), chat_stream(), list_models()
│       └── memory/      src/lib.rs   (SQLite layer; not wired to API)
└── bot/                             (Python uv project)
    ├── pyproject.toml, uv.lock, .env.example, .gitignore, README.md
    ├── src/bot/
    │   ├── main.py       Discord client, routing, slash /status, run loop
    │   ├── config.py     env settings + access rules (is_admin/is_trusted/is_permitted)
    │   ├── client.py     httpx client, SSE parser, typed HarnessError
    │   ├── backend.py    sessions API vs local fallback (/chat/stream -> /chat)
    │   ├── streaming.py  EditSink (edit mode), LineSink (lines mode)
    │   ├── render.py     ThinkStripper, chunk_text, extract_prompt
    │   ├── limits.py     per-user Cooldown
    │   └── state.py      SQLite conversation map + local history
    └── tests/            test_render, test_client, test_sinks, test_access
```

---

## Working Features

### Harness API (`http://127.0.0.1:8787`, no auth yet)
1. `GET /status` -> `{"status":"ok","version":"0.1.0"}`
2. `GET /models` -> `{"models":[...]}` (200 + `{"error":..}` on failure)
3. `POST /chat` body `{"messages":[...],"temperature":0.7,"max_tokens":200,"stream":false}` -> `{"response":"..."}` (200 + `{"error":..}` on failure)
4. `POST /chat/stream` same body -> SSE `token` / `final` / `error` events

### Bot env vars (all optional except the first two)
`DISCORD_TOKEN`, `ALLOWED_USER_IDS` (required; at least one of ALLOWED/ADMIN), `ADMIN_USER_IDS`, `HARNESS_URL`, `HARNESS_TOKEN`, `USE_THREADS`, `SHOW_THINKING`, `SYSTEM_PROMPT`, `DISCORD_GUILD_ID`, `TEMPERATURE`, `MAX_TOKENS` (1024), `HISTORY_MESSAGES` (24), `STREAM_MODE`, `LINE_INTERVAL`, `EDIT_INTERVAL`, `ALLOW_ALL_USERS`, `ALLOWED_GUILD_IDS`, `USER_COOLDOWN`, `MAX_CONCURRENT`, `ALLOW_MENTIONS`, `STATE_PATH`, `LOG_LEVEL`. Editing `.env` requires a bot restart.

### Quality
- Bot: `uv run pytest` -> 38 passed; `uv run ruff check .` clean.
- Rust: compiles clean in scratch workspace; live-tested `/chat/stream` against a fake LM Studio and the real bot backend code.

---

## Known Issues & Notes

1. **Harness is behind the spec**: no `POST /sessions`, no `/sessions/{id}/messages` SSE, no `/runs/{id}`, no bearer auth / `X-User-Id` enforcement, no server-side allowlist. The bot sends the headers already and uses fallback mode (local history in `bot_state.db`, `/chat/stream`). New conversations switch to real sessions automatically once `/sessions` exists; old `local-*` ones keep working.
2. `Backend.recover()` guesses the `GET /runs/{id}` payload keys (final_text/text/result/response/final.text) - align with openapi.yaml when it exists.
3. Streaming ignores LM Studio `delta.reasoning_content`; thinking models may show a long pause before the first visible line (typing indicator stays on).
4. Rust `/chat` and `/models` return HTTP 200 with `{"error":...}` on failure; should become proper status codes + typed error codes (`no_model_loaded`, `lm_unreachable`, `context_overflow`, ...) per spec.
5. `lms` CLI wrapper still stubs; `memory` crate not used by the API; `core` crate empty.
6. Required Cargo deps for the new Rust code (user hit a missing-crate error once): `llm-lmstudio`: `reqwest = { version="0.11", features=["json","stream"] }`, `futures-util = "0.3"`, `tokio = { version="1", features=["sync","rt"] }`; `api`: `tokio-stream = "0.1"`. I never saw the real `Cargo.toml` files.
7. Bot `state.py` uses synchronous sqlite3 on the event loop (tiny single-row ops; fine for now).
8. In line mode a model that writes a whole paragraph on one line shows it as one message when the paragraph completes (sentence-level splitting was offered, not built).
9. Shared threads: everyone who posts in a bot thread joins the same conversation/session.
10. No content moderation; model is an uncensored variant. With `ALLOW_ALL_USERS` the operator is responsible for output.
11. Not yet checked live by the user: lines mode, open access/cooldown/queue, admin `/status` split, `ALLOW_MENTIONS`.

---

## Testing Instructions

```powershell
# Harness (from white-hat/harness)
lms server start                      # LM Studio on :1234 with a model loaded
cargo run -p harness-api              # listens on 127.0.0.1:8787

# Streaming check
curl.exe -N -X POST http://localhost:8787/chat/stream -H "Content-Type: application/json" `
  -d '{\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"temperature\":0.7,\"max_tokens\":200,\"stream\":true}'

# Bot (from white-hat/bot)
uv sync
uv run harness-bot                    # or: uv run python -m bot.main
uv run pytest ; uv run ruff check .
```
Manual Discord checks: `/status` (admin), mention in a channel (thread appears), reply in thread without mention, DM, non-allowlisted account (silent unless ALLOW_ALL_USERS), long code reply (fences intact), kill the harness mid-run (warning message, bot survives), restart bot (conversation continues).

---

## Next Milestones

### Harness: spec alignment (recommended next)
- [ ] `POST /sessions` (wire up `memory` crate), `POST /sessions/{id}/messages` SSE with run_id events, `GET /runs/{id}`, `POST /sessions/{id}/cancel`
- [ ] Bearer token + `X-User-Id` enforcement and a server-side allowlist/admin check
- [ ] Proper HTTP status codes and typed error codes; `lm_unreachable`/`no_model_loaded` detection
- [ ] Consider handling `reasoning_content` in streams

### M1.5: Slash Commands & Model Picker (bot)
- [ ] `/chat <prompt>` slash command, `/models` picker (needs `lms` or `/models` + load), `/stop`, `/reset` (use `State.delete_session`)
- [ ] Per-user model selection and session management

### P2: Tool Registry & Agent Loop - gate tools on the admin check
### P3: LLMCompiler Implementation
### P4: External Tools Integration
### P5: Skills System

---

## Dependencies & Environment

### Rust (key)
axum 0.7, tokio 1, tokio-stream 0.1, sqlx 0.7 (SQLite), reqwest 0.11 (json, stream), futures-util 0.3, serde/serde_json, uuid, chrono, tracing

### Python (bot, managed by uv; uv picked CPython 3.14 on the dev machine)
discord.py >=2.4 (2.7.1 installed), httpx >=0.27, python-dotenv; dev: pytest, pytest-asyncio, ruff. Voice warnings (PyNaCl/davey) are harmless.

### External
- LM Studio on `localhost:1234`, model `gemma-4-31b-it-heretic-i1`, endpoints `/v1/models`, `/v1/chat/completions`
- Discord application "white-hat" (Message Content Intent on); consider disabling "Public Bot"

---

## Development Notes

### Architecture Decisions
1. Workspace pattern: separate crates for modularity
2. SQLite for persistence
3. OpenAI-compatible LM Studio API
4. Async everywhere (Tokio / asyncio)
5. Bot speaks the spec protocol but degrades gracefully to the current harness (probe once, remember)
6. Safety defaults: allowlist mandatory, mentions suppressed, open access and pings are explicit opt-ins
7. Discord UX: lines mode over edit mode because of edit rate limits

---

## Quick Reference for Next Session

1. Start LM Studio: `lms server start` (model loaded)
2. Run API: `cargo run -p harness-api` from `white-hat/harness`
3. Run bot: `uv run harness-bot` from `white-hat/bot`
4. Likely next task: harness sessions + SSE + auth (so streaming/cancel/`/stop` are real), then M1.5 bot commands
5. Ask for current `Cargo.toml` files if touching Rust dependencies again
