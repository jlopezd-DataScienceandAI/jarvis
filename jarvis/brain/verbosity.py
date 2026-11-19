"""How much Jarvis says: 'breve', 'normal' or 'detallado'.

Shorter answers cost fewer output tokens and, above all, fewer ElevenLabs
characters. The level travels as one line in the header of each user turn, not in
the system prompt: changing it mid-conversation then leaves the cached prompt and
the already-sent history untouched.
"""

from __future__ import annotations

from jarvis.text import normalize

LEVELS = ("breve", "normal", "detallado")

_ALIASES = {
    "breve": "breve", "escueto": "breve", "corto": "breve", "conciso": "breve", "minimo": "breve",
    "normal": "normal", "medio": "normal", "estandar": "normal",
    "detallado": "detallado", "extendido": "detallado", "extenso": "detallado", "largo": "detallado",
    "completo": "detallado",
}

# The line shown to the model. 'normal' adds nothing: the system prompt already describes it.
_INSTRUCTIONS = {
    "breve": (
        "Modo de respuesta: breve. Di solo lo imprescindible, en una frase corta. Para confirmar una "
        "acción bastan unas pocas palabras. No anuncies lo que vas a hacer, no repitas la petición y "
        "no ofrezcas ayuda adicional. Tu carácter sigue ahí, pero cabe en las palabras que eliges, "
        "no en frases añadidas."
    ),
    "normal": "",
    "detallado": (
        "Modo de respuesta: detallado. Explícate con detalle, en varias frases, dando el contexto, "
        "los datos y los matices que aporten algo. Sigue siendo texto para ser oído: prosa, y los "
        "números, porcentajes y unidades escritos en palabras."
    ),
}

DESCRIPTIONS = {
    "breve": "lo imprescindible, en una frase (gasta menos)",
    "normal": "una o dos frases",
    "detallado": "explicaciones completas (gasta más)",
}


def parse_level(word: str) -> str | None:
    """'escueto', 'Extendido', '/breve'... -> canonical level, or None if it is not one."""
    return _ALIASES.get(normalize(word).lstrip("/").strip())


def instruction(level: str) -> str:
    return _INSTRUCTIONS[level]
