"""
schedule_import.py — розпізнавання розкладу з фото, скріншота, PDF, Excel, тексту чи посилання.

Без aiogram і без БД — усе тестується окремо. Результат — список пар у форматі,
який приймає db.replace_schedule():
    {'day': 1..7, 'lesson_num': int, 'time': '08:30-09:50', 'subject': str,
     'room': str | None, 'week_type': 'every' | 'odd' | 'even'}

Два шляхи розбору:
  1. Текст у простому форматі (див. MANUAL_FORMAT_HELP) — розбирається правилами, без ключів.
  2. Усе інше (фото, PDF, Excel, текст з сайту, посилання) — через мовну модель з підтримкою зображень:
       • GEMINI_API_KEY    — Google Gemini (є безкоштовний тариф), модель у GEMINI_MODEL
       • ANTHROPIC_API_KEY — Claude API (платний), модель у ANTHROPIC_MODEL
     Якщо задано обидва ключі, береться Gemini (змінна LLM_PROVIDER=anthropic перемикає).

Автопошук (import_auto): для будь-якого закладу модель шукає розклад в інтернеті (веб-пошук
Gemini / Claude), бот відкриває знайдені сторінки по черзі й розпізнає розклад. Для Львівської
політехніки спершу пробується пряме посилання (швидше), решта закладів — через пошук.
"""

import base64
import io
import ipaddress
import json
import logging
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from html.parser import HTMLParser
from typing import Optional

from parsers import to_cyrillic_group

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TEXT_CHARS = 60_000
HTTP_TIMEOUT_SEC = 60
LLM_TIMEOUT_SEC = int(os.getenv("LLM_TIMEOUT_SEC", "60"))   # модель може думати довго

# Дзвінок Львівської політехніки (за student.lpnu.ua) (використовується, лише якщо в розкладі немає часу пари).
PAIR_TIMES = {1: ("08:30", "09:50"), 2: ("10:05", "11:25"), 3: ("11:40", "13:00"),
              4: ("13:15", "14:35"), 5: ("14:50", "16:10"), 6: ("16:25", "17:45"),
              7: ("18:00", "19:20")}

DAYS = {"пн": 1, "пон": 1, "понеділок": 1, "вт": 2, "вів": 2, "вівторок": 2,
        "ср": 3, "сер": 3, "середа": 3, "середу": 3, "чт": 4, "чет": 4, "четвер": 4,
        "пт": 5, "пят": 5, "п'ятниця": 5, "пятниця": 5, "п’ятниця": 5,
        "сб": 6, "суб": 6, "субота": 6, "нд": 7, "нед": 7, "неділя": 7}
DAY_NAMES = {1: "Понеділок", 2: "Вівторок", 3: "Середа", 4: "Четвер", 5: "П’ятниця",
             6: "Субота", 7: "Неділя"}
WEEK_LABEL = {"every": "", "odd": " (чисельник)", "even": " (знаменник)"}

MANUAL_FORMAT_HELP = (
    "Простий текстовий формат (працює без розпізнавання, з будь-якого джерела):\n"
    "<code>понеділок\n"
    "1 08:30-09:50 Фізика IV-314\n"
    "2 Математика IV-210 чис\n"
    "вівторок\n"
    "3 Програмування VIII-101 знам</code>\n\n"
    "• рядок з назвою дня починає новий день; далі — номер пари, час (необов’язково), назва, аудиторія\n"
    "• <b>чис</b> / <b>знам</b> в кінці — пара лише в чисельник / знаменник тижня (без позначки — щотижня)"
)


class ScheduleImportError(Exception):
    """Помилка імпорту — текст можна показувати користувачу."""


class ModelBusyError(ScheduleImportError):
    """Модель перевантажена (5xx) або не відповіла вчасно — можна спробувати іншу."""


class RateLimitError(ModelBusyError):
    """HTTP 429: вичерпано ліміт запитів/квоту. Інша модель чи провайдер мають власні ліміти."""


# ───────────────────────── нормалізація ─────────────────────────

def _hhmm(value: Optional[str]) -> Optional[str]:
    m = re.search(r"(\d{1,2})[:.](\d{2})", str(value or ""))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _day_number(value) -> Optional[int]:
    if isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit()):
        n = int(value)
        return n if 1 <= n <= 7 else None
    return DAYS.get(str(value or "").strip().lower().rstrip("."))


def _week_type(value) -> str:
    t = re.sub(r"[\s.()]+", " ", str(value or "").strip().lower()).strip()
    if t in ("odd", "ч", "i", "1") or t.startswith(("чис", "непарн", "1 тиж", "i тиж", "перш")):
        return "odd"
    if t in ("even", "з", "ii", "2") or t.startswith(("знам", "парн", "2 тиж", "ii тиж", "друг")):
        return "even"
    return "every"


def _lesson_num_by_time(start: str) -> Optional[int]:
    """Номер пари за часом початку (найближчий дзвінок, похибка до 20 хв)."""
    h, m = map(int, start.split(":"))
    best = None
    for num, (s, _) in PAIR_TIMES.items():
        sh, sm = map(int, s.split(":"))
        diff = abs((h * 60 + m) - (sh * 60 + sm))
        if diff <= 20 and (best is None or diff < best[0]):
            best = (diff, num)
    return best[1] if best else None


_SUBGROUP = re.compile(r"підгр|\bпг\b", re.IGNORECASE)
# «Сорока Г.М.» / «Сорока Г. М.» — це прізвище з ініціалами (викладач), а не назва предмета
_TEACHER_NAME = re.compile(r"^[А-ЯІЇЄҐ][а-яіїєґ'’]{2,}(?:-[А-ЯІЇЄҐ][а-яіїєґ'’]+)?\s+[А-ЯІЇЄҐ]\.\s?(?:[А-ЯІЇЄҐ]\.?)?$")


