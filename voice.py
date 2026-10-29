"""
voice.py — голосові повідомлення → транскрипція → завдання/нотатка.

Потік:
  1. завантажуємо голосове (OGG/Opus) з Telegram;
  2. транскрибуємо його (Gemini приймає аудіо напряму — окремий Whisper не потрібен);
  3. текст іде в LLM зі спеціальним промптом → JSON {is_task, subject, task_text, deadline};
  4. зберігаємо нотатку (notes) і, якщо це завдання, запис у tasks (source='voice', note_id → notes);
  5. відповідаємо користувачу та даємо кнопки: «лише нотатка» / «видалити»
     (а якщо модель вирішила, що це не завдання — кнопка «зробити завданням»).

Підключення в main.py:
    import voice
    ...
    dp.include_router(router)
    dp.include_router(voice.router)      # після основного роутера

Використовує GEMINI_API_KEY та ті самі моделі/таймаути, що й schedule_import.py.
"""

import asyncio
import base64
import io
import json
import logging
import os
import re
from datetime import datetime
from html import escape
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           Message)

import schedule_import as si
from briefing import format_deadline
from db import DB_PATH, add_note, add_task, get_conn, get_user
from routing import local_now

router = Router()
logger = logging.getLogger(__name__)

MAX_VOICE_SEC = int(os.getenv("MAX_VOICE_SEC", "180"))
MAX_VOICE_BYTES = 10 * 1024 * 1024
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class VoiceError(Exception):
    """Помилка обробки голосового — текст можна показувати користувачу."""


# ───────────────────────── Gemini ─────────────────────────

def _gemini(parts: list[dict], config: dict) -> str:
    """Один запит до Gemini з перебором моделей (як у schedule_import)."""
    if not os.getenv("GEMINI_API_KEY"):
        raise VoiceError("Не налаштовано GEMINI_API_KEY — голосові поки не працюють.")
    last_error: Optional[Exception] = None
    for model in si._gemini_models():
        try:
            data = si._post_json(
                GEMINI_URL.format(model=model),
                {"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
                {"contents": [{"parts": parts}], "generationConfig": config})
            return "".join(p.get("text", "")
                           for p in data["candidates"][0]["content"]["parts"]).strip()
        except si.ModelBusyError as exc:
            logger.warning("Модель %s недоступна (%s) — пробую наступну", model, exc)
            last_error = exc
        except (KeyError, IndexError, TypeError) as exc:
            raise VoiceError("Модель не повернула відповідь.") from exc
    raise last_error or VoiceError("Немає доступних моделей.")


def _config(mime: str, schema: Optional[dict] = None) -> dict:
    config = {**si._gemini_generation_config(), "responseMimeType": mime, "maxOutputTokens": 2000}
    if schema:
        config["responseSchema"] = schema
    return config


def transcribe(audio: bytes, mime: str = "audio/ogg") -> str:
    """Голосове → текст. Порожній рядок, якщо мови не чути."""
    parts = [
        {"inline_data": {"mime_type": mime, "data": base64.b64encode(audio).decode()}},
        {"text": "Дослівно транскрибуй це голосове повідомлення (українська, можливі "
                 "російські чи англійські вкраплення). Поверни ЛИШЕ текст без коментарів "
                 "і лапок. Якщо мови немає — поверни порожній рядок."},
    ]
    return _gemini(parts, _config("text/plain"))


_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "is_task": {"type": "BOOLEAN"},
        "subject": {"type": "STRING", "nullable": True},
        "task_text": {"type": "STRING"},
        "deadline": {"type": "STRING", "nullable": True},
    },
    "required": ["is_task", "task_text"],
}


def _extract_prompt(text: str, subjects: list[str], now: datetime) -> str:
    known = "; ".join(subjects) if subjects else "(розклад не завантажено)"
    return f"""Ти помічник студента. З голосової нотатки (транскрипція нижче) витягни завдання.

Зараз: {now:%Y-%m-%d %H:%M}, {si.DAY_NAMES[now.isoweekday()]}.
Предмети з розкладу студента: {known}

Поверни JSON із полями:
- is_task: true, якщо це щось, що треба зробити (здати, підготувати, купити, прийти...);
  false, якщо це просто думка, ідея чи інформація без дії.
- subject: навчальний предмет. Якщо він відповідає предмету зі списку (навіть розмовна назва,
  напр. «матан», «фізра») — поверни ТОЧНУ назву зі списку. Якщо предмета немає — null.
- task_text: суть завдання одним коротким реченням у наказовому/неозначеному вигляді,
  без вступних слів і без дати (її — у deadline).
- deadline: дедлайн у форматі "YYYY-MM-DD HH:MM" або null, якщо його не названо.
  «завтра», «в п'ятницю», «до наступного вівторка» рахуй від «Зараз». Якщо названо день,
  але не час — став 23:59. Не вигадуй дедлайн, якого немає.

Транскрипція:
\"\"\"{text}\"\"\""""


