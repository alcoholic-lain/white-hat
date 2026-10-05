"""Pure text helpers: <think> stripping, mention cleanup, Discord-safe chunking."""

from __future__ import annotations

import re

DISCORD_LIMIT = 2000
_OPEN = "<think>"
_CLOSE = "</think>"
_FENCE = "```"


def _partial_suffix(s: str, tag: str) -> int:
    """Length of the longest proper prefix of `tag` that `s` ends with."""
    for k in range(min(len(tag) - 1, len(s)), 0, -1):
        if s.endswith(tag[:k]):
            return k
    return 0


class ThinkStripper:
    """Streaming-safe removal of <think>...</think>, even when tags split across chunks.

    If the model opens <think> and never closes it, and nothing else was shown,
    finish() returns the held thinking text so the user is not left with an empty reply.
    """

    def __init__(self, show: bool = False):
        self.show = show
        self.thinking = ""  # kept for logs
        self._buf = ""
        self._in_think = False
        self._emitted = False

    def feed(self, chunk: str) -> str:
        if self.show:
            return chunk
        self._buf += chunk
        out: list[str] = []
        while True:
            if self._in_think:
                i = self._buf.find(_CLOSE)
                if i >= 0:
                    self.thinking += self._buf[:i]
                    self._buf = self._buf[i + len(_CLOSE) :]
                    self._in_think = False
                    continue
                cut = len(self._buf) - _partial_suffix(self._buf, _CLOSE)
                self.thinking += self._buf[:cut]
                self._buf = self._buf[cut:]
                break
            i = self._buf.find(_OPEN)
            if i >= 0:
                out.append(self._buf[:i])
                self._buf = self._buf[i + len(_OPEN) :]
                self._in_think = True
                continue
            cut = len(self._buf) - _partial_suffix(self._buf, _OPEN)
            out.append(self._buf[:cut])
            self._buf = self._buf[cut:]
            break
        text = "".join(out)
        if text.strip():
            self._emitted = True
        return text

    def finish(self) -> str:
        if self.show:
            return ""
        if self._in_think:
            leftover = self.thinking + self._buf
            self._buf, self._in_think = "", False
            return "" if self._emitted else leftover
        rest, self._buf = self._buf, ""
        if rest.strip():
            self._emitted = True
        return rest


def strip_thinking(text: str) -> str:
    s = ThinkStripper()
    return (s.feed(text) + s.finish()).strip()


def extract_prompt(content: str, bot_id: int | None) -> str:
    """Remove the bot's own mention(s) from a message."""
    if bot_id is not None:
        content = re.sub(rf"<@!?{bot_id}>", "", content)
    return content.strip()


def chunk_text(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Split into <= `limit` chunks; prefers line breaks; keeps code fences balanced."""
    if not text.strip():
        return []
    if len(text) <= limit:
        return [text]

    budget = limit - 40  # room for closing and re-opening a code fence
    pieces: list[str] = []
    for line in text.splitlines(keepends=True):
        while len(line) > budget:
            pieces.append(line[:budget])
            line = line[budget:]
        if line:
            pieces.append(line)

    chunks: list[str] = []
    fence_open: str | None = None  # e.g. "```python\n" while inside a block
    base = ""
    cur = ""
    for piece in pieces:
        if cur != base and len(cur) + len(piece) > budget:
            if fence_open:
                cur = cur.rstrip("\n") + "\n" + _FENCE
            chunks.append(cur)
            base = cur = fence_open or ""
        cur += piece
        if piece.lstrip().startswith(_FENCE):
            fence_open = None if fence_open else piece.strip()[:15] + "\n"
    if cur.strip() and cur != base:
        chunks.append(cur)
    return [c for c in chunks if c.strip()]
