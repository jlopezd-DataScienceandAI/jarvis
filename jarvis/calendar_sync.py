"""Mirror dated tasks into Google Calendar, so the phone notifies as well.

``memory/tasks.md`` stays the source of truth. This module compares it with what
has already been sent and asks a small Apps Script web app in the user's Google
account (``calendar_script.gs``) to create or delete events in a calendar called
"Jarvis". It is a reconciliation, not a log of operations: whatever changed the
file (a tool, the user's editor) and whenever the network was down, the next
pass sends exactly what is missing.

Event ids are derived from the task's text and date, so sending the same task
twice cannot duplicate it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from jarvis.memory_store import MemoryStore, Task

log = logging.getLogger(__name__)

URL_ENV = "JARVIS_CALENDAR_URL"
KEY_ENV = "JARVIS_CALENDAR_KEY"

_BATCH = 20  # events per request, to stay well inside Apps Script's execution limit
_CHECK_EVERY_S = 60.0  # how often tasks.md is compared with what was sent
_MAX_RETRY_S = 900.0

_SCRIPT_ERRORS = {
    "bad_key": "La clave no coincide con la del script de Google. Repite «jarvis calendar setup».",
    "no_service": (
        "Al script le falta el servicio «Google Calendar API». En el editor de Apps Script, junto a "
        "«Servicios», pulsa «+», elige «Google Calendar API» y añádelo. Después, en Implementar > "
        "Gestionar implementaciones, edita la implementación y elige «Nueva versión»."
    ),
    "unknown_action": "El script de Google es más antiguo que Jarvis. Repite «jarvis calendar setup».",
}


class CalendarError(Exception):
    """A failure talking to Google, with a message fit to show the user."""


def event_id(task: Task) -> str:
    """Stable id for a task's event. Google accepts lowercase a-v and digits."""
    stamp = task.due.strftime("%Y-%m-%d %H:%M" if task.has_time else "%Y-%m-%d") if task.due else ""
    return "jarvis" + hashlib.sha1(f"{task.text}\n{stamp}".encode()).hexdigest()


def has_passed(task: Task, now: datetime) -> bool:
    """Whether the task's moment is behind us, so a new event could no longer notify."""
    if not task.due:
        return True
    return task.due <= now if task.has_time else task.due.date() < now.date()


class CalendarClient:
    """Talks to the Apps Script web app."""

    def __init__(self, url: str, key: str, timeout_s: float = 60.0) -> None:
        self._url = url
        self._key = key
        self._timeout_s = timeout_s

    def call(self, action: str, **fields: Any) -> dict[str, Any]:
        payload = {"key": self._key, "action": action, **fields}
        try:
            # Apps Script answers with a redirect to the result; requests follows it with a GET.
            response = requests.post(self._url, json=payload, timeout=self._timeout_s)
        except requests.RequestException as exc:
            raise CalendarError(f"sin conexión con Google ({type(exc).__name__})") from exc
        if response.status_code != 200:
            raise CalendarError(f"Google ha respondido con el error {response.status_code}")
        try:
            reply = response.json()
        except ValueError as exc:
            # A sign-in page instead of JSON: the deployment is not open to anonymous requests.
            raise CalendarError(
                "Google no ha devuelto una respuesta válida. Comprueba que la dirección termina en /exec "
                "y que en la implementación «Quién tiene acceso» es «Cualquier usuario»."
            ) from exc
        if not isinstance(reply, dict) or not reply.get("ok"):
            code = str(reply.get("error", "")) if isinstance(reply, dict) else ""
            raise CalendarError(_SCRIPT_ERRORS.get(code, f"el script de Google ha fallado: {code or reply}"))
        return reply


@dataclass
class SyncResult:
    created: int = 0
    deleted: int = 0
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.created or self.deleted)

    def describe(self) -> str:
        parts = []
        if self.created:
            parts.append(f"{self.created} añadido{'s' if self.created != 1 else ''}")
        if self.deleted:
            parts.append(f"{self.deleted} quitado{'s' if self.deleted != 1 else ''}")
        return ", ".join(parts) or "sin cambios"