def _norm_deadline(value) -> Optional[datetime]:
    if not value:
        return None
    value = str(value).strip()
    for fmt, date_only in (("%Y-%m-%d %H:%M", False), ("%Y-%m-%dT%H:%M", False),
                           ("%Y-%m-%d %H:%M:%S", False), ("%Y-%m-%d", True)):
        try:
            dt = datetime.strptime(value, fmt)
        except ValueError:
            continue
        return dt.replace(hour=23, minute=59) if date_only else dt
    return None


def extract_task(text: str, subjects: list[str], now: datetime) -> dict:
    """Транскрипт → {'is_task', 'subject', 'task_text', 'deadline': datetime|None}."""
    raw = _gemini([{"text": _extract_prompt(text, subjects, now)}],
                  _config("application/json", _SCHEMA))
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError
    except ValueError as exc:
        logger.error("LLM повернула не JSON: %r", raw[:300])
        raise VoiceError("Не вдалося розібрати відповідь моделі. Спробуй сказати ще раз.") from exc

    subject = (data.get("subject") or "").strip() or None
    task_text = (data.get("task_text") or "").strip() or text.strip()
    return {"is_task": bool(data.get("is_task", True)), "subject": subject,
            "task_text": task_text, "deadline": _norm_deadline(data.get("deadline"))}


# ───────────────────────── БД-хелпери ─────────────────────────

def _group_subjects(group_name: str) -> list[str]:
    with get_conn(DB_PATH) as conn:
        rows = conn.execute("SELECT DISTINCT subject FROM schedule WHERE group_name = ? "
                            "ORDER BY subject", (group_name,)).fetchall()
    return [r["subject"] for r in rows]


def _task_note_id(user_id: int, task_id: int) -> Optional[int]:
    with get_conn(DB_PATH) as conn:
        row = conn.execute("SELECT note_id FROM tasks WHERE id = ? AND user_id = ?",
                           (task_id, user_id)).fetchone()
    return row["note_id"] if row else None


def _remove_voice_entry(user_id: int, task_id: int, keep_note: bool) -> bool:
    """Видаляє завдання; нотатку — лише якщо keep_note=False."""
    with get_conn(DB_PATH) as conn:
        row = conn.execute("SELECT note_id FROM tasks WHERE id = ? AND user_id = ?",
                           (task_id, user_id)).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id))
        if not keep_note and row["note_id"]:
            conn.execute("DELETE FROM notes WHERE id = ? AND user_id = ?",
                         (row["note_id"], user_id))
    return True


def _get_note(user_id: int, note_id: int) -> Optional[str]:
    with get_conn(DB_PATH) as conn:
        row = conn.execute("SELECT transcript FROM notes WHERE id = ? AND user_id = ?",
                           (note_id, user_id)).fetchone()
    return row["transcript"] if row else None


def _note_has_task(user_id: int, note_id: int) -> bool:
    with get_conn(DB_PATH) as conn:
        return conn.execute("SELECT 1 FROM tasks WHERE note_id = ? AND user_id = ?",
                            (note_id, user_id)).fetchone() is not None


def _delete_note(user_id: int, note_id: int) -> None:
    with get_conn(DB_PATH) as conn:
        conn.execute("DELETE FROM notes WHERE id = ? AND user_id = ?", (note_id, user_id))


# ───────────────────────── обробник голосового ─────────────────────────

def _task_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📝 Лише нотатка", callback_data=f"vt:note:{task_id}"),
        InlineKeyboardButton(text="🗑 Видалити", callback_data=f"vt:del:{task_id}"),
    ]])


def _note_keyboard(note_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Зробити завданням", callback_data=f"vt:mk:{note_id}"),
        InlineKeyboardButton(text="🗑 Видалити", callback_data=f"vt:dn:{note_id}"),
    ]])


