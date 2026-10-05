"""Chooses between the spec's session/SSE API and the current /chat fallback.

Sessions created while the harness lacks `POST /sessions` get a `local-` id and keep
their history in the bot's SQLite. Once the harness gains sessions, new conversations
use them automatically; old local ones keep working.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator

from bot.client import Event, HarnessClient, HarnessError
from bot.config import Settings
from bot.render import strip_thinking
from bot.state import State

log = logging.getLogger("bot.backend")

LOCAL_PREFIX = "local-"


class Backend:
    def __init__(self, client: HarnessClient, state: State, settings: Settings):
        self.client = client
        self.state = state
        self.settings = settings
        self._stream_supported: bool | None = None  # None = not probed yet

    async def ensure_session(self, key: str, user_id: int) -> str:
        sid = self.state.get_session(key)
        if sid:
            return sid
        try:
            sid = await self.client.create_session("discord", key, user_id)
        except HarnessError as exc:
            if exc.code != "not_found":
                raise
            log.info("Harness has no /sessions yet; using local history for %s", key)
            sid = LOCAL_PREFIX + uuid.uuid4().hex
        self.state.set_session(key, sid)
        return sid

    async def send(self, session_id: str, user_id: int, text: str) -> AsyncIterator[Event]:
        if session_id.startswith(LOCAL_PREFIX):
            async for ev in self._send_local(session_id, user_id, text):
                yield ev
        else:
            async for ev in self.client.stream_message(session_id, user_id, text):
                yield ev

    async def _send_local(self, session_id: str, user_id: int, text: str) -> AsyncIterator[Event]:
        s = self.settings
        messages: list[dict[str, str]] = []
        if s.system_prompt:
            messages.append({"role": "system", "content": s.system_prompt})
        messages += self.state.get_messages(session_id, s.history_messages)
        messages.append({"role": "user", "content": text})

        yield Event("run_started", None, {"mode": "direct"})
        reply: str | None = None

        if self._stream_supported is not False:
            parts: list[str] = []
            final_text: str | None = None
            try:
                async for ev in self.client.chat_stream(
                    messages, temperature=s.temperature, max_tokens=s.max_tokens, user_id=user_id
                ):
                    if ev.type == "token":
                        parts.append(str(ev.data.get("text", "")))
                    elif ev.type == "final":
                        final_text = ev.data.get("text")
                        continue  # we emit our own final below
                    elif ev.type == "error":
                        raise HarnessError(ev.data.get("code", "error"), ev.data.get("message", ""))
                    yield ev
                self._stream_supported = True
                reply = "".join(parts) or (final_text if isinstance(final_text, str) else "")
                yield Event("final", None, {"text": reply})
            except HarnessError as exc:
                if exc.code != "not_found" or parts:
                    raise
                log.info("Harness has no /chat/stream yet; using non-streaming /chat")
                self._stream_supported = False

        if reply is None:
            reply = await self.client.chat(
                messages, temperature=s.temperature, max_tokens=s.max_tokens, user_id=user_id
            )
            yield Event("token", None, {"text": reply})
            yield Event("final", None, {"text": reply})

        clean = strip_thinking(reply)
        if clean:
            self.state.add_message(session_id, "user", text)
            self.state.add_message(session_id, "assistant", clean)

    async def recover(self, run_id: str, user_id: int) -> str | None:
        """Best-effort fetch of a finished run after the SSE stream dropped.

        The run payload shape isn't pinned in the spec yet; check against openapi.yaml.
        """
        try:
            run = await self.client.get_run(run_id, user_id)
        except Exception as exc:  # noqa: BLE001 - recovery must never raise
            log.warning("Run recovery failed: %s", exc)
            return None
        for key in ("final_text", "text", "result", "response"):
            val = run.get(key)
            if isinstance(val, str) and val.strip():
                return val
        final = run.get("final")
        if isinstance(final, dict) and isinstance(final.get("text"), str):
            return final["text"]
        return None