class CalendarSync:
    def __init__(
        self,
        store: MemoryStore,
        state_file: Path,
        client: CalendarClient,
        reminder_minutes: int = 0,
        event_minutes: int = 30,
    ) -> None:
        self._store = store
        self._state_file = state_file
        self._client = client
        self._reminder_minutes = reminder_minutes
        self._event_minutes = event_minutes
        # Called from the worker thread with ('active' | 'error', detail) when there is news.
        self.on_status: Callable[[str, str], None] = lambda status, detail: None

        self._sync_lock = threading.Lock()
        self._flags = threading.Lock()
        self._wake = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_report: tuple[str, str] | None = None
        self._verified = False  # whether Google has answered at least once in this run

    # ------------------------------------------------------------------ state

    def _load(self) -> dict[str, str]:
        """Ids of the events already in the calendar, with a label for whoever reads the file."""
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            return dict(data["synced"])
        except FileNotFoundError:
            return {}
        except (ValueError, KeyError, TypeError):
            # Starting from nothing is safe: ids are stable, so re-sending creates no duplicates.
            log.warning("Unreadable %s; starting the calendar state from scratch", self._state_file.name)
            return {}

    def _save(self, synced: dict[str, str]) -> None:
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        temp = self._state_file.with_suffix(".tmp")
        temp.write_text(json.dumps({"synced": synced}, ensure_ascii=False, indent=1), encoding="utf-8")
        temp.replace(self._state_file)

    # ------------------------------------------------------------------- sync

    def _event(self, task: Task) -> dict[str, Any]:
        event: dict[str, Any] = {"id": event_id(task), "title": task.text}
        if task.has_time:
            start = task.due.astimezone()  # naive local time -> with this PC's UTC offset on that date
            end = start + timedelta(minutes=self._event_minutes)
            event["start"] = {"dateTime": start.isoformat(timespec="seconds")}
            event["end"] = {"dateTime": end.isoformat(timespec="seconds")}
            event["reminder"] = self._reminder_minutes
        else:
            # All-day: no reminder of ours, so the calendar's own all-day notification setting applies.
            day = task.due.date()
            event["start"] = {"date": day.isoformat()}
            event["end"] = {"date": (day + timedelta(days=1)).isoformat()}
        return event

    def plan(self, now: datetime | None = None) -> tuple[list[Task], list[str]]:
        """What the calendar is missing and what it has in excess: (tasks to create, ids to delete)."""
        now = now or datetime.now()
        synced = self._load()
        wanted = {event_id(t): t for t in self._store.tasks() if not t.done and t.due}
        create = [t for key, t in wanted.items() if key not in synced and not has_passed(t, now)]
        delete = [key for key in synced if key not in wanted]
        return create, delete

    def sync(self, now: datetime | None = None) -> SyncResult:
        """Bring the calendar up to date. Raises CalendarError if Google cannot be reached."""
        with self._sync_lock:
            create, delete = self.plan(now)
            result = SyncResult()
            if not create and not delete:
                return result
            synced = self._load()
            labels = {event_id(t): t.to_line() for t in create}
            events = [self._event(t) for t in create]
            try:
                for start in range(0, max(len(events), len(delete)), _BATCH):
                    reply = self._client.call(
                        "apply", create=events[start:start + _BATCH], delete=delete[start:start + _BATCH]
                    )
                    for key in reply.get("created", []):
                        synced[key] = labels.get(key, "")
                        result.created += 1
                    for key in reply.get("deleted", []):
                        synced.pop(key, None)
                        result.deleted += 1
                    result.failed.update(reply.get("failed") or {})
            finally:
                if result.changed:
                    self._save(synced)  # keep what did go through, even if a later batch failed
            return result

    def ping(self) -> dict[str, Any]:
        return self._client.call("ping")

    # ------------------------------------------------------------- background

    def start(self) -> None:
        """Start the background worker. Its first pass runs right away."""
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="calendar", daemon=True)
            self._thread.start()
        self.request()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def request(self) -> None:
        """Ask for a pass soon (a task was just created or completed). Never blocks."""
        if self._thread is None:
            self.start()
            return
        with self._flags:
            self._idle.clear()
            self._wake.set()

    def wait_idle(self, timeout_s: float) -> bool:
        """Wait for requested passes to finish; False if they are still running."""
        return self._idle.wait(timeout_s)

    def _loop(self) -> None:
        failures = 0
        waited = 0.0
        while not self._stop.is_set():
            requested = self._wake.wait(_CHECK_EVERY_S)
            if self._stop.is_set():
                break
            self._wake.clear()
            waited = 0.0 if requested else waited + _CHECK_EVERY_S
            # After a failure the periodic retries spread out; an explicit request always tries.
            if requested or waited >= min(_CHECK_EVERY_S * 2**failures, _MAX_RETRY_S):
                waited = 0.0
                failures = 0 if self._pass() else failures + 1
            with self._flags:
                if not self._wake.is_set():
                    self._idle.set()
        self._idle.set()

    def _pass(self) -> bool:
        try:
            result = self.sync()
            if not self._verified and not result.changed and not result.failed:
                self.ping()  # nothing to send yet: still find out now whether the connection works
            self._verified = True
        except CalendarError as exc:
            self._report("error", str(exc))
            return False
        except Exception:  # noqa: BLE001 - a malformed tasks.md must not kill the worker
            log.exception("Calendar sync failed")
            self._report("error", "fallo inesperado; el detalle está en el registro")
            return False
        if result.failed:
            log.warning("Calendar events not applied: %s", result.failed)
            self._report("error", f"{len(result.failed)} sin aplicar: {next(iter(result.failed.values()))}")
            return False
        self._report("active", result.describe() if result.changed else "")
        return True

    def _report(self, status: str, detail: str) -> None:
        """Tell the app, but not the same thing over and over while nothing changes."""
        previous, self._last_report = self._last_report, (status, detail)
        if status == "error" and previous == (status, detail):
            return
        if status == "active" and not detail and previous and previous[0] == "active":
            return
        if status == "error":
            log.warning("Calendar: %s", detail)
        try:
            self.on_status(status, detail)
        except Exception:  # noqa: BLE001 - the listener is UI code; it must not stop the sync
            log.exception("Calendar status listener failed")
