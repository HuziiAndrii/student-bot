"""
routing.py — автоматична побудова дороги «дім → корпус першої пари».

Користувач нічого не вводить: бот бере корпус з розкладу, перетворює його на адресу
і просить у Google Routes API маршрут громадським транспортом, який приїжджає ДО потрібного часу
(параметр arrivalTime). У відповіді — реальні відправлення/прибуття, пересадки і піші ділянки.

Потрібна змінна середовища GOOGLE_MAPS_API_KEY (увімкни «Routes API» у Google Cloud).
Без aiogram і без БД — легко тестується окремо. Використовує лише стандартну бібліотеку.
"""

import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
FIELD_MASK = ",".join([
    "routes.duration",
    "routes.legs.steps.travelMode",
    "routes.legs.steps.staticDuration",
    "routes.legs.steps.distanceMeters",
    "routes.legs.steps.transitDetails",
])
HTTP_TIMEOUT_SEC = 20
INTERCITY_MIN_DURATION = 35   # транспортна ділянка довша за стільки хв вважається міжміською


class RoutingError(Exception):
    """Не вдалося побудувати маршрут. Текст можна показувати користувачу."""


# ───────────────────────── часовий пояс ─────────────────────────

def _load_tz():
    name = os.getenv("BOT_TZ", "Europe/Kyiv")
    for candidate in (name, "Europe/Kiev"):
        try:
            return ZoneInfo(candidate)
        except Exception:
            continue
    logger.warning("Часовий пояс не знайдено (потрібен пакет tzdata) — беру системний.")
    return None


TZ = _load_tz()


def local_now() -> datetime:
    """Поточний локальний час (наївний datetime — так працюють усі розрахунки)."""
    return datetime.now(TZ).replace(tzinfo=None) if TZ else datetime.now()


def to_utc_iso(local_naive: datetime) -> str:
    aware = local_naive.replace(tzinfo=TZ) if TZ else local_naive.astimezone()
    return aware.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_api_time(value: Optional[str]) -> Optional[datetime]:
    """'2026-10-29T05:40:00Z' → локальний наївний datetime."""
    if not value:
        return None
    aware = datetime.fromisoformat(value.replace("Z", "+00:00"))
    local = aware.astimezone(TZ) if TZ else aware.astimezone()
    return local.replace(tzinfo=None)


# ───────────────────────── адреси корпусів ─────────────────────────

STUDY_CITY = os.getenv("STUDY_CITY", "Львів")

# Джерело: https://lpnu.ua/adresy-korpusiv
LPNU_BUILDINGS = {
    1: "вул. Старосольських, 2/4",
    2: "вул. Старосольських, 6",
    3: "пл. Святого Юра, 1",
    4: "вул. Митрополита Андрея, 5",
    5: "вул. Степана Бандери, 28а",
    6: "вул. Степана Бандери, 32",
    7: "вул. Степана Бандери, 55",
    8: "пл. Святого Юра, 2",
    9: "пл. Святого Юра, 9",
    10: "вул. Костя Устияновича, 5",
    11: "вул. Професорська, 2",
    12: "вул. Степана Бандери, 12",
    13: "вул. Степана Бандери, 12",
    14: "вул. Професорська, 1",
    15: "вул. Костя Устияновича, 1",
    16: "вул. Степана Бандери, 10",
    17: "вул. Степана Бандери, 10",
    18: "вул. Котляревського, 1",
    19: "вул. Князя Романа, 1",
    20: "вул. Князя Романа, 3а",
    21: "вул. Уласа Самчука, 14",
    22: "вул. Уласа Самчука, 14",
    23: "вул. Філарета Колесси, 2",
    24: "вул. Конюшинна, 8",
    25: "вул. Городоцька, 286",
    26: "вул. Старосольських, 8",
    27: "вул. Професорська, 1",
    28: "вул. Митрополита Андрея, 3",
    29: "вул. Коновальця, 4",
    30: "вул. Лазаренка, 42",
    31: "вул. Конюшинна, 8",
    32: "вул. Горбачевського, 18",
    33: "вул. Театральна, 11",
    34: "вул. Стуса, 2",
    35: "вул. Олеся, 25",
    36: "просп. В'ячеслава Чорновола, 57",
    37: "смт Брюховичі, вул. Сухомлинського, 16",
    38: "вул. Блажкевич, 12а",
    40: "вул. Січових Стрільців, 7",
}
LPNU_MAIN_BUILDING = "вул. Степана Бандери, 12"   # Головний корпус


