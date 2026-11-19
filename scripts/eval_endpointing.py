"""Manual evaluation of end-of-speech detection across backgrounds.

    uv run python scripts/eval_endpointing.py

Run it after changing the thresholds in jarvis/audio/recorder.py.
"""
import queue
import sys

import numpy as np

from jarvis.audio.mic import BLOCK, SAMPLE_RATE
from jarvis.audio.recorder import UtteranceRecorder
from jarvis.selftest import windows_tts

sys.stdout.reconfigure(encoding="utf-8")
short = windows_tts("Abre el bloc de notas, por favor.", "es-ES").astype(np.float64)
# One sentence on purpose: a full stop makes the synthesizer pause for over a second,
# which any endpointer must treat as the end of the utterance.
long = windows_tts("Apúntame para mañana a las diez revisar el presupuesto del coche, y recuérdame "
                   "también llamar al taller el lunes por la tarde antes de las seis.", "es-ES").astype(np.float64)
other = windows_tts("Y luego dime qué tiempo va a hacer mañana por la tarde en la ciudad.", "es-ES").astype(np.float64)
rng = np.random.default_rng(1)


def rms(x):
    return float(np.sqrt(np.mean(x ** 2)))


def trim(x):  # drop the synthesizer's own leading/trailing silence
    loud = np.flatnonzero(np.abs(x) > 200)
    return x[loud[0]:loud[-1]]


short, long = trim(short), trim(long)


def blocks(a):
    q = queue.Queue()
    a = np.clip(a, -32768, 32767).astype(np.int16)
    for o in range(0, len(a) - BLOCK + 1, BLOCK):
        q.put(a[o:o + BLOCK])
    return q


def background(kind, level, n):
    if kind == "zero":
        return np.zeros(n)
    if kind == "white":
        return rng.normal(0, level, n)
    x = np.tile(other, 8)[:n]
    return x * (level / rms(other))


bad = 0
print(f"frases: corta {len(short)/SAMPLE_RATE:.1f}s, larga {len(long)/SAMPLE_RATE:.1f}s\n")
print(f"{'fondo':16s} {'frase':18s} {'caso':22s} grabado  ideal   motivo")
for kind, level in (("zero", 0), ("white", 20), ("white", 150), ("white", 400), ("voice", 30), ("voice", 120), ("voice", 300)):
    for name, utt, scale in (("corta", short, 1.0), ("larga", long, 1.0), ("corta floja", short, 0.08)):
        for case, lead, known in (("sala conocida", 1.0, True), ("de un tirón", 0.0, True), ("sala desconocida", 1.0, False)):
            u = utt * scale
            n = int(lead * SAMPLE_RATE) + len(u) + SAMPLE_RATE * 12
            audio = background(kind, level, n)
            start = int(lead * SAMPLE_RATE)
            audio[start:start + len(u)] += u
            rec = UtteranceRecorder(silence_ms=900, max_seconds=20)
            out = rec.record(blocks(audio), start_timeout_s=3, room_level=float(level) if known else None)
            got = len(out) / SAMPLE_RATE if out is not None else 0.0
            ideal = len(u) / SAMPLE_RATE + 0.9 + (0.4 if lead else 0.0)
            ok = out is not None and -0.35 <= got - ideal <= 0.8
            # Speech barely louder than the background cannot be told apart by loudness;
            # those rows are shown for information, not counted.
            required = level == 0 or rms(u) >= 8 * level
            bad += required and not ok
            mark = "" if ok else ("   <-- MAL" if required else "   (informativo: voz casi al nivel del fondo)")
            print(f"{kind + ' ' + str(level):16s} {name + ' ' + format(rms(u), '.0f'):18s} {case:22s} "
                  f"{got:5.1f}s  {ideal:5.1f}s  {rec.last_stop_reason}{mark}")
print()
for kind, level in (("zero", 0), ("white", 150), ("white", 400), ("voice", 120)):
    for known in (True, False):
        rec = UtteranceRecorder()
        out = rec.record(blocks(background(kind, level, SAMPLE_RATE * 6)), start_timeout_s=0.5,
                         room_level=float(level) if known else None)
        ok = out is None
        required = known or kind != "voice"  # background speech with no reference level is speech
        bad += required and not ok
        print(f"solo fondo {kind} {level} ({'sala conocida' if known else 'desconocida'}): "
              f"{'nada (correcto)' if ok else f'graba {len(out)/SAMPLE_RATE:.1f}s' + ('   <-- MAL' if required else '   (informativo)')}")
print(f"\ncasos mal: {bad}")
