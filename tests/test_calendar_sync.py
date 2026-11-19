"""Google Calendar mirror: what is sent, what is remembered, and how failures are handled."""

from __future__ import annotations

import re
import threading
from datetime import datetime

import pytest
import requests

from jarvis import calendar_setup
from jarvis.brain.tools import memory as memory_tools
from jarvis.brain.tools import registry
from jarvis.calendar_sync import CalendarClient, CalendarError, CalendarSync, event_id
from jarvis.memory_store import MemoryStore, parse_task

NOW = datetime(2026, 11, 14, 12, 0)


class FakeGoogle:
    """Stands in for the Apps Script web app: remembers the events it holds."""

    def __init__(self):
        self.events: dict[str, dict] = {}
        self.calls: list[dict] = []
        self.down = False
        self.reject: set[str] = set()

    def call(self, action, **fields):
        if self.down:
            raise CalendarError("sin conexión con Google (ConnectionError)")
        self.calls.append({"action": action, **fields})
        if action == "ping":
            return {"ok": True, "version": 1}
        reply = {"ok": True, "created": [], "deleted": [], "failed": {}}
        for key in fields["delete"]:
            self.events.pop(key, None)
            reply["deleted"].append(key)
        for item in fields["create"]:
            if item["title"] in self.reject:
                reply["failed"][item["id"]] = "Invalid resource"
                continue
            self.events[item["id"]] = item
            reply["created"].append(item["id"])
        return reply

    def titles(self):
        return sorted(item["title"] for item in self.events.values())


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory")


@pytest.fixture
def google():
    return FakeGoogle()


@pytest.fixture
def calendar(store, google, tmp_path):
    return CalendarSync(store, tmp_path / "data" / "calendar_sync.json", google, reminder_minutes=0, event_minutes=30)


def test_event_ids_are_stable_and_acceptable_to_google():
    task = parse_task("- [ ] Llamar al taller ⏰ 2026-11-15 10:00")
    assert event_id(task) == event_id(parse_task("- [ ] Llamar al taller ⏰ 2026-11-15 10:00 🔔"))
    assert event_id(task) != event_id(parse_task("- [ ] Llamar al taller ⏰ 2026-11-15 10:30"))
    assert event_id(task) != event_id(parse_task("- [ ] Llamar al taller ⏰ 2026-11-15"))
    assert re.fullmatch(r"[a-v0-9]{5,1024}", event_id(task))


def test_only_open_tasks_with_a_date_still_to_come_are_sent(store, calendar, google):
    store.add_task("Con hora", datetime(2026, 11, 15, 10, 0), has_time=True)
    store.add_task("Solo día", datetime(2026, 12, 1))
    store.add_task("Hoy, sin hora", datetime(2026, 11, 14))
    store.add_task("Sin fecha")
    store.add_task("Ya pasó", datetime(2026, 11, 14, 9, 0), has_time=True)
    store.add_task("Día pasado", datetime(2026, 11, 13))
    store.complete(store.add_task("Hecha", datetime(2026, 11, 20, 9, 0), has_time=True))

    result = calendar.sync(NOW)
    assert result.created == 3 and result.deleted == 0 and result.describe() == "3 añadidos"
    assert google.titles() == ["Con hora", "Hoy, sin hora", "Solo día"]


def test_timed_events_carry_the_reminder_and_all_day_events_do_not(store, calendar, google):
    store.add_task("Con hora", datetime(2026, 11, 15, 10, 0), has_time=True)
    store.add_task("Solo día", datetime(2026, 12, 31))
    calendar.sync(NOW)
    timed, all_day = (next(e for e in google.events.values() if e["title"] == t) for t in ("Con hora", "Solo día"))

    assert timed["reminder"] == 0
    start = datetime.fromisoformat(timed["start"]["dateTime"])
    end = datetime.fromisoformat(timed["end"]["dateTime"])
    # The offset travels with the time, so Google needs no guess about the PC's time zone.
    assert start.utcoffset() is not None and start.replace(tzinfo=None) == datetime(2026, 11, 15, 10, 0)
    assert (end - start).total_seconds() == 30 * 60

    assert all_day["start"] == {"date": "2026-12-31"} and all_day["end"] == {"date": "2027-01-01"}
    assert "reminder" not in all_day


def test_nothing_is_sent_twice(store, calendar, google):
    store.add_task("Con hora", datetime(2026, 11, 15, 10, 0), has_time=True)
    calendar.sync(NOW)
    assert not calendar.sync(NOW).changed
    assert len(google.calls) == 1


