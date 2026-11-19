"""Command line: ``jarvis [run|text|briefing|selftest|voices|calendar]``."""

from __future__ import annotations

import argparse
import logging
import sys
import time

from jarvis.config import ROOT, Config, load_config


def _setup_logging(config: Config, verbose: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    file_handler = logging.FileHandler(config.data_dir / "logs" / "jarvis.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(file_handler)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO if verbose else logging.WARNING)
    console.setFormatter(logging.Formatter("  %(levelname)s %(name)s: %(message)s"))
    root.addHandler(console)
    for noisy in ("httpx", "httpx2", "httpcore", "faster_whisper", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _require_keys(config: Config, voice: bool) -> bool:
    missing = []
    if not config.has_anthropic_credentials:
        missing.append("ANTHROPIC_API_KEY (https://console.anthropic.com/settings/keys)")
    if voice and not config.elevenlabs_api_key:
        missing.append("ELEVENLABS_API_KEY (https://elevenlabs.io/app/settings/api-keys)")
    if missing:
        print(f"Faltan claves en {ROOT / '.env'}:")
        for item in missing:
            print(f"  - {item}")
        print("Copia .env.example como .env y pega tus claves.")
    return not missing


def _cmd_run(config: Config, args: argparse.Namespace) -> int:
    if not _require_keys(config, voice=True):
        return 1
    from jarvis.app import JarvisApp
    from jarvis.audio.tts import SpeechError
    from jarvis.ui.hub import Hub

    hub = Hub()
    hud = None
    if config.ui.enabled and not args.no_ui:
        from jarvis.ui.session import HudSession

        # Opened first, so the window is on screen while the models load.
        hud = HudSession(config, hub)
    try:
        try:
            app = JarvisApp(config, hub)
        except SpeechError as exc:
            print(f"No puedo arrancar la voz: {exc}")
            return 1
        if hud is not None:
            hud.attach(app)
        app.run(briefing_mode=args.briefing_mode)
    finally:
        if hud is not None:
            hud.close()
    return 0


def _cmd_text(config: Config, args: argparse.Namespace) -> int:
    """Keyboard conversation with the same brain and tools, without audio."""
    if not _require_keys(config, voice=False):
        return 1
    from jarvis import console
    from jarvis.brain.agent import Agent
    from jarvis.brain.tools import ToolContext
    from jarvis.text import parse_yes_no

    def confirm(question: str) -> bool:
        try:
            return parse_yes_no(input(f"\nJarvis: {question} [sí/no] ")) is True
        except EOFError:
            return False

    ctx = ToolContext(config=config, confirm=confirm, say=lambda text: print(f"\n(dice) {text}"))
    agent = Agent(config, ctx)
    print("Modo texto. Escribe 'salir' para terminar, o /ayuda para ver los comandos.")
    print(f"Modo de respuesta: {agent.verbosity}.\n")
    try:
        while True:
            try:
                text = input("Tú: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not text:
                continue
            if text.lower() in ("salir", "exit", "quit"):
                return 0
            if console.is_command(text):
                # Here plain lines are things said to Jarvis, so commands need the leading slash.
                print(console.handle(text, agent) + "\n")
                continue
            print("Jarvis: ", end="", flush=True)
            agent.respond(text, on_text=lambda delta: print(delta, end="", flush=True))
            print("\n")
    finally:
        from jarvis.brain.tools.memory import get_calendar

        calendar = get_calendar(config)
        if calendar is not None:
            calendar.wait_idle(10.0)  # a task created just before leaving still reaches the calendar


def _cmd_calendar(config: Config, args: argparse.Namespace) -> int:
    from jarvis import calendar_setup

    return calendar_setup.setup(config) if args.calendar_command == "setup" else calendar_setup.sync(config)


def _cmd_selftest(config: Config, args: argparse.Namespace) -> int:
    from jarvis import selftest

    if args.listen:
        return 0 if selftest.check_listening(config) else 1
    if args.wake:
        return 0 if selftest.check_wake(config) else 1
    return 0 if selftest.run(config, offline=args.offline, play=not args.no_play) else 1


def _cmd_voices(config: Config, args: argparse.Namespace) -> int:
    if not config.elevenlabs_api_key:
        print(f"Falta ELEVENLABS_API_KEY en {ROOT / '.env'}.")
        return 1
    from elevenlabs.client import ElevenLabs

    from jarvis.audio.tts import Speaker, SpeechError, is_native_spanish, spanish_voices, speaks_spanish

    if args.try_voice:
        # Audition a voice by ID with the configured model, without touching config.toml.
        config.voice.voice_id = args.try_voice
        try:
            speaker = Speaker(config)
            started = time.monotonic()
            speaker.say(f"Buenos días, {config.user.address}. Son las diez y media y tiene tres tareas pendientes.")
            speaker.say("¿Desea que le lea el resumen del día?")
            speaker.wait(timeout=60)
            speaker.close()
        except SpeechError as exc:
            print(exc)
            return 1
        print(f"Reproducida la voz {args.try_voice} ({time.monotonic() - started:.1f} s). "
              "Si te convence, pon ese ID en config.toml -> [voice] voice_id")
        return 0

    client = ElevenLabs(api_key=config.elevenlabs_api_key)
    if args.library:
        print("Voces nativas de España en la Voice Library, de más a menos usadas.")
        print("Escucha una con:  jarvis voices --try <ID>\n")
        response = client.voices.get_shared(
            language="es", locale="es-ES", page_size=40, sort="usage_character_count_1y"
        )
        for voice in response.voices:
            paid = "" if voice.free_users_allowed else "  [solo planes de pago]"
            print(f"  {voice.voice_id}  {voice.gender or '?':<7} {voice.name[:60]}{paid}")
        return 0
    try:
        voices = spanish_voices(client)
    except SpeechError as exc:
        print(exc)
        return 1
    print("Voces de tu cuenta. [ES] = nativa en español; [es] = voz de otro idioma que habla español con acento.\n")
    for voice in voices:
        labels = voice.labels or {}
        tag = "ES" if is_native_spanish(voice) else ("es" if speaks_spanish(voice) else "  ")
        details = ", ".join(v for v in (labels.get("gender"), labels.get("accent"), labels.get("language")) if v)
        print(f"  [{tag}] {voice.voice_id}  {voice.name[:44]:<44} {details}")
    print("\nVoces nativas de España:  jarvis voices --library")
    print("Escuchar una voz:         jarvis voices --try <ID>")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="jarvis", description="Asistente de voz J.A.R.V.I.S.")
    parser.add_argument("-v", "--verbose", action="store_true", help="muestra herramientas y consumo de tokens")
    parser.add_argument("--no-ui", action="store_true", help="sin ventana: solo voz y consola")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="asistente de voz (por defecto)")
    run.add_argument("--no-briefing", dest="briefing_mode", action="store_const", const="skip", default="auto",
                     help="no dar el resumen del día al arrancar")
    briefing = sub.add_parser("briefing", help="arranca dando el resumen del día")
    briefing.set_defaults(briefing_mode="force")
    sub.add_parser("text", help="conversación por teclado, sin audio")
    selftest = sub.add_parser("selftest", help="comprueba micrófono, cerebro, voz y oído")
    selftest.add_argument("--offline", action="store_true", help="solo el oído, con las voces de Windows (sin claves)")
    selftest.add_argument("--no-play", action="store_true", help="no reproducir el audio de prueba")
    selftest.add_argument("--listen", action="store_true", help="prueba real del micrófono: diga una frase")
    selftest.add_argument("--wake", action="store_true",
                          help="prueba real de la palabra de activación: diga «Jarvis» varias veces")
    voices = sub.add_parser("voices", help="lista las voces de ElevenLabs")
    voices.add_argument("--library", action="store_true", help="voces nativas de España en la Voice Library")
    voices.add_argument("--try", dest="try_voice", metavar="ID", help="reproduce una frase de muestra con esa voz")
    calendar = sub.add_parser("calendar", help="Google Calendar: avisos también en el móvil")
    calendar.add_argument("calendar_command", choices=["setup", "sync"],
                          help="setup: conectar tu cuenta paso a paso; sync: enviar ahora los cambios pendientes")

    args = parser.parse_args()
    if args.command is None:
        args.command, args.briefing_mode = "run", "auto"

    config = load_config()
    _setup_logging(config, args.verbose)
    handlers = {
        "run": _cmd_run, "briefing": _cmd_run, "text": _cmd_text,
        "selftest": _cmd_selftest, "voices": _cmd_voices, "calendar": _cmd_calendar,
    }
    return handlers[args.command](config, args)


if __name__ == "__main__":
    sys.exit(main())
