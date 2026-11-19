"""Morning briefing: weather and tasks, turned into a short spoken summary."""

from __future__ import annotations

from datetime import date, datetime

from jarvis import state
from jarvis.config import Config
from jarvis.memory_store import MemoryStore, describe_task
from jarvis.weather import WeatherError, weather_report


def gather(config: Config, store: MemoryStore, now: datetime | None = None) -> str:
    """The facts for the briefing, as text for the model."""
    now = now or datetime.now()
    sections: list[str] = []

    if config.user.city:
        try:
            sections.append(weather_report(config.user.city))
        except WeatherError as exc:
            sections.append(f"Tiempo: no disponible ({exc})")
    else:
        sections.append("Tiempo: no hay ciudad configurada.")

    open_tasks = [t for t in store.tasks() if not t.done]
    today = [t for t in open_tasks if t.is_today(now) and not t.is_overdue(now)]
    overdue = [t for t in open_tasks if t.is_overdue(now)]
    undated = [t for t in open_tasks if t.due is None]

    def block(title: str, tasks: list) -> str:
        if not tasks:
            return f"{title}: ninguna."
        return f"{title}:\n" + "\n".join(f"- {describe_task(t, now)}" for t in tasks)

    sections.append(block("Tareas para hoy", today))
    sections.append(block("Tareas atrasadas", overdue))
    if undated:
        sections.append(f"Tareas sin fecha: {len(undated)}.")
    return "\n\n".join(sections)


_LENGTH = {
    "breve": "Que sea muy corto, unos diez segundos hablado: el tiempo en una frase y cuántas tareas hay.",
    "normal": "Que dure unos treinta segundos hablado, sin enumerar una a una más de cuatro tareas.",
    "detallado": "Que dure unos cuarenta y cinco segundos hablado, repasando las tareas una a una.",
}


def prompt(config: Config, store: MemoryStore, verbosity: str = "normal") -> str:
    return (
        "[Mensaje automático de arranque, no lo ha dicho el usuario.] Da el briefing de inicio del "
        "día con estos datos: un saludo acorde a la hora, el tiempo y las tareas de hoy y atrasadas. "
        f"{_LENGTH[verbosity]} Dalo con tu carácter, no como un parte meteorológico: un comentario "
        "sobre el tiempo, sobre las tareas atrasadas o sobre lo cargado o lo tranquilo del día. "
        "No termines con una pregunta.\n\n" + gather(config, store)
    )


def is_due_today(config: Config, today: date | None = None) -> bool:
    """True the first time this is asked on a given day (if the briefing is enabled)."""
    if not config.briefing.on_first_start:
        return False
    today = today or date.today()
    return state.load(config).get("last_briefing") != today.isoformat()


def mark_done(config: Config, today: date | None = None) -> None:
    state.update(config, last_briefing=(today or date.today()).isoformat())
