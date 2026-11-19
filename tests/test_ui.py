"""The HUD: what the assistant publishes, and what the local server accepts."""

from __future__ import annotations

import http.client
import json
from datetime import datetime

import pytest

from jarvis.app import live_tail, pretty_hotkey
from jarvis.memory_store import MemoryStore
from jarvis.ui.hub import TOOL_LABELS, Hub
from jarvis.ui.monitor import SystemMonitor, today_items
from jarvis.ui.server import UiServer
from tests.test_voice_loop import make_app  # noqa: F401 - pytest fixture

# ------------------------------------------------------------------------ hub


def test_late_subscriber_gets_the_full_picture_and_then_only_changes():
    hub = Hub()
    hub.update(mode="idle", text="")
    hub.set_service("mic", "active")

    snapshot = hub.snapshot()
    assert snapshot["mode"] == "idle"
    assert snapshot["services"] == {"mic": "active", "brain": "loading", "voice": "loading", "calendar": "off"}

    updates = hub.subscribe()
    hub.update(mode="listening", text="Le escucho…")
    assert updates.get_nowait() == {"mode": "listening", "text": "Le escucho…"}
    assert updates.empty()

    hub.unsubscribe(updates)
    hub.update(mode="idle")
    assert updates.empty()


def test_a_running_tool_line_is_rewritten_when_it_finishes():
    hub = Hub()
    line = hub.tool_started("get_weather")
    assert hub.snapshot()["log"] == [{"id": line, "status": "run", "text": "clima · consultando"}]
    hub.tool_finished("get_weather", line, failed=False)
    assert hub.snapshot()["log"] == [{"id": line, "status": "ok", "text": "clima · actualizado"}]

    failed = hub.tool_started("open_app")
    hub.tool_finished("open_app", failed, failed=True)
    assert hub.snapshot()["log"][-1] == {"id": failed, "status": "err", "text": "aplicaciones · no se pudo"}


def test_log_is_bounded_and_unknown_tools_still_show():
    hub = Hub()
    for index in range(30):
        hub.log("ok", f"línea {index}")
    log = hub.snapshot()["log"]
    assert len(log) == 8 and log[-1]["text"] == "línea 29"
    hub.tool_started("herramienta_nueva")
    assert hub.snapshot()["log"][-1]["text"] == "herramienta_nueva · en curso"


def test_every_registered_tool_has_a_label():
    from jarvis.brain.tools import registry

    assert set(registry.names()) <= set(TOOL_LABELS)


def test_closing_the_hub_ends_the_streams():
    hub = Hub()
    updates = hub.subscribe()
    hub.close()
    assert updates.get_nowait() == {"mode": "offline"}
    assert updates.get_nowait() is None


# -------------------------------------------------------------------- panels


def test_today_panel_lists_overdue_and_todays_tasks_in_order(tmp_path):
    store = MemoryStore(tmp_path)
    store.add_task("Entrega del proyecto", datetime(2026, 11, 14, 18, 0), has_time=True)
    store.add_task("Reunión de equipo", datetime(2026, 11, 14, 10, 30), has_time=True)
    store.add_task("Pagar el seguro", datetime(2026, 11, 14), has_time=False)
    store.add_task("Llamar al taller", datetime(2026, 11, 12, 9, 0), has_time=True)
    store.add_task("Mañana", datetime(2026, 11, 15, 9, 0), has_time=True)
    store.add_task("Sin fecha")
    store.complete(store.add_task("Ya hecha", datetime(2026, 11, 14, 8, 0), has_time=True))

    items = today_items(store, datetime(2026, 11, 14, 12, 0))
    assert items == [
        {"when": "12 nov", "text": "Llamar al taller", "overdue": True},
        {"when": "hoy", "text": "Pagar el seguro", "overdue": False},
        {"when": "10:30", "text": "Reunión de equipo", "overdue": True},
        {"when": "18:00", "text": "Entrega del proyecto", "overdue": False},
    ]


def test_today_panel_summarizes_when_there_are_too_many(tmp_path):
    store = MemoryStore(tmp_path)
    for hour in range(9, 18):
        store.add_task(f"Tarea {hour}", datetime(2026, 11, 14, hour, 0), has_time=True)
    items = today_items(store, datetime(2026, 11, 14, 8, 0))
    assert len(items) == 6 and items[-1] == {"when": "…", "text": "y 4 más", "overdue": False}


def test_monitor_publishes_meters_and_only_republishes_today_when_it_changes(tmp_path):
    hub = Hub()
    store = MemoryStore(tmp_path)
    monitor = SystemMonitor(hub, store)
    meters = monitor.sample()
    assert set(meters) == {"cpu", "mem", "net"} and 0 <= meters["mem"] <= 100 and meters["net"] >= 0

    updates = hub.subscribe()
    monitor.refresh_today()
    monitor.refresh_today()
    assert updates.get_nowait() == {"today": []}
    assert updates.empty()


