"""The voice loop with every device and service replaced by a scripted fake."""

from __future__ import annotations

import queue
import threading

import numpy as np
import pytest

from jarvis import app as app_module
from jarvis.app import JarvisApp, State
from jarvis.audio.mic import BLOCK, SAMPLE_RATE
from jarvis.audio.recorder import UtteranceRecorder
from jarvis.text import SentenceChunker
from jarvis.ui.hub import Hub
from jarvis.usage import UsageMeter, UsageReporter


class FakeSpeaker:
    speaking = False
    last_error = None

    def __init__(self):
        self.said: list[str] = []
        self.stopped = 0

    def say(self, text):
        self.said.append(text)

    def wait(self, timeout=None):
        return True

    def stop(self):
        self.stopped += 1


class FakeAgent:
    """Replies from a script; each entry is text, or a callable(app, on_text, cancel) -> text."""

    last_error = None
    verbosity = "normal"

    def __init__(self, app, replies):
        self._app = app
        self._replies = list(replies)
        self.heard: list[str] = []

    def set_verbosity(self, level):
        self.verbosity = level

    def respond(self, text, on_text=None, cancel=None):
        self.heard.append(text)
        reply = self._replies.pop(0)
        if callable(reply):
            return reply(self._app, on_text, cancel)
        on_text(reply)
        return reply


@pytest.fixture
def make_app(config, monkeypatch):
    monkeypatch.setattr(app_module.sounds, "play", lambda *a, **k: None)

    def build(utterances, replies):
        app = JarvisApp.__new__(JarvisApp)  # skip __init__: no microphone, models or network
        app.config = config
        app.hub = Hub()
        app.state = State.IDLE
        app.events = queue.Queue()
        app.cancel = threading.Event()
        app._listen_pending = threading.Event()
        app._chunker = SentenceChunker()
        app._spoken = []
        app._tool_lines = {}
        app._output_device = None
        app.speaker = FakeSpeaker()
        app.agent = FakeAgent(app, replies)
        app.agent.usage = UsageMeter(config.data_dir / "usage.json")
        app.consumption = UsageReporter(lambda usage: app.hub.update(usage=usage), app.agent.usage)
        spoken = list(utterances)
        app.timeouts = []

        def listen(start_timeout_s, cancel=None, prompt=""):
            app.timeouts.append(start_timeout_s)
            app.state = State.BUSY
            return spoken.pop(0) if spoken else ""

        app._listen = listen
        return app

    return build


def test_single_exchange_speaks_sentence_by_sentence(make_app):
    app = make_app(["abre Chrome"], ["Enseguida, señor. Chrome está abierto."])
    app._converse()
    assert app.agent.heard == ["abre Chrome"]
    assert app.speaker.said == ["Enseguida, señor.", "Chrome está abierto."]
    assert app.state is State.IDLE
    assert len(app.timeouts) == 1  # no follow-up listening after a statement


def test_question_reopens_the_microphone_until_jarvis_stops_asking(make_app):
    app = make_app(
        ["vamos a crear un modo estudio", "abre Notion y cierra Discord"],
        ["¿Qué quiere que abra y que cierre?", "Modo estudio guardado."],
    )
    app._converse()
    assert app.agent.heard == ["vamos a crear un modo estudio", "abre Notion y cierra Discord"]
    assert len(app.timeouts) == 2


def test_silence_ends_the_conversation(make_app):
    app = make_app(["hola"], ["¿En qué puedo ayudarle?"])
    app._converse()  # second listen hears nothing
    assert app.agent.heard == ["hola"]
    assert app.state is State.IDLE


def test_activation_while_busy_interrupts_and_queues_a_new_listen(make_app):
    def long_reply(app, on_text, cancel):
        on_text("Primera frase. ")
        app.activate("tecla")  # the user presses the hotkey mid-answer
        on_text("Esto ya no debe oírse. ")
        return "Primera frase. "

    app = make_app(["cuéntame algo"], [long_reply])
    app._converse()
    assert app.speaker.said == ["Primera frase."]
    assert app.speaker.stopped == 1
    assert app.cancel.is_set()
    assert app.events.get_nowait() == ("listen", None)
    assert app.state is State.IDLE


def test_activation_is_ignored_while_already_listening_or_pending(make_app):
    app = make_app([], [])
    app.state = State.LISTENING
    app.activate("voz")
    assert app.events.empty()

    app.state = State.IDLE
    app.activate("voz")
    app.activate("orbe")  # second trigger before the first was handled
    assert app.events.qsize() == 1


@pytest.mark.parametrize(
    ("answers", "expected", "questions"),
    [
        (["sí, adelante"], True, 1),
        (["no"], False, 1),
        (["eh... ¿cómo?", "vale"], True, 2),
        (["mmm", "no sé"], False, 2),  # never clear: treated as no
        ([""], False, 2),  # silence twice
    ],
)
def test_spoken_confirmation(make_app, answers, expected, questions):
    app = make_app(answers, [])
    assert app._confirm("¿Confirma que cierro Chrome, señor?") is expected
    assert len(app.speaker.said) == questions
    assert app.speaker.said[0] == "¿Confirma que cierro Chrome, señor?"


def test_pending_speech_is_said_before_the_confirmation_question(make_app):
    app = make_app(["sí"], [])
    app._chunker.feed("Voy a cerrarlo")  # streamed text with no sentence end yet
    app._confirm("¿Confirma?")
    assert app.speaker.said == ["Voy a cerrarlo", "¿Confirma?"]