def normalize_lessons(raw: list[dict]) -> tuple[list[dict], list[str]]:
    """
    Приводить «сирі» пари (від моделі чи парсера) до формату БД.
    Повертає (пари, попередження). Некоректні пари пропускає з попередженням.
    """
    lessons: list[dict] = []
    warnings: list[str] = []
    seen: dict[tuple, dict] = {}

    for item in raw:
        if not isinstance(item, dict):
            continue
        subject = re.sub(r"\s+", " ", str(item.get("subject") or "")).strip()
        day = _day_number(item.get("day"))
        if not subject or day is None:
            warnings.append(f"Пропустив запис без назви чи дня: {str(item)[:80]}")
            continue
        if _TEACHER_NAME.match(subject):
            warnings.append(f"«{subject}» схоже на прізвище викладача, а не на предмет — пропустив.")
            continue

        start = _hhmm(item.get("start")) or _hhmm(str(item.get("time") or "").split("-")[0])
        end = _hhmm(item.get("end"))
        if not end and item.get("time") and re.search(r"[-–—]", str(item["time"])):
            end = _hhmm(re.split(r"[-–—]", str(item["time"]))[-1])

        num = item.get("lesson_num")
        num = int(num) if str(num).isdigit() and int(num) >= 1 else None
        if num is None and start:
            num = _lesson_num_by_time(start)
        if num is None:
            warnings.append(f"Не зрозумів номер пари: {DAY_NAMES[day]}, {subject} — пропустив.")
            continue
        if not start and num in PAIR_TIMES:
            start, end = PAIR_TIMES[num]
        elif start and not end and num in PAIR_TIMES and PAIR_TIMES[num][0] == start:
            end = PAIR_TIMES[num][1]
        if not start:
            warnings.append(f"Немає часу для пари №{num} ({DAY_NAMES[day]}, {subject}) — пропустив.")
            continue

        room = re.sub(r"\s+", " ", str(item.get("room") or "")).strip() or None
        lesson = {"day": day, "lesson_num": num, "time": f"{start}-{end}" if end else start,
                  "subject": subject, "room": room,
                  "week_type": _week_type(item.get("week") or item.get("week_type"))}

        key = (day, num, lesson["week_type"])
        if key in seen:
            other = seen[key]
            odd_key, even_key = (day, num, "odd"), (day, num, "even")
            two_subjects = other["subject"].casefold() != subject.casefold()
            both_subgroups = bool(_SUBGROUP.search(other["subject"]) and _SUBGROUP.search(subject))
            if (lesson["week_type"] == "every" and two_subjects and not both_subgroups
                    and odd_key not in seen and even_key not in seen):
                # Дві різні пари в один час без позначки тижня — це чисельник і знаменник:
                # перша (верхня / лівa) — чисельник, друга — знаменник.
                del seen[key]
                other["week_type"], lesson["week_type"] = "odd", "even"
                seen[odd_key], seen[even_key] = other, lesson
                lessons.append(lesson)
                warnings.append(
                    f"{DAY_NAMES[day]}, пара {num}: дві пари в один час без позначки тижня — "
                    f"«{other['subject']}» поставив на чисельник, «{subject}» на знаменник. Перевір порядок.")
                continue
            warnings.append(f"Дві пари в один слот ({DAY_NAMES[day]}, пара {num}): "
                            f"залишив «{other['subject']}», пропустив «{subject}».")
            continue
        seen[key] = lesson
        lessons.append(lesson)

    order = {"every": 0, "odd": 1, "even": 2}
    lessons.sort(key=lambda l: (l["day"], l["lesson_num"], order[l["week_type"]]))
    return lessons, warnings


# ───────────────────────── простий текстовий формат ─────────────────────────

_ROOM_TOKEN = re.compile(r"^(?:[IVXІХ]{1,4}|\d{1,2})\s*[-–/]\s*\d{1,4}[A-Za-zА-Яа-я]?$")
_LESSON_LINE = re.compile(
    r"^\s*(\d)\s*[.)]?\s*(?:(\d{1,2}[:.]\d{2})\s*[-–—]\s*(\d{1,2}[:.]\d{2}))?\s*(.+?)\s*$")


def parse_manual_text(text: str) -> Optional[list[dict]]:
    """
    Розбір простого формату правилами. None — якщо текст не схожий на цей формат
    (тоді його варто віддати моделі).
    """
    raw: list[dict] = []
    day: Optional[int] = None
    understood = total = 0

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        total += 1
        day_word = DAYS.get(line.lower().rstrip(":. "))
        if day_word:
            day = day_word
            understood += 1
            continue
        m = _LESSON_LINE.match(line)
        if not m or day is None:
            continue
        num, start, end, rest = m.groups()

        week = "every"
        wm = re.search(r"\s(чис\w*|знам\w*)\.?$", rest, re.IGNORECASE)
        if wm:
            week = "odd" if wm.group(1).lower().startswith("чис") else "even"
            rest = rest[: wm.start()].strip()

        room = None
        tokens = rest.split()
        if len(tokens) >= 2 and _ROOM_TOKEN.match(tokens[-1]):
            room, rest = tokens[-1], " ".join(tokens[:-1])
        elif len(tokens) >= 3 and re.fullmatch(r"ауд\.?", tokens[-2], re.IGNORECASE):
            room, rest = tokens[-1], " ".join(tokens[:-2])

        raw.append({"day": day, "lesson_num": int(num), "start": start, "end": end,
                    "subject": rest, "room": room, "week": week})
        understood += 1

    if not raw or understood < max(1, int(total * 0.8)):
        return None
    return raw


# ───────────────────────── підготовка вхідних даних ─────────────────────────

class _TextExtractor(HTMLParser):
    """HTML → текст із рядками таблиць, розділеними « | »."""
    _SKIP = {"script", "style", "noscript", "head", "svg"}
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "table", "section"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    return text.strip()


def xlsx_to_text(data: bytes) -> str:
    try:
        import openpyxl
    except ImportError as exc:
        raise ScheduleImportError("Для Excel потрібен пакет openpyxl (pip install openpyxl).") from exc
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as exc:
        raise ScheduleImportError("Не вдалося відкрити Excel-файл (підтримується лише .xlsx).") from exc
    lines: list[str] = []
    for ws in wb.worksheets:
        lines.append(f"### Аркуш: {ws.title}")
        for row in ws.iter_rows(values_only=True):
            cells = [re.sub(r"\s+", " ", str(c)).strip() for c in row if c not in (None, "")]
            if cells:
                lines.append(" | ".join(cells))
    return "\n".join(lines)


