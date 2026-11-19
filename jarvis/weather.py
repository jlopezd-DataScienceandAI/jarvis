"""Weather from Open-Meteo (free, no API key)."""

from __future__ import annotations

import functools

import requests

_TIMEOUT = 8

# WMO weather interpretation codes, as used by Open-Meteo.
_WMO = {
    0: "despejado", 1: "mayormente despejado", 2: "parcialmente nublado", 3: "nublado",
    45: "niebla", 48: "niebla con escarcha",
    51: "llovizna débil", 53: "llovizna", 55: "llovizna intensa",
    56: "llovizna helada", 57: "llovizna helada intensa",
    61: "lluvia débil", 63: "lluvia", 65: "lluvia intensa",
    66: "lluvia helada", 67: "lluvia helada intensa",
    71: "nevada débil", 73: "nevada", 75: "nevada intensa", 77: "granos de nieve",
    80: "chubascos débiles", 81: "chubascos", 82: "chubascos fuertes",
    85: "chubascos de nieve", 86: "chubascos de nieve fuertes",
    95: "tormenta", 96: "tormenta con granizo", 99: "tormenta con granizo fuerte",
}


class WeatherError(Exception):
    pass


@functools.lru_cache(maxsize=32)
def geocode(city: str) -> tuple[float, float, str]:
    """(latitude, longitude, resolved name) for a city name."""
    try:
        response = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1, "language": "es"},
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        results = response.json().get("results") or []
    except requests.RequestException as exc:
        raise WeatherError(f"No he podido consultar el tiempo: {exc}") from exc
    if not results:
        raise WeatherError(f"No encuentro la ciudad «{city}».")
    place = results[0]
    name = ", ".join(p for p in (place.get("name"), place.get("admin1"), place.get("country")) if p)
    return place["latitude"], place["longitude"], name


def weather_report(city: str) -> str:
    """Current conditions and today's forecast, as text for the model."""
    latitude, longitude, name = geocode(city)
    try:
        response = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": latitude,
                "longitude": longitude,
                "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "timezone": "auto",
                "forecast_days": 2,
            },
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        raise WeatherError(f"No he podido consultar el tiempo: {exc}") from exc

    current, daily = data["current"], data["daily"]
    lines = [
        f"Tiempo en {name}:",
        f"Ahora: {_WMO.get(current['weather_code'], 'variable')}, {current['temperature_2m']:.0f} °C "
        f"(sensación {current['apparent_temperature']:.0f} °C), viento {current['wind_speed_10m']:.0f} km/h.",
    ]
    for index, label in enumerate(("Hoy", "Mañana")):
        if index >= len(daily["time"]):
            break
        rain = daily["precipitation_probability_max"][index]
        rain_text = f", probabilidad de lluvia {rain:.0f}%" if rain is not None else ""
        lines.append(
            f"{label}: {_WMO.get(daily['weather_code'][index], 'variable')}, "
            f"mínima {daily['temperature_2m_min'][index]:.0f} °C, "
            f"máxima {daily['temperature_2m_max'][index]:.0f} °C{rain_text}."
        )
    return "\n".join(lines)
