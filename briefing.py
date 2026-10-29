"""
briefing.py — розрахунок часу підйому/виходу, тексти брифів і вибірка сповіщень.

Не залежить від aiogram: усю логіку можна запускати й тестувати окремо.

Маршрут бот будує сам (routing.py): знаходить першу пару дня → корпус → адреса → дорога від
дому громадським транспортом, що приїжджає за ARRIVE_EARLY_MIN хв до початку пари.
Якщо користувач колись задав маршрут вручну (/route) — береться він, і тоді рахуємо «від пари назад»:
    пара о 08:30
      − ARRIVE_EARLY_MIN               → бути в аудиторії за 5 хв до початку
      − кожна ділянка маршруту з кінця → час відправлення і час, коли треба бути на зупинці
        · якщо для ділянки є рейси з розкладу — беремо ОСТАННІЙ рейс, що ще встигає
        · якщо розкладу немає — відправлення = (потрібний час прибуття − тривалість),
          а на зупинці треба бути на «запас» раніше
      = час виходу з дому
      − (збори + запас 5–10 хв)        = час підйому
"""

import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from html import escape
from typing import Optional

from db import (DB_PATH, claim_notification, get_all_users, get_group_rooms,
                count_schedule, get_route, get_schedule_for_day, get_setting,
                get_tasks_due_before,
                get_trip_cache, save_trip_cache)
from parsers import display_room, parse_room
from weather import weather_line
from routing import (RoutingError, building_address, building_label, fetch_itineraries,
                     legs_from_json, legs_to_json)

ARRIVE_EARLY_MIN = 5          # прийти до початку пари заздалегідь
LEAVE_REMINDER_LEAD_MIN = 10  # нагадування «час збиратися» — за стільки хв до виходу
DEFAULT_PREP_MINUTES = 30     # якщо у старого профілю не вказано час на збори
AUTO_STOP_WAIT_MIN = {"city": 3, "intercity": 5}   # за скільки хв бути на зупинці (авто-маршрут)
ERROR_RETRY_SEC = 15 * 60     # після невдалого запиту до карт повторюємо не раніше ніж через 15 хв
REASON_PAST = "past"          # пара вже почалась — маршрут не потрібен


# ───────────────────────── допоміжне ─────────────────────────

def calc_buffer(prep_minutes: int, has_intercity: bool) -> int:
    """
    Запас «на всякий випадок» (5–10 хв).
    Міжміський транспорт менш передбачуваний → максимальний запас; інакше росте разом зі зборами.
    """
    if has_intercity:
        return 10
    if prep_minutes >= 45:
        return 8
    if prep_minutes >= 20:
        return 7
    return 5


WEEK_ANCHOR_KEY = "odd_week_anchor"
WEEK_NAMES = {"odd": "чисельник", "even": "знаменник"}


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def get_week_anchor(db_path: str = DB_PATH) -> tuple[Optional[date], str]:
    """
    Опорна дата «чисельника» і звідки вона: 'db' — задано командою /week,
    'env' — ODD_WEEK_ANCHOR у .env, 'iso' — не задано (працює припущення за ISO-тижнем).
    """
    for source, raw in (("db", get_setting(WEEK_ANCHOR_KEY, db_path)),
                        ("env", os.getenv("ODD_WEEK_ANCHOR"))):
        if raw:
            try:
                return date.fromisoformat(raw.strip()), source
            except ValueError:
                continue
    return None, "iso"


def get_week_type(day: date, db_path: str = DB_PATH) -> str:
    """
    'odd' (чисельник) або 'even' (знаменник) для тижня, у якому лежить day.
    Пріоритет: опорна дата з БД (/week) → ODD_WEEK_ANCHOR з .env → парність ISO-тижня (припущення).
    Тижні чергуються без пропусків: рахуємо різницю в тижнях від опорного понеділка.
    """
    anchor, _ = get_week_anchor(db_path)
    if anchor:
        weeks = (_monday(day) - _monday(anchor)).days // 7
        return "odd" if weeks % 2 == 0 else "even"
    return "odd" if day.isocalendar()[1] % 2 else "even"


