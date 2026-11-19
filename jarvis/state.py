"""Small persistent state (data/state.json): things that must survive a restart
but are not configuration the user edits by hand."""

from __future__ import annotations

import json
import threading
from typing import Any

from jarvis.config import Config

_lock = threading.Lock()


def load(config: Config) -> dict[str, Any]:
    path = config.data_dir / "state.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def update(config: Config, **values: Any) -> None:
    with _lock:
        state = load(config)
        state.update(values)
        path = config.data_dir / "state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
