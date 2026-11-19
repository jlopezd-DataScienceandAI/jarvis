"""Small text helpers shared by tools and the voice loop."""

from __future__ import annotations

import re
import unicodedata

_YES = {
    "si", "vale", "claro", "adelante", "confirmo", "confirmado", "hazlo", "correcto",
    "afirmativo", "ok", "okay", "venga", "procede", "dale", "exacto", "eso",
}
_YES_PHRASES = ("por supuesto", "de acuerdo", "por favor", "esta bien", "claro que si")
_NO = {"no", "cancela", "cancelar", "para", "dejalo", "negativo", "espera", "tampoco"}
_NO_PHRASES = ("ni hablar", "no lo hagas", "mejor no", "dejalo estar")


def normalize(text: str) -> str:
    """Lowercase, strip accents and collapse whitespace, for fuzzy matching."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", stripped).strip()


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9ñ]+", normalize(text))


def parse_yes_no(text: str) -> bool | None:
    """Interpret a spoken confirmation. None means the answer was unclear."""
    norm = normalize(text)
    tokens = set(words(text))
    # A refusal anywhere wins: "sí... no, espera" must not confirm.
    if tokens & _NO or any(p in norm for p in _NO_PHRASES):
        return False
    if tokens & _YES or any(p in norm for p in _YES_PHRASES):
        return True
    return None


_MARKDOWN = re.compile(r"[*_`#>|]+")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")


def clean_for_speech(text: str) -> str:
    """Remove markdown that a TTS engine would read aloud or stumble on."""
    text = _LINK.sub(r"\1", text)
    text = _MARKDOWN.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


class SentenceChunker:
    """Turn a stream of text deltas into complete sentences for the TTS.

    Speaking sentence by sentence lets audio start before the model has
    finished writing, while keeping natural prosody within each sentence.
    """

    # Sentence end: terminator (plus closing quote/bracket) followed by whitespace.
    _END = re.compile(r"[.!?…]+[\"')\]»]*\s+|\n+")
    _SOFT = re.compile(r"[,;:]\s+")

    def __init__(self, max_chars: int = 240) -> None:
        self._buffer = ""
        self._max_chars = max_chars

    def feed(self, delta: str) -> list[str]:
        self._buffer += delta
        out: list[str] = []
        while True:
            match = self._END.search(self._buffer)
            if match:
                cut = match.end()
            elif len(self._buffer) > self._max_chars:
                # Runaway sentence: break at the last soft pause so audio keeps flowing.
                soft = list(self._SOFT.finditer(self._buffer[: self._max_chars]))
                if not soft:
                    break
                cut = soft[-1].end()
            else:
                break
            sentence = clean_for_speech(self._buffer[:cut])
            self._buffer = self._buffer[cut:]
            if sentence:
                out.append(sentence)
        return out

    def flush(self) -> list[str]:
        sentence = clean_for_speech(self._buffer)
        self._buffer = ""
        return [sentence] if sentence else []
