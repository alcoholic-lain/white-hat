"""Discord bot entrypoint (M1.4): mention/DM/thread chat with streaming edits and allowlist."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections import defaultdict

import discord
import httpx
from discord import app_commands

from bot.backend import Backend
from bot.client import HarnessClient, HarnessError
from bot.config import ConfigError, Settings
from bot.limits import Cooldown
from bot.render import ThinkStripper, extract_prompt, strip_thinking
from bot.state import State
from bot.streaming import EditSink, LineSink

log = logging.getLogger("bot")

FRIENDLY_ERRORS = {
    "no_model_loaded": "No model is loaded. Load one in LM Studio "
    "(the `/models` picker arrives in M1.5).",
    "lm_unreachable": "The harness can't reach LM Studio. "
    "Is the server running (`lms server start`)?",
    "context_overflow": "That conversation is too long for the model's context window.",
    "unauthorized": "The harness rejected the bot's token. Check HARNESS_TOKEN.",
    "forbidden": "The harness doesn't allow this user.",
    "cancelled": "Stopped.",
    "max_steps": "The agent hit its step limit before finishing.",
}


def friendly_error(err: HarnessError) -> str:
    return (
        FRIENDLY_ERRORS.get(err.code)
        or f"Harness error `{err.code}`: {err.message or 'no details'}"
    )


class AllowlistTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        settings: Settings = interaction.client.settings  # type: ignore[attr-defined]
        if settings.is_permitted(interaction.user.id, interaction.guild_id):
            return True
        await interaction.response.send_message(
            "You're not authorised to use this bot.", ephemeral=True
        )
        return False


class HarnessBot(discord.Client):
    def __init__(self, settings: Settings):
        intents = discord.Intents.default()
        intents.message_content = True  # privileged: enable in the Developer Portal
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.settings = settings
        self.tree = AllowlistTree(self)
        self.state = State(settings.state_path)
        self.client = HarnessClient(
            settings.harness_url,
            settings.harness_token,
            default_user_id=min(settings.allowed_user_ids),
        )
        self.backend = Backend(self.client, self.state, settings)
        self.cooldown = Cooldown(settings.user_cooldown)
        self._gpu = asyncio.Semaphore(settings.max_concurrent)  # one generation at a time
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._bg: set[asyncio.Task] = set()

    # --- lifecycle ---------------------------------------------------------

    async def setup_hook(self) -> None:
        self._register_commands()
        if self.settings.guild_id:
            guild = discord.Object(id=self.settings.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        task = asyncio.create_task(self._wait_for_harness())
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def close(self) -> None:
        await self.client.aclose()
        self.state.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, getattr(self.user, "id", "?"))

    async def _wait_for_harness(self) -> None:
        delay = 1.0
        while True:
            try:
                st = await self.client.status()
                log.info("Harness reachable at %s: %s", self.settings.harness_url, st)
                return
            except HarnessError as exc:
                if exc.code in ("unauthorized", "forbidden"):
                    log.error(
                        "Harness rejected the bot (%s). Check HARNESS_TOKEN / allowlist.", exc.code
                    )
                    return
                log.error("Harness error on /status: %s; retrying in %.0fs", exc, delay)
            except httpx.HTTPError as exc:
                log.error(
                    "Harness unreachable at %s (%s); retrying in %.0fs",
                    self.settings.harness_url,
                    type(exc).__name__,
                    delay,
                )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    # --- slash commands ----------------------------------------------------

    def _register_commands(self) -> None:
        @self.tree.command(name="status", description="Show harness status")
        async def status(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            try:
                st = await self.client.status(interaction.user.id)
            except HarnessError as exc:
                await interaction.followup.send(friendly_error(exc), ephemeral=True)
                return
            except httpx.HTTPError:
                await interaction.followup.send("Can't reach the harness.", ephemeral=True)
                return
            lines = [f"**{k}**: {v}" for k, v in st.items()]
            await interaction.followup.send("\n".join(lines) or "ok", ephemeral=True)

    # --- message handling --------------------------------------------------

    def _owns_thread(self, thread: discord.Thread) -> bool:
        return thread.owner_id == getattr(self.user, "id", None) or bool(
            self.state.get_session(f"thread:{thread.id}")
        )

    async def on_message(self, msg: discord.Message) -> None:
        if msg.author.bot:  # includes ourselves
            return
        me = self.user
        if me is None:
            return

        channel = msg.channel
        mentioned = me in msg.mentions
        if msg.guild is None:
            relevant = True
        elif isinstance(channel, discord.Thread):
            relevant = mentioned or self._owns_thread(channel)
        else:
            relevant = mentioned
        if not relevant:
            return
        if not self.settings.is_permitted(msg.author.id, msg.guild.id if msg.guild else None):
            return  # silent, per M1.4-T02

        text = extract_prompt(msg.content, me.id)
        if msg.attachments:
            await channel.send("Attachments aren't supported yet, so I'll only read the text.")
        if not text:
            await channel.send("Hi! Send me a message after the mention and I'll reply.")
            return

        if msg.author.id not in self.settings.allowed_user_ids and self.cooldown.check(
            msg.author.id
        ):
            with contextlib.suppress(discord.HTTPException):
                await msg.add_reaction("⏳")  # slow down; trusted users are exempt
            return

        try:
            key, target, reply_to = await self._route(msg)
        except discord.HTTPException as exc:
            log.warning("Could not route message: %s", exc)
            return
        await self._converse(key, msg.author.id, text, target, reply_to)

    async def _route(self, msg: discord.Message):
        """Return (conversation key, where to send, message to reply to or None)."""
        channel = msg.channel
        if msg.guild is None:
            return f"dm:{channel.id}", channel, None
        if isinstance(channel, discord.Thread):
            return f"thread:{channel.id}", channel, None
        if self.settings.use_threads and isinstance(channel, discord.TextChannel):
            try:
                name = (extract_prompt(msg.content, self.user.id) or "chat")[:80]
                thread = await msg.create_thread(name=name, auto_archive_duration=1440)
                return f"thread:{thread.id}", thread, None
            except discord.Forbidden:
                log.warning("No permission to create threads in #%s; replying inline", channel)
        return f"chan:{channel.id}:{msg.author.id}", channel, msg

    async def _converse(self, key, user_id, text, target, reply_to) -> None:
        lock = self._locks[key]
        if self.settings.stream_mode == "edit":
            queued = lock.locked() or self._gpu.locked()
            first = "⏳ Queued…" if queued else "…"
            placeholder = await (
                reply_to.reply(first, mention_author=False) if reply_to else target.send(first)
            )
            sink = EditSink(placeholder, target, self.settings.edit_interval)
            async with lock, self._gpu:
                if queued:
                    await placeholder.edit(content="…")
                await self._run_turn(key, user_id, text, sink)
        else:
            sink = LineSink(target, reply_to, self.settings.line_interval)
            async with lock:
                waiting = None
                if self._gpu.locked():
                    waiting = await target.send("⏳ Waiting for the model…")
                async with self._gpu:
                    if waiting:
                        with contextlib.suppress(discord.HTTPException):
                            await waiting.delete()
                    async with target.typing():
                        await self._run_turn(key, user_id, text, sink)

    async def _run_turn(self, key, user_id, text, sink) -> None:
        s = self.settings
        stripper = ThinkStripper(show=s.show_thinking)
        visible, got_tokens, final_text, run_id = "", False, None, None
        notice = ""

        try:
            session_id = await self.backend.ensure_session(key, user_id)
            async for ev in self.backend.send(session_id, user_id, text):
                run_id = ev.run_id or run_id
                if ev.type == "token":
                    got_tokens = True
                    visible += stripper.feed(str(ev.data.get("text", "")))
                    await sink.update(visible)
                elif ev.type == "model_loading":
                    await sink.status("⏳ Loading model…")
                elif ev.type == "final":
                    final_text = ev.data.get("text")
                elif ev.type == "error":
                    raise HarnessError(ev.data.get("code", "error"), ev.data.get("message", ""))
            visible += stripper.finish()
            if not got_tokens and isinstance(final_text, str):
                visible = final_text if s.show_thinking else strip_thinking(final_text)
        except HarnessError as exc:
            visible += stripper.finish()
            notice = f"⚠️ {friendly_error(exc)}"
        except httpx.TransportError as exc:
            log.error("Harness connection problem: %s", type(exc).__name__)
            visible += stripper.finish()
            recovered = await self.backend.recover(run_id, user_id) if run_id else None
            if recovered:
                visible = recovered if s.show_thinking else strip_thinking(recovered)
            else:
                notice = "⚠️ Lost connection to the harness."
        except Exception:
            log.exception("Unexpected error handling message")
            visible += stripper.finish()
            notice = "⚠️ Something went wrong. Check the bot logs."

        if stripper.thinking:
            log.debug("Model thinking (%d chars) stripped", len(stripper.thinking))

        await sink.finish(visible, notice)


def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # request URLs only, but keep it quiet
    HarnessBot(settings).run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