def test_completing_a_task_removes_its_event(store, calendar, google):
    task = store.add_task("Con hora", datetime(2026, 11, 15, 10, 0), has_time=True)
    store.add_task("Otra", datetime(2026, 11, 16, 10, 0), has_time=True)
    calendar.sync(NOW)
    store.complete(task)
    result = calendar.sync(NOW)
    assert (result.created, result.deleted) == (0, 1) and result.describe() == "1 quitado"
    assert google.titles() == ["Otra"]


def test_a_task_edited_by_hand_moves_its_event(store, calendar, google):
    store.add_task("Dentista", datetime(2026, 11, 15, 10, 0), has_time=True)
    calendar.sync(NOW)
    text = store.tasks_file.read_text(encoding="utf-8").replace("2026-11-15 10:00", "2026-11-16 17:30")
    store.tasks_file.write_text(text, encoding="utf-8")

    result = calendar.sync(NOW)
    assert (result.created, result.deleted) == (1, 1)
    [event] = google.events.values()
    assert event["start"]["dateTime"].startswith("2026-11-16T17:30")


def test_an_event_stays_while_its_task_is_open_even_after_its_time(store, calendar, google):
    store.add_task("Con hora", datetime(2026, 11, 15, 10, 0), has_time=True)
    calendar.sync(NOW)
    assert not calendar.sync(datetime(2026, 11, 15, 10, 5)).changed
    assert google.titles() == ["Con hora"]


def test_what_could_not_be_sent_is_sent_on_the_next_pass(store, calendar, google):
    store.add_task("Con hora", datetime(2026, 11, 15, 10, 0), has_time=True)
    google.down = True
    with pytest.raises(CalendarError):
        calendar.sync(NOW)
    google.down = False
    assert calendar.sync(NOW).created == 1
    assert google.titles() == ["Con hora"]


def test_an_event_google_rejects_does_not_hold_back_the_rest(store, calendar, google):
    store.add_task("Buena", datetime(2026, 11, 15, 10, 0), has_time=True)
    store.add_task("Mala", datetime(2026, 11, 15, 11, 0), has_time=True)
    google.reject.add("Mala")
    result = calendar.sync(NOW)
    assert result.created == 1 and list(result.failed.values()) == ["Invalid resource"]
    google.reject.clear()
    assert calendar.sync(NOW).created == 1  # only the rejected one is retried
    assert [len(call["create"]) for call in google.calls] == [2, 1]


def test_many_tasks_are_sent_in_batches(store, calendar, google):
    for hour in range(24):
        for minute in (0, 30):
            store.add_task(f"Tarea {hour}:{minute}", datetime(2026, 11, 20, hour, minute), has_time=True)
    assert calendar.sync(NOW).created == 48
    assert [len(call["create"]) for call in google.calls] == [20, 20, 8]


def test_a_damaged_state_file_is_rebuilt_without_duplicates(store, calendar, google, tmp_path):
    store.add_task("Con hora", datetime(2026, 11, 15, 10, 0), has_time=True)
    calendar.sync(NOW)
    (tmp_path / "data" / "calendar_sync.json").write_text("{ esto no es json", encoding="utf-8")
    assert calendar.sync(NOW).created == 1  # sent again with the same id: Google keeps a single event
    assert len(google.events) == 1


# ------------------------------------------------------------ background worker


def test_the_worker_syncs_on_request_and_reports_once(store, calendar, google):
    reports = []
    calendar.on_status = lambda status, detail: reports.append((status, detail))
    calendar.start()
    assert calendar.wait_idle(5.0)
    assert reports == [("active", "")] and google.calls == [{"action": "ping"}]  # connection checked at start

    store.add_task("Con hora", datetime(2099, 1, 1, 10, 0), has_time=True)
    calendar.request()
    assert calendar.wait_idle(5.0)
    store.add_task("Otra", datetime(2099, 1, 2, 10, 0), has_time=True)
    calendar.request()
    assert calendar.wait_idle(5.0)
    calendar.stop()
    assert reports == [("active", ""), ("active", "1 añadido"), ("active", "1 añadido")]
    assert google.titles() == ["Con hora", "Otra"]


def test_the_worker_reports_an_outage_once_and_recovers(store, calendar, google):
    reports = []
    calendar.on_status = lambda status, detail: reports.append(status)
    store.add_task("Con hora", datetime(2099, 1, 1, 10, 0), has_time=True)
    google.down = True
    calendar.start()
    assert calendar.wait_idle(5.0)
    calendar.request()
    assert calendar.wait_idle(5.0)
    google.down = False
    calendar.request()
    assert calendar.wait_idle(5.0)
    calendar.stop()
    assert reports == ["error", "active"]
    assert google.titles() == ["Con hora"]