def _task_line(subject: Optional[str], task_text: str) -> str:
    return f"{subject}: {task_text}" if subject else task_text


@router.message(F.voice, StateFilter(None))
async def on_voice(message: Message, bot: Bot) -> None:
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Спочатку познайомимось — натисни /start 🙂")
        return

    voice = message.voice
    if voice.duration and voice.duration > MAX_VOICE_SEC:
        await message.answer(f"Занадто довге голосове (ліміт {MAX_VOICE_SEC} с). "
                             "Розбий його на кілька коротших 🙏")
        return
    if voice.file_size and voice.file_size > MAX_VOICE_BYTES:
        await message.answer("Файл завеликий — запиши коротше голосове.")
        return

    status = await message.answer("🎧 Слухаю…")
    try:
        buffer = io.BytesIO()
        await bot.download(voice, destination=buffer)
        transcript = await asyncio.to_thread(transcribe, buffer.getvalue(),
                                             voice.mime_type or "audio/ogg")
        if not transcript:
            await status.edit_text("🤷 Не почув жодних слів. Спробуй ще раз.")
            return
        await status.edit_text("🧠 Розбираю, що треба зробити…")
        subjects = await asyncio.to_thread(_group_subjects, user["group_name"])
        parsed = await asyncio.to_thread(extract_task, transcript, subjects, local_now())
    except (VoiceError, si.ScheduleImportError) as exc:
        await status.edit_text(f"😕 {escape(str(exc))}")
        return
    except Exception:
        logger.exception("Помилка обробки голосового від %s", message.from_user.id)
        await status.edit_text("😕 Щось пішло не так під час обробки голосового. Спробуй ще раз.")
        return

    uid = message.from_user.id
    note_id = add_note(uid, transcript, voice.file_id)
    heard = f"<i>Почув: «{escape(transcript[:400])}»</i>"

    if not parsed["is_task"]:
        await status.edit_text(
            f"📝 Записав як нотатку:\n{escape(transcript[:600])}\n\n"
            "Якщо це насправді завдання — натисни кнопку.",
            reply_markup=_note_keyboard(note_id))
        return

    deadline = parsed["deadline"]
    task_id = add_task(uid, _task_line(parsed["subject"], parsed["task_text"]),
                       deadline=deadline, source="voice", note_id=note_id)
    lines = ["✅ <b>Завдання записано</b>",
             f"📚 Предмет: {escape(parsed['subject'])}" if parsed["subject"] else "📚 Предмет: —",
             f"📝 {escape(parsed['task_text'])}",
             f"⏰ Дедлайн: {escape(format_deadline(deadline.strftime('%Y-%m-%d %H:%M')))}"
             if deadline else "⏰ Дедлайн: не вказано",
             "", heard]
    await status.edit_text("\n".join(lines), reply_markup=_task_keyboard(task_id))


# ───────────────────────── кнопки ─────────────────────────

@router.callback_query(F.data.regexp(r"^vt:(note|del|mk|dn):\d+$"))
async def on_voice_button(callback: CallbackQuery) -> None:
    _, action, raw_id = callback.data.split(":")
    item_id, uid = int(raw_id), callback.from_user.id

    if action == "note":          # завдання → лише нотатка
        ok = _remove_voice_entry(uid, item_id, keep_note=True)
        text = "📝 Ок, залишив лише як нотатку." if ok else "Це вже змінено."
    elif action == "del":         # видалити і завдання, і нотатку
        ok = _remove_voice_entry(uid, item_id, keep_note=False)
        text = "🗑 Видалено." if ok else "Це вже видалено."
    elif action == "dn":          # видалити нотатку
        _delete_note(uid, item_id)
        text, ok = "🗑 Видалено.", True
    else:                         # mk: нотатка → завдання
        transcript = _get_note(uid, item_id)
        if transcript is None:
            text = "Цієї нотатки вже немає."
        elif _note_has_task(uid, item_id):
            text = "Це вже завдання ✅"
        else:
            task_id = add_task(uid, transcript.strip()[:300], source="voice", note_id=item_id)
            await callback.message.edit_text(
                f"✅ Тепер це завдання (без дедлайну):\n{escape(transcript[:300])}",
                reply_markup=_task_keyboard(task_id))
            await callback.answer("Додано")
            return

    await callback.message.edit_text(text, reply_markup=None)
    await callback.answer()
