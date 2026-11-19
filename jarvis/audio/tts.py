"""Text to speech with ElevenLabs, streamed sentence by sentence.

Two worker threads form a pipeline: one synthesizes queued sentences into PCM
chunks, the other plays them. Synthesis is faster than real time, so the next
sentence is usually ready before the current one finishes and speech is gapless.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from collections.abc import Iterator
from typing import Any

import sounddevice as sd

from jarvis.config import Config

from .mic import find_device

log = logging.getLogger(__name__)

_END = object()  # marks the end of one sentence in the audio queue


class SpeechError(Exception):
    pass


_KEYS_URL = "https://elevenlabs.io/app/settings/api-keys"


def describe_api_error(exc: Any) -> str:
    """One readable line for an ElevenLabs ApiError, instead of its raw headers and body."""
    body = exc.body
    detail = body.get("detail") if isinstance(body, dict) else None
    if not isinstance(detail, dict):
        return f"ElevenLabs ({exc.status_code}): {body}"
    message = str(detail.get("message") or detail)
    if detail.get("status") == "missing_permissions":
        found = re.search(r"permission (\w+)", message)
        permission = found.group(1) if found else "necesario"
        hint = (
            f"La clave de ElevenLabs no tiene el permiso «{permission}». Edita la clave en {_KEYS_URL} "
            "y actívalo, o quítale la restricción de permisos."
        )
        if permission == "voices_read":
            hint += " Alternativa: pon el ID de la voz en config.toml ([voice] voice_id) y no hará falta listarlas."
        return hint
    return f"ElevenLabs ({exc.status_code}): {message}"


def spanish_voices(client: Any) -> list[Any]:
    """Voices in the account, Spanish ones first (Spain before other locales)."""
    from elevenlabs.core.api_error import ApiError

    try:
        voices = list(client.voices.search(page_size=100).voices)
    except ApiError as exc:
        raise SpeechError(describe_api_error(exc)) from exc

    def rank(voice: Any) -> int:
        if is_native_spanish(voice):
            accent = ((voice.labels or {}).get("accent") or "").lower()
            return 0 if accent in ("peninsular", "castilian", "spanish") else 1
        return 2 if speaks_spanish(voice) else 3

    return sorted(voices, key=rank)


def is_native_spanish(voice: Any) -> bool:
    """The voice's own language is Spanish, so it has no foreign accent."""
    return ((voice.labels or {}).get("language") or "").lower() in ("es", "spanish")


def speaks_spanish(voice: Any) -> bool:
    """Verified for Spanish. For a voice of another language this means Spanish with an accent:
    ElevenLabs' stock English voices are all in this group."""
    return any((v.language or "").lower() == "es" for v in (voice.verified_languages or []))


