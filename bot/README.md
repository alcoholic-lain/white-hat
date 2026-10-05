# harness-bot

Discord front-end for the Rust agent harness. M1.4: mention / DM / thread chat, streaming edits, allowlist.

## Setup (uv)

```powershell
cd bot
uv sync
Copy-Item .env.example .env     # then fill DISCORD_TOKEN and ALLOWED_USER_IDS
uv run harness-bot              # or: uv run python -m bot.main
```

Dev checks: `uv run pytest`, `uv run ruff check .`

## Discord Developer Portal

Bot tab -> enable **Message Content Intent**. Invite with scopes `bot` + `applications.commands` and
permissions: View Channels, Send Messages, Send Messages in Threads, Create Public Threads, Read Message History.

## Behaviour

- Mention the bot in a channel -> it opens a thread and replies there. Replies in that thread need no mention.
- DMs work with their own session. Bots (including itself) are ignored. Non-allowlisted users get no response.
- Replies stream by editing one message (~1 edit / 1.2 s), then get split into <= 2000-char chunks with code fences kept balanced.
- `<think>...</think>` is stripped unless `SHOW_THINKING=true`.

## Harness compatibility

The client speaks the spec protocol (`POST /sessions`, SSE on `/sessions/{id}/messages`, bearer token, `X-User-Id`).
The harness at M1.3 has none of that yet, so when `POST /sessions` returns 404 the bot uses **fallback mode**:
history is kept in `bot_state.db` and each turn calls `POST /chat` (no real streaming, so the reply appears when complete).
No bot change is needed when the harness gains sessions; new conversations switch automatically (existing `local-*` ones keep working).

The bot sends `X-User-Id` and `Authorization` already; the current harness ignores them.
