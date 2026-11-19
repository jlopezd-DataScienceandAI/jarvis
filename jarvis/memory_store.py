"""Markdown-backed memory: daily notes, a task list and a user profile.

Everything is plain Markdown so the folder can be opened as an Obsidian vault.
Task lines look like:

    - [ ] Revisar el presupuesto del coche ⏰ 2026-11-15 10:00
    - [x] Llamar al taller ⏰ 2026-11-14 🔔 ✅ 2026-11-14

``🔔`` marks a reminder that has already been announced.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from jarvis.text import normalize

_TASK_RE = re.compile(
    r"^- \[(?P<done>[ xX])\] (?P<text>.*?)"
    r"(?: ⏰ (?P<due>\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?))?"
    r"(?P<notified> 🔔)?"
    r"(?: ✅ (?P<completed>\d{4}-\d{2}-\d{2}))?\s*$"
)


@dataclass
class Task:
    text: str
    due: datetime | None = None
    has_time: bool = False
    done: bool = False
    notified: bool = False
    completed: date | None = None
    line_no: int = -1  # index into the file's lines; -1 for a task not yet saved
    raw: str = ""  # the line as last read or written, to find it again if the file changed

    def to_line(self) -> str:
        line = f"- [{'x' if self.done else ' '}] {self.text}"
        if self.due:
            line += " ⏰ " + self.due.strftime("%Y-%m-%d %H:%M" if self.has_time else "%Y-%m-%d")
        if self.notified:
            line += " 🔔"
        if self.completed:
            line += f" ✅ {self.completed:%Y-%m-%d}"
        return line

    def is_overdue(self, now: datetime) -> bool:
        if self.done or not self.due:
            return False
        return self.due < now if self.has_time else self.due.date() < now.date()

    def is_today(self, now: datetime) -> bool:
        return bool(self.due) and self.due.date() == now.date()


def parse_task(line: str, line_no: int = -1) -> Task | None:
    match = _TASK_RE.match(line.rstrip("\n"))
    if not match:
        return None
    due_raw = match["due"]
    due = None
    has_time = False
    if due_raw:
        has_time = " " in due_raw
        due = datetime.strptime(due_raw, "%Y-%m-%d %H:%M" if has_time else "%Y-%m-%d")
    completed = date.fromisoformat(match["completed"]) if match["completed"] else None
    return Task(
        text=match["text"].strip(),
        due=due,
        has_time=has_time,
        done=match["done"].lower() == "x",
        notified=bool(match["notified"]),
        completed=completed,
        line_no=line_no,
        raw=line.rstrip("\n"),
    )


def parse_due(raw: str) -> tuple[datetime, bool]:
    """Parse 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' (a 'T' separator is tolerated)."""
    cleaned = raw.strip().replace("T", " ")
    for fmt, has_time in (("%Y-%m-%d %H:%M", True), ("%Y-%m-%d %H:%M:%S", True), ("%Y-%m-%d", False)):
        try:
            return datetime.strptime(cleaned, fmt), has_time
        except ValueError:
            continue
    raise ValueError(f"Fecha no válida: «{raw}». Usa AAAA-MM-DD o AAAA-MM-DD HH:MM.")


class MemoryStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.notes_dir = root / "notes"
        self.tasks_file = root / "tasks.md"
        self.profile_file = root / "profile.md"
        self._lock = threading.RLock()
        self.notes_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ tasks

    def _read_task_lines(self) -> list[str]:
        if not self.tasks_file.exists():
            return ["# Tareas", ""]
        return self.tasks_file.read_text(encoding="utf-8").splitlines()

    def _write_task_lines(self, lines: list[str]) -> None:
        self.tasks_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def tasks(self) -> list[Task]:
        with self._lock:
            lines = self._read_task_lines()
        return [task for i, line in enumerate(lines) if (task := parse_task(line, i))]

    def add_task(self, text: str, due: datetime | None = None, has_time: bool = False) -> Task:
        task = Task(text=" ".join(text.split()), due=due, has_time=has_time)
        with self._lock:
            lines = self._read_task_lines()
            task.line_no = len(lines)
            task.raw = task.to_line()
            lines.append(task.raw)
            self._write_task_lines(lines)
        return task

    def _replace(self, task: Task) -> None:
        """Rewrite a task's line. The file may have been edited by hand meanwhile,
        so the line is located by content rather than trusted by position."""
        with self._lock:
            lines = self._read_task_lines()
            index = task.line_no
            if not (0 <= index < len(lines) and lines[index] == task.raw):
                if task.raw not in lines:
                    raise LookupError(f"La tarea «{task.text}» ya no está en {self.tasks_file.name}.")
                index = lines.index(task.raw)
            lines[index] = task.raw = task.to_line()
            task.line_no = index
            self._write_task_lines(lines)

    def find_open(self, query: str) -> list[Task]:
        wanted = normalize(query)
        return [t for t in self.tasks() if not t.done and wanted in normalize(t.text)]

    def complete(self, task: Task, today: date | None = None) -> Task:
        task.done = True
        task.completed = today or date.today()
        self._replace(task)
        return task

    def mark_notified(self, task: Task) -> None:
        task.notified = True
        self._replace(task)

    def due_reminders(self, now: datetime) -> list[Task]:
        """Open tasks whose time has come and that have not been announced yet.

        Date-only tasks have no moment to announce; the briefing covers them.
        """
        return [
            t for t in self.tasks()
            if not t.done and not t.notified and t.due and t.has_time and t.due <= now
        ]

    # ------------------------------------------------------------------ notes

    def add_note(self, text: str, now: datetime | None = None) -> Path:
        now = now or datetime.now()
        path = self.notes_dir / f"{now:%Y-%m-%d}.md"
        with self._lock:
            new = not path.exists()
            with path.open("a", encoding="utf-8") as fh:
                if new:
                    fh.write(f"# {now:%Y-%m-%d}\n\n")
                fh.write(f"- {now:%H:%M} {' '.join(text.split())}\n")
        return path

    def search_notes(self, query: str, days: int | None = None, limit: int = 30) -> list[str]:
        tokens = normalize(query).split()
        cutoff = (date.today() - timedelta(days=days)) if days else None
        results: list[str] = []
        files = sorted(self.notes_dir.glob("*.md"), reverse=True)
        for path in files:
            try:
                day = date.fromisoformat(path.stem)
            except ValueError:
                day = None
            if cutoff and day and day < cutoff:
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.startswith("- "):
                    continue
                if all(token in normalize(line) for token in tokens):
                    results.append(f"{path.stem} {line[2:]}")
                    if len(results) >= limit:
                        return results
        return results

    # ---------------------------------------------------------------- profile

    def profile(self) -> str:
        if not self.profile_file.exists():
            return ""
        return self.profile_file.read_text(encoding="utf-8").strip()

    def remember(self, fact: str) -> None:
        with self._lock:
            new = not self.profile_file.exists()
            with self.profile_file.open("a", encoding="utf-8") as fh:
                if new:
                    fh.write("# Perfil\n\n")
                fh.write(f"- {' '.join(fact.split())}\n")


def describe_task(task: Task, now: datetime) -> str:
    """One line for the model: text, due date and whether it is late."""
    parts = [task.text]
    if task.due:
        when = task.due.strftime("%Y-%m-%d %H:%M" if task.has_time else "%Y-%m-%d")
        parts.append(f"(vence {when}{', ATRASADA' if task.is_overdue(now) else ''})")
    if task.done:
        parts.append("[hecha]")
    return " ".join(parts)
