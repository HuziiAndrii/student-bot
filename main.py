"""
main.py — точка входу Telegram-бота-компаньйона для студентів (aiogram 3.x).

Команди:
    /start    — покрокова реєстрація (ім'я → заклад і група → звідки добираєшся → час на збори)
    /edit     — пройти реєстрацію знову (оновити профіль)
    /profile  — показати профіль
    /brief    — ранішній бриф на сьогодні: коли прокинутись, коли вийти
                і повний маршрут (бот будує його сам: дивиться, де перша пара, і шукає транспорт)
    /brieftmorrow — те саме, але на завтра
    /week     — який зараз тиждень (чисельник/знаменник); `/week чисельник|знаменник` — виправити
    /schedule — завантажити розклад групи (фото, скріншот, PDF, Excel, текст або посилання)
    /tasks, /notes — мої завдання та нотатки (кнопки «виконано», «→ завдання», «видалити»)
    /deletemydata — видалити всі свої дані (з підтвердженням)
    /cancel   — скасувати поточний діалог
    /route, /myroute — необов'язково: задати власний маршрут вручну (має пріоритет над автоматичним)

Фоново (щохвилини) бот перевіряє, кому пора надіслати:
    • ранішній бриф  — коли треба прокинутись
    • «Час збиратися!» — за 10 хв до виходу з дому

Логіка розрахунку — у briefing.py, розбір вводу — у parsers.py, дані — у db.py.
"""

import asyncio
import io
import logging
import os
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from dotenv import load_dotenv

from briefing import (WEEK_ANCHOR_KEY, WEEK_NAMES, build_day_plan, collect_due_notifications,
                      format_morning_brief, format_route, get_group_buildings,
                      get_week_anchor, get_week_type)
from db import (DB_PATH, count_schedule, delete_user_data, get_user, init_db, is_profile_complete, list_routes,
                release_notification, replace_schedule, save_route, set_setting, upsert_user)
from parsers import (LEG_FORMAT_HELP, LegParseError, parse_home_place, parse_leg_line,
                     parse_minutes, parse_name, parse_prep_minutes, parse_university_and_group)
from routing import local_now
from scheduler import setup_scheduler
from weather import weather_line
import tasks_view
import voice
from schedule_import import (MANUAL_FORMAT_HELP, MAX_FILE_BYTES, ScheduleImportError,
                             format_diff, format_lessons, import_auto, import_from_file,
                             import_from_text,
                             llm_provider)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

router = Router()
logger = logging.getLogger(__name__)


# ───────────────────────── FSM ─────────────────────────

class Registration(StatesGroup):
    name = State()
    university_group = State()
    home = State()
    prep = State()


class ScheduleImport(StatesGroup):
    waiting = State()    # чекаємо розклад (фото / файл / текст / посилання)
    confirm = State()    # показали результат, чекаємо «Зберегти»


class RouteSetup(StatesGroup):
    legs = State()
    home_walk = State()
    walk_only = State()   # наступні корпуси: лише скільки йти пішки від тієї ж зупинки


# Текстові хендлери станів ігнорують команди (/щось), щоб команди завжди працювали.
NOT_COMMAND = ~F.text.startswith("/")


# ───────────────────────── допоміжне ─────────────────────────

def format_profile(user: dict) -> str:
    return (
        f"👤 Ім’я: <b>{escape(user.get('name') or '—')}</b>\n"
        f"🎓 Навчальний заклад: <b>{escape(user['university'])}</b>\n"
        f"👥 Група: <b>{escape(user['group_name'])}</b>\n"
        f"🏠 Звідки добираєшся: <b>{escape(user.get('home_place') or '—')}</b>\n"
        f"⏱ Ранкові збори: <b>{user.get('prep_minutes') or '—'} хв</b> (+ запас 5–10 хв)"
    )


