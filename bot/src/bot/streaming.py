"""Throttled message editing while a reply streams in."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import discord

from bot.render import DISCORD_LIMIT, chunk_text

log = logging.getLogger("bot.streaming")

PREVIEW_MAX = 1900
CURSOR = " ▌"


def preview(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    if len(text) > PREVIEW_MAX:
        text = "…" + text[-PREVIEW_MAX:]
    return text + CURSOR


class StreamingReply:
    def __init__(
        self, message: Any, interval: float = 1.2, clock: Callable[[], float] = time.monotonic
    ):
        self.message = message
        self.interval = interval
        self._clock = clock
        self._last: float | None = None
        self._shown = ""

    async def _edit(self, content: str) -> None:
        try:
            await self.message.edit(content=content[:DISCORD_LIMIT])
        except discord.HTTPException as exc:
            log.warning("Edit failed: %s", exc)

    async def update(self, text: str) -> None:
        now = self._clock()
        if self._last is not None and now - self._last < self.interval:
            return
        shown = preview(text)
        if not shown or shown == self._shown:
            return
        self._last, self._shown = now, shown
        await self._edit(shown)

    async def status(self, text: str) -> None:
        self._last = self._clock()
        await self._edit(text)

    async def finish(self, text: str, send_more: Callable[[str], Awaitable[Any]]) -> None:
        chunks = chunk_text(text.strip()) or ["…"]
        await self._edit(chunks[0])
        for extra in chunks[1:]:
            await send_more(extra)


EMPTY = "_(The model returned an empty reply.)_"


class EditSink:
    """Edit-in-place mode: one message that grows (limited by Discord's edit rate)."""

    def __init__(self, placeholder: Any, target: Any, interval: float = 1.2):
        self.stream = StreamingReply(placeholder, interval)
        self.target = target

    async def update(self, visible: str) -> None:
        await self.stream.update(visible)

    async def status(self, text: str) -> None:
        await self.stream.status(text)

    async def finish(self, visible: str, notice: str = "") -> None:
        answer = visible.strip()
        if notice:
            answer = f"{answer}\n\n{notice}" if answer else notice
        elif not answer:
            answer = EMPTY
        await self.stream.finish(answer, self.target.send)


class LineSink:
    """Line mode: send each finished line as its own message while Discord shows "typing".

    - Fenced code blocks are held until closed and sent as one message.
    - If lines arrive faster than `interval`, they are batched into one message.
    """

    def __init__(
        self,
        target: Any,
        reply_to: Any = None,
        interval: float = 0.8,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.target = target
        self._reply_to = reply_to
        self.interval = interval
        self._clock = clock
        self._offset = 0
        self._in_fence = False
        self._block: list[str] = []
        self._pending: list[str] = []
        self._last: float | None = None
        self._sent_any = False
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._ticker: asyncio.Task | None = None

    # -- parsing -----------------------------------------------------------

    def _scan(self, visible: str) -> None:
        while True:
            nl = visible.find("\n", self._offset)
            if nl < 0:
                return
            line = visible[self._offset : nl]
            self._offset = nl + 1
            if line.lstrip().startswith("```"):
                if self._in_fence:
                    self._block.append(line)
                    self._pending.append("\n".join(self._block))
                    self._block, self._in_fence = [], False
                else:
                    self._in_fence, self._block = True, [line]
            elif self._in_fence:
                self._block.append(line)
            elif line.strip():
                self._pending.append(line.rstrip())

    # -- sending -----------------------------------------------------------

    async def _send(self, text: str) -> None:
        for chunk in chunk_text(text):
            try:
                if self._reply_to is not None and not self._sent_any:
                    await self._reply_to.reply(chunk, mention_author=False)
                else:
                    await self.target.send(chunk)
                self._sent_any = True
            except discord.HTTPException as exc:
                log.warning("Send failed: %s", exc)

    async def _flush(self, force: bool = False) -> None:
        async with self._lock:
            if not self._pending:
                return
            if not force and self._last is not None:
                if self._clock() - self._last < self.interval:
                    return
            text = "\n".join(self._pending)
            self._pending = []
            await self._send(text)
            self._last = self._clock()

    async def _tick(self) -> None:
        # Releases lines that were held back by pacing while the model is still thinking.
        while not self._stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=max(0.2, self.interval / 3))
            if self._stop.is_set():
                return
            try:
                await self._flush()
            except Exception:
                log.exception("Line flush failed")

    # -- sink API ----------------------------------------------------------

    async def update(self, visible: str) -> None:
        self._scan(visible)
        if self._ticker is None:
            self._ticker = asyncio.create_task(self._tick())
        await self._flush()

    async def status(self, text: str) -> None:
        pass  # the typing indicator already shows activity

    async def finish(self, visible: str, notice: str = "") -> None:
        self._stop.set()
        if self._ticker is not None:
            await self._ticker
        self._scan(visible)
        tail = visible[self._offset :]
        if self._in_fence:
            block = self._block + ([tail] if tail.strip() else [])
            self._pending.append("\n".join(block) + "\n```")
            self._block, self._in_fence = [], False
        elif tail.strip():
            self._pending.append(tail.strip())
        self._offset = len(visible)
        if notice:
            self._pending.append(notice)
        if not self._pending and not self._sent_any:
            self._pending.append(EMPTY)
        await self._flush(force=True)
