"""Speech to text with faster-whisper, running on the CPU."""

from __future__ import annotations

import logging
import time

import numpy as np
import psutil

from jarvis.config import EarsConfig
from jarvis.text import normalize

log = logging.getLogger(__name__)

# Whisper invents these on silence or noise; they come from subtitle credits in its training data.
_HALLUCINATIONS = (
    "subtitulos realizados por la comunidad de amara.org",
    "subtitulos por la comunidad de amara.org",
    "gracias por ver el video",
    "gracias por ver",
    "suscribete al canal",
    "suscribete",
)


_PROMPT = "Conversación en español con Jarvis, un asistente de voz."


def is_hallucination(text: str) -> bool:
    cleaned = normalize(text).strip(" .!¡")
    return cleaned in _HALLUCINATIONS or cleaned == normalize(_PROMPT).strip(" .")


class Transcriber:
    def __init__(self, config: EarsConfig) -> None:
        from faster_whisper import WhisperModel

        self._config = config
        started = time.monotonic()
        # Physical cores, not the library default of 4: measured ~30% faster on an 8-core CPU.
        threads = config.cpu_threads or psutil.cpu_count(logical=False) or 4
        self._model = WhisperModel(
            config.whisper_model, device="cpu", compute_type=config.compute_type, cpu_threads=threads
        )
        log.info("Whisper '%s' loaded in %.1f s", config.whisper_model, time.monotonic() - started)

    def transcribe(self, audio: np.ndarray) -> str:
        """Text of a 16 kHz int16 recording. Empty string if nothing intelligible."""
        samples = audio.astype(np.float32) / 32768.0
        segments, _info = self._model.transcribe(
            samples,
            language=self._config.language,
            beam_size=self._config.beam_size,
            vad_filter=True,
            condition_on_previous_text=False,
            # Teaches Whisper how to spell the name. Kept free of commands on purpose:
            # on unclear audio Whisper can echo its prompt, and an echoed command would be executed.
            initial_prompt=_PROMPT,
        )
        text = " ".join(segment.text.strip() for segment in segments if segment.no_speech_prob < 0.8).strip()
        return "" if is_hallucination(text) else text
