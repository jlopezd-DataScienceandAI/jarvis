"""State shared between the assistant and the HUD.

The assistant calls ``update`` and ``log``; every open HUD receives the change.
The hub keeps the latest value of everything, so a page that connects late (or
reloads) starts from the full current picture. With no HUD open, publishing
costs a dictionary update and nothing else.
"""

from __future__ import annotations

import itertools
import queue
import threading
from typing import Any

# What the tools are called in the activity log: (area, while running, when done).
TOOL_LABELS: dict[str, tuple[str, str, str]] = {
    "open_app": ("aplicaciones", "abriendo", "abierta"),
    "close_app": ("aplicaciones", "cerrando", "cerrada"),
    "open_url": ("navegador", "abriendo página", "página abierta"),
    "system_status": ("sistema", "midiendo", "estado leído"),
    "media_control": ("multimedia", "enviando orden", "orden enviada"),
    "set_volume": ("volumen", "ajustando", "ajustado"),
    "search_files": ("archivos", "buscando", "búsqueda terminada"),
    "read_file": ("archivos", "leyendo", "leído"),
    "move_or_rename": ("archivos", "moviendo", "movido"),
    "write_file": ("archivos", "escribiendo", "guardado"),
    "look_at_screen": ("pantalla", "capturando", "captura vista"),
    "save_note": ("notas", "guardando", "nota guardada"),
    "create_task": ("tareas", "creando", "tarea creada"),
    "list_tasks": ("tareas", "consultando", "leídas"),
    "complete_task": ("tareas", "marcando", "tarea hecha"),
    "search_notes": ("notas", "buscando", "búsqueda terminada"),
    "remember_fact": ("memoria", "guardando", "dato guardado"),
    "list_modes": ("modos", "consultando", "leídos"),
    "save_mode": ("modos", "guardando", "modo guardado"),
    "run_mode": ("modos", "activando", "modo activado"),
    "delete_mode": ("modos", "borrando", "modo borrado"),
    "get_weather": ("clima", "consultando", "actualizado"),
    "play_music": ("música", "buscando en Spotify", "sonando"),
    "music_now_playing": ("música", "consultando", "consultado"),
    "web_search": ("web", "buscando", "búsqueda terminada"),
    "web_fetch": ("web", "leyendo página", "página leída"),
}

_LOG_SIZE = 8


class Hub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
        self._ids = itertools.count(1)
        self._log: list[dict[str, Any]] = []
        self._state: dict[str, Any] = {
            "mode": "starting",
            "text": "Cargando el oído, la voz y el cerebro…",
            "hint": "",
            "services": {"mic": "loading", "brain": "loading", "voice": "loading", "calendar": "off"},
            "meters": {"cpu": 0, "mem": 0, "net": 0.0},
            "today": [],
            "log": [],
            "music": None,  # {"title", "artist", "app", "cover", "key"} while something is playing
            "usage": None,  # {"brain": {...}, "voice": {...} | None} once the assistant is up; see jarvis.usage
        }

    # ------------------------------------------------------------- publishing

    def update(self, **fields: Any) -> None:
        """Change top-level fields (mode, text, meters, today...) and tell every HUD."""
        with self._lock:
            self._state.update(fields)
            self._broadcast(fields)

    def set_service(self, name: str, status: str) -> None:
        """status: 'loading', 'active', 'error' or 'off' (not set up)."""
        with self._lock:
            services = {**self._state["services"], name: status}
            self._state["services"] = services
            self._broadcast({"services": services})

    def log(self, status: str, text: str, entry_id: int | None = None) -> int:
        """Add an activity line, or rewrite one (a running '[..]' that became '[ok]').

        status: 'ok', 'run' or 'err'. Returns the line's id.
        """
        with self._lock:
            if entry_id is not None:
                for entry in self._log:
                    if entry["id"] == entry_id:
                        entry.update(status=status, text=text)
                        break
                else:
                    entry_id = None
            if entry_id is None:
                entry_id = next(self._ids)
                self._log.append({"id": entry_id, "status": status, "text": text})
                del self._log[:-_LOG_SIZE]
            snapshot = [dict(entry) for entry in self._log]
            self._state["log"] = snapshot
            self._broadcast({"log": snapshot})
        return entry_id

    def tool_started(self, name: str) -> int:
        area, running, _done = TOOL_LABELS.get(name, (name, "en curso", "hecho"))
        return self.log("run", f"{area} · {running}")

    def tool_finished(self, name: str, entry_id: int, failed: bool) -> None:
        area, _running, done = TOOL_LABELS.get(name, (name, "en curso", "hecho"))
        self.log("err" if failed else "ok", f"{area} · {'no se pudo' if failed else done}", entry_id)

    # ------------------------------------------------------------ subscribing

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def close(self) -> None:
        """Tell every HUD the assistant is going away, and end their streams."""
        with self._lock:
            self._state["mode"] = "offline"
            self._broadcast({"mode": "offline"})
            for q in self._subscribers:
                self._offer(q, None)

    def _broadcast(self, fields: dict[str, Any]) -> None:
        for q in self._subscribers:
            self._offer(q, fields)

    @staticmethod
    def _offer(q: queue.Queue, item: Any) -> None:
        try:
            q.put_nowait(item)
        except queue.Full:
            pass  # a stalled page must never block the assistant; it resyncs on reconnect