def week_label(day: date, db_path: str = DB_PATH) -> str:
    """'чисельник' / 'знаменник' для показу користувачу."""
    return WEEK_NAMES[get_week_type(day, db_path)]


def lesson_start_dt(day: date, time_range: str) -> Optional[datetime]:
    """'08:30-09:50' → datetime початку пари."""
    m = re.search(r"(\d{1,2})[:.](\d{2})", time_range or "")
    if not m:
        return None
    return datetime.combine(day, time(int(m.group(1)), int(m.group(2))))


def _hm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def _emoji(transport: str) -> str:
    t = transport.lower()
    if "трамв" in t:
        return "🚋"
    if "тролей" in t:
        return "🚎"
    if "потяг" in t or "електрич" in t:
        return "🚆"
    if "маршрут" in t:
        return "🚐"
    return "🚌"


# ───────────────────────── розрахунок ─────────────────────────

@dataclass
class Step:
    leg: dict
    at_stop: datetime     # коли треба бути на зупинці
    depart: datetime      # відправлення
    arrive: datetime      # прибуття
    timetable: bool       # True — час із розкладу, False — розрахунковий


@dataclass
class TripPlan:
    lesson_start: datetime
    steps: list[Step]
    leave_home: datetime
    wake_up: datetime
    prep_minutes: int
    buffer_minutes: int
    warnings: list[str] = field(default_factory=list)
    feasible: bool = True

    @property
    def arrive_at(self) -> datetime:
        """Коли користувач буде на місці (кінець останньої ділянки)."""
        return self.steps[-1].arrive

    @property
    def travel_minutes(self) -> int:
        """Скільки хвилин триває дорога: від виходу з дому до прибуття."""
        return round((self.arrive_at - self.leave_home).total_seconds() / 60)


def plan_trip(legs: list[dict], lesson_start: datetime, prep_minutes: int) -> TripPlan:
    """Розраховує підйом і вихід із дому для першої пари. Див. опис алгоритму вгорі файлу."""
    has_intercity = any(l["kind"] == "intercity" for l in legs)
    buffer = calc_buffer(prep_minutes, has_intercity)

    t = lesson_start - timedelta(minutes=ARRIVE_EARLY_MIN)   # потрібно бути на місці
    steps: list[Step] = []
    warnings: list[str] = []
    feasible = True

    for leg in reversed(legs):
        duration = timedelta(minutes=leg["duration_min"])
        wait = timedelta(minutes=leg.get("wait_min") or 0)
        departures = leg.get("departures") or []
        timetable = bool(departures) and leg["kind"] != "walk"

        if timetable:
            candidates = [datetime.combine(t.date(), time(*map(int, h.split(":"))))
                          for h in departures]
            fitting = [d for d in candidates if d + duration <= t]
            if fitting:
                depart = max(fitting)
            else:
                depart = min(candidates)
                feasible = False
                warnings.append(
                    f"Жоден рейс «{leg['from_stop']} → {leg['to_stop']}» не встигає вчасно: "
                    f"найраніший о {_hm(depart)} прибуде о {_hm(depart + duration)}."
                )
        else:
            depart = t - duration

        at_stop = depart - wait
        steps.append(Step(leg, at_stop, depart, depart + duration, timetable))
        t = at_stop

    steps.reverse()
    leave_home = t
    wake_up = leave_home - timedelta(minutes=prep_minutes + buffer)
    return TripPlan(lesson_start, steps, leave_home, wake_up, prep_minutes, buffer,
                    warnings, feasible)