COMMANDS_HELP = (
    "Що я вмію:\n"
    "/brief — бриф на сьогодні: коли прокинутись, "
    "коли вийти і повний маршрут до першої пари — я будую його сам\n"
    "/brieftmorrow — той самий бриф, але на завтра\n"
    "/schedule — завантажити розклад групи (фото, PDF, Excel, текст, посилання)\n"
    "🎙 Надішли голосове — я запишу з нього завдання чи нотатку\n"
    "/tasks — мої завдання та нотатки (/notes — те саме)\n"
    "/profile — мій профіль, /edit — змінити\n"
    "/deletemydata — видалити всі мої дані\n"
    "/cancel — скасувати поточну дію\n\n"
    "<i>Необов’язково: /route — задати власний маршрут вручну.</i>"
)

Q_NAME = "<b>Як до тебе звертатись?</b>"
Q_UNIVERSITY = ("<b>З якого ти закладу та яка твоя група?</b>\n"
                "Однією фразою, наприклад: <i>Львівська політехніка, група АП-31 </i>")
Q_HOME = ("<b>Звідки ти добираєшся?</b>\n"
          "Напиши населений пункт, а якщо їдеш із міста — краще з вулицею, щоб маршрут був точним. "
          "Наприклад: <i>Дрогобич, Львівська обл.</i> або <i>Львів, вул. Стрийська 10</i>.")
Q_PREP = ("<b>Скільки часу тобі зазвичай потрібно на ранкові збори?</b>\n"
          "Напиши у хвилинах, наприклад: <i>40</i>. Я додам ще 5–10 хв запасу «на всякий випадок».")


# ───────────────────────── /start, /edit, /profile, /cancel ─────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = get_user(message.from_user.id)

    if is_profile_complete(user):
        await message.answer(
            f"👋 З поверненням, {escape(user['name'])}!\n\n{format_profile(user)}\n\n{COMMANDS_HELP}"
        )
        return

    if user:  # користувач із Task 2: є заклад і група, але немає нових полів
        intro = "👋 Я оновився, давай доповнимо твій профіль (4 коротких запитання, /cancel — скасувати)."
    else:
        intro = ("👋 Привіт! Я бот-компаньйон для студентів: стежу за розкладом, рахую, о котрій "
                 "прокидатись і виходити, та нагадую про дедлайни.\n\n"
                 "Давай познайомимось — 4 коротких запитання (/cancel — скасувати).")
    await message.answer(f"{intro}\n\n{Q_NAME}")
    await state.set_state(Registration.name)