def _roman_to_int(code: str) -> Optional[int]:
    """'IV' → 4, '12' → 12. None, якщо це не число."""
    if code.isdigit():
        return int(code)
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
    if not code or any(ch not in values for ch in code):
        return None
    total = 0
    for ch, nxt in zip(code, code[1:] + " "):
        v = values[ch]
        total += -v if nxt in values and values[nxt] > v else v
    return total


def _is_lpnu(university: str) -> bool:
    return bool(re.search(r"політех|лпну|нулп|lpnu", (university or "").lower()))


def building_address(university: str, building: str) -> str:
    """Адреса для геокодування. building — 'IV', '12' або '' (якщо корпус невідомий)."""
    if _is_lpnu(university):
        number = _roman_to_int(building) if building else None
        street = LPNU_BUILDINGS.get(number) if number else None
        if street:
            return f"{street}, {STUDY_CITY}, Україна"
        if not building:
            return f"{LPNU_MAIN_BUILDING}, {STUDY_CITY}, Україна"
    if building:
        return f"{university}, корпус {building}, {STUDY_CITY}"
    return f"{university}, {STUDY_CITY}"


def building_label(building: str) -> str:
    return f"корпус {building}" if building else "навчання"


# ───────────────────────── розбір відповіді Google ─────────────────────────

_VEHICLE_NAMES = {
    "BUS": "автобус", "INTERCITY_BUS": "міжміський автобус", "SHARE_TAXI": "маршрутка",
    "TRAM": "трамвай", "TROLLEYBUS": "тролейбус", "SUBWAY": "метро", "METRO_RAIL": "метро",
    "HEAVY_RAIL": "потяг", "LONG_DISTANCE_TRAIN": "потяг", "RAIL": "потяг",
    "COMMUTER_TRAIN": "електричка", "HIGH_SPEED_TRAIN": "потяг", "MONORAIL": "монорейка",
    "FERRY": "пором", "CABLE_CAR": "канатна дорога", "FUNICULAR": "фунікулер",
}
_INTERCITY_VEHICLES = {"INTERCITY_BUS", "LONG_DISTANCE_TRAIN", "HEAVY_RAIL", "RAIL",
                       "COMMUTER_TRAIN", "HIGH_SPEED_TRAIN"}


def _seconds(value: Optional[str]) -> int:
    try:
        return int(float((value or "0s").rstrip("s")))
    except ValueError:
        return 0