def plan_itinerary(legs: list[dict], lesson_start: datetime, prep_minutes: int) -> TripPlan:
    """
    Розрахунок за маршрутом від карт: у транспортних ділянок уже є реальні час відправлення
    і прибуття (depart/arrive), піші ділянки вишиковуємо навколо них.
    """
    first_transit = next((i for i, l in enumerate(legs) if l.get("depart")), None)
    if first_transit is None:   # весь шлях пішки — рахуємо назад від початку пари
        return plan_trip(legs, lesson_start, prep_minutes)

    has_intercity = any(l["kind"] == "intercity" for l in legs)
    buffer = calc_buffer(prep_minutes, has_intercity)
    steps: list[Optional[Step]] = [None] * len(legs)
    warnings: list[str] = []

    # від першого транспорту — вперед
    prev_end: Optional[datetime] = None
    for i in range(first_transit, len(legs)):
        leg = legs[i]
        if leg.get("depart"):
            wait = timedelta(minutes=AUTO_STOP_WAIT_MIN.get(leg["kind"], 3))
            steps[i] = Step(leg, leg["depart"] - wait, leg["depart"], leg["arrive"], True)
            prev_end = leg["arrive"]
        else:
            depart = prev_end
            arrive = depart + timedelta(minutes=leg["duration_min"])
            steps[i] = Step(leg, depart, depart, arrive, False)
            prev_end = arrive
        if i > first_transit and steps[i].timetable and steps[i].depart < steps[i - 1].arrive:
            warnings.append(f"Пересадка на «{leg['from_stop']}» дуже щільна — можеш не встигнути.")

    # піші ділянки ДО першого транспорту — назад від його зупинки
    t = steps[first_transit].at_stop
    for i in range(first_transit - 1, -1, -1):
        depart = t - timedelta(minutes=legs[i]["duration_min"])
        steps[i] = Step(legs[i], depart, depart, t, False)
        t = depart

    deadline = lesson_start - timedelta(minutes=ARRIVE_EARLY_MIN)
    feasible = steps[-1].arrive <= deadline
    if not feasible:
        warnings.append(f"З цим маршрутом ти приїдеш о {_hm(steps[-1].arrive)}, "
                        f"тобто пізніше, ніж за {ARRIVE_EARLY_MIN} хв до пари.")
    return TripPlan(lesson_start, steps, t, t - timedelta(minutes=prep_minutes + buffer),
                    prep_minutes, buffer, warnings, feasible)


def auto_trip(user: dict, building: str, day: date, lesson_start: datetime, prep_minutes: int,
              now: Optional[datetime], db_path: str = DB_PATH
              ) -> tuple[Optional[TripPlan], Optional[str]]:
    """
    Будує дорогу «дім → корпус» сам: адреса корпусу — з розкладу, маршрут — від карт.
    Результат кешується на день. Повертає (план, None) або (None, причина).
    """
    origin = (user.get("home_place") or user.get("home_city") or "").strip()
    if not origin:
        return None, "не вказано, звідки ти добираєшся."
    if now and lesson_start - timedelta(minutes=ARRIVE_EARLY_MIN) <= now:
        return None, REASON_PAST

    destination = building_address(user["university"], building)
    day_key = day.isoformat()
    cache_key = hashlib.sha1(
        f"{origin}|{destination}|{lesson_start:%Y-%m-%d %H:%M}".encode()).hexdigest()[:16]

    cached = get_trip_cache(user["telegram_id"], day_key, db_path)
    if cached and cached["cache_key"] == cache_key:
        if cached["legs_json"]:
            return plan_itinerary(legs_from_json(cached["legs_json"]), lesson_start,
                                  prep_minutes), None
        if cached["age_sec"] < ERROR_RETRY_SEC:
            return None, cached["error"]

    try:
        variants = fetch_itineraries(origin, destination,
                                     lesson_start - timedelta(minutes=ARRIVE_EARLY_MIN),
                                     building_label(building))
    except RoutingError as exc:
        save_trip_cache(user["telegram_id"], day_key, cache_key, None, str(exc), db_path)
        return None, str(exc)

    # серед варіантів беремо той, що встигає і дозволяє вийти найпізніше
    plans = [(plan_itinerary(v, lesson_start, prep_minutes), v) for v in variants]
    best_plan, best_legs = max(plans, key=lambda p: (p[0].feasible, p[0].leave_home))
    save_trip_cache(user["telegram_id"], day_key, cache_key, legs_to_json(best_legs), None, db_path)
    return best_plan, None