def test_a_failing_listener_does_not_stop_the_worker(store, calendar, google):
    done = threading.Event()

    def listener(status, detail):
        done.set()
        raise RuntimeError("HUD roto")

    calendar.on_status = listener
    store.add_task("Con hora", datetime(2099, 1, 1, 10, 0), has_time=True)
    calendar.start()
    assert calendar.wait_idle(5.0) and done.is_set()
    store.add_task("Otra", datetime(2099, 1, 2, 10, 0), has_time=True)
    calendar.request()
    assert calendar.wait_idle(5.0)
    calendar.stop()
    assert google.titles() == ["Con hora", "Otra"]


# ----------------------------------------------------------------------- client


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def test_client_sends_the_key_and_returns_the_reply(monkeypatch):
    sent = {}

    def post(url, json, timeout):
        sent.update(url=url, json=json)
        return FakeResponse(payload={"ok": True, "created": ["a"]})

    monkeypatch.setattr(requests, "post", post)
    reply = CalendarClient("https://script.google.com/macros/s/X/exec", "secreto").call("apply", create=[], delete=[])
    assert reply["created"] == ["a"]
    assert sent["json"] == {"key": "secreto", "action": "apply", "create": [], "delete": []}


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (FakeResponse(payload={"ok": False, "error": "bad_key"}), "clave no coincide"),
        (FakeResponse(payload={"ok": False, "error": "no_service"}), "Google Calendar API"),
        (FakeResponse(payload={"ok": False, "error": "Exception: boom"}), "boom"),
        (FakeResponse(text="<html>Sign in</html>"), "Cualquier usuario"),
        (FakeResponse(status_code=404), "404"),
    ],
)
def test_client_explains_each_kind_of_failure(monkeypatch, response, expected):
    monkeypatch.setattr(requests, "post", lambda *a, **k: response)
    with pytest.raises(CalendarError, match=expected):
        CalendarClient("https://example.invalid", "k").call("ping")


def test_client_turns_network_errors_into_calendar_errors(monkeypatch):
    def post(*args, **kwargs):
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(CalendarError, match="sin conexión"):
        CalendarClient("https://example.invalid", "k").call("ping")


# ------------------------------------------------------------------------ tools


class RecordingCalendar:
    def __init__(self):
        self.requests = 0

    def request(self):
        self.requests += 1


def test_without_setup_the_task_tools_do_not_mention_the_calendar(ctx, config):
    assert memory_tools.get_calendar(config) is None
    created = registry.call("create_task", {"text": "Revisar el presupuesto", "due": "2099-01-01 10:00"}, ctx)
    assert "Calendar" not in created.content


def test_setup_and_switch_decide_whether_there_is_a_calendar(config, monkeypatch):
    monkeypatch.setenv("JARVIS_CALENDAR_URL", "https://script.google.com/macros/s/X/exec")
    assert memory_tools.get_calendar(config) is None  # no key yet
    monkeypatch.setenv("JARVIS_CALENDAR_KEY", "secreto")
    calendar = memory_tools.get_calendar(config)
    assert calendar is not None and memory_tools.get_calendar(config) is calendar
    config.calendar.enabled = False
    assert memory_tools.get_calendar(config) is None


def test_task_tools_ask_for_a_sync_when_the_calendar_is_connected(ctx, monkeypatch):
    calendar = RecordingCalendar()
    monkeypatch.setattr(memory_tools, "get_calendar", lambda config: calendar)

    timed = registry.call("create_task", {"text": "Revisar el presupuesto", "due": "2099-01-01 10:00"}, ctx)
    assert "avisará en el móvil" in timed.content and calendar.requests == 1
    all_day = registry.call("create_task", {"text": "Renovar el seguro", "due": "2099-02-01"}, ctx)
    assert "evento de todo el día" in all_day.content and calendar.requests == 2

    undated = registry.call("create_task", {"text": "Comprar pan"}, ctx)
    assert "Calendar" not in undated.content and calendar.requests == 2  # nothing for a calendar to show

    registry.call("complete_task", {"query": "presupuesto"}, ctx)
    assert calendar.requests == 3
    registry.call("complete_task", {"query": "pan"}, ctx)
    assert calendar.requests == 3


# ------------------------------------------------------------------------ setup


def test_the_script_given_to_the_user_carries_their_key():
    script = calendar_setup.script_text("clave-de-prueba")
    assert "var KEY = 'clave-de-prueba';" in script and "__JARVIS_KEY__" not in script
    assert "function doPost(e)" in script


@pytest.mark.parametrize(
    ("url", "valid"),
    [
        ("https://script.google.com/macros/s/AKfycbx123/exec", True),
        ("https://script.google.com/macros/s/AKfycbx123/dev", False),
        ("https://script.google.com/home/projects/abc/edit", False),
        ("https://evil.example/macros/s/x/exec", False),
        ("", False),
    ],
)
def test_only_a_web_app_url_is_accepted(url, valid):
    assert calendar_setup.is_web_app_url(url) is valid
