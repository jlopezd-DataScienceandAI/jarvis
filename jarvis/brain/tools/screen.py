"""Screen vision: capture the monitor the user is working on."""

from __future__ import annotations

import base64
import ctypes
import io
from ctypes import wintypes
from typing import Any

from pydantic import BaseModel

from .registry import ToolContext, registry
from .system import foreground_window

_MAX_EDGE = 1568  # beyond this the API downscales anyway, so extra pixels only cost tokens

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]


def capture_screen() -> tuple[bytes, str]:
    """JPEG bytes of the monitor holding the active window, plus that window's title."""
    import mss
    from PIL import Image

    hwnd, title = foreground_window()
    with mss.mss() as grabber:
        monitor = grabber.monitors[1]  # primary
        if hwnd:
            rect = wintypes.RECT()
            if _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                cx, cy = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
                for candidate in grabber.monitors[1:]:
                    if (candidate["left"] <= cx < candidate["left"] + candidate["width"]
                            and candidate["top"] <= cy < candidate["top"] + candidate["height"]):
                        monitor = candidate
                        break
        shot = grabber.grab(monitor)
        image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    image.thumbnail((_MAX_EDGE, _MAX_EDGE), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    return buffer.getvalue(), title


class LookArgs(BaseModel):
    pass


@registry.tool(
    "look_at_screen",
    "Captura la pantalla del usuario (el monitor de la ventana activa) para ver lo que tiene "
    "delante: errores, menús, correos, páginas, juegos. Úsala siempre que el usuario se refiera "
    "a algo que está viendo.",
    LookArgs,
)
def _look_at_screen(args: LookArgs, ctx: ToolContext) -> list[dict[str, Any]]:
    jpeg, title = capture_screen()
    return [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.standard_b64encode(jpeg).decode("ascii"),
            },
        },
        {"type": "text", "text": f"Captura de pantalla. Ventana activa: {title or 'desconocida'}."},
    ]
