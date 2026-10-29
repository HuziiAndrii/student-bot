"""
tasks_view.py — команда /tasks (синонім /notes): мої завдання та нотатки.

Показує:
    📝 активні завдання — найближчі дедлайни першими, прострочені позначено ❗;
       під списком кнопки «✅ 1, ✅ 2 …» — позначити завдання виконаним;
    🗒 нотатки, які не стали завданнями — з кнопками «✅ → завдання» та «🗑».

Після натискання кнопки список оновлюється на місці. Усі зміни перевіряють user_id,
тож чужі завдання й нотатки зачепити неможливо.

Окремий роутер (як voice.py): у main.py підключається через dp.include_router(tasks_view.router).
Використовує лише db.py, routing.py (часовий пояс) та aiogram.
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from html import escape
from typing import Optional

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from db import DB_PATH, add_task, get_conn, get_tasks, get_user, set_task_status
from routing import TZ, local_now

router = Router()
logger = logging.getLogger(__name__)

MAX_TASKS = 15        # скільки завдань показувати одним повідомленням
MAX_NOTES = 10        # скільки останніх нотаток показувати
TASK_CLIP = 160       # обрізання довгих текстів
NOTE_CLIP = 140
BUTTONS_PER_ROW = 5


# ───────────────────────── дані ─────────────────────────

def fetch_pending_tasks(user_id: int, db_path: Optional[str] = None) -> list[dict]:
    """Активні завдання: найближчі дедлайни першими, без дедлайну — в кінці."""
    return get_tasks(user_id, "pending", db_path or DB_PATH)


def fetch_standalone_notes(user_id: int, limit: int = MAX_NOTES,
                           db_path: Optional[str] = None) -> tuple[list[dict], int]:
    """
    Нотатки, з яких НЕ створено завдання (інакше вони дублювали б список завдань).
    Повертає (останні `limit` нотаток, загальна кількість).
    """
    where = ("user_id = ? AND id NOT IN "
             "(SELECT note_id FROM tasks WHERE note_id IS NOT NULL)")
    with get_conn(db_path or DB_PATH) as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM notes WHERE {where}", (user_id,)).fetchone()[0]
        rows = conn.execute(
            f"SELECT id, transcript, created_at FROM notes WHERE {where} "
            "ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()
    return [dict(r) for r in rows], total


def mark_done(user_id: int, task_id: int, db_path: Optional[str] = None) -> bool:
    return set_task_status(task_id, user_id, "done", db_path or DB_PATH)


def note_to_task(user_id: int, note_id: int, db_path: Optional[str] = None) -> str:
    """Нотатка → завдання (без дедлайну). Повертає 'created' | 'exists' | 'missing'."""
    path = db_path or DB_PATH
    with get_conn(path) as conn:
        note = conn.execute("SELECT transcript FROM notes WHERE id = ? AND user_id = ?",
                            (note_id, user_id)).fetchone()
        if not note:
            return "missing"
        if conn.execute("SELECT 1 FROM tasks WHERE note_id = ? AND user_id = ?",
                        (note_id, user_id)).fetchone():
            return "exists"
    add_task(user_id, note["transcript"].strip()[:300], source="voice",
             note_id=note_id, db_path=path)
    return "created"


def delete_note(user_id: int, note_id: int, db_path: Optional[str] = None) -> bool:
    with get_conn(db_path or DB_PATH) as conn:
        cur = conn.execute("DELETE FROM notes WHERE id = ? AND user_id = ?", (note_id, user_id))
        return cur.rowcount > 0


# ───────────────────────── форматування ─────────────────────────

def _clip(text: Optional[str], limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _deadline_label(deadline: Optional[str], now: datetime) -> tuple[str, str]:
    """(емодзі, підпис) для дедлайну: прострочено / сьогодні / завтра / дата / без дедлайну."""
    if not deadline:
        return "▫️", "без дедлайну"
    try:
        dt = datetime.strptime(deadline, "%Y-%m-%d %H:%M")
    except ValueError:
        return "▫️", escape(str(deadline))
    stamp = dt.strftime("%d.%m %H:%M")
    if dt < now:
        return "❗", f"прострочено (було до {stamp})"
    days = (dt.date() - now.date()).days
    if days == 0:
        return "🔥", f"сьогодні до {dt:%H:%M}"
    if days == 1:
        return "⏳", f"завтра до {dt:%H:%M}"
    return "🗓", f"до {stamp}"


def _note_date(created_at: str) -> str:
    """SQLite зберігає UTC → показуємо локальний час користувача."""
    try:
        dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        dt = dt.astimezone(TZ) if TZ else dt.astimezone()
        return dt.strftime("%d.%m %H:%M")
    except (TypeError, ValueError):
        return ""


def render_tasks(tasks: list[dict], now: datetime) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    if not tasks:
        return ("📝 <b>Мої завдання</b>\n\nАктивних завдань немає 🎉\n"
                "Надішли голосове — і я створю завдання з дедлайном.", None)

    shown = tasks[:MAX_TASKS]
    lines = [f"📝 <b>Мої завдання</b> ({len(tasks)})", ""]
    for i, task in enumerate(shown, start=1):
        emoji, when = _deadline_label(task.get("deadline"), now)
        mic = " 🎙" if task.get("source") == "voice" else ""
        lines.append(f"{emoji} <b>{i}.</b> {escape(_clip(task['text'], TASK_CLIP))}{mic}\n"
                     f"      <i>{when}</i>")
    if len(tasks) > MAX_TASKS:
        lines.append(f"\n…і ще {len(tasks) - MAX_TASKS}. Виконай кілька — з’являться наступні.")
    lines.append("\nНатисни ✅ з номером, щоб позначити завдання виконаним.")

    buttons = [InlineKeyboardButton(text=f"✅ {i}", callback_data=f"tv:done:{t['id']}")
               for i, t in enumerate(shown, start=1)]
    rows = [buttons[i:i + BUTTONS_PER_ROW] for i in range(0, len(buttons), BUTTONS_PER_ROW)]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def render_notes(notes: list[dict], total: int) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    if not total:
        return "🗒 Нотаток більше немає.", None

    lines = [f"🗒 <b>Мої нотатки</b> ({total})", ""]
    for i, note in enumerate(notes, start=1):
        date = _note_date(note["created_at"])
        stamp = f"<i>{date}</i> — " if date else ""
        lines.append(f"<b>{i}.</b> {stamp}{escape(_clip(note['transcript'], NOTE_CLIP))}")
    if total > len(notes):
        lines.append(f"\n…показано {len(notes)} останніх із {total}.")

    rows = [[InlineKeyboardButton(text=f"✅ {i} → завдання", callback_data=f"tv:mk:{n['id']}"),
             InlineKeyboardButton(text=f"🗑 {i}", callback_data=f"tv:dn:{n['id']}")]
            for i, n in enumerate(notes, start=1)]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


# ───────────────────────── команда ─────────────────────────

@router.message(Command("tasks", "notes"))
async def cmd_tasks(message: Message) -> None:
    uid = message.from_user.id
    if not get_user(uid):
        await message.answer("Спочатку познайомимось — натисни /start 🙂")
        return

    tasks = await asyncio.to_thread(fetch_pending_tasks, uid)
    notes, total_notes = await asyncio.to_thread(fetch_standalone_notes, uid)

    if not tasks and not total_notes:
        await message.answer("📭 Поки що немає ні завдань, ні нотаток.\n"
                             "Надішли голосове — я запишу з нього завдання чи нотатку 🎙")
        return

    text, keyboard = render_tasks(tasks, local_now())
    await message.answer(text, reply_markup=keyboard)
    if total_notes:
        notes_text, notes_keyboard = render_notes(notes, total_notes)
        await message.answer(notes_text, reply_markup=notes_keyboard)


# ───────────────────────── кнопки ─────────────────────────

async def _edit(message: Message, text: str, keyboard: Optional[InlineKeyboardMarkup]) -> None:
    try:
        await message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest:      # «message is not modified» — повторне натискання
        pass


@router.callback_query(F.data.regexp(r"^tv:(done|mk|dn):\d+$"))
async def on_tv_button(callback: CallbackQuery) -> None:
    _, action, raw_id = callback.data.split(":")
    item_id, uid = int(raw_id), callback.from_user.id
    msg = callback.message
    if not isinstance(msg, Message):       # повідомлення надто старе/недоступне
        await callback.answer("Надішли /tasks ще раз", show_alert=False)
        return

    if action == "done":
        ok = await asyncio.to_thread(mark_done, uid, item_id)
        tasks = await asyncio.to_thread(fetch_pending_tasks, uid)
        text, keyboard = render_tasks(tasks, local_now())
        await _edit(msg, text, keyboard)
        await callback.answer("✅ Виконано" if ok else "Це вже змінено")
        return

    if action == "mk":
        result = await asyncio.to_thread(note_to_task, uid, item_id)
        answer = {"created": "✅ Додано до завдань — /tasks, щоб побачити",
                  "exists": "Це вже завдання ✅",
                  "missing": "Цієї нотатки вже немає"}[result]
    else:  # dn
        await asyncio.to_thread(delete_note, uid, item_id)
        answer = "🗑 Видалено"

    notes, total = await asyncio.to_thread(fetch_standalone_notes, uid)
    text, keyboard = render_notes(notes, total)
    await _edit(msg, text, keyboard)
    await callback.answer(answer)