# ───────────────────────── план дня ─────────────────────────

def get_group_buildings(group_name: str, db_path: str = DB_PATH) -> list[str]:
    """Корпуси, у яких групи є пари (за розкладом), від найчастіших до рідших."""
    counts: dict[str, int] = {}
    for room, n in get_group_rooms(group_name, db_path):
        building, _ = parse_room(room)
        if building:
            counts[building] = counts.get(building, 0) + n
    return sorted(counts, key=lambda c: (-counts[c], c))


@dataclass
class DayPlan:
    day: date
    lessons: list[dict]
    first: Optional[dict]
    room_display: str
    building: str
    route: Optional[dict]
    trip: Optional[TripPlan]
    tasks: list[dict]
    destination: str = ""             # куди їдемо: 'корпус IV, вул. Митрополита Андрея, 5'
    trip_error: Optional[str] = None  # чому маршрут не вдалося побудувати
    schedule_missing: bool = False    # розклад групи ще не завантажено (/schedule)
    week: str = ""                    # 'чисельник' / 'знаменник' для цього дня


def build_day_plan(user: dict, day: date, db_path: str = DB_PATH,
                   now: Optional[datetime] = None) -> DayPlan:
    """
    Збирає розклад, маршрут і розрахунок для користувача на конкретний день.
    Дивиться, де перша пара, і САМ будує дорогу з дому. Якщо користувач колись задав свій
    маршрут вручну (/route) — використовується він.
    """
    lessons = get_schedule_for_day(user["group_name"], day.isoweekday(),
                                   get_week_type(day, db_path), db_path)
    first = lessons[0] if lessons else None
    room_display = display_room(first.get("room")) if first else ""

    route = trip = None
    building = destination = ""
    trip_error = None
    if first:
        start = lesson_start_dt(day, first["time"])
        building, _ = parse_room(first.get("room"))
        street = building_address(user["university"], building).split(", Україна")[0]
        destination = f"{building_label(building)}, {street}" if building else street
        prep = user.get("prep_minutes") or DEFAULT_PREP_MINUTES

        route = get_route(user["telegram_id"], building or None, db_path)
        if route and route["legs"] and start:
            trip = plan_trip(route["legs"], start, prep)
        elif start:
            trip, trip_error = auto_trip(user, building, day, start, prep, now, db_path)
        else:
            trip_error = "не вдалося зрозуміти час початку пари."

    tasks = get_tasks_due_before(user["telegram_id"],
                                 datetime.combine(day, time(23, 59)), db_path)
    schedule_missing = not lessons and count_schedule(user["group_name"], db_path) == 0
    return DayPlan(day, lessons, first, room_display, building, route, trip, tasks,
                   destination, trip_error, schedule_missing, week_label(day, db_path))


# ───────────────────────── форматування ─────────────────────────

def format_deadline(deadline: str) -> str:
    try:
        return datetime.strptime(deadline, "%Y-%m-%d %H:%M").strftime("%d.%m %H:%M")
    except (TypeError, ValueError):
        return str(deadline)


def _step_line(step: Step) -> str:
    leg = step.leg
    dur = leg["duration_min"]
    if leg["kind"] == "walk":
        return (f"🚶 <b>{_hm(step.depart)}</b> — пішки: {escape(leg['from_stop'])} → "
                f"{escape(leg['to_stop'])} (~{dur} хв)")

    name = " ".join(x for x in (leg["transport"], leg.get("line")) if x)
    approx = "" if step.timetable else "~"
    line = (f"{_emoji(leg['transport'])} <b>{approx}{_hm(step.depart)}</b> — {escape(name)}: "
            f"«{escape(leg['from_stop'])}» → «{escape(leg['to_stop'])}» "
            f"(~{dur} хв, прибуття {_hm(step.arrive)})")
    if leg.get("headsign"):
        line += f", напрямок «{escape(leg['headsign'])}»"
    if step.at_stop != step.depart:
        line += f". Будь на зупинці о <b>{_hm(step.at_stop)}</b>"
    return line


