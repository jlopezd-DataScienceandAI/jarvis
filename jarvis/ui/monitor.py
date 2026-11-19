"""Feeds the HUD's side panels: system meters and today's agenda."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any

import psutil

from jarvis.memory_store import MemoryStore

from .hub import Hub

log = logging.getLogger(__name__)

_MONTHS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
_MAX_ITEMS = 6


def today_items(store: MemoryStore, now: datetime) -> list[dict[str, Any]]:
    """Open tasks that are overdue or due today, soonest first, for the 'Hoy' panel."""
    relevant = [t for t in store.tasks() if not t.done and t.due and (t.is_today(now) or t.is_overdue(now))]
    relevant.sort(key=lambda t: t.due)
    items = []
    for task in relevant:
        if task.is_today(now):
            when = task.due.strftime("%H:%M") if task.has_time else "hoy"
        else:
            when = f"{task.due.day} {_MONTHS[task.due.month - 1]}"
        items.append({"when": when, "text": task.text, "overdue": task.is_overdue(now)})
    if len(items) > _MAX_ITEMS:
        hidden = len(items) - (_MAX_ITEMS - 1)
        items = items[: _MAX_ITEMS - 1] + [{"when": "…", "text": f"y {hidden} más", "overdue": False}]
    return items


class SystemMonitor:
    def __init__(self, hub: Hub, store: MemoryStore, interval_s: float = 1.5) -> None:
        self._hub = hub
        self._store = store
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._last_net: tuple[float, int] | None = None
        self._last_today: list[dict[str, Any]] | None = None

    def start(self) -> None:
        psutil.cpu_percent(None)  # the first reading is meaningless; prime it
        threading.Thread(target=self._loop, name="ui-monitor", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def sample(self) -> dict[str, float]:
        counters = psutil.net_io_counters()
        total = counters.bytes_sent + counters.bytes_recv
        now = time.monotonic()
        megabits = 0.0
        if self._last_net is not None:
            elapsed = now - self._last_net[0]
            if elapsed > 0:
                megabits = max(0.0, (total - self._last_net[1]) * 8 / 1_000_000 / elapsed)
        self._last_net = (now, total)
        return {
            "cpu": round(psutil.cpu_percent(None)),
            "mem": round(psutil.virtual_memory().percent),
            "net": round(megabits, 1),
        }

    def refresh_today(self) -> None:
        items = today_items(self._store, datetime.now())
        if items != self._last_today:  # also changes on its own when a task becomes overdue
            self._last_today = items
            self._hub.update(today=items)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._hub.update(meters=self.sample())
                self.refresh_today()
            except Exception:  # noqa: BLE001 - a hand-edited tasks.md must not kill the panel
                log.exception("HUD monitor failed")
            self._stop.wait(self._interval_s)
