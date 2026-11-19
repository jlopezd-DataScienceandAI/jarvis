from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from pydantic import BaseModel, Field

from jarvis.brain.tools import ToolError, ToolRegistry, registry
from jarvis.brain.tools import system as system_tools
from jarvis.brain.tools.modes import ModeStore
from jarvis.brain.tools.system import find_app, match_processes

# ------------------------------------------------------------------ registry


def test_every_schema_is_self_contained_and_closed():
    schemas = registry.schemas()
    assert [s["name"] for s in schemas] == sorted(s["name"] for s in schemas)  # stable order for the cache
    for schema in schemas:
        dumped = json.dumps(schema)
        assert "$ref" not in dumped and "$defs" not in dumped, schema["name"]
        assert schema["input_schema"]["type"] == "object"
        assert schema["eager_input_streaming"] is True
        assert schema["description"]


def test_expected_tools_are_registered():
    assert set(registry.names()) == {
        "open_app", "close_app", "open_url", "system_status", "media_control", "set_volume",
        "search_files", "read_file", "move_or_rename", "write_file", "look_at_screen",
        "save_note", "create_task", "list_tasks", "complete_task", "search_notes", "remember_fact",
        "list_modes", "save_mode", "run_mode", "delete_mode", "get_weather",
        "play_music", "music_now_playing",
    }


def test_invalid_and_truncated_inputs_never_reach_the_tool(ctx):
    local = ToolRegistry()
    ran = []

    class Args(BaseModel):
        level: int = Field(ge=0, le=100)

    local.tool("vol", "test", Args)(lambda args, ctx: ran.append(args.level) or "ok")

    for bad in ({"level": 300}, {"level": "alto"}, {}, None, "not a dict"):
        result = local.call("vol", bad, ctx)
        assert result.is_error and "Argumentos no válidos" in result.content
    assert ran == []
    assert local.call("vol", {"level": 30}, ctx).content == "ok"
    assert local.call("nope", {}, ctx).is_error


def test_tool_exceptions_become_error_results(ctx):
    local = ToolRegistry()

    class Args(BaseModel):
        pass

    @local.tool("expected", "test", Args)
    def _expected(args, ctx):
        raise ToolError("no existe")

    @local.tool("bug", "test", Args)
    def _bug(args, ctx):
        raise RuntimeError("boom")

    assert local.call("expected", {}, ctx).content == "no existe"
    assert local.call("expected", {}, ctx).is_error
    bug = local.call("bug", {}, ctx)
    assert bug.is_error and "boom" in bug.content


# -------------------------------------------------------------- app matching


APPS = ["Google Chrome", "Chrome Remote Desktop", "Bloc de notas", "Visual Studio Code", "Spotify", "Calculadora"]


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("chrome", "Google Chrome"),
        ("Google Chrome", "Google Chrome"),
        ("bloc de notas", "Bloc de notas"),
        ("visual studio", "Visual Studio Code"),
        ("spotifi", "Spotify"),
        ("calculadora", "Calculadora"),
        ("photoshop", None),
    ],
)
def test_find_app(spoken, expected):
    assert find_app(spoken, APPS) == expected


RUNNING = {
    "chrome.exe": "Google Chrome",
    "msedge.exe": "Microsoft Edge",
    "msedgewebview2.exe": "Microsoft Edge WebView2",
    "Code.exe": "Visual Studio Code",
    "WINWORD.EXE": "Microsoft Word",
    "Discord.exe": "Discord",
    "GoogleDriveFS.exe": "Google Drive",
    "explorer.exe": "Explorador de Windows",
    "python.exe": "Python",
}
WINDOWED = {"chrome.exe", "msedge.exe", "Code.exe", "WINWORD.EXE", "explorer.exe"}


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("chrome", {"chrome.exe"}),
        ("Google Chrome", {"chrome.exe"}),  # must not drag in Google Drive
        ("edge", {"msedge.exe"}),  # nor the WebView2 runtime other apps depend on
        ("Microsoft Edge", {"msedge.exe"}),
        ("visual studio code", {"Code.exe"}),
        ("code", {"Code.exe"}),
        ("word", {"WINWORD.EXE"}),
        ("discord", {"Discord.exe"}),  # tray app with no window still matches by name
        ("explorer", set()),  # protected
        ("python", set()),  # protected: that is Jarvis itself
        ("e", set()),  # too short to match loosely
        ("photoshop", set()),
    ],
)
def test_match_processes(spoken, expected):
    assert match_processes(spoken, RUNNING, WINDOWED) == expected


# --------------------------------------------------------------------- modes


def test_mode_store_round_trip(tmp_path):
    store = ModeStore(tmp_path / "data" / "modes.yaml")
    assert store.all() == {}
    from jarvis.brain.tools.modes import Mode, Step

    mode = Mode(name="Estudio", description="Para concentrarse",
                steps=[Step(action="close_app", value="Discord"), Step(action="set_volume", value="20")])
    assert store.save(mode) is False
    assert store.save(mode) is True  # same name replaces
    assert store.get("ESTUDIO").steps[1].value == "20"  # lookup ignores case and accents
    assert store.delete("estudio") is True and store.delete("estudio") is False


