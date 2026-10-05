import json

import httpx
import pytest

from bot.backend import LOCAL_PREFIX, Backend
from bot.client import HarnessClient, HarnessError, parse_sse
from bot.config import ConfigError, Settings
from bot.state import State


def sse(*events: dict) -> bytes:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


class Chunked(httpx.AsyncByteStream):
    def __init__(self, parts: list[bytes]):
        self.parts = parts

    async def __aiter__(self):
        for p in self.parts:
            yield p


def make_settings(**over) -> Settings:
    env = {"DISCORD_TOKEN": "x", "ALLOWED_USER_IDS": "1"}
    env.update(over)
    return Settings.from_env(env)


async def collect(agen):
    return [e async for e in agen]


async def test_stream_message_parses_events_and_sends_headers():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["uid"] = request.headers.get("x-user-id")
        body = sse(
            {"type": "run_started", "run_id": "r1", "payload": {"mode": "direct"}},
            {"type": "token", "run_id": "r1", "text": "Hel"},
            {"type": "token", "run_id": "r1", "payload": {"text": "lo"}},
            {"type": "final", "run_id": "r1", "payload": {"text": "Hello"}},
        )
        return httpx.Response(200, content=body)

    c = HarnessClient("http://h", "tok", transport=httpx.MockTransport(handler))
    events = await collect(c.stream_message("s1", 42, "hi"))
    assert [e.type for e in events] == ["run_started", "token", "token", "final"]
    assert "".join(e.data["text"] for e in events if e.type == "token") == "Hello"
    assert events[0].run_id == "r1"
    assert seen == {"auth": "Bearer tok", "uid": "42"}


async def test_sse_frame_split_mid_json():  # M1.2-T02
    frame = sse({"type": "token", "run_id": "r", "payload": {"text": "héllo 👋"}})
    cut = len(frame) // 2
    parts = [frame[:cut], frame[cut:]]

    def handler(request):
        return httpx.Response(200, stream=Chunked(parts))

    c = HarnessClient("http://h", transport=httpx.MockTransport(handler))
    events = await collect(c.stream_message("s", 1, "x"))
    assert events[0].data["text"] == "héllo 👋"


async def test_parse_sse_ignores_comments_done_and_event_field():
    async def lines():
        for ln in [": ping", "event: token", 'data: {"text": "a"}', "", "data: [DONE]", ""]:
            yield ln

    events = await collect(parse_sse(lines()))
    assert len(events) == 1 and events[0].type == "token" and events[0].data == {"text": "a"}


async def test_http_errors_become_typed():
    def handler(request):
        return httpx.Response(401, json={"error": {"code": "unauthorized", "message": "nope"}})

    c = HarnessClient("http://h", transport=httpx.MockTransport(handler))
    with pytest.raises(HarnessError) as ei:
        await collect(c.stream_message("s", 1, "x"))
    assert ei.value.code == "unauthorized"


async def test_backend_falls_back_to_local_chat_and_persists(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sessions":
            return httpx.Response(404)
        if request.url.path == "/chat":
            payload = json.loads(request.content)
            calls.append(payload["messages"])
            return httpx.Response(200, json={"response": "<think>hm</think>Hi there"})
        return httpx.Response(500)

    db = tmp_path / "s.db"
    state = State(db)
    c = HarnessClient("http://h", transport=httpx.MockTransport(handler))
    backend = Backend(c, state, make_settings())

    sid = await backend.ensure_session("dm:1", 1)
    assert sid.startswith(LOCAL_PREFIX)
    assert await backend.ensure_session("dm:1", 1) == sid  # cached

    events = await collect(backend.send(sid, 1, "hello"))
    assert [e.type for e in events] == ["run_started", "token", "final"]

    await collect(backend.send(sid, 1, "again"))
    # second call replays history, with thinking stripped
    assert calls[1] == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hi there"},
        {"role": "user", "content": "again"},
    ]

    # restart: new State object on the same file keeps the session (acceptance #5)
    state.close()
    state2 = State(db)
    assert state2.get_session("dm:1") == sid
    assert len(state2.get_messages(sid, 50)) == 4


async def test_backend_uses_real_sessions_when_available(tmp_path):
    def handler(request):
        if request.url.path == "/sessions":
            return httpx.Response(200, json={"session_id": "abc"})
        return httpx.Response(
            200, content=sse({"type": "final", "run_id": "r", "payload": {"text": "ok"}})
        )

    c = HarnessClient("http://h", transport=httpx.MockTransport(handler))
    backend = Backend(c, State(tmp_path / "s.db"), make_settings())
    sid = await backend.ensure_session("thread:5", 1)
    assert sid == "abc"
    events = await collect(backend.send(sid, 1, "hi"))
    assert events[-1].type == "final"


def test_config_requires_allowlist_and_token():  # M0 / spec: allowlist mandatory
    with pytest.raises(ConfigError):
        Settings.from_env({"DISCORD_TOKEN": "x", "ALLOWED_USER_IDS": ""})
    with pytest.raises(ConfigError):
        Settings.from_env({"ALLOWED_USER_IDS": "1"})
    with pytest.raises(ConfigError):
        Settings.from_env({"DISCORD_TOKEN": "x", "ALLOWED_USER_IDS": "abc"})
    s = make_settings(ALLOWED_USER_IDS="1, 2 3")
    assert s.allowed_user_ids == {1, 2, 3}
