"""Record one utterance from the microphone, ending when the speaker stops.

A frame counts as speech only if the voice-activity detector says so *and* it is
loud enough relative to the room and to the speaker. The detector alone is not
enough: in a real room it flags stray frames of fan noise, keyboard clicks or a
distant television as voice, and each of those would postpone the end of the
recording. For the same reason the end is "almost no speech in the last 0.9 s",
not "0.9 s without a single speech frame".

The room's level should come from the caller (the microphone keeps a rolling
estimate), because recording often starts while the user is already speaking
("Jarvis, abre Chrome" in one breath) and then there is no silence to measure.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque

import numpy as np

from .mic import SAMPLE_RATE

log = logging.getLogger(__name__)

_FRAME = SAMPLE_RATE * 20 // 1000  # 20 ms, a frame size webrtcvad accepts
_PRE_ROLL_FRAMES = 20  # 400 ms kept from before speech starts, so the first syllable survives
_START_WINDOW = 5  # speech starts when most of the last 100 ms is speech
_START_VOICED = 4
_WARMUP_FRAMES = 10  # without a known room level, listen 200 ms before trusting any frame
_MIN_SPEECH_RMS = 40.0  # absolute floor, of 32768: below this nothing is treated as speech
_NOISE_MARGIN = 3.0  # speech must be this many times louder than the room
_SPEAKER_FRACTION = 0.12  # ...and, once known, at least this fraction of the speaker's own level
_END_TOLERANCE = 0.08  # share of stray "speech" frames still accepted as silence


def _rms(frame: np.ndarray) -> float:
    return float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)))


class UtteranceRecorder:
    """Voice-activity endpointing over a stream of mic blocks."""

    def __init__(self, silence_ms: int = 900, max_seconds: int = 20, aggressiveness: int = 2) -> None:
        import webrtcvad

        self._webrtcvad = webrtcvad
        self._aggressiveness = aggressiveness
        self._vad = webrtcvad.Vad(aggressiveness)
        self._silence_frames = max(5, silence_ms // 20)
        self._max_frames = max_seconds * 1000 // 20
        self.last_stop_reason = ""

    def is_speech(self, frame: np.ndarray) -> bool:
        """Raw detector verdict for one 20 ms frame, ignoring loudness."""
        return self._vad.is_speech(frame.tobytes(), SAMPLE_RATE)

    def record(
        self,
        blocks: queue.Queue[np.ndarray],
        start_timeout_s: float = 6.0,
        cancel: threading.Event | None = None,
        room_level: float | None = None,
        settle_s: float = 0.0,
    ) -> np.ndarray | None:
        """Wait for speech, then capture until the speaker stops. None if nobody spoke.

        ``room_level`` is the background RMS measured before this call, if known.
        ``settle_s`` is an initial stretch that cannot trigger the start (the
        "listening" beep plays then); it is still kept as pre-roll, so someone
        already speaking loses nothing.
        """
        settle_frames = min(_PRE_ROLL_FRAMES, int(settle_s * 1000) // 20)
        seen = 0
        # The detector adapts to what it hears; start each utterance from a clean state.
        self._vad = self._webrtcvad.Vad(self._aggressiveness)
        pre_roll: deque[np.ndarray] = deque(maxlen=_PRE_ROLL_FRAMES)
        recent: deque[bool] = deque(maxlen=_START_WINDOW)
        heard: list[float] = []  # levels before speech, used only when room_level is unknown
        tail: deque[bool] = deque(maxlen=self._silence_frames)
        captured: list[np.ndarray] = []
        speech_levels: list[float] = []
        started = False
        gate = max(_MIN_SPEECH_RMS, _NOISE_MARGIN * room_level) if room_level is not None else _MIN_SPEECH_RMS
        allowed_strays = int(self._silence_frames * _END_TOLERANCE)
        deadline = time.monotonic() + start_timeout_s
        leftover = np.empty(0, dtype=np.int16)

        def finish(reason: str) -> np.ndarray:
            self.last_stop_reason = reason
            audio = np.concatenate(captured)
            log.info(
                "Recorded %.1f s (stopped by %s; speaker level %.0f, speech gate %.0f)",
                len(audio) / SAMPLE_RATE, reason,
                float(np.percentile(speech_levels, 75)) if speech_levels else 0.0, gate,
            )
            return audio

        while True:
            if cancel is not None and cancel.is_set():
                self.last_stop_reason = "cancel"
                return None
            if not started and time.monotonic() > deadline:
                self.last_stop_reason = "no speech"
                log.info("No speech within %.0f s (speech gate %.0f)", start_timeout_s, gate)
                return None
            try:
                block = blocks.get(timeout=0.2)
            except queue.Empty:
                continue
            data = np.concatenate((leftover, block))
            usable = len(data) - len(data) % _FRAME
            leftover = data[usable:]
            for offset in range(0, usable, _FRAME):
                frame = data[offset:offset + _FRAME]
                level = _rms(frame)
                voiced = self.is_speech(frame)

                if not started:
                    pre_roll.append(frame)
                    seen += 1
                    if seen <= settle_frames:
                        continue
                    if room_level is None:
                        # Estimate the room from the quietest fifth of what has been heard so far.
                        heard.append(level)
                        if len(heard) < _WARMUP_FRAMES:
                            continue
                        gate = max(_MIN_SPEECH_RMS, _NOISE_MARGIN * float(np.percentile(heard, 20)))
                    recent.append(voiced and level >= gate)
                    if sum(recent) >= _START_VOICED:
                        started = True
                        captured.extend(pre_roll)
                    continue

                captured.append(frame)
                speaker_gate = _SPEAKER_FRACTION * float(np.percentile(speech_levels, 75)) if speech_levels else 0.0
                active = voiced and level >= max(gate, speaker_gate)
                if active:
                    speech_levels.append(level)
                tail.append(active)
                if len(tail) == tail.maxlen and sum(tail) <= allowed_strays:
                    return finish("silence")
                if len(captured) >= self._max_frames:
                    return finish("maximum length")