def _public_host_check(url: str) -> None:
    """Захист від SSRF: бот не повинен ходити на локальні та внутрішні адреси сервера."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ScheduleImportError("Посилання має починатися з http:// або https://")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    except socket.gaierror as exc:
        raise ScheduleImportError("Не вдалося відкрити посилання (невідома адреса).") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ScheduleImportError("Це посилання веде на внутрішню адресу — його не відкриваю.")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _public_host_check(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def docx_to_text(data: bytes) -> str:
    """Word (.docx) → текст без додаткових бібліотек (таблиці: клітинки через « | »)."""
    import zipfile
    from html import unescape
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read("word/document.xml").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ScheduleImportError("Не вдалося відкрити Word-файл (.docx).") from exc
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"</w:tc>", " | ", xml)
    xml = re.sub(r"<w:tab/>", " ", xml)
    text = unescape(re.sub(r"<[^>]+>", "", xml))
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def _download(url: str) -> tuple[str, bytes, str, str]:
    """Безпечно завантажує url → (content-type, байти, charset, кінцева адреса після редіректів)."""
    _public_host_check(url)
    opener = urllib.request.build_opener(_SafeRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (student-bot)"})
    try:
        with opener.open(request, timeout=HTTP_TIMEOUT_SEC) as resp:
            ctype = (resp.headers.get_content_type() or "").lower()
            data = resp.read(MAX_FILE_BYTES + 1)
            charset = resp.headers.get_content_charset() or "utf-8"
            final_url = resp.geturl() or url
    except urllib.error.HTTPError as exc:
        raise ScheduleImportError(f"Сайт відповів помилкою {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ScheduleImportError("Не вдалося завантажити посилання.") from exc
    if len(data) > MAX_FILE_BYTES:
        raise ScheduleImportError("Файл за посиланням завеликий (понад 10 МБ).")
    return ctype, data, charset, final_url


def _parts_from_bytes(ctype: str, data: bytes, charset: str, url: str = "") -> list[tuple]:
    """Вміст завантаженого файлу/сторінки → частини для моделі."""
    if ctype in ("", "application/octet-stream", "binary/octet-stream"):   # тип не вказано — за розширенням
        path = urllib.parse.urlparse(url).path.lower()
        ctype = ("application/pdf" if path.endswith(".pdf") else _XLSX_MIME if path.endswith((".xlsx", ".xlsm"))
                 else _DOCX_MIME if path.endswith(".docx") else "image/png" if path.endswith(".png")
                 else "image/jpeg" if path.endswith((".jpg", ".jpeg")) else ctype)
    if ctype == "application/pdf":
        return [("pdf", data)]
    if ctype.startswith("image/"):
        return [("image", ctype, data)]
    if ctype == _XLSX_MIME:
        return [("text", xlsx_to_text(data)[:MAX_TEXT_CHARS])]
    if ctype == _DOCX_MIME:
        return [("text", docx_to_text(data)[:MAX_TEXT_CHARS])]
    text = html_to_text(data.decode(charset, "replace"))
    if len(text) < 30:
        raise ScheduleImportError(
            "На сторінці немає видимого тексту (схоже, розклад підвантажується скриптом). "
            "Надішли скріншот сторінки або збережи її як PDF.")
    return [("text", text[:MAX_TEXT_CHARS])]


def fetch_url(url: str) -> list[tuple]:
    """Завантажує сторінку/файл за посиланням → частини для моделі."""
    ctype, data, charset, final_url = _download(url)
    return _parts_from_bytes(ctype, data, charset, final_url)


# ───────────────────────── мовна модель ─────────────────────────

def llm_provider() -> Optional[str]:
    choice = os.getenv("LLM_PROVIDER", "").lower()
    if choice == "anthropic" and os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    if choice == "gemini" and os.getenv("GEMINI_API_KEY"):
        return "gemini"
    if os.getenv("GEMINI_API_KEY"):
        return "gemini"
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    return None


def _base_prompt(group: str) -> str:
    return (
        "Це розклад занять університету (фото, скріншот, документ або текст). Витягни ВСІ пари "
        f"для групи {group} (якщо в розкладі кілька груп — лише цієї; якщо група одна — бери її).\n"
        "Поверни ТІЛЬКИ JSON без пояснень у форматі:\n"
        '{"lessons":[{"day":1,"lesson_num":1,"start":"08:30","end":"09:50",'
        '"subject":"Фізика","room":"IV-314","week":"every"}]}\n'
        "Правила:\n"
        "- day: 1=понеділок … 7=неділя.\n"
        "- lesson_num: номер пари; якщо його немає, лиши null, але вкажи start.\n"
        "- week: \"odd\" для чисельника, \"even\" для знаменника, \"every\" — щотижня.\n"
        "- ЧИСЕЛЬНИК/ЗНАМЕННИК: це різні пари в той самий день і час, що чергуються по тижнях. "
        "Ознаки: підписи «чис»/«знам», «чисельник»/«знаменник», «ч»/«з», «1 тиждень»/«2 тиждень», "
        "«непарний»/«парний»; клітинка, розділена навпіл (верх = чисельник, низ = знаменник; "
        "зліва = чисельник, справа = знаменник); два різні предмети в одному часі. "
        "КОЖНУ таку пару виведи окремим записом з тим самим day і lesson_num, але week=\"odd\" "
        "для однієї і week=\"even\" для іншої. НІКОЛИ не став \"every\" обом парам одного слота "
        "і нічого не пропускай. \"every\" — лише коли в цей час одна пара без жодної позначки тижня.\n"
        "- room: аудиторія і корпус так, як написано (наприклад «IV-314», «215, 4 корп.»); "
        "якщо немає — null.\n"
        "- subject: назва предмета; тип заняття (лекція/практична/лабораторна) і підгрупу "
        "додай у дужках до назви, якщо вони вказані.\n"
        "- Прізвище викладача (на кшталт «Сорока Г.М.») — НЕ предмет: не створюй для нього окремий запис "
        "і не клади його в subject.\n"
        "- Нічого не вигадуй. Порожні клітинки пропускай. Дві пари в один слот (різні тижні) "
        "позначай через week."
    )


MAX_RETRY_WAIT_SEC = 30   # якщо API просить почекати довше — не чекаємо, а переходимо на іншу модель


def _quota_hint(url: str, detail: str) -> str:
    """Коротка підказка, що саме вичерпано (модель, тип квоти, ліміт) — для логів і повідомлення."""
    parts = []
    m = re.search(r"/models/([\w.\-]+)", url)
    parts.append(m.group(1) if m else urllib.parse.urlparse(url).hostname or "")
    m = re.search(r'"?quota(?:Id|Metric)"?\s*[:=]\s*"([^"]+)"', detail)
    if m:
        parts.append(m.group(1))
    m = re.search(r"limit:\s*(\d+)", detail)
    if m:
        parts.append(f"ліміт {m.group(1)}")
    parts = [x for x in parts if x]
    return f" ({', '.join(parts)})" if parts else ""


def _retry_delay(exc: urllib.error.HTTPError, detail: str) -> Optional[float]:
    """Скільки секунд просить почекати API при 429. None — якщо це денна квота (чекати марно)."""
    if re.search(r"per\s*day|PerDay|daily", detail, re.IGNORECASE):
        return None
    header = exc.headers.get("Retry-After") if exc.headers else None
    if header and header.strip().isdigit():
        return float(header)
    m = re.search(r'"retryDelay"\s*:\s*"([\d.]+)s"', detail)
    return float(m.group(1)) if m else None


def _prompt(group: str, strict: bool = False) -> str:
    text = _base_prompt(group)
    if strict:   # файл знайдено серед багатьох на сторінці — групи в ньому може не бути
        text += (f"\n- ВАЖЛИВО: цей документ міг бути й не для групи {group}. Якщо її в документі немає, "
                 'поверни {"lessons":[]} — навіть якщо в документі лише одна інша група. Не підставляй чужу.')
    return text


def _post_json(url: str, headers: dict, body: dict, timeout: Optional[int] = None,
               _retried: bool = False) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout or LLM_TIMEOUT_SEC) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:1500]
        logger.error("LLM HTTP %s: %s", exc.code, detail)
        if exc.code in (401, 403):
            raise ScheduleImportError("Ключ для розпізнавання не підходить (перевір .env).") from exc
        if exc.code == 429:
            delay = _retry_delay(exc, detail)
            if not _retried and delay is not None and delay <= MAX_RETRY_WAIT_SEC:
                logger.warning("Ліміт запитів — чекаю %.0f с і повторюю", delay + 1)
                time.sleep(delay + 1)
                return _post_json(url, headers, body, timeout, _retried=True)
            raise RateLimitError(f"Ліміт розпізнавання вичерпано{_quota_hint(url, detail)}, спробуй пізніше.") from exc
        if exc.code >= 500:
            raise ModelBusyError("Сервіс розпізнавання зараз перевантажений.") from exc
        raise ScheduleImportError(f"Сервіс розпізнавання відповів помилкою {exc.code}.") from exc
    except TimeoutError as exc:
        logger.error("LLM не відповів за %s с", LLM_TIMEOUT_SEC)
        raise ModelBusyError("Модель надто довго обробляє розклад. Спробуй ще раз.") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.error("LLM недоступний: %s", exc)
        raise ScheduleImportError("Сервіс розпізнавання зараз недоступний.") from exc


def _gemini_generation_config() -> dict:
    """Параметри генерації. GEMINI_THINKING_LEVEL (наприклад low/minimal) прискорює відповідь."""
    config = {"temperature": 0, "responseMimeType": "application/json", "maxOutputTokens": 16000}
    level = os.getenv("GEMINI_THINKING_LEVEL", "").strip()
    if level:
        config["thinkingConfig"] = {"thinkingLevel": level}
    return config


# Моделі Gemini за порядком спроб. Перша — з GEMINI_MODEL (якщо задано), решта — запасні
# (GEMINI_FALLBACK_MODELS через кому). Якщо модель перевантажена (503) — береться наступна.
GEMINI_DEFAULT_MODELS = ["gemini-3-flash-preview", "gemini-3.1-flash-lite-preview"]


def _gemini_models() -> list[str]:
    main = os.getenv("GEMINI_MODEL", "").strip()
    extra = [m.strip() for m in os.getenv("GEMINI_FALLBACK_MODELS", "").split(",") if m.strip()]
    models = ([main] if main else []) + extra + GEMINI_DEFAULT_MODELS
    return list(dict.fromkeys(models))


def _call_gemini(parts: list[tuple], prompt: str) -> str:
    last_error: Optional[Exception] = None
    for model in _gemini_models():
        try:
            return _call_gemini_model(model, parts, prompt)
        except ModelBusyError as exc:
            logger.warning("Модель %s недоступна (%s) — пробую наступну", model, exc)
            last_error = exc
    raise last_error or ScheduleImportError("Немає доступних моделей розпізнавання.")


def _call_gemini_model(model: str, parts: list[tuple], prompt: str) -> str:
    content = []
    for part in parts:
        if part[0] == "text":
            content.append({"text": part[1]})
        elif part[0] == "image":
            content.append({"inline_data": {"mime_type": part[1],
                                            "data": base64.b64encode(part[2]).decode()}})
        elif part[0] == "pdf":
            content.append({"inline_data": {"mime_type": "application/pdf",
                                            "data": base64.b64encode(part[1]).decode()}})
    content.append({"text": prompt})
    data = _post_json(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        {"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
        {"contents": [{"parts": content}], "generationConfig": _gemini_generation_config()})
    try:
        return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    except (KeyError, IndexError, TypeError) as exc:
        raise ScheduleImportError("Модель не повернула відповідь (можливо, зображення заблоковано).") from exc


def _call_anthropic(parts: list[tuple], prompt: str) -> str:
    model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    content = []
    for part in parts:
        if part[0] == "text":
            content.append({"type": "text", "text": part[1]})
        elif part[0] == "image":
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": part[1],
                "data": base64.b64encode(part[2]).decode()}})
        elif part[0] == "pdf":
            content.append({"type": "document", "source": {
                "type": "base64", "media_type": "application/pdf",
                "data": base64.b64encode(part[1]).decode()}})
    content.append({"type": "text", "text": prompt})
    data = _post_json(
        "https://api.anthropic.com/v1/messages",
        {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
        {"model": model, "max_tokens": 8000, "temperature": 0,
         "messages": [{"role": "user", "content": content}]})
    try:
        return "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
    except (KeyError, TypeError) as exc:
        raise ScheduleImportError("Модель не повернула відповідь.") from exc


def parse_llm_json(text: str) -> list[dict]:
    """Дістає список пар із відповіді моделі (допускає ```json ... ``` і голий список)."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}|\[.*\]", cleaned, re.DOTALL)
        if not m:
            raise ScheduleImportError("Модель повернула відповідь не у форматі розкладу.")
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError as exc:
            raise ScheduleImportError("Не вдалося розібрати відповідь моделі.") from exc
    lessons = data.get("lessons") if isinstance(data, dict) else data
    if not isinstance(lessons, list):
        raise ScheduleImportError("У відповіді моделі немає списку пар.")
    return lessons


