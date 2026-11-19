from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.brain.tools import ToolContext
from jarvis.config import Config, FilesConfig


@pytest.fixture(autouse=True)
def no_real_calendar(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never talk to the user's Google Calendar, whatever the developer's environment holds."""
    monkeypatch.delenv("JARVIS_CALENDAR_URL", raising=False)
    monkeypatch.delenv("JARVIS_CALENDAR_KEY", raising=False)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A fake user folder: the only place file tools may touch."""
    root = tmp_path / "home"
    root.mkdir()
    return root


@pytest.fixture
def config(tmp_path: Path, home: Path) -> Config:
    cfg = Config(
        memory_dir=tmp_path / "memory",
        data_dir=tmp_path / "data",
        files=FilesConfig(allowed_roots=[str(home)]),
    )
    cfg.ensure_dirs()
    return cfg


@pytest.fixture
def answers() -> list[bool]:
    """Queue of answers the fake user gives to confirmation questions."""
    return []


@pytest.fixture
def ctx(config: Config, answers: list[bool]) -> ToolContext:
    asked: list[str] = []
    said: list[str] = []

    def confirm(question: str) -> bool:
        asked.append(question)
        return answers.pop(0) if answers else False

    context = ToolContext(config=config, confirm=confirm, say=said.append)
    context.asked = asked  # type: ignore[attr-defined]
    context.said = said  # type: ignore[attr-defined]
    return context