def _duration_text(minutes: int) -> str:
    h, m = divmod(max(minutes, 0), 60)
    if h and m:
        return f"{h} год {m} хв"
    return f"{h} год" if h else f"{m} хв"


def _arrival_text(trip: TripPlan, room_display: str) -> str:
    """Підсумок дороги: коли будеш на місці і скільки це триває."""
    margin = round((trip.lesson_start - trip.arrive_at).total_seconds() / 60)
    room = f", далі до ауд. {escape(room_display)}" if room_display else ""
    return (f"🏫 Будеш на місці о <b>{_hm(trip.arrive_at)}</b> — за {margin} хв до початку пари{room}.\n"
            f"⏱ Загалом у дорозі: <b>{_duration_text(trip.travel_minutes)}</b>.")


def _trip_block(trip: TripPlan, dp: "DayPlan", home: str) -> list[str]:
    """Покроковий маршрут + підсумок + попередження (спільне для брифу і нагадування)."""
    where = f" → {escape(dp.destination)}" if dp.destination else ""
    lines = [f"🗺 <b>Дорога</b> ({home}{where}):",
             *[f"{i}. {_step_line(s)}" for i, s in enumerate(trip.steps, start=1)],
             "", _arrival_text(trip, dp.room_display)]
    lines += [f"⚠️ {escape(w)}" for w in trip.warnings]
    return lines


def _no_trip_text(user: dict, dp: DayPlan) -> str:
    if dp.trip_error == REASON_PAST:
        return "🗺 Пара вже почалась — маршрут не будую."
    prep = user.get("prep_minutes") or DEFAULT_PREP_MINUTES
    reason = f" ({escape(dp.trip_error.rstrip('.'))})" if dp.trip_error else ""
    return (f"🧳 Збори займуть {prep} хв (+ запас 5–10 хв).\n"
            f"🗺 Дорогу автоматично побудувати не вдалося{reason}. Спробую ще раз пізніше.")


def _lessons_block(lessons: list[dict]) -> str:
    lines = ["📋 <b>Розклад:</b>"]
    for l in lessons:
        room = display_room(l.get("room"))
        lines.append(f"• {l['lesson_num']}) {escape(l['time'])} — {escape(l['subject'])}"
                     + (f" ({escape(room)})" if room else ""))
    return "\n".join(lines)


def _tasks_block(tasks: list[dict], day: date) -> str:
    lines = ["📝 <b>Дедлайни:</b>"]
    for t in tasks:
        dl = format_deadline(t["deadline"])
        if t["deadline"][:10] < day.isoformat():
            lines.append(f"• ❗ {escape(t['text'])} — було до {dl}")
        else:
            lines.append(f"• {escape(t['text'])} — до {dl}")
    return "\n".join(lines)


def format_morning_brief(user: dict, dp: DayPlan, is_tomorrow: bool = False,
                         weather: Optional[str] = None) -> str:
    """Ранішній бриф: коли прокинутись, коли вийти, яким маршрутом їхати."""
    name = escape(user.get("name") or "студенте")
    day_word = "Завтра" if is_tomorrow else "Сьогодні"
    head = (f"🌙 <b>План на завтра, {name}</b>" if is_tomorrow
            else f"☀️ <b>Доброго ранку, {name}!</b>")
    blocks = [head]
    blocks.append(f"🗓 {day_word}: <b>{dp.week}</b> ({dp.day:%d.%m})")
    if weather:
        blocks.append(escape(weather))

    if dp.schedule_missing:
        blocks.append("📅 Розкладу твоєї групи ще немає — натисни /schedule, я спробую знайти його сам.")
    elif not dp.first:
        blocks.append(f"🎉 {day_word} пар немає.")
    else:
        room_part = f" (ауд. {escape(dp.room_display)})" if dp.room_display else ""
        first_line = (f"📚 {day_word} перша пара о <b>{escape(dp.first['time'].split('-')[0])}</b>: "
                      f"{escape(dp.first['subject'])}{room_part}")

        trip = dp.trip
        if trip:
            home = escape(user.get("home_place") or user.get("home_city") or "дім")
            blocks.append("\n".join([
                first_line,
                f"⏰ Прокидайся о <b>{_hm(trip.wake_up)}</b>",
                f"🧳 Збори: {trip.prep_minutes} хв + запас {trip.buffer_minutes} хв",
                f"🚪 Виходь з дому о <b>{_hm(trip.leave_home)}</b>",
            ]))
            blocks.append("\n".join(_trip_block(trip, dp, home)))
        else:
            blocks.append(f"{first_line}\n\n{_no_trip_text(user, dp)}")
        blocks.append(_lessons_block(dp.lessons))

    if dp.tasks:
        blocks.append(_tasks_block(dp.tasks, dp.day))
    return "\n\n".join(blocks)