def _providers_in_order(primary: str) -> list[str]:
    """Основний провайдер, а за ним інший — якщо для нього є ключ (на випадок ліміту)."""
    keys = {"gemini": "GEMINI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
    return [primary] + [p for p, k in keys.items() if p != primary and os.getenv(k)]


def _dump_debug(parts: list[tuple], group: str, provider: str, answer: str) -> None:
    """SCHEDULE_DEBUG=1: дописує в schedule_debug.txt текст, який побачила модель, і її відповідь."""
    path = os.getenv("SCHEDULE_DEBUG_FILE", "schedule_debug.txt")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n{'=' * 70}\nГрупа: {group} | провайдер: {provider}\n")
            for part in parts:
                if part[0] == "text":
                    f.write(f"--- ТЕКСТ, що бачить модель ({len(part[1])} символів) ---\n{part[1][:30000]}\n")
                else:
                    f.write(f"--- {part[0]} (файл, {len(part[-1])} байт) ---\n")
            f.write(f"--- ВІДПОВІДЬ МОДЕЛІ ---\n{answer}\n")
    except OSError as exc:
        logger.warning("Не вдалося записати %s: %s", path, exc)


def extract_with_llm(parts: list[tuple], group: str, strict: bool = False) -> list[dict]:
    provider = llm_provider()
    if provider is None:
        raise ScheduleImportError(
            "Щоб читати фото, PDF, Excel і текст з сайтів, потрібен ключ розпізнавання "
            "(GEMINI_API_KEY або ANTHROPIC_API_KEY у .env). Без нього можна надіслати розклад "
            "простим текстом — формат у /schedule.")
    size = sum(len(p[1]) if p[0] == "text" else len(p[-1]) for p in parts)
    logger.info("Відправляю моделі (%s): %d символів/байтів", provider, size)
    started = time.monotonic()
    answer = None
    last_error: Optional[Exception] = None
    for prov in _providers_in_order(provider):
        try:
            answer = _call_gemini(parts, _prompt(group, strict)) if prov == "gemini" \
                else _call_anthropic(parts, _prompt(group, strict))
            break
        except ModelBusyError as exc:          # ліміт чи перевантаження — пробуємо іншого провайдера
            logger.warning("Провайдер %s не відповів (%s)", prov, exc)
            last_error = exc
    if answer is None:
        raise last_error or ScheduleImportError("Сервіс розпізнавання недоступний.")
    logger.info("Модель відповіла за %.1f с", time.monotonic() - started)
    if os.getenv("SCHEDULE_DEBUG", "").lower() in ("1", "true", "yes"):
        _dump_debug(parts, group, provider, answer)
    return parse_llm_json(answer)


# ───────────────────────── високорівневі входи ─────────────────────────

def _finish(raw: list[dict]) -> tuple[list[dict], list[str]]:
    lessons, warnings = normalize_lessons(raw)
    if not lessons:
        raise ScheduleImportError("Не знайшов у цьому жодної пари. Спробуй чіткіше фото "
                                  "або надішли розклад текстом.")
    return lessons, warnings


def import_from_text(text: str, group: str) -> tuple[list[dict], list[str]]:
    """Текст: спершу простий формат, інакше (наприклад, скопійоване з сайту) — модель."""
    text = text.strip()
    if text.lower().startswith(("http://", "https://")) and " " not in text:
        return import_from_url(text, group)
    manual = parse_manual_text(text)
    if manual is not None:
        return _finish(manual)
    return _finish(extract_with_llm([("text", text[:MAX_TEXT_CHARS])], group))


# Шаблон сторінки розкладу. {group} — код групи (підставляється закодованим), {semester} — 1 або 2.
# Для Політехніки це припущення про структуру сайту (я не міг його перевірити) — якщо адреса інша,
# задай правильну в .env: SCHEDULE_URL_TEMPLATE=https://...{group}...{semester}...
LPNU_URL_TEMPLATE = ("https://student.lpnu.ua/students_schedule"
                     "?studygroup_abbrname={group}&semestr={semester}&semestrduration=1")


def auto_schedule_url(university: str, group: str, today: Optional[date] = None) -> Optional[str]:
    """Адреса сторінки розкладу групи на сайті закладу; None — якщо заклад невідомий."""
    template = os.getenv("SCHEDULE_URL_TEMPLATE")
    if not template and re.search(r"політех|лпну|нулп|lpnu", (university or "").lower()):
        template = LPNU_URL_TEMPLATE
    if not template:
        return None
    today = today or date.today()
    semester = 1 if today.month >= 8 or today.month == 1 else 2   # осінній: серпень–січень
    return template.format(group=urllib.parse.quote(to_cyrillic_group(group)),
                           semester=semester)


# ───────────────────────── пошук розкладу в інтернеті ─────────────────────────

SEARCH_TIMEOUT_SEC = int(os.getenv("SEARCH_TIMEOUT_SEC", "90"))          # пошук довший за звичайний запит
MAX_SEARCH_CANDIDATES = int(os.getenv("SCHEDULE_SEARCH_CANDIDATES", "4"))  # скільки знайдених посилань пробувати

_URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+")
_BAD_HOST_SUFFIXES = ("bing.com", "duckduckgo.com", "youtube.com", "youtu.be", "facebook.com", "instagram.com", "t.me",
                      "wikipedia.org", "tiktok.com", "x.com", "twitter.com")
_LOOKALIKES = str.maketrans("abcehikmoptxyABCEHIKMOPTXY", "авсенікмортху" "АВСЕНІКМОРТХУ")


def _fold(text: str) -> str:
    """Для порівняння груп: нижній регістр, латинські двійники → кирилиця, лише літери й цифри."""
    return re.sub(r"[\W_]+", "", (text or "").translate(_LOOKALIKES).casefold())


def _useful_url(url: str) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if not host or host.endswith(_BAD_HOST_SUFFIXES):
        return False
    return host not in ("google.com", "www.google.com")


def _extract_urls(text: str) -> list[str]:
    return [u.rstrip(".,;:!?»") for u in _URL_RE.findall(text or "")]


class _UrlList(list):
    """Список посилань + звідки вони (джерело) і чому не спрацював пошук через модель."""
    source: str = ""
    note: str = ""


def _search_prompt(university: str, group: str, today: date) -> str:
    return (
        f"Сьогодні {today:%d.%m.%Y}. Знайди в інтернеті актуальний розклад занять (поточний семестр) "
        f"для групи {group} закладу «{university}».\n"
        "Потрібні ПРЯМІ посилання на сторінку, PDF або Excel-файл саме з розкладом (не головна сторінка "
        "сайту, не новини). Якщо розклад опублікований лише для факультету чи курсу — дай посилання на нього.\n"
        "Відповідь: до 5 посилань, по одному в рядку, найімовірніше першим, без жодних пояснень."
    )


def _gemini_search_models() -> list[str]:
    """GEMINI_SEARCH_MODELS (через кому) — моделі саме для пошуку; далі звичайний список."""
    own = [m.strip() for m in os.getenv("GEMINI_SEARCH_MODELS", "").split(",") if m.strip()]
    return list(dict.fromkeys(own + _gemini_models()))


def _search_gemini(prompt: str) -> list[str]:
    last_error: Optional[Exception] = None
    for model in _gemini_search_models():
        try:
            data = _post_json(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                {"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
                {"contents": [{"parts": [{"text": prompt}]}],
                 "tools": [{"google_search": {}}],
                 "generationConfig": {"temperature": 0}},
                SEARCH_TIMEOUT_SEC)
        except ModelBusyError as exc:
            logger.warning("Модель %s недоступна для пошуку (%s) — пробую наступну", model, exc)
            last_error = exc
            continue
        candidate = (data.get("candidates") or [{}])[0]
        text = "".join(p.get("text", "") for p in (candidate.get("content") or {}).get("parts", []))
        urls = _extract_urls(text)
        # запасний варіант: джерела, які модель реально відкривала (це redirect-посилання Google)
        for chunk in (candidate.get("groundingMetadata") or {}).get("groundingChunks", []):
            uri = (chunk.get("web") or {}).get("uri")
            if uri:
                urls.append(uri)
        return urls
    raise last_error or ScheduleImportError("Немає доступних моделей для пошуку.")


def _search_anthropic(prompt: str) -> list[str]:
    data = _post_json(
        "https://api.anthropic.com/v1/messages",
        {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
        {"model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5"), "max_tokens": 2000,
         "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
         "messages": [{"role": "user", "content": prompt}]},
        SEARCH_TIMEOUT_SEC)
    urls: list[str] = []
    found: list[str] = []
    for block in data.get("content", []):
        if block.get("type") == "text":
            urls += _extract_urls(block.get("text", ""))
        elif block.get("type") == "web_search_tool_result" and isinstance(block.get("content"), list):
            found += [r["url"] for r in block["content"] if isinstance(r, dict) and r.get("url")]
    return urls + found


_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _fetch_search_page(url: str) -> Optional[str]:
    """Сторінка видачі пошуковика або None (з поясненням у лозі: HTTP-помилка, капча, недоступність)."""
    host = urllib.parse.urlparse(url).hostname
    request = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA,
                                                   "Accept-Language": "uk,en;q=0.8"})
    try:
        with urllib.request.urlopen(request, timeout=20) as resp:
            page = resp.read(600_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        logger.warning("Пошуковик %s: HTTP %s", host, exc.code)
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("Пошуковик %s недоступний: %s", host, exc)
        return None
    if re.search(r"anomaly|captcha|unusual traffic", page[:20000], re.IGNORECASE):
        logger.warning("Пошуковик %s показав перевірку на робота (капчу) — результатів немає", host)
        return None
    return page


def _strip_tags(fragment: str) -> str:
    from html import unescape
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()[:1500]


def _ddg_results(page: str) -> list[tuple[str, str]]:
    """[(url, текст результату: заголовок + опис)] зі сторінки DuckDuckGo."""
    from html import unescape
    tags = list(re.finditer(r"<a\b[^>]*result__a[^>]*>", page))
    out = []
    for i, m in enumerate(tags):
        hm = re.search(r'href="([^"]+)"', m.group(0))
        if not hm:
            continue
        href = unescape(hm.group(1))
        target = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [href])[0]
        if target.startswith(("http://", "https://")):
            end = tags[i + 1].start() if i + 1 < len(tags) else len(page)
            out.append((target, _strip_tags(page[m.end():end])))
    return out


