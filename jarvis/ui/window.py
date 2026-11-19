"""Open the HUD in its own window, without browser chrome.

Edge (always present on Windows 11) or Chrome is started in "app" mode with a
profile folder of its own. The separate profile is what makes the window a
process this program owns: it does not merge into the user's normal browser, so
closing the window ends that process and Jarvis can stop with it.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import threading
import time
import webbrowser
from ctypes import wintypes
from pathlib import Path

log = logging.getLogger(__name__)


def find_browser() -> str | None:
    candidates = []
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LocalAppData")):
        if base:
            candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
            candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    # Edge first: it is part of Windows, so the HUD does not depend on what the user installed.
    candidates.sort(key=lambda path: path.name != "msedge.exe")
    return next((str(path) for path in candidates if path.exists()), None)


_TASKBAR_ALLOWANCE = 72  # Windows can report the whole screen as usable even with a taskbar showing


def screen_size() -> tuple[int, int] | None:
    """Primary screen in the scaled units window sizes are given in, or None if unknown."""
    try:
        user32 = ctypes.windll.user32
        scale = (user32.GetDpiForSystem() or 96) / 96
        return round(user32.GetSystemMetrics(0) / scale), round(user32.GetSystemMetrics(1) / scale)
    except (AttributeError, OSError):
        return None


def fit_window(width: int, height: int, screen: tuple[int, int] | None) -> tuple[int, int, int, int]:
    """(width, height, x, y) for a window that fits on the screen, clear of the taskbar, centred."""
    if screen is None:
        return width, height, 40, 20
    usable_height = screen[1] - _TASKBAR_ALLOWANCE
    width = min(width, screen[0] - 16)
    height = min(height, usable_height)
    return width, height, max(0, (screen[0] - width) // 2), max(0, (usable_height - height) // 2)


def open_window(url: str, profile_dir: Path, width: int = 1280, height: int = 860) -> subprocess.Popen | None:
    """Show the HUD. Returns the window's process, or None if it fell back to a browser tab."""
    browser = find_browser()
    if browser is None:
        log.warning("No Edge or Chrome found; opening the HUD in the default browser")
        webbrowser.open(url)
        return None
    profile_dir.mkdir(parents=True, exist_ok=True)
    width, height, left, top = fit_window(width, height, screen_size())
    process = subprocess.Popen(
        [
            browser,
            f"--app={url}",
            f"--user-data-dir={profile_dir}",
            f"--window-size={width},{height}",
            f"--window-position={left},{top}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--disable-extensions",
            # Animations must keep running when the window is not focused, and the page
            # must stay painted while another window covers it.
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            "--disable-features=CalculateNativeWinOcclusion",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    threading.Thread(target=bring_to_front, args=(process.pid,), name="hud-raise", daemon=True).start()
    return process


def bring_to_front(pid: int, timeout_s: float = 15.0) -> bool:
    """Raise the HUD above the terminal it was started from.

    Windows often opens a window launched from a console *behind* that console,
    and refuses SetForegroundWindow from a background process. Making the window
    topmost for an instant and then normal again raises it without needing focus.
    """
    from jarvis.brain.tools.system import visible_windows

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    no_move_no_size_show = 0x0002 | 0x0001 | 0x0040
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        handles = [hwnd for hwnd, owner, _title in visible_windows() if owner == pid]
        if handles:
            for hwnd in handles:
                user32.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0, no_move_no_size_show)  # topmost
                user32.SetWindowPos(hwnd, wintypes.HWND(-2), 0, 0, 0, 0, no_move_no_size_show)  # back to normal
                user32.SetForegroundWindow(hwnd)  # granted when Jarvis was started from the active console
            return True
        time.sleep(0.25)
    return False