def format_leave_reminder(user: dict, dp: DayPlan) -> str:
    """Нагадування «Час збиратися!» з покроковою навігацією."""
    trip = dp.trip
    assert trip is not None
    home = escape(user.get("home_place") or user.get("home_city") or "дім")
    lines = [
        f"⚠️ <b>Час збиратися!</b> Твоя пара о <b>{_hm(trip.lesson_start)}</b>"
        + (f" (ауд. {escape(dp.room_display)})" if dp.room_display else "") + ".",
        f"🚪 Виходь з дому о <b>{_hm(trip.leave_home)}</b>",
        "",
        *_trip_block(trip, dp, home),
    ]
    return "\n".join(lines)


def format_route(route: dict) -> str:
    """Короткий опис збереженого маршруту (для /myroute і підтвердження після збереження)."""
    where = f"корпус {escape(route['building_code'])}" if route["building_code"] else "усі корпуси"
    lines = [f"🗺 <b>Маршрут</b> ({where}):"]
    for i, leg in enumerate(route["legs"], start=1):
        dur = leg["duration_min"]
        if leg["kind"] == "walk":
            lines.append(f"{i}. 🚶 {escape(leg['from_stop'])} → {escape(leg['to_stop'])}, ~{dur} хв")
            continue
        name = " ".join(x for x in (leg["transport"], leg.get("line")) if x)
        extra = f", рейси: {', '.join(leg['departures'])}" if leg["departures"] else ""
        lines.append(f"{i}. {_emoji(leg['transport'])} {escape(name)}: «{escape(leg['from_stop'])}» → "
                     f"«{escape(leg['to_stop'])}», ~{dur} хв{extra}")
    return "\n".join(lines)


# ───────────────────────── планувальник сповіщень ─────────────────────────

def collect_due_notifications(now: datetime, db_path: str = DB_PATH
                              ) -> list[tuple[int, str, str, str]]:
    """
    Повертає [(user_id, kind, day_key, text)] — сповіщення, які треба надіслати зараз.
    Кожне «бронюється» в notification_log, тож не дублюється (навіть після перезапуску).

        'brief' — ранішній бриф: з моменту підйому і до виходу з дому
        'leave' — «Час збиратися!»: за LEAVE_REMINDER_LEAD_MIN хв до виходу і до початку пари
    """
    due = []
    day_key = now.date().isoformat()
    for user in get_all_users(db_path):
        if not user.get("prep_minutes"):
            continue
        dp = build_day_plan(user, now.date(), db_path, now)
        trip = dp.trip
        if not trip:
            continue
        uid = user["telegram_id"]

        if trip.wake_up <= now < trip.leave_home:
            if claim_notification(uid, "brief", day_key, db_path):
                due.append((uid, "brief", day_key,
                            format_morning_brief(user, dp, weather=weather_line(now.date()))))

        remind_from = trip.leave_home - timedelta(minutes=LEAVE_REMINDER_LEAD_MIN)
        if remind_from <= now < trip.lesson_start:
            if claim_notification(uid, "leave", day_key, db_path):
                due.append((uid, "leave", day_key, format_leave_reminder(user, dp)))
    return due
