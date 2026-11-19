"""What Jarvis has spent: Claude tokens counted here, ElevenLabs characters asked from the account.

The two services differ in what they let a normal API key read. ElevenLabs
reports the account's characters used, the plan's limit and the reset date.
Anthropic exposes neither spending nor the remaining credit to a regular key
(only to an organization's admin key), so Claude usage is counted locally from
the ``usage`` of every response and priced with the public rates. The balance
is whatever the user last typed in from the Anthropic Console, minus what Jarvis
has spent since: an estimate that drifts if the same key is used elsewhere.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# USD per million tokens: (input, 5-minute cache write, cache read, output).
# From https://platform.claude.com/docs/en/about-claude/pricing, read on 2026-11-15.
_PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-haiku-5-5": (0.10, 0.125, 0.01, 0.50),
    "claude-sonnet-5-5": (2.00, 2.50, 0.10, 10.00),
    "claude-opus-5-5": (4.00, 5.00, 0.20, 20.00),
}
# Haiku 5.5 charges more for a prompt over 100,000 tokens.
_LONG_PROMPT_TOKENS = 100_000
_LONG_PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-haiku-5-5": (0.50, 0.625, 0.05, 2.50),
}
_USD_PER_WEB_SEARCH = 0.01  # $10 per 1,000 searches; web fetch has no charge of its own

_COUNTERS = ("input", "cache_write", "cache_read", "output", "searches", "requests")


def _count(source: Any, name: str) -> int:
    return int(getattr(source, name, 0) or 0)


def cost_usd(model: str, input_tokens: int, cache_write: int, cache_read: int, output: int,
             searches: int = 0) -> float | None:
    """Price of one response at list rates, or None for a model whose rates are not known here."""
    prices = _PRICES.get(model)
    if prices is None:
        return None
    if input_tokens + cache_write + cache_read > _LONG_PROMPT_TOKENS:
        prices = _LONG_PRICES.get(model, prices)
    tokens = (input_tokens, cache_write, cache_read, output)
    return sum(n * price for n, price in zip(tokens, prices, strict=True)) / 1_000_000 + searches * _USD_PER_WEB_SEARCH


class UsageMeter:
    """Running totals of Claude usage per calendar month, kept in data/usage.json."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ file

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except (ValueError, OSError):
            log.warning("Unreadable %s; usage totals start again from zero", self._path.name)
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("months", {})
        data.setdefault("balance", None)
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        temp.replace(self._path)

    # ----------------------------------------------------------------- write

    def record(self, usage: Any, model: str, now: datetime | None = None) -> None:
        """Add one API response's ``usage`` to the totals."""
        counts = {
            "input": _count(usage, "input_tokens"),
            "cache_write": _count(usage, "cache_creation_input_tokens"),
            "cache_read": _count(usage, "cache_read_input_tokens"),
            "output": _count(usage, "output_tokens"),
            "searches": _count(getattr(usage, "server_tool_use", None), "web_search_requests"),
            "requests": 1,
        }
        cost = cost_usd(model, counts["input"], counts["cache_write"], counts["cache_read"], counts["output"],
                        counts["searches"])
        with self._lock:
            data = self._load()
            month = data["months"].setdefault(f"{now or datetime.now():%Y-%m}", {})
            for name in _COUNTERS:
                month[name] = month.get(name, 0) + counts[name]
            if cost is None:
                month["unpriced"] = month.get("unpriced", 0) + 1
            else:
                month["usd"] = month.get("usd", 0.0) + cost
                if data["balance"]:
                    data["balance"]["spent"] = data["balance"].get("spent", 0.0) + cost
            self._save(data)

    def set_balance(self, usd: float, now: datetime | None = None) -> None:
        """The credit the Anthropic Console shows right now. Spending is subtracted from here on."""
        with self._lock:
            data = self._load()
            data["balance"] = {"usd": usd, "spent": 0.0, "set_at": f"{now or datetime.now():%Y-%m-%d %H:%M}"}
            self._save(data)

    # ------------------------------------------------------------------ read

    def summary(self, now: datetime | None = None) -> dict[str, Any]:
        """This month's Claude usage and the estimated balance, for the HUD and the console."""
        with self._lock:
            data = self._load()
        month = data["months"].get(f"{now or datetime.now():%Y-%m}", {})
        balance = data["balance"]
        return {
            "tokens": sum(month.get(name, 0) for name in ("input", "cache_write", "cache_read", "output")),
            "output_tokens": month.get("output", 0),
            "searches": month.get("searches", 0),
            "usd": round(month.get("usd", 0.0), 4),
            # Some responses came from a model with no known price: the cost shown is short of the real one.
            "unpriced": month.get("unpriced", 0) > 0,
            "credit": balance["usd"] if balance else None,
            "left": round(max(0.0, balance["usd"] - balance.get("spent", 0.0)), 4) if balance else None,
        }


class UsageReporter:
    """Publishes the usage panel: Claude from the meter, ElevenLabs from the account when it can be read."""

    def __init__(
        self,
        publish: Callable[[dict[str, Any]], None],
        meter: UsageMeter,
        voice_quota: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._publish = publish
        self._meter = meter
        self._voice_quota = voice_quota
        self._voice: dict[str, Any] | None = None
        self._fetching = threading.Lock()

    def current(self) -> dict[str, Any]:
        return {"brain": self._meter.summary(), "voice": self._voice}

    def refresh(self, voice: bool = True) -> None:
        """Publish now what is known locally and, if asked, the voice quota once the account answers."""
        self._publish(self.current())
        if voice and self._voice_quota is not None:
            threading.Thread(target=self._fetch_voice, name="usage", daemon=True).start()

    def _fetch_voice(self) -> None:
        if not self._fetching.acquire(blocking=False):
            return  # a request is already on its way
        try:
            self._voice = self._voice_quota()
        except Exception as exc:  # noqa: BLE001 - a panel must never disturb the assistant
            log.info("Voice quota not available: %s", exc)
            # Keep the last good reading; say why only if there has never been one.
            if self._voice is None or "error" in self._voice:
                self._voice = {"error": str(exc)}
        finally:
            self._fetching.release()
        self._publish(self.current())


def describe(summary: dict[str, Any], voice: dict[str, Any] | None = None) -> str:
    """The usage as a few console lines."""
    def money(value: float) -> str:
        return f"{value:.2f}".replace(".", ",") + " $"

    def number(value: int) -> str:
        return f"{value:,}".replace(",", ".")

    approx = " (incompleto: hay respuestas de un modelo sin precio conocido)" if summary["unpriced"] else ""
    lines = [f"Claude, este mes: {money(summary['usd'])}{approx} en {number(summary['tokens'])} tokens"
             + (f" y {summary['searches']} búsquedas web." if summary["searches"] else ".")]
    if summary["left"] is None:
        lines.append("  Saldo: sin indicar. Escribe «saldo 12,50» con la cifra que veas en la consola de Anthropic.")
    else:
        lines.append(f"  Saldo estimado: {money(summary['left'])} de {money(summary['credit'])}.")
    if voice and "error" not in voice:
        left = max(0, voice["limit"] - voice["used"])
        lines.append(f"ElevenLabs: {number(voice['used'])} de {number(voice['limit'])} caracteres; "
                     f"quedan {number(left)}.")
    elif voice:
        lines.append(f"ElevenLabs: no se puede leer el consumo ({voice['error']}).")
    return "\n".join(lines)
