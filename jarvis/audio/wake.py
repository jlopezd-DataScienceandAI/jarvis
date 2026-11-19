"""Activation: the wake word, heard two ways, and a global hotkey.

``WakeWord`` is openWakeWord's ``hey_jarvis`` model: instant, and it works while
Jarvis is talking, but it was trained on English voices. It hears "Hey Jarvis"
from a Spanish speaker and not the bare name (the user's own voice scored under
0.05 saying "Jarvis").

``NameSpotter`` covers the bare name. Whisper writes "Jarvis" for every Spanish
way of saying it that was tried, but takes about a second per call, far too slow
to run on everything the microphone picks up. So the spotter looks for the one
shape the name has when it is used to call someone - a short burst of speech with
silence before and after - and only those bursts are transcribed.
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from collections.abc import Callable

import numpy as np

from jarvis.text import words

from .mic import SAMPLE_RATE

log = logging.getLogger(__name__)

_FRAME = SAMPLE_RATE * 20 // 1000  # 20 ms, a frame size webrtcvad accepts
_MIN_SPEECH_RMS = 40.0  # of 32768: below this nothing counts as speech
_NOISE_MARGIN = 3.0  # speech must be this many times louder than the room
_START_WINDOW = 5  # a burst starts when most of the last 100 ms is speech
_START_VOICED = 4
_PRE_ROLL_FRAMES = 15  # 300 ms kept from before the burst, so the first syllable survives
_QUIET_BEFORE_FRAMES = 20  # 400 ms without speech before it: the name opens the utterance
_QUIET_AFTER_FRAMES = 16  # 320 ms without speech after it: nothing follows in the same breath
_MIN_BURST_FRAMES = 10  # 200 ms: shorter is a click or a cough
_MAX_BURST_FRAMES = 75  # 1.5 s: longer is a sentence, not a name

_NAMES = {"jarvis", "yarvis", "jarbis", "yarbis"}
_GREETINGS = {"hey", "ey", "ei", "eh", "oye", "hola"}


def is_wake_phrase(text: str) -> bool:
    """Whether a transcribed burst is someone calling Jarvis: 'Jarvis', 'Oye, Jarvis', 'Jarvis, para'."""
    heard = words(text)
    if heard and heard[0] in _GREETINGS:
        heard = heard[1:]
    return bool(heard) and heard[0] in _NAMES


class NameSpotter:
    """Finds short, isolated bursts of speech and asks Whisper whether they are the name."""

    def __init__(
        self,
        transcribe: Callable[[np.ndarray], str],
        is_speech: Callable[[np.ndarray], bool] | None = None,
    ) -> None:
        self._transcribe = transcribe
        if is_speech is None:
            import webrtcvad

            vad = webrtcvad.Vad(2)
            is_speech = lambda frame: vad.is_speech(frame.tobytes(), SAMPLE_RATE)  # noqa: E731
        self._is_speech = is_speech
        self.reset()

    def reset(self) -> None:
        """Forget everything heard so far (the app was busy: that audio was not addressed to the spotter)."""
        self._leftover = np.empty(0, dtype=np.int16)
        self._pre_roll: deque[np.ndarray] = deque(maxlen=_PRE_ROLL_FRAMES)
        self._recent: deque[bool] = deque(maxlen=_START_WINDOW)
        self._quiet = 0  # frames without speech before the current burst
        self._burst: list[np.ndarray] | None = None  # audio of the burst in progress
        self._isolated = False  # whether it started after enough quiet
        self._length = 0  # its frames so far, from the first spoken one
        self._silent_tail = 0  # how many of those, at the end, are silence

    def feed(self, block: np.ndarray, room_level: float | None = None) -> np.ndarray | None:
        """Take a block of microphone audio; return a burst worth transcribing when one has just ended."""
        gate = max(_MIN_SPEECH_RMS, _NOISE_MARGIN * room_level) if room_level is not None else _MIN_SPEECH_RMS
        data = np.concatenate((self._leftover, block))
        usable = len(data) - len(data) % _FRAME
        self._leftover = data[usable:]
        found = None
        for offset in range(0, usable, _FRAME):
            frame = data[offset:offset + _FRAME]
            level = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)))
            candidate = self._frame(frame, level >= gate and self._is_speech(frame))
            if candidate is not None:
                found = candidate
        return found

    def _frame(self, frame: np.ndarray, active: bool) -> np.ndarray | None:
        if self._burst is None:
            self._pre_roll.append(frame)
            self._recent.append(active)
            if sum(self._recent) >= _START_VOICED:
                self._isolated = self._quiet >= _QUIET_BEFORE_FRAMES
                self._burst = list(self._pre_roll)
                self._length = len(self._recent)
                self._silent_tail = 0
                self._recent.clear()
            elif not active:
                self._quiet += 1
            return None

        self._length += 1
        self._silent_tail = 0 if active else self._silent_tail + 1
        spoken = self._length - self._silent_tail
        if spoken <= _MAX_BURST_FRAMES:
            self._burst.append(frame)  # a sentence is never transcribed, so its audio is not kept either
        if self._silent_tail < _QUIET_AFTER_FRAMES:
            return None
        # The burst is over. Whatever it was, the silence that ended it counts as quiet before the next.
        burst, self._burst = self._burst, None
        self._quiet = self._silent_tail
        self._pre_roll.clear()
        if self._isolated and _MIN_BURST_FRAMES <= spoken <= _MAX_BURST_FRAMES:
            return np.concatenate(burst)
        return None

    def hear(self, block: np.ndarray, room_level: float | None = None) -> str | None:
        """Feed a block; when a burst has just ended, what Whisper heard in it (may be empty)."""
        burst = self.feed(block, room_level)
        if burst is None:
            return None
        heard = self._transcribe(burst)
        # Only the name is logged as heard: other people's stray words do not belong in a log file.
        if is_wake_phrase(heard):
            log.info("Short burst (%.1f s) heard as %r", len(burst) / SAMPLE_RATE, heard)
        else:
            log.debug("Short burst (%.1f s) was not the name", len(burst) / SAMPLE_RATE)
        return heard

    def detect(self, block: np.ndarray, room_level: float | None = None) -> bool:
        """Feed a block; True when a burst has just ended and it was the name."""
        heard = self.hear(block, room_level)
        return heard is not None and is_wake_phrase(heard)


class WakeWord:
    """openWakeWord detector. Feed it 80 ms blocks of 16 kHz int16 audio."""

    def __init__(self, model_name: str = "hey_jarvis", threshold: float = 0.5, cooldown_s: float = 2.0) -> None:
        import openwakeword
        from openwakeword.model import Model
        from openwakeword.utils import download_models

        self.threshold = threshold
        self._cooldown_s = cooldown_s
        self._last_trigger = 0.0

        onnx_path = openwakeword.MODELS[model_name]["model_path"].replace(".tflite", ".onnx")
        if not os.path.exists(onnx_path):
            log.info("Downloading wake word model '%s' (first run only)", model_name)
            download_models(model_names=[model_name])
        self._model = Model(wakeword_models=[model_name], inference_framework="onnx")

    def score(self, block: np.ndarray) -> float:
        """Highest wake-word probability for this block (0..1)."""
        return float(max(self._model.predict(block).values()))

    def detect(self, block: np.ndarray) -> bool:
        if self.score(block) < self.threshold:
            return False
        now = time.monotonic()
        if now - self._last_trigger < self._cooldown_s:
            return False
        self._last_trigger = now
        self.reset()
        return True

    def reset(self) -> None:
        """Forget buffered audio so one utterance cannot trigger twice."""
        self._model.reset()


class Hotkey:
    """Global hotkey, e.g. '<ctrl>+<alt>+j' (pynput syntax)."""

    def __init__(self, combination: str, on_press: Callable[[], None]) -> None:
        from pynput import keyboard

        self._listener = keyboard.GlobalHotKeys({combination: on_press})

    def start(self) -> None:
        self._listener.start()

    def stop(self) -> None:
        self._listener.stop()