def _bing_results(page: str) -> list[tuple[str, str]]:
    """[(url, текст результату)] зі сторінки Bing."""
    from html import unescape
    out = []
    for block in re.findall(r'<li class="b_algo".*?</li>', page, re.DOTALL):
        m = re.search(r'<a[^>]+href="(https?://[^"]+)"', block)
        if not m:
            continue
        href = unescape(m.group(1))
        if "bing.com/ck/a" in href:      # redirect: параметр u = "a1" + base64 адреси
            encoded = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("u", [""])[0]
            if not encoded.startswith("a1"):
                continue
            try:
                href = base64.urlsafe_b64decode(encoded[2:] + "=" * (-len(encoded[2:]) % 4)).decode()
            except (ValueError, UnicodeDecodeError):
                continue
        out.append((href, _strip_tags(block)))
    return out


def _relevant(text: str, university: str, group: str) -> bool:
    """Результат пошуковика підходить, лише якщо в ньому є група або (майже) назва закладу."""
    folded = _fold(text)
    if _fold(group) in folded:
        return True
    stems = {_fold(w)[:5] for w in re.findall(r"\w{4,}", university or "")}
    stems.discard("")
    if not stems:
        return False
    return sum(1 for st in stems if st in folded) >= min(2, len(stems))


def _search_queries(university: str, group: str) -> list[str]:
    """Пошукові запити: «<заклад> розклад <група>» (назва закладу першою, без лапок)."""
    university = re.sub(r"\s+", " ", university or "").strip()
    return [f"{university} розклад {group}".strip(),
            f"{university} розклад занять групи {group}".strip()]


