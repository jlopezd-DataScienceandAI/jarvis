import random
from datetime import datetime

from jarvis import phrases
from jarvis.brain.prompt import build_system_prompt


def test_greeting_suits_the_time_of_day_and_uses_the_address():
    rng = random.Random(0)
    morning = phrases.greeting("señor", datetime(2026, 11, 19, 8, 0), rng)
    evening = phrases.greeting("señor", datetime(2026, 11, 19, 22, 0), rng)
    assert "señor" in morning.lower() and "señor" in evening.lower()
    assert "Buenas noches" not in morning and "Buenos días" not in evening


def test_greetings_vary():
    seen = {phrases.greeting("señor", datetime(2026, 11, 19, 16, 0), random.Random(seed)) for seed in range(40)}
    assert len(seen) > 1


def test_reminder_includes_the_task_once_punctuated():
    line = phrases.reminder("señor", "Llamar al dentista.", random.Random(1))
    assert "Llamar al dentista" in line and line.endswith(".") and not line.endswith("..")
    assert "señor" in line.lower()


def test_every_line_starting_with_the_address_is_capitalized():
    for seed in range(40):
        line = phrases.reminder("señor", "algo", random.Random(seed))
        assert not line.startswith("señor")


def test_system_prompt_uses_the_configured_address():
    prompt = build_system_prompt("señora", {}, "")
    assert "«señora»" in prompt
