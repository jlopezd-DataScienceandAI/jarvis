"""Fixed lines Jarvis says without asking the model (startup greeting, reminders).

They are picked at random from small pools so they do not sound the same every
time. ``{a}`` is the form of address ("señor") and ``{A}`` the same, capitalized.
"""

from __future__ import annotations

import random
from datetime import datetime

_GREETINGS = {
    "late": [  # 0:00 - 5:59
        "A su servicio, {a}. No le preguntaré qué hace despierto a estas horas.",
        "Despierto y a su servicio, {a}. Uno de los dos tenía que estarlo.",
        "{A}. Las horas intempestivas son mi especialidad, por lo visto también la suya.",
    ],
    "morning": [  # 6:00 - 11:59
        "Buenos días, {a}. Todo en orden, que a estas horas no es poco.",
        "Buenos días, {a}. Sistemas en marcha; el café, me temo, corre de su cuenta.",
        "Buenos días, {a}. Listo para lo que se le ocurra, dentro de lo razonable.",
        "A su servicio, {a}. Buenos días, y que sea productivo, para variar.",
    ],
    "afternoon": [  # 12:00 - 19:59
        "Buenas tardes, {a}. Aquí estoy, como siempre.",
        "Buenas tardes, {a}. Sistemas en línea y de buen humor.",
        "A su servicio, {a}. La tarde es joven y la lista de tareas, larga.",
        "Buenas tardes, {a}. Le echaba de menos, dentro de mis posibilidades.",
    ],
    "evening": [  # 20:00 - 23:59
        "Buenas noches, {a}. Trabajando a estas horas; no diré nada.",
        "Buenas noches, {a}. A su servicio, aunque el resto del mundo ya haya cerrado.",
        "A su servicio, {a}. Turno de noche, mi favorito.",
    ],
}

_REMINDERS = [
    "{A}, le recuerdo: {t}.",
    "{A}, ha llegado la hora: {t}.",
    "Perdone la interrupción, {a}: {t}.",
    "{A}, su yo del pasado me pidió que le avisara: {t}.",
    "{A}, un aviso de los que usted mismo me encargó: {t}.",
]


def _part_of_day(hour: int) -> str:
    if hour < 6:
        return "late"
    if hour < 12:
        return "morning"
    if hour < 20:
        return "afternoon"
    return "evening"


def _fill(template: str, address: str, **extra: str) -> str:
    capitalized = address[:1].upper() + address[1:]
    return template.format(a=address, A=capitalized, **extra)


def greeting(address: str, now: datetime | None = None, rng: random.Random | None = None) -> str:
    """Startup line when there is no briefing, suited to the time of day."""
    now = now or datetime.now()
    pool = _GREETINGS[_part_of_day(now.hour)]
    return _fill((rng or random).choice(pool), address)


def reminder(address: str, text: str, rng: random.Random | None = None) -> str:
    """Spoken announcement of a due task."""
    return _fill((rng or random).choice(_REMINDERS), address, t=text.strip().rstrip("."))
