"""End-to-end checks that need no human at the microphone.

The audio path is tested by synthesizing "Jarvis. ¿Qué hora es en Madrid?" and feeding
that recording to the wake word detector and to Whisper. With ``offline=True`` the
test audio comes from the voices built into Windows, so the ears can be checked
without API keys or ElevenLabs credits.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

from jarvis.audio.mic import BLOCK, SAMPLE_RATE
from jarvis.config import Config
from jarvis.text import normalize

_PHRASE_WAKE = "Jarvis"
_PHRASE_COMMAND = "¿Qué hora es en Madrid?"
_MIC_SIGNAL_RMS = 15  # below this a one-second sample is indistinguishable from a muted microphone
_WAKE_NOTICE = 0.05  # wake-word scores above this are the detector reacting to something, not noise


def _ok(passed: bool) -> str:
    return "OK   " if passed else "FALLO"


def resample_to_16k(pcm: np.ndarray, rate: int) -> np.ndarray:
    if rate == SAMPLE_RATE:
        return pcm
    from scipy.signal import resample_poly

    out = resample_poly(pcm.astype(np.float32), SAMPLE_RATE, rate)
    return np.clip(out, -32768, 32767).astype(np.int16)


def windows_tts(text: str, culture: str) -> np.ndarray:
    """16 kHz int16 speech from the Windows built-in synthesizer."""
    with tempfile.TemporaryDirectory() as tmp:
        wav_path = Path(tmp) / "speech.wav"
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"try {{ $s.SelectVoiceByHints('NotSet', 'NotSet', 0, [cultureinfo]'{culture}') }} catch {{}}; "
            "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, "
            "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, "
            "[System.Speech.AudioFormat.AudioChannel]::Mono); "
            f"$s.SetOutputToWaveFile('{wav_path}', $f); "
            "$s.Speak($env:JARVIS_TTS_TEXT); $s.Dispose()"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            check=True, capture_output=True, timeout=60,
            env={**os.environ, "JARVIS_TTS_TEXT": text},
        )
        with wave.open(str(wav_path), "rb") as wav:
            return np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)


def wake_score(audio_16k: np.ndarray, config: Config) -> float:
    """Peak wake-word probability over a recording padded with silence."""
    from jarvis.audio.wake import WakeWord

    detector = WakeWord(config.ears.wake_model, config.ears.wake_threshold)
    silence = np.zeros(SAMPLE_RATE, dtype=np.int16)
    padded = np.concatenate((silence, audio_16k, silence))
    peak = 0.0
    for offset in range(0, len(padded) - BLOCK + 1, BLOCK):
        peak = max(peak, detector.score(padded[offset:offset + BLOCK]))
    return peak


def check_ears(wake_audio: np.ndarray, command_audio: np.ndarray, config: Config) -> bool:
    passed = True

    score = wake_score(wake_audio, config)
    hit = score >= config.ears.wake_threshold
    passed &= hit
    print(f"[{_ok(hit)}] Palabra de activación: puntuación {score:.2f} (umbral {config.ears.wake_threshold})")

    from jarvis.audio.recorder import UtteranceRecorder

    recorder = UtteranceRecorder()
    frame = SAMPLE_RATE * 20 // 1000
    voiced = sum(
        recorder.is_speech(command_audio[i:i + frame])
        for i in range(0, len(command_audio) - frame + 1, frame)
    )
    total = max(1, len(command_audio) // frame)
    vad_ok = voiced / total > 0.3
    passed &= vad_ok
    print(f"[{_ok(vad_ok)}] Detector de voz: {100 * voiced / total:.0f}% de la frase marcada como habla")

    from jarvis.audio.stt import Transcriber

    transcriber = Transcriber(config.ears)
    started = time.monotonic()
    text = transcriber.transcribe(command_audio)
    elapsed = time.monotonic() - started
    understood = "hora" in normalize(text) and "madrid" in normalize(text)
    passed &= understood
    seconds = len(command_audio) / SAMPLE_RATE
    print(f"[{_ok(understood)}] Whisper ({config.ears.whisper_model}): «{text}» "
          f"en {elapsed:.1f} s para {seconds:.1f} s de audio")
    return passed


def check_microphone(config: Config) -> bool:
    from jarvis.audio.mic import Microphone

    mic = Microphone(config.ears.input_device)
    blocks = mic.subscribe()
    mic.start()
    time.sleep(1.2)
    mic.stop()
    collected = []
    while not blocks.empty():
        collected.append(blocks.get_nowait())
    if not collected:
        print(f"[{_ok(False)}] Micrófono: no llega audio")
        return False
    level = float(np.sqrt(np.mean(np.concatenate(collected).astype(np.float64) ** 2)))
    if level >= _MIC_SIGNAL_RMS:
        print(f"[{_ok(True)}] Micrófono: llega señal (nivel RMS {level:.0f})")
    else:
        # A silent room and a muted microphone look the same from here (noise
        # suppression gates quiet input to zero), so this cannot be called a pass.
        print(f"[AVISO] Micrófono: abre, pero casi no llega señal (nivel RMS {level:.1f}). Es normal en "
              "silencio; si Jarvis no le oye, compruebe que no está silenciado con: "
              "jarvis selftest --listen")
    return True


def check_listening(config: Config) -> bool:
    """The real input path with a human: microphone -> end-of-speech detection -> Whisper."""
    from jarvis.audio import sounds
    from jarvis.audio.mic import Microphone
    from jarvis.audio.recorder import UtteranceRecorder
    from jarvis.audio.stt import Transcriber

    print("Cargando Whisper...")
    transcriber = Transcriber(config.ears)
    recorder = UtteranceRecorder(config.ears.silence_ms, config.ears.max_utterance_seconds)
    mic = Microphone(config.ears.input_device)
    blocks = mic.subscribe()
    mic.start()
    try:
        time.sleep(1.2)  # measure the room before asking for speech
        room_level = mic.room_level()
        sounds.play("listening")
        print(f"Diga una frase ahora (tiene 8 segundos para empezar; nivel de la sala: {room_level or 0:.0f})...")
        started = time.monotonic()
        audio = recorder.record(blocks, start_timeout_s=8.0, room_level=room_level, settle_s=0.25)
        waited = time.monotonic() - started
    finally:
        mic.stop()
    if audio is not None:
        print(f"        Grabación cerrada a los {waited:.1f} s por: "
              f"{'silencio' if recorder.last_stop_reason == 'silence' else 'duración máxima'}")
    if audio is None:
        print(f"[{_ok(False)}] No he detectado voz. Revise que el micrófono no esté silenciado y que "
              "sea el dispositivo de entrada predeterminado de Windows.")
        return False
    level = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    started = time.monotonic()
    text = transcriber.transcribe(audio)
    elapsed = time.monotonic() - started
    print(f"[{_ok(bool(text))}] He oído {len(audio) / SAMPLE_RATE:.1f} s (nivel RMS {level:.0f}) "
          f"y he entendido en {elapsed:.1f} s: «{text}»")
    return bool(text)


def check_wake(config: Config, seconds: float = 20.0) -> bool:
    """The wake word with a human: say it a few times and see what each detector makes of it."""
    import queue

    from jarvis.audio.mic import Microphone
    from jarvis.audio.wake import NameSpotter, WakeWord, is_wake_phrase

    threshold = config.ears.wake_threshold
    detector = WakeWord(config.ears.wake_model, threshold)
    spotter = None
    if config.ears.wake_by_name:
        from jarvis.audio.stt import Transcriber

        print("Cargando Whisper...")
        spotter = NameSpotter(Transcriber(config.ears).transcribe)
    mic = Microphone(config.ears.input_device)
    blocks = mic.subscribe()
    mic.start()
    print(f"Diga «Jarvis» varias veces durante {seconds:.0f} segundos, sola y dejando un par de segundos "
          "entre una y otra.")
    results: list[bool] = []

    def report(what: str, would_wake: bool) -> None:
        results.append(would_wake)
        print(f"  {len(results)}. {what}: {'activaría' if would_wake else 'NO activaría'}")

    peak, quiet_blocks = 0.0, 0
    deadline = time.monotonic() + seconds
    try:
        # Transcribing takes a moment, so audio queues up: what was said in time is still processed.
        while time.monotonic() < deadline or (not blocks.empty() and time.monotonic() < deadline + 5):
            try:
                block = blocks.get(timeout=0.5)
            except queue.Empty:
                continue
            score = detector.score(block)
            if score >= _WAKE_NOTICE:
                peak, quiet_blocks = max(peak, score), 0
            elif peak:
                quiet_blocks += 1
                if quiet_blocks * BLOCK / SAMPLE_RATE >= 0.8:  # the phrase is over: report it
                    if peak >= threshold:
                        report(f"detector rápido de «Hey Jarvis», puntuación {peak:.2f}", True)
                    else:
                        # Not an attempt of its own: it is the same word the name spotter also judges.
                        print(f"     (el detector rápido de «Hey Jarvis» apenas ha reaccionado: {peak:.2f} "
                              f"de {threshold}; no cuenta como intento)")
                    peak = 0.0
            if spotter is not None:
                heard = spotter.hear(block, mic.room_level())
                if heard is not None:
                    report(f"he entendido «{heard}»" if heard else "un sonido corto que no era una palabra",
                           is_wake_phrase(heard))
    finally:
        mic.stop()

    hits = sum(results)
    if not results:
        print(f"[{_ok(False)}] No he oído ninguna palabra suelta. Dígala sola, con una pausa antes y después; "
              "si tampoco, el micrófono se comprueba con: jarvis selftest --listen")
    elif not hits:
        print(f"[{_ok(False)}] He oído {len(results)} cosas, pero ninguna la he entendido como «Jarvis». "
              "Arriba aparece lo que he entendido cada vez.")
    else:
        print(f"[{_ok(True)}] Palabra de activación: Jarvis se habría activado {hits} de {len(results)} veces.")
    return hits > 0


def check_brain(config: Config) -> bool:
    from jarvis.brain.agent import Agent
    from jarvis.brain.tools import ToolContext

    agent = Agent(config, ToolContext(config=config))
    started = time.monotonic()
    first_text_at: list[float] = []

    def on_text(_delta: str) -> None:
        if not first_text_at:
            first_text_at.append(time.monotonic() - started)

    reply = agent.respond("Prueba de sistemas. Responde solo: sistemas en línea.", on_text)
    answered = bool(first_text_at) and len(agent.messages) >= 2
    latency = f"{first_text_at[0]:.1f} s hasta el primer texto" if first_text_at else "sin respuesta"
    print(f"[{_ok(answered)}] Cerebro ({config.brain.model}): «{reply.strip()}» ({latency})")
    if not answered:
        return False

    agent.respond("¿Qué día de la semana es hoy?")
    cached = getattr(agent.last_usage, "cache_read_input_tokens", 0) or 0
    print(f"[{_ok(cached > 0)}] Caché de prompt: {cached} tokens leídos de caché en el segundo turno")
    return True


def check_voice(config: Config, play: bool) -> tuple[bool, np.ndarray | None, np.ndarray | None]:
    """Synthesize the test phrases. Returns (passed, wake audio, command audio) at 16 kHz."""
    import sounddevice as sd

    from jarvis.audio.tts import Speaker

    speaker = Speaker(config)
    rate = config.voice.sample_rate
    passed = True
    clips: dict[str, np.ndarray] = {}
    alternative = "eleven_flash_v2_5" if config.voice.model != "eleven_flash_v2_5" else "eleven_v4_turbo"

    for model in (config.voice.model, alternative):
        try:
            started = time.monotonic()
            first = None
            chunks = []
            for chunk in speaker.synthesize(f"{_PHRASE_WAKE}. {_PHRASE_COMMAND}", model):
                first = first or time.monotonic() - started
                chunks.append(chunk)
            raw = b"".join(chunks)
            pcm = np.frombuffer(raw[: len(raw) - len(raw) % 2], dtype=np.int16)
            print(f"[{_ok(True)}] Voz {model}: primer audio en {first:.2f} s, {len(pcm) / rate:.1f} s de audio")
            clips[model] = pcm
            if play:
                sd.play(pcm, rate)
                sd.wait()
        except Exception as exc:  # noqa: BLE001 - report and try the other model
            is_main = model == config.voice.model
            passed &= not is_main
            print(f"[{_ok(False)}] Voz {model}: {exc}" + ("" if is_main else " (modelo alternativo, no crítico)"))

    if config.voice.model not in clips:
        speaker.close()
        return False, None, None
    try:
        # Separate clips: the wake word and the command are checked by different detectors.
        wake = b"".join(speaker.synthesize(_PHRASE_WAKE))
        command = b"".join(speaker.synthesize(_PHRASE_COMMAND))
    except Exception as exc:  # noqa: BLE001
        print(f"[{_ok(False)}] Voz: {exc}")
        return False, None, None
    finally:
        speaker.close()

    def to_16k(raw: bytes) -> np.ndarray:
        return resample_to_16k(np.frombuffer(raw[: len(raw) - len(raw) % 2], dtype=np.int16), rate)

    return passed, to_16k(wake), to_16k(command)


def run(config: Config, offline: bool = False, play: bool = True) -> bool:
    print("Autodiagnóstico de J.A.R.V.I.S.\n")
    passed = True

    try:
        passed &= check_microphone(config)
    except Exception as exc:  # noqa: BLE001
        passed = False
        print(f"[{_ok(False)}] Micrófono: {exc}")

    if offline:
        print("Modo sin conexión: audio de prueba generado con las voces de Windows.")
        wake_audio = windows_tts(_PHRASE_WAKE, "en-US")
        command_audio = windows_tts(_PHRASE_COMMAND, "es-ES")
    else:
        if not config.has_anthropic_credentials:
            passed = False
            print(f"[{_ok(False)}] Cerebro: falta ANTHROPIC_API_KEY en .env")
        else:
            try:
                passed &= check_brain(config)
            except Exception as exc:  # noqa: BLE001
                passed = False
                print(f"[{_ok(False)}] Cerebro: {exc}")
        wake_audio = command_audio = None
        if not config.elevenlabs_api_key:
            passed = False
            print(f"[{_ok(False)}] Voz: falta ELEVENLABS_API_KEY en .env")
        else:
            try:
                voice_ok, wake_audio, command_audio = check_voice(config, play)
                passed &= voice_ok
            except Exception as exc:  # noqa: BLE001
                passed = False
                print(f"[{_ok(False)}] Voz: {exc}")
        if wake_audio is None or command_audio is None:
            # The ears do not depend on ElevenLabs: still check them, with Windows voices.
            print("        (el oído se comprueba con las voces de Windows)")
            wake_audio = windows_tts(_PHRASE_WAKE, "en-US")
            command_audio = windows_tts(_PHRASE_COMMAND, "es-ES")

    passed &= check_ears(wake_audio, command_audio, config)
    print("\nTodo en orden." if passed else "\nHay fallos que revisar.")
    return passed
