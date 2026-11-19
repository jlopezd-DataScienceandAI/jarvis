from __future__ import annotations

from datetime import date, datetime

import pytest

from jarvis.memory_store import MemoryStore, Task, parse_due, parse_task
from jarvis.reminders import ReminderWatcher


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory")


def test_task_line_round_trip():
    line = "- [x] Llamar al taller ⏰ 2026-11-14 09:30 🔔 ✅ 2026-11-14"
    task = parse_task(line)
    assert task == Task(
        text="Llamar al taller", due=datetime(2026, 11, 14, 9, 30), has_time=True,
        done=True, notified=True, completed=date(2026, 11, 14), raw=line,
    )
    assert task.to_line() == line


def test_plain_and_date_only_tasks():
    assert parse_task("- [ ] Comprar pan").due is None
    task = parse_task("- [ ] Renovar el seguro ⏰ 2026-12-01")
    assert task.due == datetime(2026, 12, 1) and not task.has_time


def test_non_task_lines_are_ignored():
    assert parse_task("# Tareas") is None
    assert parse_task("texto suelto") is None


def test_parse_due_accepts_iso_t_and_rejects_garbage():
    assert parse_due("2026-11-15T10:00") == (datetime(2026, 11, 15, 10, 0), True)
    assert parse_due("2026-11-15") == (datetime(2026, 11, 15), False)
    with pytest.raises(ValueError):
        parse_due("mañana a las diez")


def test_add_complete_and_overdue(store):
    store.add_task("Revisar el presupuesto del coche", datetime(2026, 11, 15, 10, 0), has_time=True)
    store.add_task("Comprar pan")
    now = datetime(2026, 11, 15, 10, 1)
    first, second = store.tasks()
    assert first.is_overdue(now) and not second.is_overdue(now)

    [match] = store.find_open("presupuesto")
    store.complete(match, today=date(2026, 11, 15))
    reloaded = store.tasks()[0]
    assert reloaded.done and reloaded.completed == date(2026, 11, 15)
    assert store.find_open("presupuesto") == []


def test_date_only_task_is_overdue_only_after_its_day():
    task = parse_task("- [ ] Renovar el seguro ⏰ 2026-12-01")
    assert not task.is_overdue(datetime(2026, 12, 1, 23, 0))
    assert task.is_overdue(datetime(2026, 12, 2, 0, 1))


def test_due_reminders_fire_once(store):
    store.add_task("Sacar la pizza", datetime(2026, 11, 15, 10, 0), has_time=True)
    store.add_task("Algo sin hora", datetime(2026, 11, 15), has_time=False)
    store.add_task("Más tarde", datetime(2026, 11, 15, 18, 0), has_time=True)

    assert store.due_reminders(datetime(2026, 11, 15, 9, 59)) == []
    [due] = store.due_reminders(datetime(2026, 11, 15, 10, 0))
    assert due.text == "Sacar la pizza"
    store.mark_notified(due)
    assert store.due_reminders(datetime(2026, 11, 15, 10, 5)) == []
    assert "🔔" in store.tasks_file.read_text(encoding="utf-8")


def test_update_survives_manual_edits_to_the_file(store):
    task = store.add_task("Sacar la pizza", datetime(2026, 11, 15, 10, 0), has_time=True)
    # The user inserts a line above in their editor: positions shift.
    lines = store.tasks_file.read_text(encoding="utf-8").splitlines()
    lines.insert(1, "- [ ] Añadida a mano")
    store.tasks_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    store.mark_notified(task)
    texts = {t.text: t for t in store.tasks()}
    assert texts["Sacar la pizza"].notified
    assert not texts["Añadida a mano"].notified


def test_update_of_deleted_task_raises(store):
    task = store.add_task("Efímera")
    store.tasks_file.write_text("# Tareas\n", encoding="utf-8")
    with pytest.raises(LookupError):
        store.complete(task)


def test_watcher_hands_each_reminder_over_once(store):
    store.add_task("Sacar la pizza", datetime(2026, 11, 15, 10, 0), has_time=True)
    seen = []
    watcher = ReminderWatcher(store, seen.append)
    now = datetime(2026, 11, 15, 10, 0)
    watcher.check(now)
    watcher.check(now)  # still unannounced (the app is busy): must not queue again
    assert [t.text for t in seen] == ["Sacar la pizza"]


def test_notes_and_search(store):
    store.add_note("La contraseña del wifi   está en el router", datetime(2026, 11, 14, 18, 5))
    store.add_note("Idea: automatizar las facturas", datetime(2026, 11, 15, 9, 0))
    text = (store.notes_dir / "2026-11-14.md").read_text(encoding="utf-8")
    assert text == "# 2026-11-14\n\n- 18:05 La contraseña del wifi está en el router\n"
    assert store.search_notes("CONTRASEÑA wifi") == ["2026-11-14 18:05 La contraseña del wifi está en el router"]
    assert store.search_notes("inexistente") == []


def test_profile(store):
    assert store.profile() == ""
    store.remember("Prefiere el café solo.")
    store.remember("Su hermana se llama Ana.")
    assert store.profile() == "# Perfil\n\n- Prefiere el café solo.\n- Su hermana se llama Ana."
