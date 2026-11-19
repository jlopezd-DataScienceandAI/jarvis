"""Commands typed in the console while Jarvis is running."""

from __future__ import annotations

import re
from typing import Any

from jarvis import usage
from jarvis.brain import verbosity
from jarvis.brain.agent import Agent
from jarvis.text import normalize

HELP = (
    "Comandos de consola:\n"
    + "\n".join(f"  {level:<10} {verbosity.DESCRIPTIONS[level]}" for level in verbosity.LEVELS)
    + "\n  estado     muestra el modo de respuesta actual\n"
    + "  consumo    lo gastado este mes en Claude y en ElevenLabs\n"
    + "  saldo 12,50  anota el saldo que ves en la consola de Anthropic, para calcular cuánto queda\n"
    + "  ayuda      muestra esta lista"
)

_AMOUNT = re.compile(r"(\d+(?:[.,]\d+)?)")


def is_command(line: str) -> bool:
    """In text mode only lines starting with '/' are commands; the rest is said to Jarvis."""
    return line.strip().startswith("/")


def handle(line: str, agent: Agent, voice_quota: dict[str, Any] | None = None) -> str:
    """Run one console command and return what to print.

    ``voice_quota`` is the last ElevenLabs reading, if the caller has one, for the usage summary.
    """
    word = normalize(line).lstrip("/").strip()
    if not word:
        return ""
    if word in ("ayuda", "help", "?", "comandos"):
        return HELP
    if word in ("estado", "modo"):
        return f"Modo de respuesta: {agent.verbosity} ({verbosity.DESCRIPTIONS[agent.verbosity]})."
    if word in ("consumo", "gasto", "uso"):
        return usage.describe(agent.usage.summary(), voice_quota)
    if word == "saldo" or word.startswith("saldo "):
        amount = _AMOUNT.search(word)
        if amount is None:
            return usage.describe(agent.usage.summary(), voice_quota)
        agent.usage.set_balance(float(amount.group(1).replace(",", ".")))
        left = f"{agent.usage.summary()['left']:.2f}".replace(".", ",")
        return f"Saldo de Claude anotado: {left} $. A partir de ahora le iré restando lo que gaste Jarvis."
    level = verbosity.parse_level(word)
    if level is None:
        return f"No conozco el comando «{line.strip()}». Escribe «ayuda» para ver los disponibles."
    agent.set_verbosity(level)
    return f"Modo de respuesta: {level} ({verbosity.DESCRIPTIONS[level]}). Se aplica desde la próxima respuesta."
