"""Waking on the bare name: which bursts of speech get transcribed, and which texts count."""

from __future__ import annotations

import numpy as np
import pytest

from jarvis.audio.mic import BLOCK, SAMPLE_RATE
from jarvis.audio.wake import NameSpotter, is_wake_phrase

LOUD, QUIET = 3000, 0


def audio(*parts: tuple[float, int]) -> np.ndarray:
    """Build a recording from (seconds, level) stretches; 'speech' is simply anything loud."""
    return np.concatenate([np.full(int(seconds * SAMPLE_RATE), level, dtype=np.int16) for seconds, level in parts])


class Ear:
    """A spotter whose voice detector is 'loud means speech' and whose Whisper is scripted."""

    def __init__(self, heard="Jarvis."):
        self.transcribed: list[float] = []  # length in seconds of every burst sent to Whisper
        self._heard = heard
        self.spotter = NameSpotter(self._transcribe, is_speech=lambda frame: abs(int(frame[0])) > 1000)

    def _transcribe(self, burst):
        self.transcribed.append(len(burst) / SAMPLE_RATE)
        return self._heard

    def play(self, recording, room_level=None) -> int:
        """Feed a recording block by block; how many times the spotter woke."""
        hits = 0
        for offset in range(0, len(recording) - BLOCK + 1, BLOCK):
            hits += self.spotter.detect(recording[offset:offset + BLOCK], room_level)
        return hits


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Jarvis.", True), ("¡Jarvis!", True), ("jarvis", True), ("Yarvis", True), ("Oye, Jarvis.", True),
        ("Hey Jarvis", True), ("Jarvis, para.", True),
        ("", False), ("Gracias.", False), ("Javi.", False), ("Elvis", False), ("Oye", False),
        ("Dile a Jarvis que pare", False),  # talking about him, not to him
    ],
)
def test_which_transcriptions_count_as_calling_jarvis(text, expected):
    assert is_wake_phrase(text) is expected


def test_the_name_said_alone_wakes():
    ear = Ear()
    assert ear.play(audio((1.0, QUIET), (0.6, LOUD), (1.0, QUIET))) == 1
    [seconds] = ear.transcribed
    assert 0.6 < seconds < 1.4  # the word, a little before it and the silence that ended it


def test_another_short_word_is_transcribed_but_does_not_wake():
    ear = Ear(heard="Gracias.")
    assert ear.play(audio((1.0, QUIET), (0.6, LOUD), (1.0, QUIET))) == 0
    assert len(ear.transcribed) == 1


def test_sentences_are_never_sent_to_whisper():
    ear = Ear()
    assert ear.play(audio((1.0, QUIET), (4.0, LOUD), (1.0, QUIET))) == 0
    assert ear.transcribed == []


def test_a_word_tacked_onto_a_sentence_is_not_a_call():
    ear = Ear()
    # A sentence, a breath of 0.2 s and a short word: there was no silence before the word.
    assert ear.play(audio((1.0, QUIET), (3.0, LOUD), (0.2, QUIET), (0.5, LOUD), (1.0, QUIET))) == 0
    assert ear.transcribed == []


def test_the_name_after_a_sentence_and_a_pause_wakes():
    ear = Ear()
    assert ear.play(audio((1.0, QUIET), (3.0, LOUD), (0.8, QUIET), (0.5, LOUD), (1.0, QUIET))) == 1
    assert len(ear.transcribed) == 1


def test_clicks_are_ignored():
    ear = Ear()
    assert ear.play(audio((1.0, QUIET), (0.1, LOUD), (1.0, QUIET), (0.12, LOUD), (1.0, QUIET))) == 0
    assert ear.transcribed == []


def test_each_call_wakes_once():
    ear = Ear()
    recording = audio((1.0, QUIET), (0.5, LOUD), (1.5, QUIET), (0.7, LOUD), (1.5, QUIET))
    assert ear.play(recording) == 2


def test_speech_must_stand_out_from_the_room():
    ear = Ear()
    recording = audio((1.0, QUIET), (0.6, LOUD), (1.0, QUIET))
    assert ear.play(recording, room_level=2000.0) == 0  # a room that loud drowns a voice at 3000
    assert ear.play(recording, room_level=100.0) == 1


def test_reset_forgets_the_burst_in_progress():
    ear = Ear()
    start = audio((1.0, QUIET), (0.4, LOUD))
    ear.play(start)
    ear.spotter.reset()  # Jarvis became busy mid-word
    assert ear.play(audio((0.3, LOUD), (1.0, QUIET))) == 0
    assert ear.transcribed == []
