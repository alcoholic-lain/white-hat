"""Environment-based configuration. Fails fast with readable errors."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(Exception):
    pass


def _bool(raw: str | None, default: bool) -> bool:
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _num(env: Mapping[str, str], key: str, default: float, cast: type) -> float:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc


def parse_ids(raw: str, name: str = "ALLOWED_USER_IDS") -> frozenset[int]:
    ids: set[int] = set()
    for tok in re.split(r"[,\s]+", raw.strip()):
        if not tok:
            continue
        try:
            ids.add(int(tok))
        except ValueError as exc:
            raise ConfigError(f"{name} has a non-numeric value: {tok!r}") from exc
    return frozenset(ids)


@dataclass(frozen=True)
class Settings:
    discord_token: str
    harness_url: str
    harness_token: str | None
    allowed_user_ids: frozenset[int]
    use_threads: bool
    show_thinking: bool
    state_path: Path
    guild_id: int | None
    system_prompt: str | None
    temperature: float
    max_tokens: int
    history_messages: int
    edit_interval: float
    allow_all: bool
    allowed_guild_ids: frozenset[int]
    user_cooldown: float
    max_concurrent: int
    stream_mode: str
    line_interval: float
    log_level: str

    def is_permitted(self, user_id: int, guild_id: int | None) -> bool:
        """Trusted users always; everyone else only if ALLOW_ALL_USERS (optionally per guild)."""
        if user_id in self.allowed_user_ids:
            return True
        if not self.allow_all:
            return False
        if self.allowed_guild_ids and guild_id not in self.allowed_guild_ids:
            return False
        return True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        if env is None:
            load_dotenv()
            env = os.environ

        token = env.get("DISCORD_TOKEN", "").strip()
        if not token:
            raise ConfigError("DISCORD_TOKEN is not set")

        allowed = parse_ids(env.get("ALLOWED_USER_IDS", ""))
        if not allowed:
            raise ConfigError("ALLOWED_USER_IDS is empty; the allowlist is mandatory")

        guild_raw = env.get("DISCORD_GUILD_ID", "").strip()
        try:
            guild_id = int(guild_raw) if guild_raw else None
        except ValueError as exc:
            raise ConfigError("DISCORD_GUILD_ID must be numeric") from exc

        stream_mode = env.get("STREAM_MODE", "lines").strip().lower() or "lines"
        if stream_mode not in ("lines", "edit"):
            raise ConfigError("STREAM_MODE must be 'lines' or 'edit'")

        return cls(
            discord_token=token,
            harness_url=env.get("HARNESS_URL", "http://127.0.0.1:8787").strip().rstrip("/"),
            harness_token=env.get("HARNESS_TOKEN", "").strip() or None,
            allowed_user_ids=allowed,
            use_threads=_bool(env.get("USE_THREADS"), True),
            show_thinking=_bool(env.get("SHOW_THINKING"), False),
            state_path=Path(env.get("STATE_PATH", "bot_state.db")),
            guild_id=guild_id,
            system_prompt=env.get("SYSTEM_PROMPT", "").strip() or None,
            temperature=float(_num(env, "TEMPERATURE", 0.7, float)),
            max_tokens=int(_num(env, "MAX_TOKENS", 1024, int)),
            history_messages=int(_num(env, "HISTORY_MESSAGES", 24, int)),
            edit_interval=float(_num(env, "EDIT_INTERVAL", 1.2, float)),
            allow_all=_bool(env.get("ALLOW_ALL_USERS"), False),
            allowed_guild_ids=parse_ids(env.get("ALLOWED_GUILD_IDS", ""), "ALLOWED_GUILD_IDS"),
            user_cooldown=float(_num(env, "USER_COOLDOWN", 5.0, float)),
            max_concurrent=max(1, int(_num(env, "MAX_CONCURRENT", 1, int))),
            stream_mode=stream_mode,
            line_interval=float(_num(env, "LINE_INTERVAL", 0.8, float)),
            log_level=env.get("LOG_LEVEL", "INFO").strip().upper(),
        )
