from __future__ import annotations

import pytest

from jarvis import briefing, console, state
from jarvis.brain import verbosity
from jarvis.brain.agent import Agent
from jarvis.brain.tools import ToolRegistry
from jarvis.memory_store import MemoryStore
from tests.test_agent import FakeClient, message, text_block


def make_agent(config, ctx, *responses):
    config.brain.web_search = False
    client = FakeClient(*responses)
    return Agent(config, ctx, client=client, registry=ToolRegistry()), client


@pytest.mark.parametrize(
    ("typed", "level"),
    [
        ("breve", "breve"), ("Escueto", "breve"), ("/corto", "breve"),
        ("normal", "normal"),
        ("detallado", "detallado"), ("EXTENDIDO", "detallado"), (" extenso ", "detallado"),
        ("hola", None), ("", None),
    ],
)
def test_level_names_and_synonyms(typed, level):
    assert verbosity.parse_level(typed) == level


def test_mode_line_travels_in_the_user_turn_and_never_in_the_system_prompt(config, ctx):
    agent, client = make_agent(config, ctx, *(message([text_block("ok")]) for _ in range(3)))
    system_before = agent.system

    agent.respond("uno")
    agent.set_verbosity("breve")
    agent.respond("dos")
    agent.set_verbosity("detallado")
    agent.respond("tres")

    first, second, third = (request["messages"][-1]["content"] for request in client.requests)
    assert "Modo de respuesta" not in first  # normal adds nothing
    assert "Modo de respuesta: breve" in second and second.endswith("\ndos")
    assert "Modo de respuesta: detallado" in third
    # Switching must not touch the cached prefix nor rewrite turns already sent.
    assert all(request["system"] == system_before for request in client.requests)
    assert client.requests[2]["messages"][0]["content"] == first
    assert client.requests[2]["messages"][2]["content"] == second


def test_choice_survives_a_restart_and_config_is_only_the_default(config, ctx):
    config.brain.verbosity = "detallado"
    agent, _ = make_agent(config, ctx)
    assert agent.verbosity == "detallado"  # nothing chosen yet: the config default

    agent.set_verbosity("breve")
    assert state.load(config)["verbosity"] == "breve"
    restarted, _ = make_agent(config, ctx)
    assert restarted.verbosity == "breve"

    with pytest.raises(ValueError):
        agent.set_verbosity("gritando")


def test_state_file_keeps_unrelated_values(config):
    briefing.mark_done(config)
    state.update(config, verbosity="breve")
    saved = state.load(config)
    assert saved["verbosity"] == "breve" and "last_briefing" in saved
    assert not briefing.is_due_today(config)

    (config.data_dir / "state.json").write_text("{ roto", encoding="utf-8")
    assert state.load(config) == {}  # a corrupt file is treated as empty, not fatal


def test_console_commands(config, ctx):
    agent, _ = make_agent(config, ctx)
    assert "breve" in console.handle("breve", agent) and agent.verbosity == "breve"
    assert agent.verbosity in console.handle("estado", agent)
    console.handle("/Extendido", agent)
    assert agent.verbosity == "detallado"
    assert "detallado" in console.handle("ayuda", agent) and "breve" in console.handle("ayuda", agent)

    unknown = console.handle("abre chrome", agent)
    assert "No conozco" in unknown and agent.verbosity == "detallado"  # typed text is never sent to Jarvis
    assert console.handle("   ", agent) == ""


def test_text_mode_only_treats_slash_lines_as_commands():
    assert console.is_command("/breve") and console.is_command("  /ayuda")
    assert not console.is_command("breve")  # in text mode this is something said to Jarvis


def test_briefing_length_follows_the_mode(config):
    store = MemoryStore(config.memory_dir)
    short = briefing.prompt(config, store, "breve")
    default = briefing.prompt(config, store)
    long = briefing.prompt(config, store, "detallado")
    assert "diez segundos" in short and "treinta segundos" in default and "cuarenta y cinco" in long
    assert short.count("\n\n") >= 1 and "Tareas para hoy" in short
