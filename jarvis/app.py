"""The voice loop: wait for activation, listen, think, speak.

One main thread runs conversations. Background threads (wake word, hotkey,
reminders, the HUD's web server) never act on their own: they post events, and
for an interruption they set ``cancel`` and silence the speaker so the main
thread unwinds quickly.

Everything the user should see goes through ``self.hub``, which the HUD mirrors.
With no HUD open the hub is just a cheap in-memory record.
"""

from __future__ import annotations

import logging
import queue
import threading
from enum import Enum

from jarvis import briefing, console, phrases
from jarvis.audio import sounds
from jarvis.audio.mic import SAMPLE_RATE, Microphone, drain, find_device
from jarvis.audio.recorder import UtteranceRecorder
from jarvis.audio.stt import Transcriber
from jarvis.audio.tts import Speaker
from jarvis.audio.wake import Hotkey, NameSpotter, WakeWord
from jarvis.brain.agent import Agent
from jarvis.brain.tools import ToolContext
from jarvis.brain.tools.memory import get_calendar, get_store
from jarvis.config import Config
from jarvis.memory_store import Task
from jarvis.reminders import ReminderWatcher, toast
from jarvis.text import SentenceChunker, parse_yes_no
from jarvis.ui.hub import Hub
from jarvis.usage import UsageReporter

log = logging.getLogger(__name__)

_FOLLOW_UP_TIMEOUT_S = 7.0
_BEEP_SETTLE_S = 0.25  # the "listening" beep must not be mistaken for the start of speech
_LIVE_TEXT_CHARS = 260  # the HUD shows the end of a long answer, not all of it


class State(Enum):
    IDLE = "en reposo"
    LISTENING = "escuchando"
    BUSY = "pensando"


def live_tail(sentences: list[str], limit: int = _LIVE_TEXT_CHARS) -> str:
    """The most recent sentences that fit in the HUD's live text area."""
    shown: list[str] = []
    length = 0
    for sentence in reversed(sentences):
        if shown and length + len(sentence) + 1 > limit:
            break
        shown.insert(0, sentence)
        length += len(sentence) + 1
    text = " ".join(shown)
    if len(shown) < len(sentences):
        text = "… " + text
    return text if len(text) <= limit + 2 else "…" + text[-limit:]


def pretty_hotkey(combination: str) -> str:
    """'<ctrl>+<alt>+j' -> 'Ctrl+Alt+J'."""
    return "+".join(part.strip("<>").capitalize() for part in combination.split("+"))


