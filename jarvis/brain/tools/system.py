"""PC control: apps, processes, media keys, volume and system status."""

from __future__ import annotations

import ctypes
import difflib
import functools
import json
import os
import re
import shutil
import struct
import subprocess
import threading
import time
import webbrowser
from ctypes import wintypes
from typing import Literal

import psutil
from pydantic import BaseModel, Field

from jarvis.text import normalize

from .registry import ToolContext, ToolError, registry


class AppNotRunning(ToolError):
    """Asked to close something that is not open."""


# --------------------------------------------------------------------------- apps

_apps_lock = threading.Lock()
_apps_cache: dict[str, str] | None = None  # display name -> AppUserModelID


def installed_apps(refresh: bool = False) -> dict[str, str]:
    """Start-menu apps (Win32 and Store), as Windows itself lists them."""
    global _apps_cache
    with _apps_lock:
        if _apps_cache is None or refresh:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                 "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"],
                capture_output=True, text=True, encoding="utf-8", timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            entries = json.loads(result.stdout or "[]")
            if isinstance(entries, dict):
                entries = [entries]
            _apps_cache = {e["Name"]: e["AppID"] for e in entries if e.get("Name") and e.get("AppID")}
        return _apps_cache


def find_app(query: str, names: list[str]) -> str | None:
    """Best display-name match for a spoken app name, or None."""
    wanted = normalize(query)
    if not wanted:
        return None
    by_norm = {normalize(name): name for name in names}
    if wanted in by_norm:
        return by_norm[wanted]
    # Prefer the shortest name containing the query as a word: "chrome" should
    # give "Google Chrome", not "Chrome Remote Desktop".
    containing = [n for n in by_norm if wanted in n.split() or n.startswith(wanted)]
    if not containing and len(wanted) >= 3:
        containing = [n for n in by_norm if wanted in n]
    if containing:
        return by_norm[min(containing, key=len)]
    close = difflib.get_close_matches(wanted, list(by_norm), n=1, cutoff=0.75)
    return by_norm[close[0]] if close else None


def open_app(name: str) -> str:
    apps = installed_apps()
    match = find_app(name, list(apps))
    if not match:
        apps = installed_apps(refresh=True)  # it may have been installed since startup
        match = find_app(name, list(apps))
    if match:
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{apps[match]}"])
        return f"Abierto: {match}"
    executable = shutil.which(name)
    if executable:
        subprocess.Popen([executable])
        return f"Abierto: {name}"
    raise ToolError(f"No encuentro ninguna aplicación instalada que se llame «{name}».")


# --------------------------------------------------------------------- windows

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_version = ctypes.WinDLL("version", use_last_error=True)
_WM_CLOSE = 0x0010
_EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

_user32.EnumWindows.argtypes = [_EnumWindowsProc, wintypes.LPARAM]
_user32.IsWindow.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.GetForegroundWindow.restype = wintypes.HWND
_version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
_version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]
_version.VerQueryValueW.argtypes = [
    wintypes.LPCVOID, wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.UINT),
]


def visible_windows() -> list[tuple[int, int, str]]:
    """(hwnd, pid, title) for every visible top-level window with a title."""
    found: list[tuple[int, int, str]] = []

    def callback(hwnd, _lparam):
        if _user32.IsWindowVisible(hwnd):
            length = _user32.GetWindowTextLengthW(hwnd)
            if length:
                buffer = ctypes.create_unicode_buffer(length + 1)
                _user32.GetWindowTextW(hwnd, buffer, length + 1)
                pid = wintypes.DWORD()
                _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                found.append((hwnd, pid.value, buffer.value))
        return True

    _user32.EnumWindows(_EnumWindowsProc(callback), 0)
    return found


def foreground_window() -> tuple[int | None, str]:
    """(hwnd, title) of the window the user is working in."""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return None, ""
    length = _user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buffer, length + 1)
    return hwnd, buffer.value