# ----------------------------------------------------------------- live text


def test_live_text_keeps_the_end_of_a_long_answer():
    sentences = [f"Frase número {index} de una respuesta bastante larga." for index in range(12)]
    tail = live_tail(sentences, limit=120)
    assert tail.startswith("… ") and tail.endswith(sentences[-1]) and len(tail) <= 125
    assert live_tail(["Hecho, señor."]) == "Hecho, señor."
    assert live_tail(["x" * 500], limit=100).startswith("…") and len(live_tail(["x" * 500], limit=100)) == 101


def test_hotkey_is_shown_the_way_keys_are_labelled():
    assert pretty_hotkey("<ctrl>+<alt>+j") == "Ctrl+Alt+J"
    assert pretty_hotkey("<f9>") == "F9"


def test_the_hud_follows_a_conversation(make_app):  # noqa: F811
    app = make_app(["abre Chrome"], ["Enseguida, señor. Chrome está abierto."])
    updates = app.hub.subscribe()
    app._converse()
    modes = []
    texts = []
    while not updates.empty():
        change = updates.get_nowait()
        if "mode" in change:
            modes.append(change["mode"])
        if "text" in change:
            texts.append(change["text"])
    assert modes[0] == "thinking" and "speaking" in modes and modes[-1] == "idle"
    assert texts[-1] == "Enseguida, señor. Chrome está abierto."  # the answer stays on screen


def test_typed_text_is_answered_and_slash_lines_are_commands(make_app):  # noqa: F811
    app = make_app([], ["Son las diez, señor."])
    app._handle_typed("¿qué hora es?")
    assert app.agent.heard == ["¿qué hora es?"]
    assert app.speaker.said == ["Son las diez, señor."]

    app._handle_typed("/breve")
    assert app.agent.verbosity == "breve"
    assert app.agent.heard == ["¿qué hora es?"]  # the command was not said to Jarvis
    assert "breve" in app.hub.snapshot()["log"][-1]["text"]


def test_tool_activity_reaches_the_log(make_app):  # noqa: F811
    app = make_app([], [])
    app._on_tool("get_weather", "start")
    assert app.hub.snapshot()["log"][-1]["status"] == "run"
    app._on_tool("get_weather", "end", False)
    assert app.hub.snapshot()["log"][-1] == {"id": 1, "status": "ok", "text": "clima · actualizado"}


# -------------------------------------------------------------------- server


@pytest.fixture
def server():
    ui = UiServer(Hub())
    ui.activated = 0
    ui.said = []

    def activate():
        ui.activated += 1

    ui.on_activate = activate
    ui.on_say = ui.said.append
    ui.start()
    yield ui
    ui.stop()


