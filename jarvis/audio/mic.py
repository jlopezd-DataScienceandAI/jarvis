"""Microphone capture: one input stream fanned out to any number of listeners."""

from __future__ import annotations

import logging
import queue
import threading
from collections import deque

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
BLOCK = 1280  # 80 ms: the frame size openWakeWord expects, and 4 x 20 ms VAD frames


def find_device(name: str, kind: str) -> int | None:
    """Index of the first input/output device whose name contains ``name``."""
    if not name:
        return None
    channels_key = "max_input_channels" if kind == "input" else "max_output_channels"
    for index, device in enumerate(sd.query_devices()):
        if device[channels_key] > 0 and name.lower() in device["name"].lower():
            return index
    raise ValueError(f"No encuentro el dispositivo de audio «{name}». Revisa config.toml.")


class Microphone:
    """Delivers 80 ms blocks of 16 kHz mono int16 audio to subscriber queues."""

    def __init__(self, device: str = "") -> None:
        self._device = find_device(device, "input")
        self._subscribers: list[queue.Queue[np.ndarray]] = []
        self._lock = threading.Lock()
        self._stream: sd.InputStream | None = None
        self._native_rate = SAMPLE_RATE
        # Loudness of the last ~10 s, one value per block, to estimate the room's background level.
        self._levels: deque[float] = deque(maxlen=125)

    def subscribe(self, maxsize: int = 400) -> queue.Queue[np.ndarray]:
        q: queue.Queue[np.ndarray] = queue.Queue(maxsize=maxsize)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[np.ndarray]) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def room_level(self) -> float | None:
        """Background RMS: the quietest fifth of the last ~10 s. None until 1 s has been heard.

        A low percentile ignores speech and other bursts, so it stays valid even
        when the user (or Jarvis) was talking during most of the window.
        """
        levels = list(self._levels)
        if len(levels) < 12:
            return None
        return float(np.percentile(levels, 20))

    def _publish(self, block: np.ndarray) -> None:
        self._levels.append(float(np.sqrt(np.mean(block.astype(np.float64) ** 2))))
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(block)
            except queue.Full:
                pass  # a stalled listener must never block the audio callback

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            log.debug("Input status: %s", status)
        block = indata[:, 0]
        if self._native_rate != SAMPLE_RATE:
            from scipy.signal import resample_poly

            resampled = resample_poly(block.astype(np.float32), SAMPLE_RATE, self._native_rate)
            block = np.clip(resampled, -32768, 32767).astype(np.int16)
        else:
            block = block.copy()
        self._publish(block)

    def start(self) -> None:
        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE, blocksize=BLOCK, channels=1, dtype="int16",
                device=self._device, callback=self._callback,
            )
        except sd.PortAudioError:
            # Some drivers refuse 16 kHz: capture at the native rate and resample.
            self._native_rate = int(sd.query_devices(self._device, "input")["default_samplerate"])
            log.info("Microphone does not accept 16 kHz; resampling from %d Hz", self._native_rate)
            self._stream = sd.InputStream(
                samplerate=self._native_rate, blocksize=BLOCK * self._native_rate // SAMPLE_RATE,
                channels=1, dtype="int16", device=self._device, callback=self._callback,
            )
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


def drain(q: queue.Queue) -> None:
    """Throw away whatever is queued, so the next read is fresh audio."""
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return