@functools.lru_cache(maxsize=1024)
def file_description(path: str) -> str:
    """The friendly name Task Manager shows, e.g. msedge.exe -> 'Microsoft Edge'."""
    size = _version.GetFileVersionInfoSizeW(path, None)
    if not size:
        return ""
    data = ctypes.create_string_buffer(size)
    if not _version.GetFileVersionInfoW(path, 0, size, data):
        return ""
    pointer = wintypes.LPVOID()
    length = wintypes.UINT()
    if not _version.VerQueryValueW(data, "\\VarFileInfo\\Translation", ctypes.byref(pointer), ctypes.byref(length)):
        return ""
    if length.value < 4:
        return ""
    language, codepage = struct.unpack("HH", ctypes.string_at(pointer.value, 4))
    key = f"\\StringFileInfo\\{language:04x}{codepage:04x}\\FileDescription"
    if not _version.VerQueryValueW(data, key, ctypes.byref(pointer), ctypes.byref(length)) or not length.value:
        return ""
    # Some executables report a length that runs past the terminator ("Spotify\x004File..."):
    # keep only what precedes the first NUL.
    return ctypes.wstring_at(pointer.value, length.value).split("\x00", 1)[0].strip()


# ------------------------------------------------------------------- processes

# Never close these, whatever the model asks for.
_PROTECTED = {
    "explorer", "system", "registry", "csrss", "wininit", "winlogon", "services", "lsass",
    "svchost", "dwm", "smss", "python", "pythonw", "conhost", "windowsterminal", "cmd",
    "powershell", "pwsh", "searchhost", "startmenuexperiencehost", "shellexperiencehost",
    "applicationframehost", "textinputhost", "sihost", "fontdrvhost", "uv",
}


def process_stem(exe_name: str) -> str:
    return normalize(re.sub(r"\.exe$", "", exe_name, flags=re.IGNORECASE))


def match_processes(query: str, descriptions: dict[str, str], windowed: set[str]) -> set[str]:
    """Pick the executables a spoken app name refers to.

    ``descriptions`` maps exe name -> friendly description; ``windowed`` holds the
    exe names that currently own a visible window. Exact matches win outright, so
    "edge" never drags in "msedgewebview2" when msedge itself is running.
    """
    wanted = normalize(query)
    compact = wanted.replace(" ", "")
    if not wanted:
        return set()
    candidates = {
        name: (process_stem(name), normalize(desc))
        for name, desc in descriptions.items()
        if process_stem(name) not in _PROTECTED
    }
    exact = {n for n, (stem, desc) in candidates.items() if compact == stem.replace(" ", "") or wanted == desc}
    if exact:
        return exact
    if len(wanted) < 3:
        return set()
    partial = {n for n, (stem, desc) in candidates.items() if wanted in stem or wanted in desc}
    # Among loose matches, the app the user means is the one with a window.
    return (partial & windowed) or partial


def _running() -> dict[str, tuple[str, set[int]]]:
    """exe name -> (friendly description, pids)."""
    running: dict[str, tuple[str, set[int]]] = {}
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        name = proc.info["name"]
        if not name:
            continue
        if name not in running:
            exe = proc.info["exe"]
            running[name] = (file_description(exe) if exe else "", set())
        running[name][1].add(proc.info["pid"])
    return running


def close_app(name: str, force: bool = False) -> str:
    running = _running()
    windows = visible_windows()
    window_pids = {pid for _hwnd, pid, _title in windows}
    windowed = {exe for exe, (_desc, pids) in running.items() if pids & window_pids}
    targets = match_processes(name, {exe: desc for exe, (desc, _pids) in running.items()}, windowed)

    if not targets:
        return _close_by_window_title(name, running, windows)

    def alive() -> list[psutil.Process]:
        return [p for p in psutil.process_iter(["name"]) if p.info["name"] in targets]

    pids = set().union(*(running[exe][1] for exe in targets))
    for hwnd, pid, _title in windows:
        if pid in pids:
            _user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and alive():
        time.sleep(0.2)

    remaining = alive()
    label = ", ".join(sorted(running[exe][0] or process_stem(exe) for exe in targets))
    if not remaining:
        return f"Cerrado: {label}"
    remaining_pids = {p.pid for p in remaining}
    if any(pid in remaining_pids for _hwnd, pid, _title in visible_windows()):
        # A window survived WM_CLOSE: almost always a "save changes?" dialog.
        return (f"{label} sigue abierto con una ventana en pantalla, probablemente pidiendo "
                "guardar cambios. No lo he forzado para no perder trabajo.")
    if not force:
        return (f"{label} ha cerrado sus ventanas pero sigue en segundo plano. "
                "Llama de nuevo con force=true para terminar el proceso.")
    for proc in remaining:
        try:
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    psutil.wait_procs(remaining, timeout=3)
    if alive():
        return f"No he podido terminar {label} (permisos insuficientes)."
    return f"Cerrado: {label} (proceso en segundo plano terminado)"