def request(ui, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", ui.port, timeout=5)
    payload = json.dumps(body).encode() if body is not None else None
    connection.request(method, path, body=payload, headers=headers or {})
    response = connection.getresponse()
    # An event stream never ends: leave it for the caller to read line by line.
    streaming = (response.getheader("Content-Type") or "").startswith("text/event-stream")
    data = b"" if streaming else response.read()
    return response, data, connection


def test_page_is_served(server):
    response, data, _ = request(server, "GET", "/")
    assert response.status == 200 and response.getheader("Content-Type").startswith("text/html")
    page = data.decode("utf-8")
    assert "J.A.R.V.I.S" in page and "PRÓXIMAMENTE" in page and "Escribe un comando" in page


def test_actions_need_the_token(server):
    assert request(server, "POST", "/api/activate")[0].status == 403
    assert request(server, "POST", "/api/activate", headers={"X-Jarvis-Token": "adivinado"})[0].status == 403
    assert server.activated == 0

    ok = {"X-Jarvis-Token": server.token}
    assert request(server, "POST", "/api/activate", headers=ok)[0].status == 204
    assert server.activated == 1
    assert request(server, "POST", "/api/say", {"text": "  abre Chrome  "}, ok)[0].status == 204
    assert server.said == ["abre Chrome"]


def test_requests_from_other_web_pages_are_refused(server):
    ok = {"X-Jarvis-Token": server.token}
    # Another site calling localhost: the browser sends its own Origin.
    assert request(server, "POST", "/api/say", {"text": "x"}, {**ok, "Origin": "https://evil.example"})[0].status == 403
    # DNS rebinding: a foreign hostname resolving to 127.0.0.1.
    assert request(server, "POST", "/api/say", {"text": "x"}, {**ok, "Host": "evil.example"})[0].status == 403
    assert request(server, "GET", "/", headers={"Host": "evil.example"})[0].status == 403
    assert server.said == []
    # The HUD itself, with its own origin, is accepted.
    assert request(server, "POST", "/api/say", {"text": "hola"}, {**ok, "Origin": server.origin})[0].status == 204


def test_bad_bodies_are_rejected(server):
    ok = {"X-Jarvis-Token": server.token}
    assert request(server, "POST", "/api/say", {"text": "   "}, ok)[0].status == 400
    assert request(server, "POST", "/api/say", {"nada": 1}, ok)[0].status == 400
    assert request(server, "POST", "/api/say", ["lista"], ok)[0].status == 400
    assert request(server, "POST", "/api/say", {"text": "x" * 9000}, ok)[0].status == 413
    assert request(server, "POST", "/api/otra", {}, ok)[0].status == 404
    assert server.said == []


def test_actions_are_refused_until_the_assistant_is_ready():
    ui = UiServer(Hub())
    ui.start()
    try:
        ok = {"X-Jarvis-Token": ui.token}
        assert request(ui, "POST", "/api/activate", headers=ok)[0].status == 503
        assert request(ui, "POST", "/api/say", {"text": "hola"}, ok)[0].status == 503
    finally:
        ui.stop()


def test_event_stream_needs_the_token_and_delivers_state(server):
    assert request(server, "GET", "/events")[0].status == 403
    assert request(server, "GET", "/events?token=mal")[0].status == 403

    response, _, connection = request(server, "GET", f"/events?token={server.token}")
    assert response.status == 200 and response.getheader("Content-Type").startswith("text/event-stream")

    def next_event():
        line = response.fp.readline().decode("utf-8")
        assert line.startswith("data: ")
        response.fp.readline()  # blank separator
        return json.loads(line[6:])

    assert next_event()["mode"] == "starting"  # full snapshot first
    server.hub.update(mode="listening", text="Le escucho…")
    assert next_event() == {"mode": "listening", "text": "Le escucho…"}
    connection.close()


# -------------------------------------------------------------------- window


def test_window_is_sized_to_fit_the_screen_and_clear_of_the_taskbar():
    from jarvis.ui.window import fit_window

    # A 1440x900 laptop screen: the default 1280x860 window must shrink to leave the taskbar free.
    width, height, left, top = fit_window(1280, 860, (1440, 900))
    assert (width, left) == (1280, 80)
    assert top + height <= 900 - 72
    # A large monitor keeps the requested size, centred.
    assert fit_window(1280, 860, (2560, 1440)) == (1280, 860, 640, 254)
    # A screen narrower than the window.
    width, height, left, top = fit_window(1280, 860, (1024, 768))
    assert width <= 1024 and left >= 0 and top >= 0
    # Unknown screen: the requested size, near the corner.
    assert fit_window(1280, 860, None) == (1280, 860, 40, 20)


def test_closing_the_window_stops_jarvis_even_mid_answer(make_app):  # noqa: F811
    from jarvis.app import State

    app = make_app([], [])
    app.state = State.BUSY
    app.request_quit()
    assert app.cancel.is_set() and app.speaker.stopped == 1
    assert app.events.get_nowait() == ("quit", None)


# --------------------------------------------------------------------- music


def test_player_names_are_readable():
    from jarvis.ui.media import app_label

    assert app_label("SpotifyAB.SpotifyMusic_zpdnekdrzrea0!SpotifyCli") == "Spotify"
    assert app_label("Chrome") == "Chrome"
    assert app_label("MSEdge") == "Edge"
    assert app_label("Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic") == "Reproductor multimedia"
    assert app_label("foobar2000.exe") == "foobar2000"


def test_cover_becomes_a_small_jpeg_and_bad_images_are_ignored():
    import base64
    import io

    from PIL import Image

    from jarvis.ui.media import cover_data_url

    source = io.BytesIO()
    Image.new("RGBA", (300, 300), (200, 30, 30, 255)).save(source, format="PNG")
    url = cover_data_url(source.getvalue())
    assert url.startswith("data:image/jpeg;base64,")
    image = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
    assert image.size == (128, 128) and image.format == "JPEG"
    assert cover_data_url(b"esto no es una imagen") == ""


def test_music_panel_only_changes_when_the_track_does():
    from jarvis.ui.media import NowPlayingWatcher

    hub = Hub()
    watcher = NowPlayingWatcher(hub)
    updates = hub.subscribe()
    song = {"title": "Thunderstruck", "artist": "AC/DC", "app": "Spotify", "key": "Spotify|AC/DC|Thunderstruck",
            "cover": "data:image/jpeg;base64,AAAA"}

    watcher.apply(None)  # nothing playing and nothing shown: no message
    assert updates.empty()

    watcher.apply(song)
    assert updates.get_nowait() == {"music": song}
    # Later readings of the same song come without the cover and must not replace it.
    watcher.apply({k: v for k, v in song.items() if k != "cover"})
    assert updates.empty() and hub.snapshot()["music"]["cover"] == song["cover"]

    other = {**song, "title": "Back in Black", "key": "Spotify|AC/DC|Back in Black"}
    watcher.apply(other)
    assert updates.get_nowait() == {"music": other}

    watcher.apply(None)  # paused or stopped: the panel goes away
    assert updates.get_nowait() == {"music": None}
    watcher.apply(None)
    assert updates.empty()
