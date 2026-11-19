"""The usage panel: counting Claude tokens, pricing them, the balance, and the voice quota."""

from __future__ import annotations

import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

from jarvis import console
from jarvis.audio.tts import Speaker, SpeechError
from jarvis.usage import UsageMeter, UsageReporter, cost_usd, describe
from tests.test_agent import FakeClient, make_agent, message, text_block, tool_block, tools  # noqa: F401 - pytest fixture
from tests.test_voice_loop import make_app  # noqa: F401 - pytest fixture

NOVEMBER = datetime(2026, 11, 15, 12, 0)


def response_usage(input_tokens=4, cache_write=300, cache_read=20_000, output=150, searches=None):
    usage = SimpleNamespace(
        input_tokens=input_tokens, cache_creation_input_tokens=cache_write,
        cache_read_input_tokens=cache_read, output_tokens=output,
    )
    if searches is not None:
        usage.server_tool_use = SimpleNamespace(web_search_requests=searches)
    return usage


@pytest.fixture
def meter(tmp_path):
    return UsageMeter(tmp_path / "data" / "usage.json")


# ---------------------------------------------------------------------- pricing


def test_a_typical_turn_is_priced_at_the_published_rates():
    # 4 input, 300 written to cache, 20,000 read from cache, 150 output on Haiku 5.5.
    expected = (4 * 0.10 + 300 * 0.125 + 20_000 * 0.01 + 150 * 0.50) / 1_000_000
    assert cost_usd("claude-haiku-5-5", 4, 300, 20_000, 150) == pytest.approx(expected)


def test_web_searches_cost_a_cent_each():
    without = cost_usd("claude-haiku-5-5", 0, 0, 0, 100)
    assert cost_usd("claude-haiku-5-5", 0, 0, 0, 100, searches=3) == pytest.approx(without + 0.03)


def test_long_prompts_on_haiku_use_the_higher_rates():
    short = cost_usd("claude-haiku-5-5", 0, 0, 100_000, 0)
    long = cost_usd("claude-haiku-5-5", 0, 0, 100_001, 0)
    assert short == pytest.approx(100_000 * 0.01 / 1e6) and long == pytest.approx(100_001 * 0.05 / 1e6)
    # Models with a single rate are unaffected by the prompt's size.
    assert cost_usd("claude-sonnet-5-5", 0, 0, 200_000, 0) == pytest.approx(200_000 * 0.10 / 1e6)


def test_an_unknown_model_has_no_price():
    assert cost_usd("claude-de-otro-planeta", 100, 0, 0, 100) is None


# ------------------------------------------------------------------------ meter


def test_usage_adds_up_within_the_month_and_survives_a_restart(meter, tmp_path):
    meter.record(response_usage(), "claude-haiku-5-5", NOVEMBER)
    meter.record(response_usage(searches=2), "claude-haiku-5-5", NOVEMBER)
    summary = UsageMeter(tmp_path / "data" / "usage.json").summary(NOVEMBER)  # a new process reads the same file
    assert summary["tokens"] == 2 * (4 + 300 + 20_000 + 150)
    assert summary["searches"] == 2 and not summary["unpriced"]
    assert summary["usd"] == pytest.approx(2 * cost_usd("claude-haiku-5-5", 4, 300, 20_000, 150) + 0.02, abs=1e-4)


def test_each_month_starts_from_zero(meter):
    meter.record(response_usage(), "claude-haiku-5-5", NOVEMBER)
    assert meter.summary(datetime(2026, 12, 1, 9, 0))["tokens"] == 0
    assert meter.summary(NOVEMBER)["tokens"] > 0


def test_usage_objects_with_missing_fields_are_accepted(meter):
    meter.record(SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=None), "claude-haiku-5-5",
                 NOVEMBER)
    assert meter.summary(NOVEMBER)["tokens"] == 120


def test_the_balance_goes_down_only_with_what_is_spent_after_setting_it(meter):
    meter.record(response_usage(output=1_000_000), "claude-haiku-5-5", NOVEMBER)  # spent before: not subtracted
    assert meter.summary(NOVEMBER)["left"] is None
    meter.set_balance(5.0, NOVEMBER)
    meter.record(response_usage(input_tokens=0, cache_write=0, cache_read=0, output=1_000_000), "claude-haiku-5-5",
                 NOVEMBER)
    summary = meter.summary(NOVEMBER)
    assert summary["credit"] == 5.0 and summary["left"] == pytest.approx(4.5)
    # The balance carries over into the next month; the monthly total does not.
    december = meter.summary(datetime(2026, 12, 2, 9, 0))
    assert december["left"] == pytest.approx(4.5) and december["usd"] == 0


def test_the_balance_never_shows_below_zero(meter):
    meter.set_balance(0.10, NOVEMBER)
    meter.record(response_usage(output=1_000_000), "claude-haiku-5-5", NOVEMBER)
    assert meter.summary(NOVEMBER)["left"] == 0


def test_a_model_without_a_known_price_counts_tokens_and_flags_the_cost(meter):
    meter.set_balance(5.0, NOVEMBER)
    meter.record(response_usage(), "claude-de-otro-planeta", NOVEMBER)
    summary = meter.summary(NOVEMBER)
    assert summary["tokens"] > 0 and summary["usd"] == 0 and summary["unpriced"] and summary["left"] == 5.0