def _close_by_window_title(name: str, running: dict[str, tuple[str, set[int]]], windows) -> str:
    """Fallback for apps whose process name says nothing (Store apps, launchers)."""
    wanted = normalize(name)
    exe_by_pid = {pid: exe for exe, (_desc, pids) in running.items() for pid in pids}
    closable = [
        (hwnd, pid, title) for hwnd, pid, title in windows
        if process_stem(exe_by_pid.get(pid, "")) not in _PROTECTED - {"applicationframehost"}
    ]
    matched = [hwnd for hwnd, _pid, title in closable if len(wanted) >= 3 and wanted in normalize(title)]
    if not matched:
        open_now = sorted({running[exe_by_pid[pid]][0] or exe_by_pid[pid]
                           for _hwnd, pid, _title in closable if pid in exe_by_pid})
        raise AppNotRunning(
            f"No hay ninguna aplicación abierta que coincida con «{name}». "
            f"Abiertas ahora: {', '.join(open_now) or 'ninguna'}."
        )
    for hwnd in matched:
        _user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
    time.sleep(1.5)
    if any(_user32.IsWindow(hwnd) and _user32.IsWindowVisible(hwnd) for hwnd in matched):
        return f"He pedido a {name} que se cierre, pero su ventana sigue abierta (quizá pide guardar cambios)."
    return f"Cerrado: {name}"


# ---------------------------------------------------------------------- status


def system_status() -> str:
    cores = psutil.cpu_count() or 1
    procs = list(psutil.process_iter(["name"]))
    for proc in procs:
        try:
            proc.cpu_percent(None)
        except psutil.Error:
            pass
    total_cpu = psutil.cpu_percent(interval=1.0)

    cpu_by_name: dict[str, float] = {}
    ram_by_name: dict[str, float] = {}
    for proc in procs:
        try:
            name = proc.info["name"] or "?"
            cpu_by_name[name] = cpu_by_name.get(name, 0.0) + proc.cpu_percent(None) / cores
            ram_by_name[name] = ram_by_name.get(name, 0.0) + proc.memory_info().rss
        except psutil.Error:
            continue
    cpu_by_name.pop("System Idle Process", None)

    mem = psutil.virtual_memory()
    lines = [
        f"CPU total: {total_cpu:.0f}% ({cores} núcleos lógicos)",
        f"RAM: {mem.percent:.0f}% usada ({mem.used / 2**30:.1f} de {mem.total / 2**30:.1f} GB)",
    ]
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        lines.append(f"Disco {part.device} {usage.percent:.0f}% lleno ({usage.free / 2**30:.0f} GB libres)")
    battery = psutil.sensors_battery()
    if battery is not None:
        plugged = "enchufado" if battery.power_plugged else "con batería"
        lines.append(f"Batería: {battery.percent:.0f}% ({plugged})")

    lines.append("Procesos que más CPU usan:")
    for name, cpu in sorted(cpu_by_name.items(), key=lambda kv: kv[1], reverse=True)[:5]:
        lines.append(f"  {name}: {cpu:.1f}%")
    lines.append("Procesos que más RAM usan:")
    for name, rss in sorted(ram_by_name.items(), key=lambda kv: kv[1], reverse=True)[:5]:
        lines.append(f"  {name}: {rss / 2**30:.2f} GB")
    return "\n".join(lines)


# ----------------------------------------------------------------------- media

MediaAction = Literal["play_pause", "next", "previous", "volume_up", "volume_down", "mute"]


def media_control(action: MediaAction) -> str:
    from pynput.keyboard import Controller, Key

    keys = {
        "play_pause": Key.media_play_pause,
        "next": Key.media_next,
        "previous": Key.media_previous,
        "volume_up": Key.media_volume_up,
        "volume_down": Key.media_volume_down,
        "mute": Key.media_volume_mute,
    }
    keyboard = Controller()
    keyboard.press(keys[action])
    keyboard.release(keys[action])
    return f"Hecho: {action}"