@router.message(Command("edit"))
async def cmd_edit(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(f"Оновимо профіль. {Q_NAME}")
    await state.set_state(Registration.name)


@router.message(Command("profile"))
async def cmd_profile(message: Message) -> None:
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Ми ще не знайомі — натисни /start 🙂")
        return
    await message.answer(format_profile(user))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer("Нічого скасовувати 🙂")
        return
    await state.clear()
    await message.answer("Скасовано. Коли будеш готовий — /start, /edit або /brief.")


# ───────────────────────── реєстрація (FSM) ─────────────────────────

@router.message(Registration.name, F.text, NOT_COMMAND)
async def reg_name(message: Message, state: FSMContext) -> None:
    name = parse_name(message.text)
    if name is None:
        await message.answer("Напиши, будь ласка, ім’я текстом (до 50 символів), наприклад: <i>Олег</i>")
        return
    await state.update_data(name=name)
    await message.answer(f"Приємно, {escape(name)}! 🙌\n\n{Q_UNIVERSITY}")
    await state.set_state(Registration.university_group)


@router.message(Registration.university_group, F.text, NOT_COMMAND)
async def reg_university_group(message: Message, state: FSMContext) -> None:
    parsed = parse_university_and_group(message.text)
    if parsed is None:
        await message.answer(
            "🤔 Не вдалося розпізнати. Напиши у форматі «Назва закладу, група КОД», наприклад: "
            "<i>Львівська політехніка, група КІ-108</i>"
        )
        return
    university, group_name = parsed
    await state.update_data(university=university, group_name=group_name)
    await message.answer(Q_HOME)
    await state.set_state(Registration.home)


@router.message(Registration.home, F.text, NOT_COMMAND)
async def reg_home(message: Message, state: FSMContext) -> None:
    parsed = parse_home_place(message.text)
    if parsed is None:
        await message.answer("Напиши місто чи село, звідки їдеш, наприклад: <i>Дрогобич, Львівська обл.</i>")
        return
    home_place, home_city = parsed
    await state.update_data(home_place=home_place, home_city=home_city)
    await message.answer(Q_PREP)
    await state.set_state(Registration.prep)


@router.message(Registration.prep, F.text, NOT_COMMAND)
async def reg_prep(message: Message, state: FSMContext) -> None:
    prep = parse_prep_minutes(message.text)
    if prep is None:
        await message.answer("Напиши кількість хвилин від 5 до 240, наприклад: <i>40</i> (або «1 год 15 хв»).")
        return

    data = await state.get_data()
    upsert_user(
        message.from_user.id, data["university"], data["group_name"],
        name=data["name"], home_place=data["home_place"],
        home_city=data["home_city"], prep_minutes=prep,
    )
    await state.clear()
    logger.info("Профіль збережено: %s (%s, %s)", message.from_user.id,
                data["university"], data["group_name"])

    user = get_user(message.from_user.id)
    needs_schedule = await asyncio.to_thread(count_schedule, data["group_name"]) == 0
    if needs_schedule:
        route_hint = ("📅 Розкладу твоєї групи в мене ще немає — зараз спробую знайти його сам. "
                      "Далі я стежитиму, де перша пара, і рахуватиму дорогу.")
    else:
        route_hint = ("🗺 Маршрут до пар я побудую сам: дивлюсь у розклад, де перша пара. "
                      "Спробуй: /brief або /brieftmorrow")
    await message.answer(f"✅ <b>Профіль збережено!</b>\n\n{format_profile(user)}\n\n{route_hint}")
    if needs_schedule:
        await _auto_schedule(message, state, user)


@router.message(StateFilter(Registration), ~F.text)
async def reg_not_text(message: Message) -> None:
    await message.answer("Мені потрібна текстова відповідь 🙂 (/cancel — скасувати)")


# ───────────────────────── маршрут (FSM) ─────────────────────────

async def _prompt_legs(message: Message, intro: str) -> None:
    await message.answer(
        f"{intro}\n\n"
        "Додавай ділянки дороги <b>по порядку</b> — від першої зупинки до корпусу. "
        "Можна кілька рядків в одному повідомленні.\n\n"
        f"{LEG_FORMAT_HELP}\n\n"
        "Приклад (підстав свої дані):\n"
        "<code>міжміський | маршрутка №100 | Миколаїв, центр → Львів, автовокзал | 70 | 06:40, 07:15</code>\n"
        "<code>міський | трамвай №3 | Автовокзал → Наукова | 20</code>\n"
        "<code>пішки | Наукова → корпус IV | 10</code>\n\n"
        "Останньою ділянкою додай пішу — від зупинки до корпусу. Коли закінчиш — напиши <b>готово</b>."
    )


@router.message(Command("route"))
async def cmd_route(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Спочатку познайомимось — натисни /start 🙂")
        return

    # Корпуси беремо з розкладу групи — користувача про них не питаємо.
    buildings = await asyncio.to_thread(get_group_buildings, user["group_name"])
    head = "🗺 Налаштуємо маршрут (/cancel — скасувати).\n\n"
    if not buildings:
        await state.update_data(building="", queue=[], legs=[])
        intro = (head + "У розкладі групи ще немає пар з аудиторіями, тому збережу дорогу як маршрут "
                 "для будь-якого корпусу.")
    else:
        first, rest = buildings[0], buildings[1:]
        await state.update_data(building=first, queue=rest, legs=[])
        if rest:
            intro = (head + f"З розкладу бачу, що пари проходять у корпусах: <b>{', '.join(buildings)}</b>. "
                     f"Пройдемо по черзі, починаю з <b>{first}</b> (там найбільше пар).")
        else:
            intro = head + f"З розкладу бачу, що твої пари проходять у корпусі <b>{first}</b> — налаштовую дорогу до нього."
    await _prompt_legs(message, intro)
    await state.set_state(RouteSetup.legs)


@router.message(RouteSetup.legs, F.text, NOT_COMMAND)
async def route_legs(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    legs: list[dict] = data.get("legs", [])

    if message.text.strip().lower() in ("готово", "все", "done"):
        if not legs:
            await message.answer("Поки жодної ділянки. Додай хоча б одну або /cancel.")
            return
        note = ""
        if legs[-1]["kind"] != "walk":
            note = ("\n\n⚠️ Остання ділянка — не пішки. Якщо від зупинки до корпусу ще йти, "
                    "потім перезапиши маршрут через /route і додай пішу ділянку.")
        await message.answer(
            "Скільки хвилин іти <b>від дому до першої зупинки</b>? (0 — якщо вже на місці)" + note
        )
        await state.set_state(RouteSetup.home_walk)
        return

    new_legs, lines = [], [ln for ln in message.text.splitlines() if ln.strip()]
    for number, line in enumerate(lines, start=1):
        try:
            new_legs.append(parse_leg_line(line))
        except LegParseError as exc:
            prefix = f"Рядок {number}: " if len(lines) > 1 else ""
            await message.answer(f"❌ {prefix}{escape(str(exc))}\n\nНічого не додано — виправ і надішли ще раз.")
            return

    legs.extend(new_legs)
    await state.update_data(legs=legs)
    await message.answer(
        f"✅ Додано ділянок: {len(new_legs)} (усього {len(legs)}). "
        "Надсилай наступну або напиши <b>готово</b>."
    )


async def _save_and_continue(message: Message, state: FSMContext, legs: list[dict],
                             building: str) -> None:
    """Зберігає маршрут і переходить до наступного корпусу з розкладу (якщо він є)."""
    title = f"дім → корпус {building}" if building else "дім → навчання"
    await asyncio.to_thread(save_route, message.from_user.id, legs, building, title)

    data = await state.get_data()
    queue: list[str] = data.get("queue") or []

    if not queue:
        await state.clear()
        routes = await asyncio.to_thread(list_routes, message.from_user.id)
        await message.answer("✅ <b>Готово, маршрути збережено!</b>\n\n"
                             + "\n\n".join(format_route(r) for r in routes)
                             + "\n\nПеревір, як це виглядатиме в брифі: /brief")
        return

    # Для наступного корпусу пропонуємо взяти ту саму дорогу — міняється лише остання пішка.
    if legs[-1]["kind"] == "walk":
        base, stop = legs[:-1], legs[-1]["from_stop"]
    else:
        base, stop = legs, legs[-1]["to_stop"]
    nxt, rest = queue[0], queue[1:]
    await state.update_data(queue=rest, building=nxt, base_legs=base, base_stop=stop, legs=[])
    await message.answer(
        f"✅ Корпус {escape(building)} збережено. Далі — корпус <b>{escape(nxt)}</b> (там теж є пари).\n\n"
        f"Якщо їдеш так само і виходиш на зупинці «{escape(stop)}» — напиши, скільки хвилин звідти "
        f"пішки до корпусу {escape(nxt)}. Якщо дорога інша — напиши <b>інакше</b>."
    )
    await state.set_state(RouteSetup.walk_only)


@router.message(RouteSetup.home_walk, F.text, NOT_COMMAND)
async def route_home_walk(message: Message, state: FSMContext) -> None:
    minutes = parse_minutes(message.text, 0, 120)
    if minutes is None:
        await message.answer("Напиши число хвилин від 0 до 120, наприклад: <i>7</i>")
        return

    data = await state.get_data()
    legs: list[dict] = data.get("legs") or []
    if not legs:  # стан міг загубитись (напр., після перезапуску бота)
        await state.clear()
        await message.answer("Дані маршруту втрачено — почнімо спочатку: /route")
        return
    if minutes > 0:
        legs = [{"kind": "walk", "transport": "пішки", "line": None, "from_stop": "дім",
                 "to_stop": legs[0]["from_stop"], "duration_min": minutes,
                 "wait_min": 0, "departures": []}] + legs

    await _save_and_continue(message, state, legs, data.get("building", ""))


@router.message(RouteSetup.walk_only, F.text, NOT_COMMAND)
async def route_walk_only(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    base: list[dict] = data.get("base_legs") or []
    building: str = data.get("building", "")
    if not base:
        await state.clear()
        await message.answer("Дані маршруту втрачено — почнімо спочатку: /route")
        return

    if message.text.strip().lower() in ("інакше", "інша", "іншою", "інший", "other"):
        await state.update_data(legs=[])
        await _prompt_legs(message, f"Добре, для корпусу <b>{escape(building)}</b> введи дорогу окремо.")
        await state.set_state(RouteSetup.legs)
        return

    minutes = parse_minutes(message.text, 1, 120)
    if minutes is None:
        await message.answer("Напиши число хвилин пішки (1–120) або слово <b>інакше</b>.")
        return

    walk = {"kind": "walk", "transport": "пішки", "line": None, "from_stop": data["base_stop"],
            "to_stop": f"корпус {building}", "duration_min": minutes, "wait_min": 0, "departures": []}
    await _save_and_continue(message, state, base + [walk], building)


@router.message(StateFilter(RouteSetup), ~F.text)
async def route_not_text(message: Message) -> None:
    await message.answer("Мені потрібен текст 🙂 (/cancel — скасувати)")


@router.message(Command("myroute"))
async def cmd_myroute(message: Message) -> None:
    routes = await asyncio.to_thread(list_routes, message.from_user.id)
    if not routes:
        await message.answer("Власних маршрутів немає — я будую дорогу сам під кожен день у /brief. "
                             "Якщо хочеш задати свій маршрут вручну — /route.")
        return
    await message.answer("\n\n".join(format_route(r) for r in routes)
                         + "\n\nЗмінити маршрут для корпусу — /route (старий буде замінено).")


# ───────────────────────── розклад: розпізнавання ─────────────────────────

SCHEDULE_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="✅ Зберегти", callback_data="sched:save"),
    InlineKeyboardButton(text="❌ Скасувати", callback_data="sched:no"),
]])