def _search_api(university: str, group: str) -> list[str]:
    """Пошуковий API, якщо в .env є ключ: TAVILY_API_KEY або BRAVE_API_KEY. Не залежить від квоти моделі."""
    tavily, brave = os.getenv("TAVILY_API_KEY"), os.getenv("BRAVE_API_KEY")
    urls: list[str] = []
    for query in _search_queries(university, group):
        logger.info("Пошуковий запит (API): %s", query)
        try:
            if tavily:
                data = _post_json("https://api.tavily.com/search", {"Authorization": f"Bearer {tavily}"},
                                  {"query": query, "max_results": 8}, 30)
                urls += [r["url"] for r in data.get("results", []) if r.get("url")]
            elif brave:
                request = urllib.request.Request(
                    "https://api.search.brave.com/res/v1/web/search?"
                    + urllib.parse.urlencode({"q": query, "count": 10}),
                    headers={"X-Subscription-Token": brave, "Accept": "application/json"})
                with urllib.request.urlopen(request, timeout=20) as resp:
                    data = json.load(resp)
                urls += [r["url"] for r in (data.get("web") or {}).get("results", []) if r.get("url")]
        except (ScheduleImportError, urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            logger.warning("Пошуковий API не відповів: %s", exc)
    return urls


def _search_web(university: str, group: str) -> list[str]:
    """Звичайні пошуковики (без ключів і без квоти моделі): DuckDuckGo, потім Bing. Лише релевантні результати."""
    queries = _search_queries(university, group)
    engines = (
        ("duckduckgo", "https://html.duckduckgo.com/html/?" + "{q}", _ddg_results),
        ("bing", "https://www.bing.com/search?setlang=uk&" + "{q}", _bing_results),
    )
    for name, template, parse in engines:
        found: list[tuple[str, str]] = []
        for query in queries:
            logger.info("Пошуковий запит (%s): %s", name, query)
            page = _fetch_search_page(template.format(q=urllib.parse.urlencode({"q": query})))
            if page:
                found += parse(page)
        kept = [url for url, text in found if _relevant(text, university, group)]
        logger.info("Пошуковик %s: знайдено %d, схожих на запит %d", name, len(found), len(kept))
        if kept:
            return kept
    return []


def find_schedule_urls(university: str, group: str, today: Optional[date] = None) -> list[str]:
    """
    Шукає в інтернеті сторінки/файли з розкладом групи. Спершу — модель із веб-пошуком (точніше),
    а якщо вона недоступна чи ліміт вичерпано — звичайний пошуковик (не витрачає квоту моделі).
    """
    provider = llm_provider()
    if provider is None:
        raise ScheduleImportError(
            "Щоб шукати розклад в інтернеті, потрібен ключ GEMINI_API_KEY або ANTHROPIC_API_KEY у .env.")
    prompt = _search_prompt(university, group, today or date.today())
    started = time.monotonic()
    raw: list[str] = []
    last_error: Optional[Exception] = None
    for prov in _providers_in_order(provider):
        try:
            raw = _search_gemini(prompt) if prov == "gemini" else _search_anthropic(prompt)
            provider = prov
            break
        except ScheduleImportError as exc:     # ліміт, пошук недоступний тощо — пробуємо іншого провайдера
            logger.warning("Пошук через %s не вдався: %s", prov, exc)
            last_error = exc
    source = "пошук через модель"
    if not raw and (os.getenv("TAVILY_API_KEY") or os.getenv("BRAVE_API_KEY")):
        raw = _search_api(university, group)
        provider, source = "api", "пошуковий API"
    if not raw:
        raw = _search_web(university, group)
        provider = "web"
        source = "звичайний пошуковик"
        if not raw and last_error:
            raise ScheduleImportError(
                f"пошук через модель недоступний ({last_error}), а звичайні пошуковики не знайшли нічого, схожого на розклад цієї групи")
    logger.info("Пошук розкладу (%s) зайняв %.1f с, знайдено посилань: %d",
                provider, time.monotonic() - started, len(raw))
    result = _UrlList(u for u in dict.fromkeys(raw) if _useful_url(u))
    result.source = source
    result.note = str(last_error) if last_error else ""
    return result


MAX_CRAWL_DOWNLOADS = int(os.getenv("SCHEDULE_CRAWL_FILES", "8"))   # скільки файлів за посиланнями відкривати
MAX_CRAWL_LLM = int(os.getenv("SCHEDULE_CRAWL_LLM", "3"))           # скільки з них віддавати моделі
_FILE_EXTS = (".pdf", ".xlsx", ".xlsm", ".docx", ".png", ".jpg", ".jpeg")


class _LinkCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: Optional[str] = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href, self._text = dict(attrs).get("href"), []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, re.sub(r"\s+", " ", " ".join(self._text)).strip()))
            self._href = None