def set_volume(level: int) -> str:
    import comtypes
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

    comtypes.CoInitialize()
    try:
        speakers = AudioUtilities.GetSpeakers()
        endpoint = getattr(speakers, "EndpointVolume", None)
        if endpoint is None:  # older pycaw returns the raw IMMDevice
            interface = speakers.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
            endpoint = interface.QueryInterface(IAudioEndpointVolume)
        endpoint.SetMasterVolumeLevelScalar(level / 100, None)
        endpoint.SetMute(0, None)
    finally:
        comtypes.CoUninitialize()
    return f"Volumen al {level}%"


def open_url(url: str) -> str:
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    webbrowser.open(url)
    return f"Abierto en el navegador: {url}"


def user_folders() -> dict[str, str]:
    """Real locations of the user's shell folders (they move under OneDrive)."""
    import winreg

    wanted = {
        "Escritorio": "Desktop",
        "Documentos": "Personal",
        "Descargas": "{374DE290-123F-4565-9164-39C4925E467B}",
        "Imágenes": "My Pictures",
        "Música": "My Music",
        "Vídeos": "My Video",
    }
    folders: dict[str, str] = {}
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        for label, value_name in wanted.items():
            try:
                value, _type = winreg.QueryValueEx(key, value_name)
            except FileNotFoundError:
                continue
            folders[label] = os.path.expandvars(value)
    return folders


# ----------------------------------------------------------------- registration


class OpenAppArgs(BaseModel):
    name: str = Field(
        description="Nombre de la aplicación tal como la diría el usuario, p. ej. 'Chrome' o 'Bloc de notas'."
    )


class CloseAppArgs(BaseModel):
    name: str = Field(description="Nombre de la aplicación a cerrar.")
    force: bool = Field(
        default=False,
        description="Terminar también el proceso si sigue en segundo plano tras cerrar sus ventanas. "
        "Nunca fuerza una aplicación que tenga un diálogo abierto.",
    )


class OpenUrlArgs(BaseModel):
    url: str = Field(description="Dirección web completa.")


class NoArgs(BaseModel):
    pass


class MediaArgs(BaseModel):
    action: MediaAction = Field(description="Tecla multimedia a pulsar.")


class VolumeArgs(BaseModel):
    level: int = Field(ge=0, le=100, description="Volumen general de 0 a 100.")


@registry.tool("open_app", "Abre una aplicación instalada en el PC.", OpenAppArgs)
def _open_app(args: OpenAppArgs, ctx: ToolContext) -> str:
    return open_app(args.name)


@registry.tool(
    "close_app",
    "Cierra una aplicación abierta. Primero pide a sus ventanas que se cierren, de modo que la "
    "aplicación pueda avisar si hay cambios sin guardar.",
    CloseAppArgs,
    confirm=lambda args, ctx: f"¿Confirma que cierro {args.name}, {ctx.config.user.address}?",
)
def _close_app(args: CloseAppArgs, ctx: ToolContext) -> str:
    return close_app(args.name, args.force)


@registry.tool("open_url", "Abre una página web en el navegador predeterminado.", OpenUrlArgs)
def _open_url(args: OpenUrlArgs, ctx: ToolContext) -> str:
    return open_url(args.url)


@registry.tool(
    "system_status",
    "Estado actual del PC: uso de CPU, RAM, disco y batería, y los procesos que más consumen. "
    "Úsala para diagnosticar lentitud.",
    NoArgs,
)
def _system_status(args: NoArgs, ctx: ToolContext) -> str:
    return system_status()


@registry.tool(
    "media_control",
    "Pulsa una tecla multimedia: reproducir/pausar, siguiente, anterior, subir o bajar volumen, silenciar.",
    MediaArgs,
)
def _media_control(args: MediaArgs, ctx: ToolContext) -> str:
    return media_control(args.action)


@registry.tool("set_volume", "Fija el volumen general del sistema a un porcentaje exacto.", VolumeArgs)
def _set_volume(args: VolumeArgs, ctx: ToolContext) -> str:
    return set_volume(args.level)