async def _ask_for_schedule(message: Message, state: FSMContext, user: dict, prefix: str = "") -> None:
    """Просимо надіслати розклад самому — коли автопошук не спрацював."""
    if llm_provider():
        sources = ("Надішли розклад групи <b>{g}</b> будь-яким способом:\n"
                   "📷 фото або скріншот (по одному зображенню)\n"
                   "📄 PDF або Excel (.xlsx)\n"
                   "🔗 посилання на сторінку з розкладом\n"
                   "✍️ текст — скопійований із сайту або у простому форматі нижче")
    else:
        sources = ("Надішли розклад групи <b>{g}</b> простим текстом. (Фото, PDF, Excel і посилання "
                   "бот зможе читати, коли в .env буде GEMINI_API_KEY або ANTHROPIC_API_KEY.)")
    await message.answer(f"{prefix}{sources.format(g=escape(user['group_name']))}\n\n{MANUAL_FORMAT_HELP}\n\n"
                         "Перед збереженням я покажу, що розпізнав. /cancel — скасувати.")
    await state.set_state(ScheduleImport.waiting)


async def _auto_schedule(message: Message, state: FSMContext, user: dict) -> None:
    """Бот сам шукає розклад групи на сайті закладу; якщо не вдалось — просить надіслати."""
    await _import_and_preview(message, state, import_auto, user["university"], ask_on_fail=True)


