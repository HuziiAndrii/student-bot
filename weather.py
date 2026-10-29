"""
weather.py — прогноз погоди на день без API-ключа (Open-Meteo).

Місто беремо з STUDY_CITY (за замовчуванням «Львів»); координати можна задати вручну
змінними WEATHER_LAT / WEATHER_LON. Відповідь кешується на 30 хв, збій — на 5 хв,
тож бриф для сотні користувачів = один запит. Без aiogram; лише стандартна бібліотека.
Функції блокуючі — з async-коду викликай через asyncio.to_thread.
"""

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HTTP_TIMEOUT_SEC = 10
CACHE_TTL_SEC = 30 * 60
FAIL_TTL_SEC = 5 * 60
LVIV_FALLBACK = (49.8397, 24.0297)

# WMO weather code → (емодзі, опис)
_WMO = [
    ({0}, "☀️", "ясно"),
    ({1, 2}, "🌤", "малохмарно"),
    ({3}, "☁️", "хмарно"),
    ({45, 48}, "🌫", "туман"),
    ({51, 53, 55, 56, 57}, "🌦", "мряка"),
    ({61, 63, 65, 66, 67}, "🌧", "дощ"),
    ({71, 73, 75, 77}, "🌨", "сніг"),
    ({80, 81, 82}, "🌧", "злива"),
    ({85, 86}, "🌨", "снігопад"),
    ({95, 96, 99}, "⛈", "гроза"),
]
_WET_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}

_cache: dict = {"at": 0.0, "ttl": 0, "data": None}
_coords: Optional[tuple[float, float]] = None


def _get_json(url: str, params: dict) -> dict:
    full = f"{url}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(full, timeout=HTTP_TIMEOUT_SEC) as resp:
        return json.load(resp)


def _city() -> str:
    return os.getenv("STUDY_CITY", "Львів")


def _resolve_coords() -> tuple[float, float]:
    global _coords
    if _coords:
        return _coords
    lat, lon = os.getenv("WEATHER_LAT"), os.getenv("WEATHER_LON")
    if lat and lon:
        try:
            _coords = (float(lat), float(lon))
            return _coords
        except ValueError:
            logger.warning("WEATHER_LAT/WEATHER_LON — не числа, ігнорую.")
    try:
        data = _get_json(GEOCODE_URL, {"name": _city(), "count": 1,
                                       "language": "uk", "countryCode": "UA"})
        hit = (data.get("results") or [None])[0]
        if hit:
            _coords = (float(hit["latitude"]), float(hit["longitude"]))
            return _coords
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не вдалося знайти координати «%s»: %s", _city(), exc)
    if _city().casefold().startswith("львів"):
        _coords = LVIV_FALLBACK
        return _coords
    raise RuntimeError("координати міста невідомі")


def _fetch_daily() -> dict:
    """Денний прогноз {'time': [...], 'weather_code': [...], ...} (кеш 30 хв)."""
    now = time.monotonic()
    if _cache["data"] is not None and now - _cache["at"] < _cache["ttl"]:
        return _cache["data"]
    try:
        lat, lon = _resolve_coords()
        data = _get_json(FORECAST_URL, {
            "latitude": lat, "longitude": lon, "forecast_days": 3,
            "timezone": os.getenv("BOT_TZ", "Europe/Kyiv"),
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                     "precipitation_probability_max",
        })["daily"]
        _cache.update(at=now, ttl=CACHE_TTL_SEC, data=data)
        return data
    except Exception as exc:  # noqa: BLE001
        logger.warning("Погода недоступна: %s", exc)
        _cache.update(at=now, ttl=FAIL_TTL_SEC, data={})
        return {}


def weather_line(day: date) -> Optional[str]:
    """'🌧 Погода у Львові: дощ, 7…12°C, опади 80% — візьми парасольку ☂️' або None."""
    daily = _fetch_daily()
    try:
        i = daily["time"].index(day.isoformat())
        code = int(daily["weather_code"][i])
        tmin, tmax = round(daily["temperature_2m_min"][i]), round(daily["temperature_2m_max"][i])
        rain = daily["precipitation_probability_max"][i]
    except (KeyError, ValueError, TypeError, IndexError):
        return None
    emoji, desc = next(((e, d) for codes, e, d in _WMO if code in codes), ("🌡", "змінна погода"))
    temp = f"{tmin}…{tmax}°C" if tmin != tmax else f"{tmax}°C"
    line = f"{emoji} Погода ({_city()}): {desc}, {temp}"
    if rain is not None and rain >= 30:
        line += f", опади {int(rain)}%"
    if code in _WET_CODES or (rain or 0) >= 50:
        line += " — візьми парасольку ☂️"
    return line
