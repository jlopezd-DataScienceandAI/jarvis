from __future__ import annotations

import pytest

from jarvis.text import SentenceChunker, clean_for_speech, normalize, parse_yes_no


def test_normalize_strips_accents_case_and_spaces():
    assert normalize("  Música   CLÁSICA ") == "musica clasica"


@pytest.mark.parametrize("answer", ["Sí", "sí, adelante", "Claro que sí", "vale", "por supuesto", "Hazlo."])
def test_yes(answer):
    assert parse_yes_no(answer) is True


@pytest.mark.parametrize("answer", ["No", "no, déjalo", "mejor no", "cancela", "Sí... no, espera"])
def test_no_wins_over_yes(answer):
    assert parse_yes_no(answer) is False


@pytest.mark.parametrize("answer", ["", "mañana llueve", "¿qué?"])
def test_unclear_answer_is_none(answer):
    assert parse_yes_no(answer) is None


def test_clean_for_speech_removes_markdown():
    spoken = clean_for_speech("**Hecho**, mira `config.toml` y [la web](https://x.y)")
    assert spoken == "Hecho, mira config.toml y la web"


def test_chunker_emits_sentences_as_they_complete():
    chunker = SentenceChunker()
    assert chunker.feed("Enseguida, se") == []
    assert chunker.feed("ñor. Abro ") == ["Enseguida, señor."]
    assert chunker.feed("Chrome") == []
    assert chunker.flush() == ["Abro Chrome"]
    assert chunker.flush() == []


def test_chunker_does_not_split_decimals_or_wait_for_more_after_question():
    chunker = SentenceChunker()
    assert chunker.feed("Cuesta 3.5 euros. ¿Lo compro? ") == ["Cuesta 3.5 euros.", "¿Lo compro?"]


def test_chunker_breaks_runaway_sentence_at_a_comma():
    chunker = SentenceChunker(max_chars=40)
    out = chunker.feed("uno dos tres cuatro, cinco seis siete ocho nueve diez once doce trece")
    assert out == ["uno dos tres cuatro,"]
    assert chunker.flush() == ["cinco seis siete ocho nueve diez once doce trece"]