@router.message(Command("schedule"))
async def cmd_schedule(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Спочатку познайомимось — натисни /start 🙂")
        return
    await _auto_schedule(message, state, user)


async def _import_and_preview(message: Message, state: FSMContext, func, *args,
                              ask_on_fail: bool = False) -> None:
    """
    Запускає розпізнавання у фоні, показує результат і чекає підтвердження.
    ask_on_fail=True (автопошук): при невдачі пояснює причину і просить надіслати розклад самому.
    """
    user = get_user(message.from_user.id)
    if not user:
        await state.clear()
        await message.answer("Профіль не знайдено — натисни /start 🙂")
        return
    status = await message.answer(
        f"🔎 Шукаю розклад групи {escape(user['group_name'])} в інтернеті — це може тривати до хвилини…" if ask_on_fail
        else "⏳ Розпізнаю розклад, це може тривати до хвилини…")
    try:
        lessons, warnings = await asyncio.to_thread(func, *args, user["group_name"])
    except ScheduleImportError as exc:
        if ask_on_fail:
            await status.edit_text(f"😕 {escape(str(exc))}")
            await _ask_for_schedule(message, state, user, "Тоді надішли розклад мені сам.\n\n")
        else:
            await status.edit_text(f"❌ {escape(str(exc))}\n\nСпробуй ще раз або /cancel.")
        return
    except Exception:
        logger.exception("Помилка імпорту розкладу")
        if ask_on_fail:
            await status.edit_text("😕 Автопошук розкладу не вдався.")
            await _ask_for_schedule(message, state, user, "Надішли розклад мені сам.\n\n")
        else:
            await status.edit_text("❌ Щось пішло не так під час розпізнавання. Спробуй ще раз або /cancel.")
        return

    await state.update_data(lessons=lessons)
    await state.set_state(ScheduleImport.confirm)
    text = (f"📅 <b>Розпізнав {len(lessons)} пар для {escape(user['group_name'])}:</b>\n\n"
            f"{format_lessons(lessons)}")
    if warnings:
        text += "\n\n⚠️ " + "\n⚠️ ".join(escape(w) for w in warnings[:8])
    text += ("\n\nПеревір (особливо час, аудиторії й чисельник/знаменник). Збереження замінить "
             "<b>увесь</b> розклад групи.")
    await status.edit_text(text[:4000], reply_markup=SCHEDULE_KEYBOARD)


@router.message(ScheduleImport.waiting, F.photo)
async def schedule_photo(message: Message, state: FSMContext) -> None:
    buf = io.BytesIO()
    await message.bot.download(message.photo[-1], destination=buf)
    await _import_and_preview(message, state, import_from_file, buf.getvalue(), "photo.jpg", "image/jpeg")


@router.message(ScheduleImport.waiting, F.document)
async def schedule_document(message: Message, state: FSMContext) -> None:
    doc = message.document
    if doc.file_size and doc.file_size > MAX_FILE_BYTES:
        await message.answer("Файл завеликий (понад 10 МБ). Надішли менший або скріншот.")
        return
    buf = io.BytesIO()
    await message.bot.download(doc, destination=buf)
    await _import_and_preview(message, state, import_from_file, buf.getvalue(),
                              doc.file_name or "", doc.mime_type or "")


@router.message(ScheduleImport.waiting, F.text, NOT_COMMAND)
async def schedule_text(message: Message, state: FSMContext) -> None:
    await _import_and_preview(message, state, import_from_text, message.text)


@router.message(StateFilter(ScheduleImport.waiting))
async def schedule_unsupported(message: Message) -> None:
    await message.answer("Мені потрібне фото, PDF, Excel, текст або посилання 🙂 (/cancel — скасувати)")


@router.callback_query(ScheduleImport.confirm, F.data == "sched:save")
async def schedule_save(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    lessons = data.get("lessons")
    user = get_user(callback.from_user.id)
    await state.clear()
    await callback.answer()
    if not lessons or not user:
        await callback.bot.send_message(callback.from_user.id, "Дані загубились — почни знову: /schedule")
        return
    diff = await asyncio.to_thread(replace_schedule, user["group_name"], lessons)
    logger.info("Розклад %s оновлено користувачем %s: %s", user["group_name"],
                callback.from_user.id, {k: len(v) for k, v in diff.items()})
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    await callback.bot.send_message(
        callback.from_user.id,
        f"✅ <b>Розклад збережено</b> ({len(lessons)} пар): {format_diff(diff)}.\n\n"
        "Тепер я знаю, де перша пара. Глянь бриф: /brief або /brieftmorrow")


@router.callback_query(F.data == "sched:no")
async def schedule_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Скасовано")
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    await callback.bot.send_message(callback.from_user.id,
                                    "Нічого не змінював. Спробувати ще раз — /schedule")


# ───────────────────────── /brief ─────────────────────────

@router.message(Command("brief"))
async def cmd_brief(message: Message) -> None:
    await _send_brief(message, is_tomorrow=False)


@router.message(Command("brieftmorrow"))
async def cmd_brief_tomorrow(message: Message) -> None:
    await _send_brief(message, is_tomorrow=True)


async def _send_brief(message: Message, is_tomorrow: bool) -> None:
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Спочатку познайомимось — натисни /start 🙂")
        return

    day = local_now().date() + timedelta(days=1 if is_tomorrow else 0)

    await message.bot.send_chat_action(message.chat.id, "typing")   # побудова маршруту може тривати кілька секунд
    dp = await asyncio.to_thread(build_day_plan, user, day, DB_PATH, local_now())
    weather = await asyncio.to_thread(weather_line, day)
    await message.answer(format_morning_brief(user, dp, is_tomorrow=is_tomorrow, weather=weather))


# ───────────────────────── /week ─────────────────────────

_WEEK_ARGS = {"чис": "odd", "чисельник": "odd", "odd": "odd",
              "знам": "even", "знаменник": "even", "even": "even"}


def _week_status_text() -> str:
    today = local_now().date()
    anchor, source = get_week_anchor(DB_PATH)
    this_week = get_week_type(today, DB_PATH)
    next_week = "even" if this_week == "odd" else "odd"
    text = (f"🗓 Зараз <b>{WEEK_NAMES[this_week]}</b> (сьогодні {today:%d.%m}).\n"
            f"Наступного тижня — <b>{WEEK_NAMES[next_week]}</b>.")
    if source == "iso":
        text += ("\n\n⚠️ Я ще не знаю, який тиждень у вас чисельник, тому здогадуюсь за номером тижня "
                 "в році — це може не збігатися з розкладом.\n"
                 "Скажи мені: <code>/week чисельник</code> або <code>/week знаменник</code> "
                 "(який тиждень ЗАРАЗ за розкладом) — і я запам’ятаю.")
    else:
        text += "\n\nНе збігається з розкладом? Виправ: <code>/week чисельник</code> або <code>/week знаменник</code>."
    return text


@router.message(Command("week"))
async def cmd_week(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip().lower()
    if not arg:
        await message.answer(_week_status_text())
        return
    wanted = _WEEK_ARGS.get(arg)
    if not wanted:
        await message.answer("Напиши <code>/week чисельник</code> або <code>/week знаменник</code> — "
                             "який тиждень зараз за розкладом. Просто <code>/week</code> покаже поточний.")
        return
    today = local_now().date()
    monday = today - timedelta(days=today.weekday())
    # опорний понеділок чисельника: цей тиждень, якщо зараз чисельник, інакше попередній
    anchor = monday if wanted == "odd" else monday - timedelta(days=7)
    await asyncio.to_thread(set_setting, WEEK_ANCHOR_KEY, anchor.isoformat(), DB_PATH)
    await message.answer("✅ Запам’ятав.\n" + _week_status_text())


# ───────────────────────── видалення всіх даних ─────────────────────────

WIPE_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="🗑 Так, видалити все", callback_data="wipe:yes"),
    InlineKeyboardButton(text="↩️ Скасувати", callback_data="wipe:no"),
]])


