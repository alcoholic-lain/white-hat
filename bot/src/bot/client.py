"""Async HTTP/SSE client for the harness (spec section 4)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("bot.client")


@dataclass
class Event:
    type: str
    run_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


class HarnessError(Exception):
    """A typed harness failure (spec error codes, plus http-derived ones)."""

    def __init__(self, code: str, message: str = "", status: int | None = None):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
        self.status = status


_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "not_found",
}


def _error_from_response(resp: httpx.Response) -> HarnessError:
    code = _STATUS_CODES.get(resp.status_code, "http_error")
    message = resp.text.strip()[:300] or f"HTTP {resp.status_code}"
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, str):
            message = err
        elif isinstance(err, dict):
            code = err.get("code", code)
            message = err.get("message", message)
    return HarnessError(code, message, resp.status_code)


def _build_event(event_name: str | None, data_lines: list[str]) -> Event | None:
    raw = "\n".join(data_lines)
    if raw.strip() == "[DONE]":
        return None
    try:
        obj = json.loads(raw)
    except ValueError:
        log.warning("Skipping non-JSON SSE frame (%d bytes)", len(raw))
        return None
    if not isinstance(obj, dict):
        return None
    etype = obj.get("type") or event_name
    if not etype:
        return None
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        payload = {k: v for k, v in obj.items() if k not in ("type", "run_id")}
    return Event(type=etype, run_id=obj.get("run_id"), data=payload)


async def parse_sse(lines: AsyncIterator[str]) -> AsyncIterator[Event]:
    """Parse an SSE line stream into events. Tolerates comments and a trailing frame."""
    event_name: str | None = None
    data_lines: list[str] = []
    async for line in lines:
        if line == "":
            if data_lines:
                ev = _build_event(event_name, data_lines)
                if ev:
                    yield ev
            event_name, data_lines = None, []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "event":
            event_name = value
        elif name == "data":
            data_lines.append(value)
    if data_lines:
        ev = _build_event(event_name, data_lines)
        if ev:
            yield ev


class HarnessClient:
    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        default_user_id: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._token = token
        self._default_user = default_user_id
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(10.0, read=300.0),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def _headers(self, user_id: int | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        uid = user_id if user_id is not None else self._default_user
        if uid is not None:
            headers["X-User-Id"] = str(uid)
        return headers

    async def _json(
        self, method: str, path: str, *, user_id: int | None = None, **kwargs: Any
    ) -> Any:
        resp = await self._http.request(method, path, headers=self._headers(user_id), **kwargs)
        if resp.status_code >= 400:
            raise _error_from_response(resp)
        return resp.json()

    # --- spec routes -------------------------------------------------------

    async def status(self, user_id: int | None = None) -> dict[str, Any]:
        return await self._json("GET", "/status", user_id=user_id)

    async def models(self, user_id: int | None = None) -> Any:
        return await self._json("GET", "/models", user_id=user_id)

    async def create_session(self, client: str, external_id: str, user_id: int) -> str:
        data = await self._json(
            "POST",
            "/sessions",
            user_id=user_id,
            json={"client": client, "external_id": external_id},
        )
        return str(data["session_id"])

    async def cancel(self, session_id: str, user_id: int) -> None:
        await self._json("POST", f"/sessions/{session_id}/cancel", user_id=user_id)

    async def get_run(self, run_id: str, user_id: int) -> dict[str, Any]:
        return await self._json("GET", f"/runs/{run_id}", user_id=user_id)

    async def stream_message(
        self, session_id: str, user_id: int, text: str
    ) -> AsyncIterator[Event]:
        async with self._http.stream(
            "POST",
            f"/sessions/{session_id}/messages",
            headers={**self._headers(user_id), "Accept": "text/event-stream"},
            json={"text": text},
        ) as resp:
            if resp.status_code >= 400:
                await resp.aread()
                raise _error_from_response(resp)
            async for ev in parse_sse(resp.aiter_lines()):
                yield ev

    # --- current backend (M1.3): non-streaming /chat ----------------------

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
        user_id: int | None = None,
    ) -> str:
        data = await self._json(
            "POST",
            "/chat",
            user_id=user_id,
            json={
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": False,
            },
        )
        if isinstance(data, dict) and "error" in data and "response" not in data:
            msg = str(data["error"])
            code = "lm_unreachable" if msg.startswith("Request failed") else "lm_error"
            raise HarnessError(code, msg)
        return str(data.get("response", ""))

    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
        user_id: int | None = None,
    ) -> AsyncIterator[Event]:
        """Streaming variant of /chat (SSE: token / final / error events)."""
        async with self._http.stream(
            "POST",
            "/chat/stream",
            headers={**self._headers(user_id), "Accept": "text/event-stream"},
            json={
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": True,
            },
        ) as resp:
            if resp.status_code >= 400:
                await resp.aread()
                raise _error_from_response(resp)
            async for ev in parse_sse(resp.aiter_lines()):
                yield ev