def _normalize_link(url: str) -> str:
    """Google Таблиці / Документи / Диск → пряме посилання на завантаження."""
    m = re.match(r"https://docs\.google\.com/spreadsheets/d/([\w-]+)", url)
    if m:
        return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=xlsx"
    m = re.match(r"https://docs\.google\.com/document/d/([\w-]+)", url)
    if m:
        return f"https://docs.google.com/document/d/{m.group(1)}/export?format=pdf"
    m = re.match(r"https://drive\.google\.com/(?:file/d/|open\?id=|uc\?(?:export=\w+&)?id=)([\w-]+)", url)
    if m:
        return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    return url


def _rank_links(links: list[tuple[str, str]], base: str, group: str) -> list[str]:
    """Посилання зі сторінки, які варто відкрити: файли розкладів і все, де згадано групу. Найкращі першими."""
    gfold, scored, seen = _fold(group), [], {base}
    for href, text in links:
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urllib.parse.urljoin(base, href.strip())
        if not absolute.startswith(("http://", "https://")) or absolute in seen:
            continue
        seen.add(absolute)
        target = _normalize_link(absolute)
        is_file = target != absolute or urllib.parse.urlparse(absolute).path.lower().endswith(_FILE_EXTS)
        label = f"{text} {urllib.parse.unquote(absolute)}"
        has_group = gfold in _fold(label)
        if not (is_file or has_group):
            continue
        score = (100 if has_group else 0) + (10 if is_file else 0) \
            + (5 if re.search(r"розклад|заняття|курс|відділ|семестр|rozklad|schedule", label.lower()) else 0)
        scored.append((score, target))
    return [t for _, t in sorted(scored, key=lambda x: -x[0])]


def _pdf_text(data: bytes) -> Optional[str]:
    """Текст PDF локально (якщо встановлено pypdf) — щоб не віддавати моделі файл без нашої групи."""
    try:
        from pypdf import PdfReader
        text = "\n".join((pg.extract_text() or "") for pg in PdfReader(io.BytesIO(data)).pages[:40])
    except Exception:  # noqa: BLE001 — немає pypdf, зашифрований чи пошкоджений PDF
        return None
    return text if len(text.strip()) > 50 else None


def _local_text(parts: list[tuple]) -> Optional[str]:
    """Весь текст, який можна отримати без моделі. None — якщо файл не прочитати локально (скан, фото)."""
    chunks = []
    for part in parts:
        if part[0] == "text":
            chunks.append(part[1])
        elif part[0] == "pdf":
            text = _pdf_text(part[1])
            if text is None:
                return None
            chunks.append(text)
        else:
            return None
    return "\n".join(chunks)


def _import_from_linked_files(url: str, group: str, budget: dict) -> Optional[tuple[list[dict], list[str]]]:
    """
    Сторінка — це каталог (список посилань на PDF/Word/Excel/Google-файли розкладів). Відкриваємо найімовірніші
    файли по черзі, і якщо в ньому є наша група — розпізнаємо. Ліміти — у budget (завантаження і виклики моделі).
    """
    ctype, data, charset, final_url = _download(url)
    if ctype not in ("text/html", "application/xhtml+xml"):
        return None
    collector = _LinkCollector()
    collector.feed(data.decode(charset, "replace"))
    for link in _rank_links(collector.links, final_url, group):
        if budget["downloads"] <= 0 or budget["llm"] <= 0:
            break
        budget["downloads"] -= 1
        try:
            l_ctype, l_data, l_charset, l_final = _download(link)
            parts = _parts_from_bytes(l_ctype, l_data, l_charset, l_final)
        except ScheduleImportError as exc:
            logger.info("Файл %s пропущено: %s", link, exc)
            continue
        local = _local_text(parts)
        if local is not None and _fold(group) not in _fold(local):
            logger.info("У файлі %s групи %s немає", link, group)
            continue
        budget["llm"] -= 1
        logger.info("Розпізнаю файл за посиланням: %s", link)
        try:
            return _finish(extract_with_llm(parts, group, strict=True))
        except RateLimitError:
            raise
        except ScheduleImportError as exc:
            logger.info("З файлу %s розклад не вийшов: %s", link, exc)
    return None