@router.message(Command("deletemydata", "delete_my_data"))
async def cmd_delete_my_data(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not get_user(message.from_user.id):
        await message.answer("Я нічого про тебе не зберігаю — видаляти нічого 🙂")
        return
    await message.answer(
        "⚠️ <b>Видалити всі твої дані?</b>\n\n"
        "Буде безповоротно стерто: профіль, нотатки, завдання, власні маршрути, "
        "збережений маршрут на день і журнал сповіщень. Ранкові брифи та нагадування "
        "припиняться.\n"
        "Розклад групи — спільний, він не персональний і залишиться.",
        reply_markup=WIPE_KEYBOARD,
    )


@router.callback_query(F.data == "wipe:yes")
async def wipe_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    result = await asyncio.to_thread(delete_user_data, callback.from_user.id)
    logger.info("Дані користувача %s видалено: %s", callback.from_user.id, result)
    await callback.answer("Готово")
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    if result["deleted"]:
        text = ("✅ <b>Усі твої дані видалено.</b>\n"
                f"(нотаток: {result['notes']}, завдань: {result['tasks']}, "
                f"маршрутів: {result['routes']})\n\n"
                "Якщо захочеш повернутись — просто натисни /start.")
    else:
        text = "Я вже нічого про тебе не зберігаю 🙂"
    await callback.bot.send_message(callback.from_user.id, text)


@router.callback_query(F.data == "wipe:no")
async def wipe_cancel(callback: CallbackQuery) -> None:
    await callback.answer("Скасовано")
    if callback.message:
        try:
            await callback.message.edit_text("Добре, нічого не видаляю 🙂")
        except Exception:
            pass


# ───────────────────────── фонові сповіщення ─────────────────────────

async def notifier_loop(bot: Bot) -> None:
    """Щохвилини перевіряє, кому пора надіслати бриф або «Час збиратися!»."""
    while True:
        try:
            due = await asyncio.to_thread(collect_due_notifications, local_now())
            for user_id, kind, day_key, text in due:
                try:
                    await bot.send_message(user_id, text)
                    logger.info("Надіслано '%s' користувачу %s", kind, user_id)
                except TelegramForbiddenError:
                    logger.info("Користувач %s заблокував бота — пропускаю", user_id)
                except Exception:
                    logger.exception("Не вдалося надіслати '%s' користувачу %s", kind, user_id)
                    await asyncio.to_thread(release_notification, user_id, kind, day_key)
        except Exception:
            logger.exception("Помилка в циклі сповіщень")
        await asyncio.sleep(60 - local_now().second)  # вирівнюємо на початок хвилини


# ───────────────────────── запуск ─────────────────────────

async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    if not BOT_TOKEN:
        raise RuntimeError(
            "Не знайдено BOT_TOKEN. Створи файл .env з рядком BOT_TOKEN=твій_токен "
            "(див. .env.example)."
        )

    init_db()
    if not os.getenv("GOOGLE_MAPS_API_KEY"):
        logger.warning("Не задано GOOGLE_MAPS_API_KEY — автоматичні маршрути вимкнені "
                       "(працюватимуть лише власні маршрути з /route).")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()  # FSM у пам'яті (MemoryStorage)
    dp.include_router(router)
    dp.include_router(tasks_view.router)   # /tasks, /notes + кнопки списку
    dp.include_router(voice.router)   # голосові → завдання/нотатки

    await bot.set_my_commands([
        BotCommand(command="brief", description="Бриф на сьогодні"),
        BotCommand(command="brieftmorrow", description="Бриф на завтра"),
        BotCommand(command="schedule", description="Завантажити розклад групи"),
        BotCommand(command="tasks", description="Мої завдання та нотатки"),
        BotCommand(command="week", description="Який зараз тиждень: чисельник чи знаменник"),
        BotCommand(command="profile", description="Мій профіль"),
        BotCommand(command="edit", description="Змінити профіль"),
        BotCommand(command="deletemydata", description="Видалити всі мої дані"),
        BotCommand(command="cancel", description="Скасувати дію"),
    ])

    notifier = asyncio.create_task(notifier_loop(bot))
    scheduler = setup_scheduler(bot)   # APScheduler: бриф о 7:30 + «пара закінчилась»
    logger.info("Бот запущено")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        notifier.cancel()
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот зупинено")