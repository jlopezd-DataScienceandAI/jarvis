"""Short audio cues, so the user knows the state without looking at the screen."""

from __future__ import annotations

import numpy as np
import sounddevice as sd

_RATE = 24000


def _tone(frequency: float, duration: float, volume: float = 0.18) -> np.ndarray:
    t = np.arange(int(_RATE * duration)) / _RATE
    wave = np.sin(2 * np.pi * frequency * t)
    fade = int(_RATE * 0.012)
    envelope = np.ones_like(wave)
    envelope[:fade] = np.linspace(0, 1, fade)
    envelope[-fade:] = np.linspace(1, 0, fade)
    return (wave * envelope * volume).astype(np.float32)


_CUES = {
    "listening": np.concatenate((_tone(660, 0.07), _tone(990, 0.09))),  # rising: go ahead
    "done": np.concatenate((_tone(880, 0.06), _tone(587, 0.09))),  # falling: got it
    "cancel": _tone(330, 0.16),  # low: nothing heard
    "reminder": np.concatenate((_tone(784, 0.12), _tone(1047, 0.12), _tone(1319, 0.18))),
}


def play(cue: str, device: int | None = None, wait: bool = False) -> None:
    try:
        sd.play(_CUES[cue], _RATE, device=device)
        if wait:
            sd.wait()
    except sd.PortAudioError:
        pass  # a missing beep is not worth interrupting anything for