class JarvisApp:
    def __init__(self, config: Config, hub: Hub | None = None) -> None:
        self.config = config
        self.hub = hub or Hub()
        self.state = State.IDLE
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self._listen_pending = threading.Event()
        self._running = False
        self._chunker = SentenceChunker()
        self._spoken: list[str] = []  # sentences of the answer in progress, for the HUD
        self._tool_lines: dict[str, int] = {}

        print("Cargando el oído (Whisper)...")
        self.stt = Transcriber(config.ears)
        print("Cargando la palabra de activación...")
        self.wake = WakeWord(config.ears.wake_model, config.ears.wake_threshold)
        self.recorder = UtteranceRecorder(config.ears.silence_ms, config.ears.max_utterance_seconds)
        print("Conectando la voz (ElevenLabs)...")
        self.speaker = Speaker(config)
        self.hub.set_service("voice", "active")
        self._output_device = find_device(config.ears.output_device, "output")

        self.store = get_store(config.memory_dir)
        self.ctx = ToolContext(config=config, confirm=self._confirm, say=self._say)
        self.agent = Agent(config, self.ctx)
        self.agent.on_tool = self._on_tool
        self.hub.set_service("brain", "active")
        self.consumption = UsageReporter(
            lambda usage: self.hub.update(usage=usage), self.agent.usage, self.speaker.quota
        )

        self.mic = Microphone(config.ears.input_device)
        self._wake_blocks = self.mic.subscribe()
        self._speech_blocks = self.mic.subscribe()
        self.spotter = NameSpotter(self.stt.transcribe) if config.ears.wake_by_name else None
        self._name_blocks = self.mic.subscribe() if self.spotter else None
        self.hotkey = Hotkey(config.ears.hotkey, lambda: self.activate("tecla"))
        self.reminders = ReminderWatcher(self.store, lambda task: self.events.put(("reminder", task)))
        self.calendar = get_calendar(config)
        if self.calendar is not None:
            self.calendar.on_status = self._on_calendar
            self.hub.set_service("calendar", "loading")
        self.hub.update(hint=f"Di «Jarvis», pulsa el orbe o {pretty_hotkey(config.ears.hotkey)} para empezar.")

    # ------------------------------------------------------------------- HUD

    def _show(self, mode: str, text: str | None = None) -> None:
        """Tell the HUD what Jarvis is doing. ``text=None`` leaves the live text as it is."""
        if text is None:
            self.hub.update(mode=mode)
        else:
            self.hub.update(mode=mode, text=text)

    def _say(self, sentence: str) -> None:
        """Speak one sentence and show it."""
        self.speaker.say(sentence)
        self._spoken.append(sentence)
        self._show("speaking", live_tail(self._spoken))

    def _on_tool(self, name: str, phase: str, failed: bool = False) -> None:
        """Called by the agent around every tool call: keeps the HUD's activity log."""
        if phase == "start":
            self._tool_lines[name] = self.hub.tool_started(name)
            if not self.speaker.speaking:
                self._show("thinking")
        else:
            line = self._tool_lines.pop(name, None)
            if line is not None:
                self.hub.tool_finished(name, line, failed)

    def _on_calendar(self, status: str, detail: str) -> None:
        """News from the Google Calendar sync. Called from its background thread."""
        self.hub.set_service("calendar", status)
        if detail:
            self.hub.log("ok" if status == "active" else "err", f"calendario · {detail}")

    # ------------------------------------------------------------ activation

    def activate(self, source: str) -> None:
        """Wake word, hotkey or the HUD's orb. Called from background threads."""
        if self.state is State.LISTENING or self._listen_pending.is_set():
            return
        if self.state is State.BUSY:
            # Interrupt: silence Jarvis now; the main loop unwinds and then listens.
            self.cancel.set()
            self.speaker.stop()
        log.info("Activated by %s", source)
        self._listen_pending.set()
        self.events.put(("listen", None))

    def submit_text(self, text: str) -> None:
        """Something typed in the HUD. Called from the web server's thread."""
        self.events.put(("text", text))

    def request_quit(self) -> None:
        """Stop as soon as possible: the HUD window was closed. Called from a background thread."""
        if self.state is State.BUSY:
            self.cancel.set()  # do not keep talking to a window that is gone
            self.speaker.stop()
        self.events.put(("quit", None))

    def _wake_loop(self) -> None:
        while self._running:
            try:
                block = self._wake_blocks.get(timeout=0.5)
            except queue.Empty:
                continue
            # Always feed the detector so its audio context stays current, but
            # ignore hits while the microphone is already open for the user.
            if self.wake.detect(block) and self.state is not State.LISTENING:
                self.activate("voz")

    def _name_loop(self) -> None:
        """Wake on the bare name. Its own thread: a transcription must not hold up the fast detector."""
        while self._running:
            try:
                block = self._name_blocks.get(timeout=0.5)
            except queue.Empty:
                continue
            if self.state is not State.IDLE or self._listen_pending.is_set():
                # That audio is the user's command or Jarvis's own voice, not someone calling him.
                self.spotter.reset()
                continue
            # The state is checked again: it may have changed during the second the transcription took.
            if self.spotter.detect(block, self.mic.room_level()) and self.state is State.IDLE:
                self.activate("nombre")

    # -------------------------------------------------------------- listening

    def _listen(self, start_timeout_s: float, cancel: threading.Event | None = None, prompt: str = "") -> str:
        """Open the microphone for one utterance and return its transcription."""
        self.state = State.LISTENING
        self._show("listening", prompt or "Le escucho…")
        room_level = self.mic.room_level()  # measured before the beep, which is not part of the room
        drain(self._speech_blocks)
        sounds.play("listening", self._output_device)
        print("  [escuchando...]")
        audio = self.recorder.record(
            self._speech_blocks, start_timeout_s, cancel, room_level=room_level, settle_s=_BEEP_SETTLE_S
        )
        self.state = State.BUSY
        if audio is None:
            print("  [no he oído nada]")
            return ""
        sounds.play("done", self._output_device)
        seconds = len(audio) / SAMPLE_RATE
        cut = " (cortado por duración máxima)" if self.recorder.last_stop_reason == "maximum length" else ""
        print(f"  [he oído {seconds:.1f} s{cut}; transcribiendo...]")
        self._show("thinking", "Transcribiendo…")
        text = self.stt.transcribe(audio)
        print(f"Tú: {text}" if text else "  [no he entendido nada]")
        if text:
            self._show("thinking", f"«{text}»")
        return text

    def _confirm(self, question: str) -> bool:
        """Ask a yes/no question out loud. Anything but a clear yes is a no."""
        for sentence in self._chunker.flush():  # say what was pending before the question
            self._say(sentence)
        for attempt in range(2):
            asked = question if attempt == 0 else "No le he entendido. ¿Sí o no?"
            self.speaker.say(asked)
            self._show("speaking", asked)
            self.speaker.wait()
            if self.cancel.is_set():
                return False
            print(f"Jarvis: {question}")
            answer = parse_yes_no(self._listen(_FOLLOW_UP_TIMEOUT_S, self.cancel, prompt=question))
            if answer is not None:
                self.hub.log("ok" if answer else "err", "confirmación · " + ("aceptada" if answer else "rechazada"))
                self._show("thinking")
                return answer
        self.hub.log("err", "confirmación · sin respuesta clara")
        return False

    # ------------------------------------------------------------ conversing

    def _answer(self, text: str) -> tuple[str, bool]:
        """Run one turn. Returns (what Jarvis said, whether the user interrupted)."""
        self.state = State.BUSY
        self._chunker = SentenceChunker()
        self._spoken = []
        self._show("thinking")

        def on_text(delta: str) -> None:
            if self.cancel.is_set():
                return
            for sentence in self._chunker.feed(delta):
                self._say(sentence)

        reply = self.agent.respond(text, on_text, self.cancel)
        if not self.cancel.is_set():
            for sentence in self._chunker.flush():
                self._say(sentence)
            self.speaker.wait()
        interrupted = self.cancel.is_set()

        if self.agent.last_error:
            self.hub.set_service("brain", "error")
            self.hub.log("err", f"modelo de lenguaje · {self.agent.last_error}")
        else:
            self.hub.set_service("brain", "active")
        if self.speaker.last_error:
            self.hub.set_service("voice", "error")
            self.hub.log("err", f"voz · {self.speaker.last_error}")
        else:
            self.hub.set_service("voice", "active")
        for name, line in list(self._tool_lines.items()):  # a tool cut short by an interruption
            self.hub.tool_finished(name, line, failed=True)
        self._tool_lines.clear()
        self.consumption.refresh()

        if reply.strip():
            print(f"Jarvis: {reply.strip()}" + ("  [interrumpido]" if interrupted else ""))
        return reply, interrupted

    def _converse(self, typed: str | None = None) -> None:
        """A conversation: keeps the microphone open while Jarvis is asking questions.

        ``typed`` starts it from text entered in the HUD instead of from the microphone.
        """
        self._listen_pending.clear()
        self.cancel.clear()
        timeout = 6.0
        heard_nothing = False
        try:
            while True:
                if typed is not None:
                    text, typed = typed, None
                    print(f"Tú (teclado): {text}")
                    self.state = State.BUSY
                    self._show("thinking", f"«{text}»")
                else:
                    text = self._listen(timeout)
                if not text:
                    sounds.play("cancel", self._output_device)
                    heard_nothing = True
                    return
                reply, interrupted = self._answer(text)
                if interrupted or not reply.rstrip().endswith("?"):
                    return
                timeout = _FOLLOW_UP_TIMEOUT_S
        finally:
            self.state = State.IDLE
            if not self._listen_pending.is_set():
                # After an answer its text stays up (the HUD swaps in the hint a bit later).
                self._show("idle", "" if heard_nothing else None)
                print("  [en reposo: di «Jarvis» o pulsa la tecla]\n")

    def _handle_typed(self, text: str) -> None:
        if console.is_command(text):
            result = self._console_command(text)
            print(result)
            self.hub.log("ok", result.splitlines()[0].split(". ")[0].rstrip("."))
            return
        self._converse(typed=text)

    def _console_command(self, line: str) -> str:
        """Run a console command (response mode, usage, balance) and keep the usage panel in step."""
        result = console.handle(line, self.agent, self.consumption.current()["voice"])
        self.consumption.refresh(voice=False)
        return result

    def _announce_reminder(self, task: Task) -> None:
        self.state = State.BUSY
        self.cancel.clear()
        try:
            print(f"Recordatorio: {task.text}")
            self._show("speaking", f"Recordatorio: {task.text}")
            self.hub.log("ok", f"recordatorio · {task.text}")
            sounds.play("reminder", self._output_device, wait=True)
            toast("Recordatorio de Jarvis", task.text)
            self.speaker.say(phrases.reminder(self.config.user.address, task.text))
            self.speaker.wait()
            self.store.mark_notified(task)
        except LookupError:
            log.info("Reminder '%s' was edited or removed before it could be marked", task.text)
        finally:
            self.state = State.IDLE
            self._show("idle")
            self.consumption.refresh()  # the announcement spent voice characters

    def _startup_message(self, briefing_mode: str) -> None:
        self.state = State.BUSY
        try:
            if briefing_mode == "force" or (briefing_mode == "auto" and briefing.is_due_today(self.config)):
                print("Briefing del día...")
                self.hub.log("run", "briefing · preparando el resumen del día")
                self._answer(briefing.prompt(self.config, self.store, self.agent.verbosity))
                briefing.mark_done(self.config)
                self._show("idle")
            else:
                greeting = phrases.greeting(self.config.user.address)
                self.speaker.say(greeting)
                self._show("speaking", greeting)
                self.speaker.wait()
                self._show("idle", "")
        finally:
            self.state = State.IDLE

    def _console_loop(self) -> None:
        """Read commands typed in the console (response mode, help). Runs in its own thread."""
        while self._running:
            try:
                line = input()
            except (EOFError, OSError):
                return  # no console attached (started minimized, or input was closed)
            if line.strip():
                print(self._console_command(line))

    # ------------------------------------------------------------------- run

    def run(self, briefing_mode: str = "auto") -> None:
        """Run until Ctrl+C or a quit request. ``briefing_mode``: 'auto', 'force' or 'skip'."""
        self._running = True
        self.mic.start()
        self.hub.set_service("mic", "active")
        threading.Thread(target=self._wake_loop, name="wake", daemon=True).start()
        if self.spotter is not None:
            threading.Thread(target=self._name_loop, name="wake-name", daemon=True).start()
        self.hotkey.start()
        self.reminders.start()
        if self.calendar is not None:
            self.calendar.start()
        self.consumption.refresh()
        try:
            self._startup_message(briefing_mode)
            print(f"\nListo. Di «Jarvis» o pulsa {self.config.ears.hotkey}. Ctrl+C para salir.")
            print(f"Modo de respuesta: {self.agent.verbosity}. Para cambiarlo escribe breve, normal o detallado "
                  "y pulsa Intro.\n")
            threading.Thread(target=self._console_loop, name="console", daemon=True).start()
            while True:
                try:
                    kind, payload = self.events.get(timeout=0.5)  # timeout keeps Ctrl+C responsive
                except queue.Empty:
                    continue
                if kind == "listen":
                    self._converse()
                elif kind == "text":
                    self._handle_typed(payload)
                elif kind == "reminder":
                    self._announce_reminder(payload)
                elif kind == "quit":
                    print("\nHasta luego.")
                    return
        except KeyboardInterrupt:
            print("\nHasta luego.")
        finally:
            self.close()

    def close(self) -> None:
        self._running = False
        self.reminders.stop()
        if self.calendar is not None:
            self.calendar.wait_idle(5.0)  # let a task created just before leaving reach the phone
            self.calendar.stop()
        self.hotkey.stop()
        self.mic.stop()
        self.speaker.close()
