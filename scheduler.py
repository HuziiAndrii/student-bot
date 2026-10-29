"""
scheduler.py — фонові задачі на APScheduler (AsyncIOScheduler).

  1. morning_brief_job — щодня о MORNING_BRIEF_TIME (за замовчуванням 07:30): розклад на сьогодні,
     погода, маршрут. Використовує ту саму бронь 'brief', що й notifier_loop (бриф «у момент підйому»):
     хто вже отримав бриф, вдруге не отримає, а ті, кого notifier_loop не обслуговує
     (немає часу на збори / маршруту), отримають його о 7:30.
  2. lesson_end_job — щохвилини: якщо пара щойно закінчилась, надсилає «Пара закінчилась! Наступна
     пара у …» і нагадує про домашнє завдання (задачі користувача, що стосуються наступної пари).

Дублі виключені через notification_log (claim_notification), навіть після перезапуску бота.
Логіка збору повідомлень — синхронна (collect_*), тож тестується без aiogram і без scheduler.
"""

import asyncio
import logging
import os
import re
from datetime import date, datetime, time, timedelta
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from briefing import build_day_plan, format_morning_brief, get_week_type, lesson_start_dt
from db import (DB_PATH, claim_notification, get_all_users, get_schedule_for_day, get_tasks,
                release_notification)
from parsers import display_room, parse_room
from routing import TZ, local_now
from weather import weather_line

logger = logging.getLogger(__name__)

DEFAULT_BRIEF_TIME = "07:30"
LESSON_END_WINDOW_MIN = 3      # скільки хвилин після кінця пари ще вважаємо «щойно закінчилась»
MAX_HOMEWORK_ITEMS = 3

Notification = tuple[int, str, str, str]   # (user_id, kind, day_key, text)


# ───────────────────────── допоміжне ─────────────────────────

def lesson_end_dt(day: date, time_range: str) -> datetime | None:
    """'08:30-09:50' → datetime кінця пари (09:50)."""
    found = re.findall(r"(\d{1,2})[:.](\d{2})", time_range or "")
    if len(found) < 2:
        return None
    return datetime.combine(day, time(int(found[1][0]), int(found[1][1])))


def _stem(word: str) -> str:
    """Грубий корінь слова, щоб «фізика» знаходилась у «зроби лабу з фізики»."""
    word = word.casefold()
    return word[:5] if len(word) >= 5 else word


def _mentions_subject(text: str, subject: str) -> bool:
    text = text.casefold()
    stems = [_stem(w) for w in re.findall(r"[A-Za-zА-Яа-яІіЇїЄєҐґ']{4,}", subject)]
    return any(s in text for s in stems)


def _room_text(room: str | None) -> str:
    building, number = parse_room(room)
    if building and number:
        return f"корпус {building}, ауд. {number}"
    return display_room(room) or "аудиторія не вказана"


def _homework_for(user_id: int, next_lesson: dict | None, next_start: datetime | None,
                  db_path: str) -> list[str]:
    """Активні задачі, що стосуються наступної пари: згадують її предмет або мають дедлайн до її початку."""
    tasks = get_tasks(user_id, "pending", db_path)
    picked = []
    for t in tasks:
        by_subject = bool(next_lesson) and _mentions_subject(t["text"], next_lesson["subject"])
        by_deadline = bool(next_start and t.get("deadline")
                           and t["deadline"] <= next_start.strftime("%Y-%m-%d %H:%M"))
        if by_subject or by_deadline:
            picked.append(t["text"])
    return picked[:MAX_HOMEWORK_ITEMS]


# ───────────────────────── задача 1: ранковий бриф ─────────────────────────

def collect_morning_briefs(now: datetime, db_path: str = DB_PATH) -> list[Notification]:
    """Бриф на сьогодні (розклад + погода + маршрут) для тих, у кого сьогодні є пари."""
    due: list[Notification] = []
    day = now.date()
    day_key = day.isoformat()
    weather = weather_line(day)
    for user in get_all_users(db_path):
        if not user.get("group_name"):
            continue
        uid = user["telegram_id"]
        try:
            dp = build_day_plan(user, day, db_path, now)
            if not dp.lessons:                       # вихідний або розкладу немає — не турбуємо
                continue
            if claim_notification(uid, "brief", day_key, db_path):
                due.append((uid, "brief", day_key, format_morning_brief(user, dp, weather=weather)))
        except Exception:  # noqa: BLE001 — один збійний користувач не має ламати решту
            logger.exception("Не вдалося скласти бриф для %s", uid)
    return due


