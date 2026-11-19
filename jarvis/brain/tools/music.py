"""Music through the Spotify desktop app.

The Spotify app from the Microsoft Store ships with its own command-line tool,
``spotify_cli.exe``, which searches the catalogue and drives the running app
using the account already signed in there. That makes this work with no API
keys, developer registration or login of our own.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from jarvis.text import normalize

from .registry import ToolContext, ToolError, registry
from .system import NoArgs

Kind = Literal["auto", "track", "artist", "album", "playlist"]

_CLI = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "spotify_cli.exe"
_PLURAL = {"track": "tracks", "artist": "artists", "album": "albums", "playlist": "playlists"}
_KIND_ES = {"track": "canción", "artist": "artista", "album": "álbum", "playlist": "lista"}
_START_TIMEOUT_S = 30.0
_SESSION_GRACE_S = 4.0
_PLAY_CONFIRM_S = 8.0


def _run(*args: str, timeout: float = 25.0) -> tuple[int, str]:
    """Run the Spotify CLI. Returns (exit code, combined output)."""
    if not _CLI.exists():
        raise ToolError(
            "No encuentro la herramienta de Spotify (spotify_cli.exe). Hace falta la aplicación de "
            "Spotify de Microsoft Store, actualizada."
        )
    try:
        result = subprocess.run(
            [str(_CLI), *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError("Spotify no responde.") from exc
    return result.returncode, (result.stdout + result.stderr).strip()


def _json(*args: str) -> dict[str, Any]:
    code, output = _run(*args, "--format", "json")
    if code != 0:
        raise ToolError(f"Spotify ha devuelto un error: {output or 'sin detalle'}")
    try:
        data = json.loads(output or "{}")
    except json.JSONDecodeError as exc:
        raise ToolError(f"No entiendo la respuesta de Spotify: {output[:200]}") from exc
    return data if isinstance(data, dict) else {}


def _do(*args: str) -> None:
    """Run a command that acts rather than answers ('play', 'queue add'). These reply with
    plain text or nothing even when JSON is requested, so only the exit code is checked."""
    code, output = _run(*args)
    if code != 0:
        raise ToolError(f"Spotify ha devuelto un error: {output or 'sin detalle'}")


def ensure_running() -> None:
    """Make sure the Spotify app is open and signed in, starting it if needed."""
    code, output = _run("status")
    was_closed = code != 0
    if was_closed:
        _run("open")
    # A freshly opened app answers before it has restored the session ("Logged in: No" for a
    # few seconds), so both conditions are waited for. An app that was already open gets a
    # short grace only: if it is signed out, that will not fix itself.
    deadline = time.monotonic() + (_START_TIMEOUT_S if was_closed else _SESSION_GRACE_S)
    while code != 0 or "logged in: yes" not in output.lower():
        if time.monotonic() > deadline:
            if code != 0:
                raise ToolError("He abierto Spotify pero no termina de arrancar.")
            raise ToolError("Spotify está abierto pero sin sesión iniciada. Inicia sesión en la aplicación.")
        time.sleep(0.5)
        code, output = _run("status")


def _label(item: dict[str, Any]) -> str:
    """'Mr. Brightside — The Killers', or a playlist's 'Deep Focus (de Spotify)'."""
    name = item.get("name") or "(sin nombre)"
    artists = item.get("artists") or []
    if artists:
        return f"{name} — {', '.join(artists)}"
    if item.get("author"):
        return f"{name} (de {item['author']})"
    return name


def match_own_playlist(query: str, playlists: list[dict[str, Any]], exact: bool) -> dict[str, Any] | None:
    """One of the user's saved playlists by name. ``exact`` demands the whole name."""
    wanted = normalize(query)
    named = [(normalize(p.get("name") or ""), p) for p in playlists]
    for name, playlist in named:
        if name and name == wanted:
            return playlist
    if exact or len(wanted) < 3:
        return None
    containing = [(name, p) for name, p in named if wanted in name]
    return min(containing, key=lambda pair: len(pair[0]))[1] if containing else None


