"""
parsers.py — розбір текстового вводу користувача.

Без залежностей від aiogram та БД, тому легко тестується окремо.
Усе, що не вдалося розібрати, повертає None (або кидає LegParseError з людським поясненням).
"""

import re
from typing import Optional

_LETTERS = "A-Za-zА-Яа-яІіЇїЄєҐґ"


# ───────────────────────── ім'я ─────────────────────────

def parse_name(text: str) -> Optional[str]:
    """'Мене звати Олег' → 'Олег'. None, якщо це не схоже на ім'я."""
    name = re.sub(r"\s+", " ", text).strip()
    name = re.sub(r"^(?:мене\s+звати|мене\s+звуть|звати|звуть)\s+", "", name,
                  flags=re.IGNORECASE)
    name = name.strip(" .,!")
    if not (1 <= len(name) <= 50) or not re.search(rf"[{_LETTERS}]", name):
        return None
    return name


# ───────────────────────── заклад і група ─────────────────────────

# Код групи: літери + (необов'язковий дефіс/пробіл) + цифри + (необов'язкова літера в кінці).
_GROUP_CODE = rf"[{_LETTERS}]{{1,8}}\s*[-–—]?\s*\d{{1,4}}[{_LETTERS}]{{0,3}}"
_GROUP_WORD = r"(?:група|групи|групу|гр\.?|group)"


# Латинські літери, схожі на українські (часто з'являються при швидкому наборі)
_LAT_TO_CYR = str.maketrans({
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "I": "І", "K": "К",
    "M": "М", "N": "Н", "O": "О", "P": "Р", "S": "С", "T": "Т", "X": "Х", "Y": "У",
})


def to_cyrillic_group(group: str) -> str:
    """'KI-108' → 'КІ-108' (латинські літери → українські)."""
    return group.upper().translate(_LAT_TO_CYR)


def _normalize_group(raw: str) -> str:
    """'кі 108' / 'кі–108' / 'KI-108' → 'КІ-108' (єдиний формат, щоб збігався з розкладом)."""
    group = to_cyrillic_group(raw.strip())
    group = re.sub(r"[–—]", "-", group)
    group = re.sub(r"\s*-\s*", "-", group)
    group = re.sub(rf"([{_LETTERS}])\s+(\d)", r"\1-\2", group)
    group = re.sub(rf"^([{_LETTERS}]+)(\d)", r"\1-\2", group)
    return group


def parse_university_and_group(text: str) -> Optional[tuple[str, str]]:
    """
    'Львівська політехніка, група КІ-108' → ('Львівська політехніка', 'КІ-108').
    Підтримує також «ЛП, КІ-108», «… група КІ-108» без коми, «НУЛП кн 21».
    """
    text = re.sub(r"\s+", " ", text).strip().strip(".")
    if not text:
        return None

    if "," in text:
        university, _, group_part = text.rpartition(",")
        group_part = re.sub(rf"^\s*{_GROUP_WORD}\s*", "", group_part, flags=re.IGNORECASE)
        group_part = group_part.strip()
        if not group_part or len(group_part) > 20 or not re.search(r"\w", group_part):
            return None
    else:
        matches = list(re.finditer(_GROUP_CODE, text))
        if not matches:
            return None
        last = matches[-1]
        group_part = last.group(0)
        university = text[: last.start()]
        university = re.sub(rf"\s*{_GROUP_WORD}\s*$", "", university, flags=re.IGNORECASE)

    university = university.strip(" ,;:-–—")
    group_name = _normalize_group(group_part)

    if len(university) < 2 or not group_name:
        return None
    return university, group_name


# ───────────────────────── звідки добирається ─────────────────────────

_PLACE_PREFIX = re.compile(r"^(?:м\.|с\.|смт\.?|місто|село|селище)\s*", re.IGNORECASE)


def parse_home_place(text: str) -> Optional[tuple[str, str]]:
    """
    'Дрогобич, Львівська обл.' → ('Дрогобич, Львівська обл.', 'Дрогобич').
    Повертає (як написав користувач, лише місто/село).
    """
    place = re.sub(r"\s+", " ", text).strip().strip(".")
    if len(place) < 2 or not re.search(rf"[{_LETTERS}]", place):
        return None
    if re.search(r"(?:обл|р-н)$", place, re.IGNORECASE):
        place += "."                       # «обл» → «обл.» (крапку прибрав strip)

    if "," in place:
        city = place.split(",")[0]
    elif re.search(r"\s(?:обл|область|району?|р-н)", place, re.IGNORECASE):
        city = place.split()[0]
    else:
        city = place
    city = _PLACE_PREFIX.sub("", city.strip()).strip()
    if len(city) < 2:
        return None
    return place, city


