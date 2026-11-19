"""Memory tools: notes, tasks with reminders, and durable facts about the user."""

from __future__ import annotations

import functools
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from jarvis.calendar_sync import CalendarClient, CalendarSync
from jarvis.config import Config
from jarvis.memory_store import MemoryStore, describe_task, parse_due

from .registry import ToolContext, ToolError, registry

_calendars: dict[Path, CalendarSync] = {}


@functools.lru_cache(maxsize=4)
def get_store(root: Path) -> MemoryStore:
    return MemoryStore(root)


def get_calendar(config: Config) -> CalendarSync | None:
    """The Google Calendar mirror of the task list, or None while it is not set up."""
    if not config.calendar_ready:
        return None
    if config.data_dir not in _calendars:
        _calendars[config.data_dir] = CalendarSync(
            get_store(config.memory_dir),
            config.data_dir / "calendar_sync.json",
            CalendarClient(config.calendar_url, config.calendar_key),
            reminder_minutes=config.calendar.reminder_minutes,
            event_minutes=config.calendar.event_minutes,
        )
    return _calendars[config.data_dir]


def _store(ctx: ToolContext) -> MemoryStore:
    return get_store(ctx.config.memory_dir)


def _send_to_calendar(ctx: ToolContext) -> bool:
    """Ask for the calendar to be brought up to date in the background. False if there is none."""
    calendar = get_calendar(ctx.config)
    if calendar is None:
        return False
    calendar.request()
    return True


class SaveNoteArgs(BaseModel):
    text: str = Field(description="La nota, redactada de forma que se entienda sola más adelante.")


class CreateTaskArgs(BaseModel):
    text: str = Field(description="Qué hay que hacer, sin la fecha.")
    due: str | None = Field(
        default=None,
        description="Cuándo vence, en formato 'AAAA-MM-DD HH:MM' (con hora: Jarvis avisará en ese "
        "momento) o 'AAAA-MM-DD' (solo día). Calcúlalo a partir de la fecha y hora actuales.",
    )


class ListTasksArgs(BaseModel):
    scope: Literal["pending", "today", "overdue", "all"] = Field(
        default="pending",
        description="pending: sin hacer; today: vencen hoy; overdue: atrasadas; all: incluye las hechas.",
    )


class CompleteTaskArgs(BaseModel):
    query: str = Field(description="Palabras del texto de la tarea que identifican cuál marcar como hecha.")


class SearchNotesArgs(BaseModel):
    query: str = Field(description="Palabras a buscar en las notas.")
    days: int | None = Field(default=None, ge=1, description="Limitar a los últimos N días.")


class RememberArgs(BaseModel):
    fact: str = Field(description="Dato duradero sobre el usuario, en una frase completa.")


@registry.tool("save_note", "Guarda una nota rápida en las notas de hoy.", SaveNoteArgs)
def _save_note(args: SaveNoteArgs, ctx: ToolContext) -> str:
    path = _store(ctx).add_note(args.text)
    return f"Nota guardada en {path.name}."


@registry.tool(
    "create_task",
    "Crea una tarea o recordatorio. Si lleva hora, Jarvis lo anuncia en voz alta al llegar el momento. "
    "Si Google Calendar está conectado, las tareas con fecha se envían también allí y el resultado lo indica.",
    CreateTaskArgs,
)
def _create_task(args: CreateTaskArgs, ctx: ToolContext) -> str:
    due, has_time = (None, False)
    if args.due:
        try:
            due, has_time = parse_due(args.due)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        if has_time and due < datetime.now():
            raise ToolError(f"Esa hora ya ha pasado ({due:%Y-%m-%d %H:%M}). Revisa la fecha.")
    task = _store(ctx).add_task(args.text, due, has_time)
    result = "Tarea creada: " + describe_task(task, datetime.now())
    if task.due and _send_to_calendar(ctx):
        result += (
            ". Se está enviando a Google Calendar, que avisará en el móvil a esa hora."
            if has_time
            else ". Se está enviando a Google Calendar como evento de todo el día."
        )
    return result


@registry.tool("list_tasks", "Lista las tareas y recordatorios guardados.", ListTasksArgs)
def _list_tasks(args: ListTasksArgs, ctx: ToolContext) -> str:
    now = datetime.now()
    tasks = _store(ctx).tasks()
    if args.scope == "pending":
        tasks = [t for t in tasks if not t.done]
    elif args.scope == "today":
        tasks = [t for t in tasks if not t.done and t.is_today(now)]
    elif args.scope == "overdue":
        tasks = [t for t in tasks if t.is_overdue(now)]
    if not tasks:
        return "No hay tareas."
    # Dated tasks first, soonest first; undated ones after.
    tasks.sort(key=lambda t: (t.due is None, t.due or datetime.max))
    return "\n".join(f"- {describe_task(t, now)}" for t in tasks)


@registry.tool("complete_task", "Marca una tarea como hecha.", CompleteTaskArgs)
def _complete_task(args: CompleteTaskArgs, ctx: ToolContext) -> str:
    store = _store(ctx)
    matches = store.find_open(args.query)
    if not matches:
        raise ToolError(f"No hay ninguna tarea pendiente que contenga «{args.query}».")
    if len(matches) > 1:
        options = "\n".join(f"- {describe_task(t, datetime.now())}" for t in matches)
        raise ToolError(f"Hay varias tareas que coinciden; concreta cuál:\n{options}")
    task = store.complete(matches[0])
    if task.due:
        _send_to_calendar(ctx)  # its event is removed, so the phone does not notify a finished task
    return f"Hecha: {task.text}"


@registry.tool("search_notes", "Busca en las notas guardadas.", SearchNotesArgs)
def _search_notes(args: SearchNotesArgs, ctx: ToolContext) -> str:
    results = _store(ctx).search_notes(args.query, args.days)
    return "\n".join(results) if results else "No hay notas que coincidan."


@registry.tool(
    "remember_fact",
    "Guarda un dato duradero sobre el usuario (preferencias, nombres, rutinas) para recordarlo en "
    "futuras conversaciones. No la uses para tareas ni notas puntuales.",
    RememberArgs,
)
def _remember_fact(args: RememberArgs, ctx: ToolContext) -> str:
    _store(ctx).remember(args.fact)
    return "Guardado en el perfil."