def test_save_mode_validates_steps(ctx):
    bad_volume = registry.call("save_mode", {"name": "x", "steps": [{"action": "set_volume", "value": "alto"}]}, ctx)
    assert bad_volume.is_error and "0 a 100" in bad_volume.content
    empty = registry.call("save_mode", {"name": "x", "steps": []}, ctx)
    assert empty.is_error
    unknown = registry.call("save_mode", {"name": "x", "steps": [{"action": "delete_all", "value": "C:"}]}, ctx)
    assert unknown.is_error


def test_create_run_and_delete_mode(ctx, answers, monkeypatch):
    calls = []
    monkeypatch.setattr(system_tools, "open_app", lambda name: calls.append(("open", name)) or f"Abierto: {name}")
    monkeypatch.setattr(system_tools, "set_volume", lambda level: calls.append(("volume", level)) or "ok")

    def fake_close(name, force=False):
        calls.append(("close", name, force))
        if name == "Steam":
            raise system_tools.AppNotRunning("no está abierto")
        return f"Cerrado: {name}"

    monkeypatch.setattr(system_tools, "close_app", fake_close)

    steps = [
        {"action": "close_app", "value": "Discord"},
        {"action": "close_app", "value": "Steam"},
        {"action": "open_app", "value": "Notion"},
        {"action": "set_volume", "value": "30"},
        {"action": "say", "value": "Modo trabajo activado."},
    ]
    created = registry.call("save_mode", {"name": "Trabajo", "description": "Concentración", "steps": steps}, ctx)
    assert created.content.startswith("Modo creado: Trabajo")
    assert "Trabajo (Concentración)" in registry.call("list_modes", {}, ctx).content

    result = registry.call("run_mode", {"name": "trabajo"}, ctx)
    assert not result.is_error and "FALLÓ" not in result.content
    assert "Steam no estaba abierto" in result.content
    # Mode steps run without asking: the user approved them when defining the mode.
    assert ctx.asked == []
    assert calls == [("close", "Discord", True), ("close", "Steam", True), ("open", "Notion"), ("volume", 30)]
    assert ctx.said == ["Modo trabajo activado."]

    assert registry.call("run_mode", {"name": "inexistente"}, ctx).is_error

    answers.append(False)
    registry.call("delete_mode", {"name": "trabajo"}, ctx)
    assert "Trabajo" in registry.call("list_modes", {}, ctx).content
    answers.append(True)
    registry.call("delete_mode", {"name": "trabajo"}, ctx)
    assert registry.call("list_modes", {}, ctx).content == "No hay modos guardados todavía."


def test_one_failing_step_does_not_stop_the_mode(ctx, monkeypatch):
    def broken(name):
        raise ToolError("no instalada")

    monkeypatch.setattr(system_tools, "open_app", broken)
    monkeypatch.setattr(system_tools, "set_volume", lambda level: "Volumen al 10%")
    registry.call("save_mode", {"name": "m", "steps": [
        {"action": "open_app", "value": "Fantasma"}, {"action": "set_volume", "value": "10"}]}, ctx)
    result = registry.call("run_mode", {"name": "m"}, ctx).content
    assert "FALLÓ open_app Fantasma" in result and "Volumen al 10%" in result


def test_close_app_tool_requires_confirmation(ctx, answers, monkeypatch):
    closed = []
    monkeypatch.setattr(system_tools, "close_app", lambda name, force=False: closed.append(name) or "Cerrado")
    registry.call("close_app", {"name": "Chrome"}, ctx)
    assert closed == [] and ctx.asked == ["¿Confirma que cierro Chrome, señor?"]
    answers.append(True)
    registry.call("close_app", {"name": "Chrome"}, ctx)
    assert closed == ["Chrome"]


# -------------------------------------------------------------------- memory


def test_task_tools(ctx):
    future = datetime.now() + timedelta(days=1)
    created = registry.call("create_task", {"text": "Revisar el presupuesto", "due": f"{future:%Y-%m-%d} 10:00"}, ctx)
    assert "Tarea creada: Revisar el presupuesto" in created.content
    registry.call("create_task", {"text": "Revisar el correo"}, ctx)

    past = registry.call("create_task", {"text": "tarde", "due": "2020-01-01 10:00"}, ctx)
    assert past.is_error and "ya ha pasado" in past.content
    assert registry.call("create_task", {"text": "x", "due": "el martes"}, ctx).is_error

    ambiguous = registry.call("complete_task", {"query": "revisar"}, ctx)
    assert ambiguous.is_error and "varias" in ambiguous.content
    assert registry.call("complete_task", {"query": "presupuesto"}, ctx).content == "Hecha: Revisar el presupuesto"
    pending = registry.call("list_tasks", {}, ctx).content
    assert "correo" in pending and "presupuesto" not in pending
    assert "[hecha]" in registry.call("list_tasks", {"scope": "all"}, ctx).content


def test_note_and_fact_tools(ctx, config):
    registry.call("save_note", {"text": "El wifi del estudio va mal"}, ctx)
    assert "wifi" in registry.call("search_notes", {"query": "wifi"}, ctx).content
    registry.call("remember_fact", {"fact": "Prefiere la luz cálida."}, ctx)
    assert "luz cálida" in (config.memory_dir / "profile.md").read_text(encoding="utf-8")


def test_weather_without_city_explains_what_to_do(ctx):
    result = registry.call("get_weather", {}, ctx)
    assert result.is_error and "ciudad" in result.content