class Speaker:
    def __init__(self, config: Config) -> None:
        from elevenlabs.client import ElevenLabs

        if not config.elevenlabs_api_key:
            raise SpeechError("Falta ELEVENLABS_API_KEY en el archivo .env.")
        self._config = config.voice
        self._client = ElevenLabs(api_key=config.elevenlabs_api_key)
        self._model = config.voice.model
        self._normalize = config.voice.text_normalization
        self._language: str | None = config.voice.language or None
        self.voice_id = config.voice.voice_id or self._pick_voice()

        self._sentences: queue.Queue[tuple[int, str]] = queue.Queue()
        self._audio: queue.Queue[tuple[int, Any]] = queue.Queue(maxsize=200)
        self._lock = threading.Lock()
        self._generation = 0  # bumped by stop(); anything older is discarded
        self._pending = 0  # sentences queued or playing in the current generation
        self._idle = threading.Event()
        self._idle.set()
        self.last_latency_s: float | None = None
        self.last_error: str | None = None  # why the most recent sentence could not be spoken, if so

        self._stream = sd.RawOutputStream(
            samplerate=config.voice.sample_rate, channels=1, dtype="int16",
            device=find_device(config.ears.output_device, "output"),
            latency="low",  # less audio left in the device buffer when the user interrupts
        )
        self._stream.start()
        threading.Thread(target=self._synth_loop, name="tts-synth", daemon=True).start()
        threading.Thread(target=self._play_loop, name="tts-play", daemon=True).start()

    def _pick_voice(self) -> str:
        voices = spanish_voices(self._client)
        if not voices:
            raise SpeechError("Tu cuenta de ElevenLabs no tiene ninguna voz.")
        chosen = voices[0]
        if is_native_spanish(chosen):
            log.info("Using native Spanish voice '%s' (%s)", chosen.name, chosen.voice_id)
        else:
            log.warning(
                "La cuenta no tiene ninguna voz nativa en español; uso '%s', que hablará con acento "
                "extranjero. Elige una nativa con 'jarvis voices --library' y pon su ID en "
                "config.toml ([voice] voice_id).", chosen.name,
            )
        return chosen.voice_id

    # ------------------------------------------------------------ public API

    @property
    def speaking(self) -> bool:
        return not self._idle.is_set()

    def say(self, text: str) -> None:
        """Queue one sentence. Returns immediately."""
        text = text.strip()
        if not text:
            return
        with self._lock:
            self._pending += 1
            self._idle.clear()
            self._sentences.put((self._generation, text))

    def stop(self) -> None:
        """Cut speech now and drop everything queued."""
        with self._lock:
            self._generation += 1
            self._pending = 0
            for q in (self._sentences, self._audio):
                while True:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break
            self._idle.set()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until everything queued has been spoken (or stop() was called)."""
        return self._idle.wait(timeout)

    def quota(self) -> dict[str, int]:
        """The account's characters used and allowed in this billing period, and when it resets (unix time)."""
        from elevenlabs.core.api_error import ApiError

        try:
            subscription = self._client.user.subscription.get()
        except ApiError as exc:
            raise SpeechError(describe_api_error(exc)) from exc
        return {
            "used": int(subscription.character_count),
            "limit": int(subscription.character_limit),
            "resets_at": int(subscription.next_character_count_reset_unix or 0),
        }

    def synthesize(self, text: str, model: str | None = None) -> Iterator[bytes]:
        """Raw PCM chunks for a text. Retries without optional parameters a model rejects."""
        from elevenlabs.core.api_error import ApiError

        for _attempt in range(3):
            kwargs: dict[str, Any] = {
                "voice_id": self.voice_id,
                "text": text,
                "model_id": model or self._model,
                "output_format": self._config.output_format,
            }
            if self._language:
                kwargs["language_code"] = self._language
            if self._normalize:
                kwargs["apply_text_normalization"] = "on"
            produced = False
            try:
                for chunk in self._client.text_to_speech.stream(**kwargs):
                    produced = True
                    yield chunk
                return
            except ApiError as exc:
                detail = str(exc.body).lower()
                if produced or exc.status_code not in (400, 422):
                    raise SpeechError(describe_api_error(exc)) from exc
                if self._normalize and "normalization" in detail:
                    log.warning("This model/plan rejects text normalization; disabling it")
                    self._normalize = False
                elif self._language and "language" in detail:
                    log.warning("This model rejects language_code; relying on auto-detection")
                    self._language = None
                else:
                    raise SpeechError(describe_api_error(exc)) from exc
        raise SpeechError("ElevenLabs rechazó la petición varias veces.")

    # --------------------------------------------------------------- workers

    def _current(self, generation: int) -> bool:
        return generation == self._generation

    def _synth_loop(self) -> None:
        while True:
            generation, text = self._sentences.get()
            if not self._current(generation):
                continue
            started = time.monotonic()
            first = True
            try:
                for chunk in self.synthesize(text):
                    if not self._current(generation):
                        break
                    if not chunk:
                        continue
                    if first:
                        self.last_latency_s = time.monotonic() - started
                        log.info("TTS first audio in %.2f s", self.last_latency_s)
                        first = False
                    self._audio.put((generation, chunk))
                self.last_error = None
            except Exception as exc:  # noqa: BLE001 - keep the voice loop alive
                log.error("Speech synthesis failed: %s", exc)
                self.last_error = str(exc)
                print(f"[sin voz] {text}")
            self._audio.put((generation, _END))

    def _play_loop(self) -> None:
        carry = b""
        while True:
            generation, chunk = self._audio.get()
            if not self._current(generation):
                carry = b""
                continue
            if chunk is _END:
                carry = b""
                with self._lock:
                    if not self._current(generation):
                        continue
                    self._pending -= 1
                    finished = self._pending <= 0
                if finished:
                    # Let the device buffer play out before reporting silence.
                    time.sleep(self._stream.latency)
                    with self._lock:
                        if self._current(generation) and self._pending <= 0:
                            self._idle.set()
                continue
            data = carry + chunk
            usable = len(data) - len(data) % 2  # int16 samples must not be split
            carry = data[usable:]
            if usable:
                self._stream.write(data[:usable])

    def close(self) -> None:
        self.stop()
        self._stream.stop()
        self._stream.close()


class PrintSpeaker:
    """Stand-in used in text mode: 'speaks' by doing nothing, the caller prints."""

    speaking = False
    last_latency_s = None
    last_error = None

    def say(self, text: str) -> None:
        pass

    def stop(self) -> None:
        pass

    def wait(self, timeout: float | None = None) -> bool:
        return True

    def close(self) -> None:
        pass