# ------------------------------------------------------------------ recorder


def _blocks(audio: np.ndarray) -> queue.Queue:
    q: queue.Queue = queue.Queue()
    for offset in range(0, len(audio) - BLOCK + 1, BLOCK):
        q.put(audio[offset:offset + BLOCK])
    return q


@pytest.fixture(scope="module")
def speech() -> np.ndarray:
    from jarvis.selftest import windows_tts

    try:
        return windows_tts("Abre el bloc de notas, por favor.", "es-ES")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Windows speech synthesis unavailable: {exc}")


def _trim(audio: np.ndarray) -> np.ndarray:
    loud = np.flatnonzero(np.abs(audio.astype(np.int32)) > 200)
    return audio[loud[0]:loud[-1]]


def _scene(speech: np.ndarray, background: np.ndarray, lead_s: float = 1.0) -> np.ndarray:
    """Speech starting ``lead_s`` into a long stretch of background sound."""
    scene = background.astype(np.float64).copy()
    start = int(lead_s * SAMPLE_RATE)
    scene[start:start + len(speech)] += speech
    return np.clip(scene, -32768, 32767).astype(np.int16)


def _background(kind: str, level: float, seconds: float, speech: np.ndarray) -> np.ndarray:
    n = int(seconds * SAMPLE_RATE)
    if kind == "silence":
        return np.zeros(n)
    if kind == "noise":
        return np.random.default_rng(7).normal(0, level, n)
    # "voice": someone else talking quietly (a television, another room)
    other = np.tile(speech[::-1].astype(np.float64), 20)[:n]
    return other * (level / np.sqrt(np.mean(other ** 2)))


@pytest.mark.parametrize(
    ("kind", "level"),
    [("silence", 0), ("noise", 20), ("noise", 150), ("noise", 400), ("voice", 30), ("voice", 120)],
)
@pytest.mark.parametrize("lead_s", [1.0, 0.0])  # 0.0: already speaking when the microphone opens
def test_recorder_stops_when_the_speaker_stops_whatever_the_background(speech, kind, level, lead_s):
    spoken = _trim(speech)
    seconds = len(spoken) / SAMPLE_RATE
    scene = _scene(spoken, _background(kind, level, seconds + 14, spoken), lead_s)
    recorder = UtteranceRecorder(silence_ms=900, max_seconds=20)
    audio = recorder.record(_blocks(scene), start_timeout_s=5, room_level=float(level))

    assert audio is not None
    assert recorder.last_stop_reason == "silence"
    # Everything that was said, the pre-roll and the closing pause - not seconds of background.
    assert seconds + 0.5 < len(audio) / SAMPLE_RATE < seconds + 2.2


@pytest.mark.parametrize(("kind", "level"), [("silence", 0), ("noise", 150), ("noise", 400), ("voice", 120)])
def test_recorder_ignores_background_alone(speech, kind, level):
    recorder = UtteranceRecorder()
    scene = _background(kind, level, 6, _trim(speech)).astype(np.int16)
    assert recorder.record(_blocks(scene), start_timeout_s=0.5, room_level=float(level)) is None
    assert recorder.last_stop_reason == "no speech"


def test_recorder_without_a_known_room_level_still_rejects_noise_and_hears_speech(speech):
    spoken = _trim(speech)
    noise = _background("noise", 150, 10, spoken)
    assert UtteranceRecorder().record(_blocks(noise.astype(np.int16)), start_timeout_s=0.5) is None
    audio = UtteranceRecorder().record(_blocks(_scene(spoken, noise)), start_timeout_s=5)
    assert audio is not None and len(audio) / SAMPLE_RATE < len(spoken) / SAMPLE_RATE + 2.2


def test_the_listening_beep_cannot_start_a_recording(speech):
    """A loud burst right when the microphone opens is the beep, not the user."""
    spoken = _trim(speech)
    burst = spoken[: int(0.2 * SAMPLE_RATE)]
    silence = np.zeros(SAMPLE_RATE * 3, dtype=np.int16)
    scene = np.concatenate((burst, silence))
    assert UtteranceRecorder().record(_blocks(scene), start_timeout_s=0.5, room_level=0.0, settle_s=0.25) is None
    # ...but someone who keeps talking through the beep is recorded from the first syllable.
    talking = np.concatenate((spoken, silence))
    audio = UtteranceRecorder().record(_blocks(talking), start_timeout_s=2, room_level=0.0, settle_s=0.25)
    assert audio is not None and len(audio) >= len(spoken)


def test_recorder_gives_up_on_silence():
    recorder = UtteranceRecorder()
    silence = np.zeros(SAMPLE_RATE * 3, dtype=np.int16)
    assert recorder.record(_blocks(silence), start_timeout_s=0.5) is None


def test_recorder_respects_the_maximum_length(speech):
    recorder = UtteranceRecorder(silence_ms=700, max_seconds=1)
    audio = recorder.record(_blocks(np.tile(speech, 4)), start_timeout_s=5)
    assert audio is not None and len(audio) / SAMPLE_RATE <= 1.05


def test_recorder_can_be_cancelled():
    cancel = threading.Event()
    cancel.set()
    assert UtteranceRecorder().record(queue.Queue(), start_timeout_s=5, cancel=cancel) is None
