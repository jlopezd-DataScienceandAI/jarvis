"""Work modes: named sequences of actions the user defines by talking to Jarvis."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from jarvis.text import normalize

from . import music, system
from .registry import ToolContext, ToolError, registry

StepAction = Literal["open_app", "close_app", "open_url", "set_volume", "media_control", "play_music", "say"]
_MEDIA_ACTIONS = {"play_pause", "next", "previous", "volume_up", "volume_down", "mute"}


class Step(BaseModel):
    action: StepAction = Field(description="Qué hace el paso.")
    value: str = Field(
        description="open_app/close_app: nombre de la aplicación. open_url: dirección web. "
        "set_volume: número de 0 a 100. media_control: play_pause, next, previous, volume_up, "
        "volume_down o mute. play_music: lo que debe sonar en Spotify (una lista del usuario, un "
        "artista, un álbum o una canción). say: frase que Jarvis dirá."
    )

    @model_validator(mode="after")
    def _check_value(self) -> Step:
        value = self.value.strip()
        if not value:
            raise ValueError("value no puede estar vacío")
        if self.action == "set_volume" and not (value.isdigit() and 0 <= int(value) <= 100):
            raise ValueError("set_volume necesita un número de 0 a 100")
        if self.action == "media_control" and value not in _MEDIA_ACTIONS:
            raise ValueError(f"media_control admite: {', '.join(sorted(_MEDIA_ACTIONS))}")
        self.value = value
        return self


class Mode(BaseModel):
    name: str
    description: str = ""
    steps: list[Step]


class ModeStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def all(self) -> dict[str, Mode]:
        """Modes keyed by normalized name."""
        with self._lock:
            if not self.path.exists():
                return {}
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        return {normalize(m["name"]): Mode.model_validate(m) for m in raw.get("modes", [])}

    def _write(self, modes: dict[str, Mode]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"modes": [m.model_dump() for m in modes.values()]}
        self.path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def get(self, name: str) -> Mode | None:
        return self.all().get(normalize(name))

    def save(self, mode: Mode) -> bool:
        """Create or replace a mode. Returns True if it replaced an existing one."""
        with self._lock:
            modes = self.all()
            existed = normalize(mode.name) in modes
            modes[normalize(mode.name)] = mode
            self._write(modes)
        return existed

    def delete(self, name: str) -> bool:
        with self._lock:
            modes = self.all()
            if normalize(name) not in modes:
                return False
            del modes[normalize(name)]
            self._write(modes)
        return True


def _store(ctx: ToolContext) -> ModeStore:
    return ModeStore(ctx.config.data_dir / "modes.yaml")


def run_step(step: Step, ctx: ToolContext) -> str:
    """Run one step directly. The user approved these when defining the mode."""
    if step.action == "open_app":
        return system.open_app(step.value)
    if step.action == "close_app":
        # force: tray apps (Discord, Steam) only hide on close. An app showing a
        # "save changes?" dialog is still left alone by close_app itself.
        try:
            return system.close_app(step.value, force=True)
        except system.AppNotRunning:
            return f"{step.value} no estaba abierto"
    if step.action == "open_url":
        return system.open_url(step.value)
    if step.action == "set_volume":
        return system.set_volume(int(step.value))
    if step.action == "media_control":
        return system.media_control(step.value)  # type: ignore[arg-type]
    if step.action == "play_music":
        return music.play_music(step.value)
    ctx.say(step.value)
    return f"Dicho: {step.value}"


def _describe(mode: Mode) -> str:
    steps = "; ".join(f"{s.action} {s.value}" for s in mode.steps)
    description = f" ({mode.description})" if mode.description else ""
    return f"{mode.name}{description}: {steps}"


class SaveModeArgs(BaseModel):
    name: str = Field(description="Nombre corto del modo, p. ej. 'trabajo' o 'estudio'.")
    description: str = Field(default="", description="Para qué sirve, en una frase.")
    steps: list[Step] = Field(min_length=1, description="Pasos en el orden en que se ejecutarán.")


class ModeNameArgs(BaseModel):
    name: str = Field(description="Nombre del modo.")


@registry.tool("list_modes", "Lista los modos de trabajo guardados con sus pasos.", system.NoArgs)
def _list_modes(args: system.NoArgs, ctx: ToolContext) -> str:
    modes = _store(ctx).all()
    if not modes:
        return "No hay modos guardados todavía."
    return "\n".join(f"- {_describe(m)}" for m in modes.values())


@registry.tool(
    "save_mode",
    "Crea un modo de trabajo o reemplaza uno existente con el mismo nombre. Para modificar un "
    "modo, consulta sus pasos con list_modes y guarda la lista completa actualizada.",
    SaveModeArgs,
)
def _save_mode(args: SaveModeArgs, ctx: ToolContext) -> str:
    mode = Mode(name=args.name.strip(), description=args.description.strip(), steps=args.steps)
    replaced = _store(ctx).save(mode)
    return ("Modo actualizado: " if replaced else "Modo creado: ") + _describe(mode)


@registry.tool(
    "run_mode",
    "Activa un modo de trabajo guardado, ejecutando todos sus pasos.",
    ModeNameArgs,
)
def _run_mode(args: ModeNameArgs, ctx: ToolContext) -> str:
    store = _store(ctx)
    mode = store.get(args.name)
    if mode is None:
        available = ", ".join(m.name for m in store.all().values()) or "ninguno"
        raise ToolError(f"No existe el modo «{args.name}». Modos guardados: {available}.")
    lines = [f"Modo {mode.name}:"]
    for step in mode.steps:
        try:
            lines.append(f"- {run_step(step, ctx)}")
        except Exception as exc:  # noqa: BLE001 - one failed step must not stop the rest
            lines.append(f"- FALLÓ {step.action} {step.value}: {exc}")
    return "\n".join(lines)


@registry.tool(
    "delete_mode",
    "Borra un modo de trabajo guardado.",
    ModeNameArgs,
    confirm=lambda args, ctx: f"¿Confirma que borro el modo {args.name}, {ctx.config.user.address}?",
)
def _delete_mode(args: ModeNameArgs, ctx: ToolContext) -> str:
    if not _store(ctx).delete(args.name):
        raise ToolError(f"No existe el modo «{args.name}».")
    return f"Modo {args.name} borrado."