def test_a_damaged_file_starts_over_instead_of_failing(meter, tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "usage.json").write_text("{ roto", encoding="utf-8")
    meter.record(response_usage(), "claude-haiku-5-5", NOVEMBER)
    assert meter.summary(NOVEMBER)["tokens"] == 20_454


# --------------------------------------------------------------------- reporter


def test_reporter_publishes_claude_at_once_and_the_voice_when_it_arrives(meter):
    published = []
    arrived = threading.Event()

    def publish(usage):
        published.append(usage)
        if usage["voice"]:
            arrived.set()

    reporter = UsageReporter(publish, meter, lambda: {"used": 100, "limit": 1000, "resets_at": 0})
    reporter.refresh()
    assert published[0]["voice"] is None and published[0]["brain"]["tokens"] == 0
    assert arrived.wait(5.0)
    assert published[-1]["voice"] == {"used": 100, "limit": 1000, "resets_at": 0}


def test_reporter_keeps_the_last_reading_when_the_account_stops_answering(meter):
    answers = [{"used": 100, "limit": 1000, "resets_at": 0}, SpeechError("sin conexión")]
    done = threading.Event()

    def quota():
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    reporter = UsageReporter(lambda usage: done.set() if usage["voice"] else None, meter, quota)
    reporter.refresh()
    assert done.wait(5.0)
    done.clear()
    reporter.refresh()
    assert done.wait(5.0)
    assert reporter.current()["voice"]["used"] == 100


def test_reporter_explains_a_key_that_cannot_read_the_usage(meter):
    done = threading.Event()

    def quota():
        raise SpeechError("La clave de ElevenLabs no tiene el permiso «user_read».")

    reporter = UsageReporter(lambda usage: done.set() if usage["voice"] else None, meter, quota)
    reporter.refresh()
    assert done.wait(5.0)
    assert "user_read" in reporter.current()["voice"]["error"]


def test_reporter_without_a_voice_account_publishes_only_claude(meter):
    published = []
    UsageReporter(published.append, meter).refresh()
    assert published == [{"brain": meter.summary(), "voice": None}]


# ---------------------------------------------------------------- voice account


def test_speaker_quota_reads_the_subscription():
    speaker = Speaker.__new__(Speaker)  # skip __init__: no audio device or network
    subscription = SimpleNamespace(character_count=14_733, character_limit=40_000,
                                   next_character_count_reset_unix=1_797_246_660)
    speaker._client = SimpleNamespace(user=SimpleNamespace(subscription=SimpleNamespace(get=lambda: subscription)))
    assert speaker.quota() == {"used": 14_733, "limit": 40_000, "resets_at": 1_797_246_660}


# ---------------------------------------------------------------------- console


def test_console_balance_and_usage_commands(make_app):  # noqa: F811
    app = make_app([], [])
    agent = app.agent
    assert "sin indicar" in console.handle("consumo", agent)
    assert "4,87 $" in console.handle("saldo 4,87", agent)
    assert agent.usage.summary()["credit"] == 4.87
    assert console.handle("/saldo 12.5 $", agent).startswith("Saldo de Claude anotado: 12,50 $")
    shown = console.handle("saldo", agent, {"used": 14_733, "limit": 40_000, "resets_at": 0})
    assert "12,50 $ de 12,50 $" in shown and "14.733 de 40.000 caracteres; quedan 25.267" in shown
    assert "saldo" in console.handle("ayuda", agent) and "consumo" in console.handle("ayuda", agent)


def test_describe_flags_an_incomplete_cost_and_an_unreadable_voice_account():
    summary = {"tokens": 1500, "searches": 2, "usd": 0.5, "unpriced": True, "credit": None, "left": None}
    text = describe(summary, {"error": "sin permiso"})
    assert "incompleto" in text and "2 búsquedas web" in text and "no se puede leer el consumo (sin permiso)" in text


# -------------------------------------------------------------------------- app


def test_a_typed_balance_reaches_the_hud(make_app):  # noqa: F811
    app = make_app([], [])
    app._handle_typed("/saldo 5")
    usage = app.hub.snapshot()["usage"]
    assert usage["brain"]["left"] == 5.0 and usage["voice"] is None
    assert app.hub.snapshot()["log"][-1]["text"].startswith("Saldo de Claude anotado")


def test_every_answer_refreshes_the_panel(make_app):  # noqa: F811
    app = make_app([], ["Hecho, señor."])
    assert app.hub.snapshot()["usage"] is None
    app._answer("hola")
    assert app.hub.snapshot()["usage"]["brain"]["tokens"] == 0  # the fake agent spends nothing, but the panel is fed


def test_the_agent_counts_every_response_of_a_turn(config, ctx, tools):  # noqa: F811
    # Two API calls in one turn: the model asks for a tool, then answers.
    client = FakeClient(
        message([tool_block("echo", {"value": "x"})], stop_reason="tool_use", cache_read=5_000),
        message([text_block("Hecho.")], cache_read=5_200),
    )
    agent = make_agent(config, ctx, tools, client)
    agent.respond("haz eco")
    summary = agent.usage.summary()
    assert summary["tokens"] == (100 + 20 + 5_000) + (100 + 20 + 5_200)
    assert summary["usd"] == pytest.approx((200 * 0.10 + 10_200 * 0.01 + 40 * 0.50) / 1e6, abs=1e-4)