# ───────────────────────── задача 2: кінець пари ─────────────────────────

def collect_lesson_end_notifications(now: datetime, db_path: str = DB_PATH) -> list[Notification]:
    due: list[Notification] = []
    day = now.date()
    day_key = day.isoformat()
    week_type = get_week_type(day, db_path)
    schedule_cache: dict[str, list[dict]] = {}

    for user in get_all_users(db_path):
        group = user.get("group_name")
        if not group:
            continue
        if group not in schedule_cache:
            schedule_cache[group] = get_schedule_for_day(group, day.isoweekday(), week_type, db_path)
        lessons = schedule_cache[group]
        uid = user["telegram_id"]

        for i, lesson in enumerate(lessons):
            end = lesson_end_dt(day, lesson["time"])
            if not end or not (end <= now < end + timedelta(minutes=LESSON_END_WINDOW_MIN)):
                continue
            kind = f"end{lesson['lesson_num']}"
            if not claim_notification(uid, kind, day_key, db_path):
                continue
            nxt = lessons[i + 1] if i + 1 < len(lessons) else None
            due.append((uid, kind, day_key, _format_lesson_end(user, lesson, nxt, day, now, db_path)))
    return due


def _format_lesson_end(user: dict, ended: dict, nxt: dict | None, day: date, now: datetime,
                       db_path: str) -> str:
    lines = [f"🔔 <b>Пара закінчилась!</b> ({escape(ended['subject'])})"]
    next_start = None
    if nxt:
        next_start = lesson_start_dt(day, nxt["time"])
        when = ""
        if next_start:
            gap = max(0, round((next_start - now).total_seconds() / 60))
            when = f" о <b>{next_start:%H:%M}</b> (через {gap} хв)"
        lines.append(f"➡️ Наступна пара — {escape(nxt['subject'])}{when}, "
                     f"<b>{escape(_room_text(nxt.get('room')))}</b>.")
    else:
        lines.append("🎉 Це була остання пара на сьогодні.")

    homework = _homework_for(user["telegram_id"], nxt, next_start, db_path)
    if homework:
        lines.append("📝 Не забудь про домашнє завдання:")
        lines += [f"• {escape(h)}" for h in homework]
    return "\n".join(lines)


# ───────────────────────── доставка і запуск ─────────────────────────

async def _deliver(bot: Bot, due: list[Notification]) -> None:
    for user_id, kind, day_key, text in due:
        try:
            await bot.send_message(user_id, text)
            logger.info("Надіслано '%s' користувачу %s", kind, user_id)
        except TelegramForbiddenError:
            logger.info("Користувач %s заблокував бота — пропускаю", user_id)
        except Exception:  # noqa: BLE001
            logger.exception("Не вдалося надіслати '%s' користувачу %s", kind, user_id)
            await asyncio.to_thread(release_notification, user_id, kind, day_key)
        await asyncio.sleep(0.05)       # ліміт Telegram ≈ 30 повідомлень/с


async def morning_brief_job(bot: Bot) -> None:
    due = await asyncio.to_thread(collect_morning_briefs, local_now())
    await _deliver(bot, due)


async def lesson_end_job(bot: Bot) -> None:
    due = await asyncio.to_thread(collect_lesson_end_notifications, local_now())
    await _deliver(bot, due)


def _brief_time() -> tuple[int, int]:
    raw = os.getenv("MORNING_BRIEF_TIME", DEFAULT_BRIEF_TIME).strip()
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", raw)
    if m and int(m.group(1)) <= 23 and int(m.group(2)) <= 59:
        return int(m.group(1)), int(m.group(2))
    logger.warning("MORNING_BRIEF_TIME=%r некоректний — беру %s", raw, DEFAULT_BRIEF_TIME)
    h, mi = DEFAULT_BRIEF_TIME.split(":")
    return int(h), int(mi)


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    """Створює і запускає планувальник. Викликати з async-коду (коли вже працює event loop)."""
    tz = TZ or ZoneInfo("Europe/Kyiv")
    scheduler = AsyncIOScheduler(
        timezone=tz,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 120},
    )
    hour, minute = _brief_time()
    scheduler.add_job(morning_brief_job, CronTrigger(hour=hour, minute=minute, timezone=tz),
                      args=[bot], id="morning_brief", replace_existing=True)
    scheduler.add_job(lesson_end_job, CronTrigger(minute="*", timezone=tz),
                      args=[bot], id="lesson_end", replace_existing=True)
    scheduler.start()
    logger.info("Планувальник запущено: бриф о %02d:%02d, кінець пар — щохвилини", hour, minute)
    return scheduler