def choose(query: str, kind: Kind, results: dict[str, Any], own_playlists: list[dict[str, Any]]) -> tuple[
    str, dict[str, Any] | None, list[dict[str, Any]]
]:
    """Pick what to play. Returns (kind chosen, item or None, other candidates of that kind)."""
    if kind in ("auto", "playlist"):
        own = match_own_playlist(query, own_playlists, exact=kind == "auto")
        if own is not None:
            return "playlist", own, []
    if kind == "auto":
        artists = results.get("artists") or []
        tracks = results.get("tracks") or []
        # A query that is exactly an artist's name means "put on this artist".
        if artists and normalize(artists[0].get("name") or "") == normalize(query):
            return "artist", artists[0], []
        for candidate in ("track", "album", "playlist", "artist"):
            items = results.get(_PLURAL[candidate]) or []
            if items:
                return candidate, items[0], items[1:4] if candidate == "track" else []
        return "track", None, tracks
    items = results.get(_PLURAL[kind]) or []
    return kind, (items[0] if items else None), items[1:4]


def now_playing() -> str:
    ensure_running()
    current = _json("now-playing").get("currently_playing") or {}
    if not current.get("description"):
        return "Ahora mismo no hay nada cargado en Spotify."
    state = "Sonando" if current.get("is_playing") else "En pausa"
    context = f" (de: {current['context_description']})" if current.get("context_description") else ""
    return f"{state}: {current['description']}{context}"


def play_music(query: str, kind: Kind = "auto", queue: bool = False) -> str:
    query = " ".join(query.split())
    if not query:
        raise ToolError("Falta qué poner.")
    ensure_running()

    own_playlists: list[dict[str, Any]] = []
    if kind in ("auto", "playlist"):
        own_playlists = _json("library", "list", "--type", "playlist", "--limit", "200").get("items") or []
    search_args = ["search", query, "--limit", "5"]
    if kind != "auto":
        search_args += ["--type", kind]
    chosen_kind, item, others = choose(query, kind, _json(*search_args), own_playlists)
    if item is None:
        raise ToolError(f"No encuentro «{query}» en Spotify.")

    alternatives = "; ".join(_label(other) for other in others)
    hint = f" Otras coincidencias: {alternatives}." if alternatives else ""

    if queue:
        if chosen_kind != "track":
            raise ToolError("Solo puedo añadir canciones sueltas a la cola.")
        _do("queue", "add", item["uri"])
        return f"Añadida a la cola: {_label(item)}.{hint}"

    _do("play", item["uri"])
    # The CLI reports success even for content that cannot be played, so check what is really on.
    deadline = time.monotonic() + _PLAY_CONFIRM_S
    current: dict[str, Any] = {}
    while time.monotonic() < deadline:
        current = _json("now-playing").get("currently_playing") or {}
        right_track = chosen_kind != "track" or current.get("uri") == item["uri"]
        if current.get("is_playing") and right_track:
            what = current.get("description") or _label(item)
            origin = f" ({_KIND_ES[chosen_kind]}: {_label(item)})" if chosen_kind != "track" else ""
            return f"Sonando: {what}{origin}.{hint}"
        time.sleep(0.5)
    raise ToolError(
        f"He pedido a Spotify {_label(item)}, pero no ha empezado a sonar. "
        f"Estado actual: {current.get('description') or 'nada cargado'}."
    )


# ----------------------------------------------------------------- registration


class PlayMusicArgs(BaseModel):
    query: str = Field(
        description="Qué poner: título de la canción (mejor con el artista), nombre del artista, "
        "del álbum o de la lista."
    )
    kind: Kind = Field(
        default="auto",
        description="track: una canción concreta. artist: música de un artista. album: un álbum entero. "
        "playlist: una lista (primero se buscan las listas guardadas del usuario). auto: decide "
        "según lo que encuentre; úsalo solo si no está claro.",
    )
    queue: bool = Field(default=False, description="Añadir la canción a la cola en vez de ponerla ahora.")


@registry.tool(
    "play_music",
    "Pone música en la aplicación de Spotify del PC: una canción, un artista, un álbum o una lista. "
    "Abre Spotify si está cerrado. Devuelve lo que ha empezado a sonar.",
    PlayMusicArgs,
)
def _play_music(args: PlayMusicArgs, ctx: ToolContext) -> str:
    return play_music(args.query, args.kind, args.queue)


@registry.tool(
    "music_now_playing",
    "Dice qué canción está sonando o en pausa en Spotify. Para pausar, reanudar o pasar de canción "
    "usa media_control.",
    NoArgs,
)
def _music_now_playing(args: NoArgs, ctx: ToolContext) -> str:
    return now_playing()
