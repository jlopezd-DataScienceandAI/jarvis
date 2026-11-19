"""``jarvis calendar``: connect Google Calendar step by step, and sync on demand."""

from __future__ import annotations

import os
import secrets
import subprocess
import webbrowser
from pathlib import Path

from dotenv import set_key

from jarvis.calendar_sync import KEY_ENV, URL_ENV, CalendarError, CalendarSync
from jarvis.config import ROOT, Config

_SCRIPT_TEMPLATE = Path(__file__).with_name("calendar_script.gs")
_NEW_SCRIPT_URL = "https://script.new"
_URL_PREFIX = "https://script.google.com/macros/s/"

_STEPS = """\
Se ha abierto el editor de Google Apps Script en el navegador (si no, entra en {new_script}).
Los nombres de los botones pueden variar un poco según el idioma de tu cuenta.

  1. Borra el código que aparece y pega el que tienes en el portapapeles (Ctrl+V).
     {clipboard_note}
  2. En el panel izquierdo, junto a «Servicios», pulsa «+», elige «Google Calendar API»
     y pulsa «Añadir».
  3. Guarda con Ctrl+S.
  4. Arriba a la derecha: «Implementar» > «Nueva implementación». En la rueda dentada de
     «Seleccionar tipo» elige «Aplicación web» y pon:
         Ejecutar como:        Yo
         Quién tiene acceso:   Cualquier usuario
     Pulsa «Implementar».
  5. Google te pedirá permiso («Autorizar acceso»). Elige tu cuenta. Saldrá el aviso
     «Google no ha verificado esta aplicación»: es normal, la aplicación es tuya.
     Pulsa «Configuración avanzada» > «Ir a ... (no seguro)» > «Permitir».
  6. Copia la «URL de la aplicación web» (termina en /exec).
"""


def script_text(key: str) -> str:
    """The Apps Script code with this installation's key filled in."""
    return _SCRIPT_TEMPLATE.read_text(encoding="utf-8").replace("__JARVIS_KEY__", key)


def is_web_app_url(url: str) -> bool:
    return url.startswith(_URL_PREFIX) and url.endswith("/exec")


def _copy_to_clipboard(text: str) -> bool:
    try:
        # clip.exe reads UTF-16 with a byte-order mark, which keeps the accents intact.
        subprocess.run(["clip"], input=text.encode("utf-16"), check=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _save_secret(name: str, value: str) -> None:
    env_file = ROOT / ".env"
    env_file.touch(exist_ok=True)
    set_key(str(env_file), name, value, quote_mode="never")
    os.environ[name] = value


def _calendar(config: Config) -> CalendarSync | None:
    from jarvis.brain.tools.memory import get_calendar

    return get_calendar(config)


def sync(config: Config) -> int:
    """Send pending changes now and say what happened."""
    if not config.calendar.enabled:
        print("Google Calendar está desactivado en config.toml ([calendar] enabled = false).")
        return 1
    calendar = _calendar(config)
    if calendar is None:
        print("Google Calendar no está conectado todavía. Ejecuta: jarvis calendar setup")
        return 1
    try:
        calendar.ping()
        result = calendar.sync()
    except CalendarError as exc:
        print(f"No se ha podido sincronizar: {exc}")
        return 1
    print(f"Conexión con Google Calendar correcta. Calendario «Jarvis»: {result.describe()}.")
    for message in result.failed.values():
        print(f"  No aplicado: {message}")
    return 1 if result.failed else 0


def setup(config: Config) -> int:
    """Walk the user through installing the bridge script in their Google account."""
    print("Conectar Jarvis con Google Calendar\n")
    key = config.calendar_key
    if not key:
        key = secrets.token_urlsafe(32)
        _save_secret(KEY_ENV, key)

    script = script_text(key)
    script_file = config.data_dir / "google_calendar_script.gs"
    script_file.write_text(script, encoding="utf-8")
    copied = _copy_to_clipboard(script)
    webbrowser.open(_NEW_SCRIPT_URL)
    note = (
        f"También está guardado en {script_file}."
        if copied
        else f"No he podido copiarlo: ábrelo desde {script_file} y cópialo a mano."
    )
    print(_STEPS.format(new_script=_NEW_SCRIPT_URL, clipboard_note=note))

    while True:
        try:
            url = input("Pega aquí la URL y pulsa Intro (vacío para cancelar): ").strip().strip('"')
        except (EOFError, KeyboardInterrupt):
            url = ""
        if not url:
            print("\nCancelado. Puedes repetir «jarvis calendar setup» cuando quieras.")
            return 1
        if is_web_app_url(url):
            break
        print(f"  Esa no parece la URL de la aplicación web: debe empezar por {_URL_PREFIX} y acabar en /exec.")

    _save_secret(URL_ENV, url)
    print("\nComprobando la conexión con Google...")
    calendar = _calendar(config)
    if calendar is None:
        print("Guardado, pero Google Calendar está desactivado en config.toml ([calendar] enabled = false).")
        return 1
    try:
        calendar.ping()
        result = calendar.sync()
    except CalendarError as exc:
        print(f"La URL está guardada, pero la prueba ha fallado: {exc}")
        print("Cuando lo corrijas, comprueba con: jarvis calendar sync")
        return 1
    print(f"Conectado. En tu Google Calendar hay ahora un calendario «Jarvis» ({result.describe()}).")
    print(
        "\nEn el móvil, abre Google Calendar y comprueba en el menú lateral que el calendario «Jarvis»\n"
        "está marcado. Desde ahora, cada tarea con fecha que le pidas a Jarvis aparecerá ahí y el\n"
        "teléfono avisará a su hora. Reinicia Jarvis si lo tenías abierto."
    )
    return 0
