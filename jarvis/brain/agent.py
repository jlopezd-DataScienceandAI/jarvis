"""Conversation loop with Claude: streaming text out, tool calls in between.

The loop is written by hand (rather than with the SDK tool runner) because the
voice pipeline needs three things at once: text deltas as they arrive, so speech
can start before the reply is finished; image blocks in tool results, for screen
captures; and cancellation mid-stream when the user interrupts.

The history is append-only. Claude Haiku 5.5 returns thinking blocks that are only
valid while everything sent before them is unchanged, so earlier turns, the system
prompt and the tool list are never edited inside a conversation; a new conversation
is started instead.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import anthropic

from jarvis import state
from jarvis.config import Config
from jarvis.usage import UsageMeter

from . import verbosity
from .prompt import build_system_prompt, spoken_timestamp
from .tools import ToolContext, ToolRegistry
from .tools import registry as default_registry
from .tools.memory import get_store
from .tools.system import installed_apps, user_folders
from .tools.web import server_tools

log = logging.getLogger(__name__)

_MAX_PAUSE_RESUMES = 5
_MAX_JSON_RETRIES = 2


class Agent:
    def __init__(
        self,
        config: Config,
        ctx: ToolContext,
        client: anthropic.Anthropic | None = None,
        registry: ToolRegistry = default_registry,
    ) -> None:
        self.config = config
        self.ctx = ctx
        self.registry = registry
        # Resolves ANTHROPIC_API_KEY (or another configured credential) from the environment.
        self.client = client or anthropic.Anthropic()
        # Computed once: the tool list is part of the cached prompt prefix.
        self._client_tools = registry.schemas()
        self._server_tools = server_tools(config)
        self._folders = user_folders()
        # Listing installed apps takes a few seconds; do it now so the first "abre..." is instant.
        threading.Thread(target=installed_apps, name="apps-warmup", daemon=True).start()
        self.messages: list[dict[str, Any]] = []
        self.system = ""
        self.context_tokens = 0
        self.last_usage: Any = None
        self.usage = UsageMeter(config.data_dir / "usage.json")  # running totals, for the HUD's usage panel
        self._last_activity = time.monotonic()
        self._interrupted = False
        # Optional observer, called as on_tool(name, "start" | "end", failed) around each tool call.
        self.on_tool: Callable[[str, str, bool], None] | None = None
        # Short reason the last turn failed at the API, or None if it went through.
        self.last_error: str | None = None
        # The level chosen at the console outlives restarts; config.toml only gives the default.
        saved = verbosity.parse_level(str(state.load(config).get("verbosity", "")))
        self.verbosity: str = saved or config.brain.verbosity
        self.new_conversation()

    def set_verbosity(self, level: str) -> None:
        """Change how much Jarvis says, from the next turn on. Remembered across restarts."""
        if level not in verbosity.LEVELS:
            raise ValueError(f"Unknown verbosity level: {level}")
        self.verbosity = level
        state.update(self.config, verbosity=level)

    # ------------------------------------------------------------ lifecycle

    @property
    def tools(self) -> list[dict[str, Any]]:
        return self._client_tools + self._server_tools

    def new_conversation(self) -> None:
        """Start fresh. The profile is re-read here, so new facts take effect."""
        self.messages = []
        self.system = build_system_prompt(
            address=self.config.user.address,
            folders=self._folders,
            profile=get_store(self.config.memory_dir).profile(),
            city=self.config.user.city,
        )
        self.context_tokens = 0
        self._interrupted = False

    def _reset_if_stale(self) -> None:
        idle = time.monotonic() - self._last_activity
        if not self.messages:
            return
        if idle > self.config.brain.idle_reset_minutes * 60:
            log.info("Conversation idle for %.0f s: starting a new one", idle)
            self.new_conversation()
        elif self.context_tokens > self.config.brain.max_context_tokens:
            log.info("Context at %d tokens: starting a new conversation", self.context_tokens)
            self.new_conversation()

    # -------------------------------------------------------------- respond

    def respond(
        self,
        user_text: str,
        on_text: Callable[[str], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> str:
        """Answer one user turn. ``on_text`` receives text deltas as they stream.

        Returns everything Jarvis said. Never raises for API failures: the user
        is told what went wrong in the same voice.
        """
        cancel = cancel or threading.Event()
        said: list[str] = []

        def emit(text: str) -> None:
            said.append(text)
            if on_text:
                on_text(text)

        self._reset_if_stale()
        # The timestamp goes in the user turn, not the system prompt, so the cached prefix stays stable.
        header = f"[{spoken_timestamp(datetime.now())}]"
        # Same reasoning for the response mode: one line per turn, so switching it never
        # edits what was already sent (earlier thinking blocks stay valid) nor the cache.
        if verbosity.instruction(self.verbosity):
            header += f"\n[{verbosity.instruction(self.verbosity)}]"
        if self._interrupted:
            header += "\n[El usuario interrumpió tu respuesta anterior antes de que terminara.]"
            self._interrupted = False
        self.messages.append({"role": "user", "content": f"{header}\n{user_text}"})

        address = self.config.user.address
        self.last_error = None
        try:
            self._run_turn(emit, cancel)
        except anthropic.AuthenticationError:
            self.last_error = "clave no válida"
            emit(f"La clave de Anthropic no es válida, {address}. Revise el archivo de claves; "
                 "sin ella me temo que no doy para mucho.")
        except anthropic.PermissionDeniedError:
            self.last_error = "clave sin permiso"
            emit(f"La clave de Anthropic no tiene permiso para esto, {address}.")
        except anthropic.RateLimitError:
            self.last_error = "límite de peticiones"
            emit(f"He alcanzado el límite de peticiones, {address}. Deme un momento para recobrar el aliento.")
        except anthropic.BadRequestError as exc:
            log.error("Bad request: %s", exc)
            self.last_error = "petición rechazada"
            self.new_conversation()  # the history may be what the API rejected
            emit(f"La petición ha sido rechazada, {address}. He reiniciado la conversación.")
        except anthropic.APIStatusError as exc:
            log.error("API error %s: %s", exc.status_code, exc)
            self.last_error = f"servicio con errores ({exc.status_code})"
            emit(f"El servicio no responde bien ahora mismo, {address}. No es usted, son ellos. "
                 "Inténtelo en un momento.")
        except anthropic.APIConnectionError:
            self.last_error = "sin conexión"
            emit(f"No tengo conexión a internet, {address}. Estoy, por así decirlo, incomunicado.")
        finally:
            self._last_activity = time.monotonic()
        return "".join(said)

    def _notify_tool(self, name: str, phase: str, failed: bool = False) -> None:
        """Report a tool starting or ending to whoever is displaying activity."""
        if self.on_tool is None:
            return
        try:
            self.on_tool(name, phase, failed)
        except Exception:  # noqa: BLE001 - a display problem must never break the conversation
            log.exception("on_tool callback failed")

    def _request(self, emit: Callable[[str], None], cancel: threading.Event) -> Any | None:
        """One streamed API call. Returns the final message, or None if cancelled."""
        with self.client.messages.stream(
            model=self.config.brain.model,
            max_tokens=self.config.brain.max_tokens,
            system=self.system,
            tools=self.tools,
            messages=self.messages,
            output_config={"effort": self.config.brain.effort},
            cache_control={"type": "ephemeral"},
        ) as stream:
            last_char = ""
            server_tool: str | None = None  # web search/fetch run on Anthropic's side, mid-stream
            for event in stream:
                if cancel.is_set():
                    return None
                if event.type == "text":
                    emit(event.text)
                    last_char = event.text[-1:] or last_char
                elif event.type == "content_block_start":
                    kind = getattr(event.content_block, "type", "")
                    if kind == "server_tool_use":
                        server_tool = event.content_block.name
                        self._notify_tool(server_tool, "start")
                    elif kind.endswith("_tool_result") and server_tool:
                        failed = getattr(getattr(event.content_block, "content", None), "error_code", None) is not None
                        self._notify_tool(server_tool, "end", failed)
                        server_tool = None
                elif event.type == "content_block_stop" and getattr(event.content_block, "type", "") == "text":
                    # A finished sentence at the end of a text block ("Enseguida, señor." before a
                    # tool call) gets a trailing space: the speech chunker then says it right away
                    # instead of holding it until the tool returns, and it never fuses with the
                    # next block ("señor.He encontrado"). Blocks split mid-sentence by citations
                    # are left untouched.
                    if last_char in ".!?…:":
                        emit(" ")
                        last_char = " "
            return stream.get_final_message()

    def _run_turn(self, emit: Callable[[str], None], cancel: threading.Event) -> None:
        address = self.config.user.address
        json_retries = 0
        pauses = 0
        dropped_server_tools = False

        while True:
            if cancel.is_set():
                self._interrupted = True
                return
            try:
                response = self._request(emit, cancel)
            except ValueError:
                # Tool input JSON the SDK could not parse at all (eager input streaming).
                # No tool_use block completed, so there is nothing to answer: re-issue the turn.
                json_retries += 1
                if json_retries > _MAX_JSON_RETRIES:
                    raise
                continue
            except anthropic.BadRequestError as exc:
                # Typically web search not enabled for the organization, or a tool
                # version this model does not accept. Carry on without web tools.
                if self._server_tools and not dropped_server_tools and _mentions_server_tool(exc):
                    log.warning("Server tools rejected (%s); continuing without web access", exc)
                    dropped_server_tools = True
                    self._server_tools = []
                    # The tool list changed, so earlier thinking blocks are no longer
                    # valid: restart the conversation from the current user turn.
                    self.messages = self.messages[-1:]
                    continue
                raise
            if response is None:
                self._interrupted = True
                return
            json_retries = 0
            self._track_usage(response.usage)

            if response.stop_reason == "refusal":
                self.new_conversation()
                emit(f"Con eso no puedo ayudarle, {address}.")
                return

            if response.stop_reason == "pause_turn":
                # A server tool hit its iteration limit: send the turn back to continue it.
                self.messages.append({"role": "assistant", "content": response.content})
                pauses += 1
                if pauses > _MAX_PAUSE_RESUMES:
                    emit(f"La búsqueda se está alargando demasiado, {address}. Lo dejo aquí, por el bien de los dos.")
                    return
                continue

            tool_uses = [block for block in response.content if block.type == "tool_use"]

            if response.stop_reason == "max_tokens" and tool_uses:
                # A truncated tool input parses as a valid partial object; never run it.
                emit(f"La respuesta se ha cortado por ser demasiado larga, {address}.")
                return

            self.messages.append({"role": "assistant", "content": response.content})
            if not tool_uses:
                return

            results = []
            for block in tool_uses:
                log.info("Tool call: %s %s", block.name, block.input)
                self._notify_tool(block.name, "start")
                result = self.registry.call(block.name, block.input, self.ctx)
                self._notify_tool(block.name, "end", result.is_error)
                log.info("Tool result (%s): %.300s", "error" if result.is_error else "ok", result.content)
                item: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result.content,
                }
                if result.is_error:
                    item["is_error"] = True
                results.append(item)
            # All results go back in a single user message.
            self.messages.append({"role": "user", "content": results})

    def _track_usage(self, usage: Any) -> None:
        self.last_usage = usage
        try:
            self.usage.record(usage, self.config.brain.model)
        except Exception:  # noqa: BLE001 - bookkeeping must never cost the user an answer
            log.exception("Could not record usage")
        self.context_tokens = (
            (usage.input_tokens or 0)
            + (getattr(usage, "cache_read_input_tokens", 0) or 0)
            + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
        )
        log.info(
            "Usage: in=%s cache_read=%s cache_write=%s out=%s",
            usage.input_tokens,
            getattr(usage, "cache_read_input_tokens", None),
            getattr(usage, "cache_creation_input_tokens", None),
            usage.output_tokens,
        )


def _mentions_server_tool(exc: anthropic.BadRequestError) -> bool:
    message = str(exc).lower()
    return "web_search" in message or "web_fetch" in message or "web search" in message
