from __future__ import annotations

from types import SimpleNamespace

from jarvis.audio.tts import describe_api_error


def test_missing_permission_is_explained_with_the_fix():
    exc = SimpleNamespace(status_code=401, body={"detail": {
        "type": "authentication_error", "code": "unauthorized", "status": "missing_permissions",
        "message": "The API key you used is missing the permission voices_read to execute this operation.",
    }})
    text = describe_api_error(exc)
    assert "«voices_read»" in text and "api-keys" in text and "voice_id" in text
    assert "headers" not in text


def test_other_errors_keep_the_service_message():
    exc = SimpleNamespace(status_code=400, body={"detail": {"status": "invalid", "message": "Unsupported model"}})
    assert describe_api_error(exc) == "ElevenLabs (400): Unsupported model"
    assert describe_api_error(SimpleNamespace(status_code=500, body="boom")) == "ElevenLabs (500): boom"


def _voice(name, language, accent=None, verified=()):
    return SimpleNamespace(
        name=name, voice_id=name, labels={"language": language, "accent": accent},
        verified_languages=[SimpleNamespace(language=lang, locale=None) for lang in verified],
    )


def test_native_spanish_voices_outrank_english_voices_that_merely_speak_spanish():
    from jarvis.audio.tts import is_native_spanish, spanish_voices, speaks_spanish

    roger = _voice("Roger", "en", "american", verified=("en", "es"))  # stock voice: accented Spanish
    bella = _voice("Bella", "en", "american")
    mexican = _voice("Mateo", "es", "mexican", verified=("es",))
    castilian = _voice("David", "es", "peninsular", verified=("es",))
    client = SimpleNamespace(voices=SimpleNamespace(
        search=lambda page_size: SimpleNamespace(voices=[bella, roger, mexican, castilian])))

    assert [v.name for v in spanish_voices(client)] == ["David", "Mateo", "Roger", "Bella"]
    assert not is_native_spanish(roger) and speaks_spanish(roger)
    assert is_native_spanish(castilian)