def is_lviv(city: Optional[str]) -> bool:
    return bool(city) and city.casefold().startswith(("львів", "lviv", "lvov"))


# ───────────────────────── хвилини ─────────────────────────

def parse_prep_minutes(text: str) -> Optional[int]:
    """
    '40', '40 хв', '1 год', '1.5 год', '1 год 20 хв', 'півгодини' → хвилини (5..240).
    """
    t = text.lower().replace(",", ".")
    total = 0.0
    found = False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(год\w*|хв\w*)?", t):
        found = True
        total += float(num) * (60 if unit.startswith("год") else 1)
    if not found:
        if re.search(r"пів\s*години|півгодини", t):
            total = 30
        elif re.search(r"\bгодин[аиу]\b", t):
            total = 60
        else:
            return None
    minutes = round(total)
    return minutes if 5 <= minutes <= 240 else None


def parse_minutes(text: str, lo: int = 0, hi: int = 600) -> Optional[int]:
    """Одне ціле число хвилин у межах [lo, hi]; 'нема/немає/нуль' → 0 (якщо lo == 0)."""
    t = text.strip().lower()
    if lo == 0 and t in ("нема", "немає", "нуль", "ні", "-", "—"):
        return 0
    if re.search(r"-\s*\d", t):          # від'ємне число
        return None
    m = re.search(r"\d+", t)
    if not m:
        return None
    value = int(m.group())
    return value if lo <= value <= hi else None


def parse_departures(text: str) -> Optional[list[str]]:
    """'6:40, 07:15' → ['06:40', '07:15'] (відсортовано). None, якщо є некоректні значення."""
    result = []
    for token in re.split(r"[,\s;]+", text.strip()):
        if not token:
            continue
        m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", token)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            return None
        result.append(f"{int(m.group(1)):02d}:{m.group(2)}")
    return sorted(set(result)) or None


# ───────────────────────── корпус і аудиторія ─────────────────────────

# Українці часто пишуть римські цифри кирилицею (І, Х, С, М) — приводимо до латиниці.
_ROMAN_LOOKALIKES = str.maketrans({"І": "I", "і": "I", "Х": "X", "х": "X",
                                   "С": "C", "с": "C", "М": "M", "м": "M"})
_ROMAN_CHARS = "IVXLCІХСМ"
_BUILDING = rf"[{_ROMAN_CHARS}]{{1,5}}|\d{{1,2}}(?!\d)"