def _import_from_candidate(url: str, group: str, trusted: bool,
                           budget: Optional[dict] = None) -> tuple[list[dict], list[str]]:
    """
    Відкриває одне посилання і розпізнає розклад. Для знайдених пошуком сторінок перевіряє, що там є наша група;
    якщо сторінка — лише каталог файлів, пробує файли за посиланнями (budget обмежує їх кількість).
    """
    parts = fetch_url(url)
    if not trusted:
        texts = [p[1] for p in parts if p[0] == "text"]
        in_url = _fold(group) in _fold(urllib.parse.unquote(url))   # напр. ...?group=ДШВ-11
        if texts and not in_url and _fold(group) not in _fold(" ".join(texts)):
            reason = f"на сторінці немає групи {group}"
            if budget is not None:
                before = budget["downloads"]
                try:
                    result = _import_from_linked_files(url, group, budget)
                except RateLimitError:
                    raise
                except ScheduleImportError as exc:
                    logger.info("Не вдалося обійти посилання сторінки %s: %s", url, exc)
                    result = None
                if result:
                    return result
                if budget["downloads"] < before:
                    reason += f" (і в {before - budget['downloads']} файлах за її посиланнями теж)"
            raise ScheduleImportError(reason)
    return _finish(extract_with_llm(parts, group))


def import_auto(university: str, group: str) -> tuple[list[dict], list[str]]:
    """
    Бот сам шукає розклад групи будь-якого закладу:
      1) якщо відоме пряме посилання (Львівська політехніка / SCHEDULE_URL_TEMPLATE) — пробує його;
      2) інакше (або якщо воно не спрацювало) — шукає в інтернеті і пробує знайдені посилання по черзі.
    """
    reasons: list[str] = []
    tried: set[str] = set()

    known = auto_schedule_url(university, group)
    if known:
        tried.add(known)
        logger.info("Шукаю розклад групи %s: %s", group, known)
        try:
            return _import_from_candidate(known, group, trusted=True)
        except RateLimitError:
            raise
        except ScheduleImportError as exc:
            logger.warning("Пряме посилання не спрацювало: %s", exc)
            reasons.append(str(exc))

    try:
        urls = find_schedule_urls(university, group)
    except ScheduleImportError as exc:
        raise ScheduleImportError(f"Автопошук не вдався: {exc}") from exc

    candidates = [u for u in urls if u not in tried][:MAX_SEARCH_CANDIDATES]
    if not candidates:
        raise ScheduleImportError(
            f"В інтернеті не знайшов сторінку з розкладом групи {group} ({university}).")

    attempts: list[str] = []
    budget = {"downloads": MAX_CRAWL_DOWNLOADS, "llm": MAX_CRAWL_LLM}
    for url in candidates:
        logger.info("Пробую знайдене посилання: %s", url)
        try:
            return _import_from_candidate(url, group, trusted=False, budget=budget)
        except RateLimitError:
            raise
        except ScheduleImportError as exc:
            logger.info("Посилання %s не підійшло: %s", url, exc)
            attempts.append(f"{len(attempts) + 1}. {url[:90]} — {exc}")

    origin = getattr(urls, "source", "") or "пошук"
    if getattr(urls, "note", ""):
        origin += f"; пошук через модель не вдався: {urls.note}"
    raise ScheduleImportError(
        f"Знайшов {len(candidates)} посилань ({origin}), але розклад групи {group} з них витягти не вдалося:\n"
        + "\n".join(attempts)
        + "\n\nЯкщо знаєш правильне посилання на розклад — просто надішли його мені.")


def import_from_url(url: str, group: str) -> tuple[list[dict], list[str]]:
    return _finish(extract_with_llm(fetch_url(url), group))


def import_from_file(data: bytes, filename: str, mime: str, group: str
                     ) -> tuple[list[dict], list[str]]:
    """Фото/скріншот, PDF, Excel, CSV, текстовий файл."""
    if len(data) > MAX_FILE_BYTES:
        raise ScheduleImportError("Файл завеликий (понад 10 МБ).")
    name = (filename or "").lower()
    mime = (mime or "").lower()

    if mime.startswith("image/"):
        return _finish(extract_with_llm([("image", mime, data)], group))
    if mime == "application/pdf" or name.endswith(".pdf"):
        return _finish(extract_with_llm([("pdf", data)], group))
    if name.endswith((".xlsx", ".xlsm")):
        return _finish(extract_with_llm([("text", xlsx_to_text(data)[:MAX_TEXT_CHARS])], group))
    if name.endswith(".docx"):
        return _finish(extract_with_llm([("text", docx_to_text(data)[:MAX_TEXT_CHARS])], group))
    if name.endswith(".xls"):
        raise ScheduleImportError("Старий формат .xls не підтримується — збережи файл як .xlsx.")
    if name.endswith((".txt", ".csv", ".md")) or mime.startswith("text/"):
        return import_from_text(data.decode("utf-8", "replace"), group)
    if name.endswith((".html", ".htm")):
        return _finish(extract_with_llm([("text", html_to_text(data.decode("utf-8", "replace"))
                                          [:MAX_TEXT_CHARS])], group))
    raise ScheduleImportError("Такий тип файлу не підтримую. Надішли фото, PDF, Excel (.xlsx), "
                              "текст або посилання.")


# ───────────────────────── показ користувачу ─────────────────────────

def format_lessons(lessons: list[dict], limit: int = 60) -> str:
    from html import escape
    lines: list[str] = []
    current = None
    for l in lessons[:limit]:
        if l["day"] != current:
            current = l["day"]
            lines.append(f"\n<b>{DAY_NAMES[current]}</b>")
        room = f" ({escape(l['room'])})" if l.get("room") else ""
        lines.append(f"{l['lesson_num']}) {l['time']} — {escape(l['subject'])}{room}"
                     f"{WEEK_LABEL[l['week_type']]}")
    if len(lessons) > limit:
        lines.append(f"\n… і ще {len(lessons) - limit}")
    return "\n".join(lines).strip()


def format_diff(diff: dict) -> str:
    return (f"➕ додано: {len(diff['added'])}, ✏️ змінено: {len(diff['changed'])}, "
            f"➖ видалено: {len(diff['removed'])}")