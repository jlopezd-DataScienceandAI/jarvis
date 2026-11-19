"""Web access: Anthropic's server-side search/fetch tools, plus a weather lookup."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from jarvis.config import Config
from jarvis.weather import WeatherError, weather_report

from .registry import ToolContext, ToolError, registry


def server_tools(config: Config) -> list[dict[str, Any]]:
    """Server tools run on Anthropic's side: declared here, never executed locally."""
    if not config.brain.web_search:
        return []
    return [
        {"type": config.brain.web_search_tool, "name": "web_search", "max_uses": 6},
        {"type": config.brain.web_fetch_tool, "name": "web_fetch", "max_uses": 4},
    ]


class WeatherArgs(BaseModel):
    city: str | None = Field(
        default=None,
        description="Ciudad. Omítela para usar la ciudad configurada del usuario.",
    )


@registry.tool(
    "get_weather",
    "Tiempo actual y previsión de hoy y mañana para una ciudad.",
    WeatherArgs,
)
def _get_weather(args: WeatherArgs, ctx: ToolContext) -> str:
    city = (args.city or ctx.config.user.city).strip()
    if not city:
        raise ToolError(
            "No hay ciudad configurada. Pregunta al usuario por su ciudad, o que la ponga en "
            "config.toml ([user] city)."
        )
    try:
        return weather_report(city)
    except WeatherError as exc:
        raise ToolError(str(exc)) from exc