def _to_roman(n: int) -> str:
    result = ""
    for value, symbol in ((10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        while n >= value:
            result += symbol
            n -= value
    return result


def normalize_building_code(raw: str) -> str:
    """'4' → 'IV', 'ІV' (кирилична І) → 'IV', 'iv' → 'IV'. Порожній рядок, якщо коду немає."""
    s = raw.strip().translate(_ROMAN_LOOKALIKES).upper()
    s = re.sub(r"[^IVXLCDM0-9]", "", s)
    if s.isdigit():
        n = int(s)
        return _to_roman(n) if 1 <= n <= 39 else s
    return s


def parse_building_input(text: str) -> Optional[str]:
    """
    Відповідь на «для якого корпусу маршрут?»: '' = для всіх, 'IV' = конкретний корпус,
    None = не вдалося розібрати.
    """
    t = text.strip().lower()
    if t in ("усі", "всі", "усе", "все", "будь-який", "будь який", "за замовчуванням",
             "all", "default", "-", "—"):
        return ""
    t = re.sub(r"корп\w*\.?", "", t).strip()
    code = normalize_building_code(t)
    return code or None


def parse_room(room: Optional[str]) -> tuple[str, str]:
    """
    Витягує (код корпусу, номер аудиторії) з рядка розкладу. ('', '') якщо не вдалося.
        'IV-314' → ('IV', '314');   'ІV–314' → ('IV', '314')
        '215, 4 корп.' → ('IV', '215');   'корп. IV, ауд. 314' → ('IV', '314')
    """
    if not room:
        return "", ""
    s = room.strip()

    # 1) 'IV-314', '4-210'
    m = re.match(rf"^\s*(?:корп\w*\.?\s*)?({_BUILDING})\s*[-–—/]\s*(\d{{1,4}}[A-Za-zА-Яа-я]?)\b", s)
    if m:
        return normalize_building_code(m.group(1)), m.group(2)

    # 2) '4 корп.', 'IV-й корпус'
    m = re.search(rf"\b({_BUILDING})\s*-?\s*(?:й\s*)?корп", s, re.IGNORECASE)
    # 3) 'корп. IV', 'корпус 4'
    if not m:
        m = re.search(rf"корп\w*\.?\s*[№#]?\s*({_BUILDING})", s, re.IGNORECASE)

    building = ""
    if m:
        building = normalize_building_code(m.group(1))
        s = s[: m.start()] + " " + s[m.end():]

    num = re.search(r"(\d{1,4}[A-Za-zА-Яа-я]?)", s)
    return building, (num.group(1) if num else "")


def display_room(room: Optional[str]) -> str:
    """Рядок для показу користувачу: 'IV-314' або сирий рядок, якщо не розпізнали."""
    building, number = parse_room(room)
    if building and number:
        return f"{building}-{number}"
    return (room or "").strip()


# ───────────────────────── ділянки маршруту ─────────────────────────

class LegParseError(ValueError):
    """Помилка розбору ділянки — текст повідомлення можна показувати користувачу."""


LEG_FORMAT_HELP = (
    "Формат (один рядок = одна ділянка, поля через «|»):\n"
    "<code>пішки | звідки → куди | хвилин</code>\n"
    "<code>міський | транспорт №лінія | звідки → куди | хвилин | [відправлення] | [запас]</code>\n"
    "<code>міжміський | транспорт №лінія | звідки → куди | хвилин | [відправлення] | [запас]</code>\n\n"
    "• <b>відправлення</b> — рейси з розкладу через кому (необов’язково; без них вважаю, що транспорт їздить за інтервалом)\n"
    "• <b>запас</b> — за скільки хв бути на зупинці / скільки в середньому чекати (за замовчуванням: міжміський 5, міський 4)"
)

_DEFAULT_WAIT = {"intercity": 5, "city": 4, "walk": 0}


def _parse_kind(raw: str) -> Optional[str]:
    t = raw.strip().lower()
    for kind, stems in (("intercity", ("міжміськ", "інтерсіті", "intercity")),
                        ("walk", ("пішк", "піш", "walk")),
                        ("city", ("міськ", "місто", "city"))):
        if any(t.startswith(stem) for stem in stems):
            return kind
    return None


def _split_route(raw: str) -> tuple[str, str]:
    parts = [p.strip() for p in re.split(r"\s*(?:→|->|=>|⇒)\s*", raw) if p.strip()]
    if len(parts) != 2:
        raise LegParseError("Вкажи звідки і куди через стрілку: «Зупинка А → Зупинка Б».")
    return parts[0], parts[1]


def _split_transport(raw: str) -> tuple[str, Optional[str]]:
    m = re.match(r"^(.*?)\s*(?:№|#)?\s*(\d+[A-Za-zА-Яа-я]?)?$", raw.strip())
    transport = (m.group(1) if m else raw).strip()
    line = f"№{m.group(2)}" if m and m.group(2) else None
    if not transport:
        raise LegParseError("Вкажи транспорт, наприклад: «маршрутка №100» або «трамвай №3».")
    return transport, line


def _parse_duration(raw: str) -> int:
    m = re.search(r"\d+", raw)
    if not m or not (1 <= int(m.group()) <= 600):
        raise LegParseError("Не бачу тривалість у хвилинах (число від 1 до 600), наприклад: 75.")
    return int(m.group())


def parse_leg_line(line: str) -> dict:
    """Розбирає один рядок маршруту в словник ділянки. Кидає LegParseError з поясненням."""
    parts = [p.strip() for p in line.split("|")]
    kind = _parse_kind(parts[0]) if parts and parts[0] else None
    if kind is None:
        raise LegParseError("Тип ділянки має бути: «пішки», «міський» або «міжміський».")

    if kind == "walk":
        if len(parts) < 3:
            raise LegParseError("Для пішої ділянки: пішки | звідки → куди | хвилин")
        frm, to = _split_route(parts[1])
        return {"kind": "walk", "transport": "пішки", "line": None, "from_stop": frm,
                "to_stop": to, "duration_min": _parse_duration(parts[2]),
                "wait_min": 0, "departures": []}

    if len(parts) < 4:
        raise LegParseError("Не вистачає полів: тип | транспорт №лінія | звідки → куди | хвилин")
    transport, line_no = _split_transport(parts[1])
    frm, to = _split_route(parts[2])
    duration = _parse_duration(parts[3])

    departures: list[str] = []
    if len(parts) > 4 and parts[4] not in ("", "-", "—"):
        parsed = parse_departures(parts[4])
        if parsed is None:
            raise LegParseError("Рейси вкажи як години через кому, наприклад: 06:40, 07:15.")
        departures = parsed

    wait = _DEFAULT_WAIT[kind]
    if len(parts) > 5 and parts[5] not in ("", "-", "—"):
        w = parse_minutes(parts[5], 0, 60)
        if w is None:
            raise LegParseError("Запас — число хвилин від 0 до 60.")
        wait = w

    return {"kind": kind, "transport": transport, "line": line_no, "from_stop": frm,
            "to_stop": to, "duration_min": duration, "wait_min": wait,
            "departures": departures}