def _minutes(seconds: int) -> int:
    return max(1, -(-seconds // 60))   # вгору, мінімум 1 хв


def _line_label(line: dict) -> Optional[str]:
    short = (line.get("nameShort") or "").strip()
    if short:
        return f"№{short}" if re.fullmatch(r"[\w-]{1,5}", short) else short
    name = (line.get("name") or "").strip()
    return name or None


def parse_route(route: dict, dest_label: str, home_label: str = "дім") -> list[dict]:
    """
    Перетворює один маршрут Google на список ділянок:
      {'kind': 'walk'|'city'|'intercity', 'transport', 'line', 'from_stop', 'to_stop',
       'duration_min', 'depart': datetime|None, 'arrive': datetime|None, 'headsign'}
    Час відправлення/прибуття є лише в транспортних ділянок; піші — без часу.
    """
    steps = [s for leg in route.get("legs", []) for s in leg.get("steps", [])]
    legs: list[dict] = []

    for step in steps:
        seconds = _seconds(step.get("staticDuration"))
        td = step.get("transitDetails")
        if step.get("travelMode") == "TRANSIT" and td:
            stops = td.get("stopDetails", {})
            depart = from_api_time(stops.get("departureTime"))
            arrive = from_api_time(stops.get("arrivalTime"))
            if not depart or not arrive:
                raise RoutingError("У відповіді карт немає часу відправлення транспорту.")
            line = td.get("transitLine", {})
            vehicle = (line.get("vehicle") or {}).get("type", "")
            duration = max(1, round((arrive - depart).total_seconds() / 60))
            kind = ("intercity" if vehicle in _INTERCITY_VEHICLES
                    or duration >= INTERCITY_MIN_DURATION else "city")
            transport = _VEHICLE_NAMES.get(
                vehicle, ((line.get("vehicle") or {}).get("name") or {}).get("text") or "транспорт")
            label = _line_label(line)
            if label and label.lower() in transport.lower():   # «потяг» + «Потяг» → не дублюємо
                label = None
            legs.append({
                "kind": kind, "transport": transport, "line": label,
                "from_stop": (stops.get("departureStop") or {}).get("name") or "?",
                "to_stop": (stops.get("arrivalStop") or {}).get("name") or "?",
                "duration_min": duration, "depart": depart, "arrive": arrive,
                "headsign": td.get("headsign") or None,
            })
        else:
            if legs and legs[-1]["kind"] == "walk":          # склеюємо сусідні пішки
                legs[-1]["duration_min"] += _minutes(seconds)
                continue
            legs.append({
                "kind": "walk", "transport": "пішки", "line": None, "from_stop": "",
                "to_stop": "", "duration_min": _minutes(seconds), "depart": None,
                "arrive": None, "headsign": None,
            })

    if not legs:
        raise RoutingError("Карти повернули порожній маршрут.")

    # Назви для пішок беремо із сусідніх зупинок.
    for i, leg in enumerate(legs):
        if leg["kind"] != "walk":
            continue
        leg["from_stop"] = legs[i - 1]["to_stop"] if i > 0 else home_label
        leg["to_stop"] = legs[i + 1]["from_stop"] if i + 1 < len(legs) else dest_label
    return legs


def fetch_itineraries(origin: str, destination: str, arrive_by: datetime,
                      dest_label: str, home_label: str = "дім") -> list[list[dict]]:
    """
    Запитує в Google маршрути громадським транспортом, що прибувають до arrive_by
    (локальний наївний час). Повертає список варіантів (кожен — список ділянок).
    Кидає RoutingError з поясненням українською.
    """
    api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key:
        raise RoutingError("не налаштовано доступ до карт (GOOGLE_MAPS_API_KEY).")

    body = {
        "origin": {"address": origin},
        "destination": {"address": destination},
        "travelMode": "TRANSIT",
        "arrivalTime": to_utc_iso(arrive_by),
        "computeAlternativeRoutes": True,
        "languageCode": "uk",
        "regionCode": "UA",
        "units": "METRIC",
    }
    request = urllib.request.Request(
        ROUTES_URL, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "X-Goog-Api-Key": api_key,
                 "X-Goog-FieldMask": FIELD_MASK},
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SEC) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        logger.error("Routes API HTTP %s: %s", exc.code, detail)
        raise RoutingError(f"сервіс карт відповів помилкою {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.error("Routes API недоступний: %s", exc)
        raise RoutingError("сервіс карт зараз недоступний.") from exc

    routes = data.get("routes") or []
    if not routes:
        raise RoutingError("карти не знайшли транспорту з твоєї адреси до корпусу на цей час.")

    result = []
    for route in routes:
        try:
            result.append(parse_route(route, dest_label, home_label))
        except RoutingError as exc:
            logger.warning("Пропускаю варіант маршруту: %s", exc)
    if not result:
        raise RoutingError("не вдалося розібрати маршрут від карт.")
    return result


# ───────────────────────── (де)серіалізація для кешу ─────────────────────────

def legs_to_json(legs: list[dict]) -> str:
    def conv(leg):
        return {**leg,
                "depart": leg["depart"].isoformat() if leg.get("depart") else None,
                "arrive": leg["arrive"].isoformat() if leg.get("arrive") else None}
    return json.dumps([conv(l) for l in legs], ensure_ascii=False)


def legs_from_json(raw: str) -> list[dict]:
    legs = json.loads(raw)
    for leg in legs:
        leg["depart"] = datetime.fromisoformat(leg["depart"]) if leg.get("depart") else None
        leg["arrive"] = datetime.fromisoformat(leg["arrive"]) if leg.get("arrive") else None
    return legs
