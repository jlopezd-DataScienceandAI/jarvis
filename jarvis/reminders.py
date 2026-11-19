"""Background watcher that reports tasks whose time has come."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime

from jarvis.memory_store import MemoryStore, Task

log = logging.getLogger(__name__)


class ReminderWatcher:
    def __init__(self, store: MemoryStore, on_due: Callable[[Task], None], interval_s: float = 20.0) -> None:
        self._store = store
        self._on_due = on_due
        self._interval_s = interval_s
        self._stop = threading.Event()
        # Lines already handed over, so a task still waiting to be announced is not queued twice.
        self._handed_over: set[str] = set()

    def start(self) -> None:
        threading.Thread(target=self._loop, name="reminders", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def check(self, now: datetime | None = None) -> list[Task]:
        due = [t for t in self._store.due_reminders(now or datetime.now()) if t.raw not in self._handed_over]
        for task in due:
            self._handed_over.add(task.raw)
            self._on_due(task)
        return due

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.check()
            except Exception:  # noqa: BLE001 - a malformed tasks.md must not kill the watcher
                log.exception("Reminder check failed")
            self._stop.wait(self._interval_s)


def toast(title: str, message: str) -> None:
    """Windows notification, for when the user is not within earshot."""
    try:
        from winotify import Notification

        Notification(app_id="J.A.R.V.I.S.", title=title, msg=message).show()
    except Exception:  # noqa: BLE001 - notifications are best-effort
        log.debug("Toast failed", exc_info=True)
