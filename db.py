"""
db.py — база даних Telegram-бота-компаньйона для студентів (SQLite, без зовнішніх залежностей).

Таблиці:
    users             — профіль (ім'я, заклад, група, звідки добирається, час на збори)
    schedule          — розклад груп
    tasks             — завдання/дедлайни
    notes             — голосові/текстові нотатки
    routes            — маршрути «дім → корпус» (окремий маршрут на корпус або один за замовчуванням)
    route_legs        — ділянки маршруту: пішки / міський транспорт / міжміський транспорт
    notification_log  — які сповіщення вже надіслано (щоб не дублювати після перезапуску)
    trip_cache        — кеш автоматично побудованого маршруту (щоб не смикати карти щохвилини)

Старі бази (з Task 1–2) мігруються автоматично: init_db() додає нові колонки в users.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

DB_PATH = "bot.db"

TASK_STATUSES = ("pending", "done", "cancelled")
LEG_KINDS = ("walk", "intercity", "city")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id   INTEGER PRIMARY KEY,
    university    TEXT NOT NULL,
    group_name    TEXT NOT NULL,
    name          TEXT,                -- як звертатись
    home_place    TEXT,                -- як написав користувач: 'Миколаїв, Львівська обл.'
    home_city     TEXT,                -- лише місто/село: 'Миколаїв'
    prep_minutes  INTEGER,             -- ранкові збори, хв
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS schedule (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_name  TEXT    NOT NULL,
    day         INTEGER NOT NULL CHECK (day BETWEEN 1 AND 7),   -- 1 = понеділок
    lesson_num  INTEGER NOT NULL CHECK (lesson_num >= 1),
    time        TEXT    NOT NULL,                               -- напр. '08:30-09:50'
    subject     TEXT    NOT NULL,
    room        TEXT,                                           -- напр. 'IV-314'
    week_type   TEXT    NOT NULL DEFAULT 'every'
                CHECK (week_type IN ('every', 'odd', 'even')),
    UNIQUE (group_name, day, lesson_num, week_type)
);
CREATE INDEX IF NOT EXISTS idx_schedule_group_day ON schedule (group_name, day);

CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users (telegram_id) ON DELETE CASCADE,
    transcript  TEXT    NOT NULL,
    voice_file_id TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users (telegram_id) ON DELETE CASCADE,
    text        TEXT    NOT NULL,
    deadline    TEXT,                      -- 'YYYY-MM-DD HH:MM' (може бути NULL)
    status      TEXT    NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'done', 'cancelled')),
    source      TEXT    NOT NULL DEFAULT 'manual',
    note_id     INTEGER REFERENCES notes (id) ON DELETE SET NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tasks_user_status ON tasks (user_id, status, deadline);

-- Маршрут користувача. building_code = '' означає «за замовчуванням для будь-якого корпусу»;
-- якщо є маршрут саме для корпусу пари (напр. 'IV'), використовується він.
CREATE TABLE IF NOT EXISTS routes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users (telegram_id) ON DELETE CASCADE,
    building_code  TEXT    NOT NULL DEFAULT '',
    title          TEXT,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, building_code)
);

CREATE TABLE IF NOT EXISTS route_legs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id      INTEGER NOT NULL REFERENCES routes (id) ON DELETE CASCADE,
    leg_order     INTEGER NOT NULL,
    kind          TEXT    NOT NULL CHECK (kind IN ('walk', 'intercity', 'city')),
    transport     TEXT    NOT NULL,        -- 'маршрутка', 'трамвай', 'пішки', ...
    line          TEXT,                    -- '№100' (може бути NULL)
    from_stop     TEXT    NOT NULL,
    to_stop       TEXT    NOT NULL,
    duration_min  INTEGER NOT NULL CHECK (duration_min > 0),
    wait_min      INTEGER NOT NULL DEFAULT 0,   -- за скільки хв бути на зупинці / середнє очікування
    departures    TEXT,                    -- рейси з розкладу: '06:40,07:15' (NULL = їздить за інтервалом)
    UNIQUE (route_id, leg_order)
);

CREATE TABLE IF NOT EXISTS notification_log (
    user_id  INTEGER NOT NULL,
    kind     TEXT    NOT NULL,             -- 'brief' | 'leave'
    day      TEXT    NOT NULL,             -- 'YYYY-MM-DD'
    sent_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, kind, day)
);

-- Кеш автоматично побудованого маршруту: один запит до карт на користувача за день.
-- legs_json = NULL і error != NULL означає «спроба невдала» (повторюємо не раніше ніж через кілька хв).
CREATE TABLE IF NOT EXISTS trip_cache (
    user_id     INTEGER NOT NULL REFERENCES users (telegram_id) ON DELETE CASCADE,
    day         TEXT    NOT NULL,             -- 'YYYY-MM-DD'
    cache_key   TEXT    NOT NULL,             -- хеш (звідки, куди, час пари)
    legs_json   TEXT,
    error       TEXT,
    fetched_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, day)
);

-- Загальні налаштування бота (ключ → значення), напр. 'odd_week_anchor' = 'YYYY-MM-DD'.
CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""

# Колонки, яких не було в users у Task 1–2 (для міграції існуючих баз)
USER_EXTRA_COLUMNS = {
    "name": "TEXT",
    "home_place": "TEXT",
    "home_city": "TEXT",
    "prep_minutes": "INTEGER",
}


# ───────────────────────── підключення ─────────────────────────

@contextmanager
def get_conn(db_path: str = DB_PATH):
    """Контекстний менеджер: commit при успіху, rollback при помилці."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Додає нові колонки в users, якщо база створена старою версією скрипта."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    for column, ddl in USER_EXTRA_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE users ADD COLUMN {column} {ddl}")


def init_db(db_path: str = DB_PATH) -> None:
    """Створює таблиці та мігрує стару схему (безпечно викликати повторно)."""
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.execute("PRAGMA journal_mode = WAL")


# ───────────────────────── users ─────────────────────────

def upsert_user(telegram_id: int, university: str, group_name: str, *,
                name: Optional[str] = None, home_place: Optional[str] = None,
                home_city: Optional[str] = None, prep_minutes: Optional[int] = None,
                db_path: str = DB_PATH) -> None:
    """
    Створює користувача або оновлює його дані.
    Поля, передані як None, НЕ затирають уже збережені значення.
    """
    with get_conn(db_path) as conn:
        conn.execute(
            """INSERT INTO users
                   (telegram_id, university, group_name, name, home_place, home_city, prep_minutes)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(telegram_id) DO UPDATE SET
                   university   = excluded.university,
                   group_name   = excluded.group_name,
                   name         = COALESCE(excluded.name, users.name),
                   home_place   = COALESCE(excluded.home_place, users.home_place),
                   home_city    = COALESCE(excluded.home_city, users.home_city),
                   prep_minutes = COALESCE(excluded.prep_minutes, users.prep_minutes)""",
            (telegram_id, university, group_name, name, home_place, home_city, prep_minutes),
        )


def get_user(telegram_id: int, db_path: str = DB_PATH) -> Optional[dict]:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        return dict(row) if row else None


def is_profile_complete(user: Optional[dict]) -> bool:
    """Профіль повний, якщо є ім'я, місце, звідки добирається, і час на збори."""
    return bool(user and user.get("name") and user.get("home_city")
                and user.get("prep_minutes"))


def get_all_users(db_path: str = DB_PATH) -> list[dict]:
    with get_conn(db_path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM users").fetchall()]


def get_users_by_group(group_name: str, db_path: str = DB_PATH) -> list[dict]:
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE group_name = ?", (group_name,)
        ).fetchall()
        return [dict(r) for r in rows]


def delete_user_data(telegram_id: int, db_path: str = DB_PATH) -> dict:
    """
    Повністю видаляє все, що бот знає про користувача: профіль, нотатки, завдання,
    збережені маршрути, кеш маршруту і журнал сповіщень.
    Розклад групи — спільні дані (не персональні), тому не чіпається.
    Повертає {'deleted': bool, 'notes': n, 'tasks': n, 'routes': n}.
    """
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA secure_delete = ON")   # занулити видалене, а не лишати у файлі
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id = ?",
                                (telegram_id,)).fetchone()[0]
            for table in ("notes", "tasks", "routes")
        }
        conn.execute("DELETE FROM route_legs WHERE route_id IN "
                     "(SELECT id FROM routes WHERE user_id = ?)", (telegram_id,))
        for table in ("routes", "tasks", "notes", "trip_cache", "notification_log"):
            conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (telegram_id,))
        deleted = conn.execute("DELETE FROM users WHERE telegram_id = ?",
                               (telegram_id,)).rowcount > 0

    try:  # скинути WAL у основний файл, щоб старі копії рядків не лежали в журналі
        with get_conn(db_path) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass
    return {"deleted": deleted, **counts}


def get_all_groups(db_path: str = DB_PATH) -> list[str]:
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT group_name FROM users ORDER BY group_name"
        ).fetchall()
        return [r["group_name"] for r in rows]


# ───────────────────────── schedule ─────────────────────────

def replace_schedule(group_name: str, lessons: list[dict],
                     db_path: str = DB_PATH) -> dict:
    """
    Повністю замінює розклад групи і повертає різницю зі старим:
    {'added': [...], 'removed': [...], 'changed': [{'old': ..., 'new': ...}]}
    """
    def key(l):
        return (l["day"], l["lesson_num"], l.get("week_type", "every"))

    def val(l):
        return (l["time"], l["subject"], l.get("room"))

    with get_conn(db_path) as conn:
        old_rows = conn.execute(
            "SELECT day, lesson_num, time, subject, room, week_type "
            "FROM schedule WHERE group_name = ?", (group_name,)
        ).fetchall()
        old = {key(dict(r)): dict(r) for r in old_rows}
        new = {key(l): {**l, "week_type": l.get("week_type", "every")} for l in lessons}

        diff = {
            "added":   [new[k] for k in new if k not in old],
            "removed": [old[k] for k in old if k not in new],
            "changed": [{"old": old[k], "new": new[k]}
                        for k in new if k in old and val(new[k]) != val(old[k])],
        }

        conn.execute("DELETE FROM schedule WHERE group_name = ?", (group_name,))
        conn.executemany(
            """INSERT INTO schedule
                   (group_name, day, lesson_num, time, subject, room, week_type)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [(group_name, l["day"], l["lesson_num"], l["time"], l["subject"],
              l.get("room"), l.get("week_type", "every")) for l in lessons],
        )
        return diff


def get_schedule_for_day(group_name: str, day: int, week_type: str = "odd",
                         db_path: str = DB_PATH) -> list[dict]:
    """Пари групи на день (1 = пн ... 7 = нд) за номером пари; 'every' додаються завжди."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """SELECT * FROM schedule
               WHERE group_name = ? AND day = ? AND week_type IN ('every', ?)
               ORDER BY lesson_num""",
            (group_name, day, week_type),
        ).fetchall()
        return [dict(r) for r in rows]


def count_schedule(group_name: str, db_path: str = DB_PATH) -> int:
    """Скільки пар у розкладі групи (0 — розклад ще не завантажено)."""
    with get_conn(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM schedule WHERE group_name = ?",
                            (group_name,)).fetchone()[0]


def get_group_rooms(group_name: str, db_path: str = DB_PATH) -> list[tuple[str, int]]:
    """Аудиторії з розкладу групи та скільки разів вони трапляються: [('IV-314', 3), ...]."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """SELECT room, COUNT(*) AS n FROM schedule
               WHERE group_name = ? AND room IS NOT NULL AND room != ''
               GROUP BY room ORDER BY n DESC, room""",
            (group_name,),
        ).fetchall()
        return [(r["room"], r["n"]) for r in rows]


# ───────────────────────── notes ─────────────────────────

def add_note(user_id: int, transcript: str, voice_file_id: Optional[str] = None,
             db_path: str = DB_PATH) -> int:
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO notes (user_id, transcript, voice_file_id) VALUES (?, ?, ?)",
            (user_id, transcript, voice_file_id),
        )
        return cur.lastrowid


# ───────────────────────── tasks ─────────────────────────

def add_task(user_id: int, text: str, deadline: Optional[datetime | str] = None,
             source: str = "manual", note_id: Optional[int] = None,
             db_path: str = DB_PATH) -> int:
    if isinstance(deadline, datetime):
        deadline = deadline.strftime("%Y-%m-%d %H:%M")
    with get_conn(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO tasks (user_id, text, deadline, source, note_id)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, text, deadline, source, note_id),
        )
        return cur.lastrowid


def get_tasks(user_id: int, status: Optional[str] = "pending",
              db_path: str = DB_PATH) -> list[dict]:
    query = "SELECT * FROM tasks WHERE user_id = ?"
    params: list = [user_id]
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY deadline IS NULL, deadline, id"
    with get_conn(db_path) as conn:
        return [dict(r) for r in conn.execute(query, params).fetchall()]


def get_tasks_due_before(user_id: int, before: datetime | str,
                         db_path: str = DB_PATH) -> list[dict]:
    """Активні завдання з дедлайном до вказаного моменту (включно з простроченими)."""
    if isinstance(before, datetime):
        before = before.strftime("%Y-%m-%d %H:%M")
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """SELECT * FROM tasks
               WHERE user_id = ? AND status = 'pending'
                 AND deadline IS NOT NULL AND deadline <= ?
               ORDER BY deadline""",
            (user_id, before),
        ).fetchall()
        return [dict(r) for r in rows]


def set_task_status(task_id: int, user_id: int, status: str,
                    db_path: str = DB_PATH) -> bool:
    if status not in TASK_STATUSES:
        raise ValueError(f"Невідомий статус: {status}")
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE tasks SET status = ? WHERE id = ? AND user_id = ?",
            (status, task_id, user_id),
        )
        return cur.rowcount > 0


def delete_task(task_id: int, user_id: int, db_path: str = DB_PATH) -> bool:
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
        )
        return cur.rowcount > 0


# ───────────────────────── routes ─────────────────────────

def _legs_for_route(conn: sqlite3.Connection, route_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM route_legs WHERE route_id = ? ORDER BY leg_order", (route_id,)
    ).fetchall()
    legs = []
    for row in rows:
        leg = dict(row)
        leg["departures"] = [x for x in (leg["departures"] or "").split(",") if x]
        legs.append(leg)
    return legs


def save_route(user_id: int, legs: list[dict], building_code: str = "",
               title: Optional[str] = None, db_path: str = DB_PATH) -> int:
    """
    Зберігає маршрут (замінює попередній для того ж building_code), повертає id.

    Кожна ділянка: {'kind': 'walk'|'intercity'|'city', 'transport': 'маршрутка',
                    'line': '№100' | None, 'from_stop': ..., 'to_stop': ...,
                    'duration_min': 70, 'wait_min': 5, 'departures': ['06:40', '07:15']}
    """
    if not legs:
        raise ValueError("Маршрут має містити хоча б одну ділянку")
    for leg in legs:
        if leg["kind"] not in LEG_KINDS:
            raise ValueError(f"Невідомий тип ділянки: {leg['kind']}")

    with get_conn(db_path) as conn:
        conn.execute(
            "DELETE FROM routes WHERE user_id = ? AND building_code = ?",
            (user_id, building_code),
        )
        cur = conn.execute(
            "INSERT INTO routes (user_id, building_code, title) VALUES (?, ?, ?)",
            (user_id, building_code, title),
        )
        route_id = cur.lastrowid
        conn.executemany(
            """INSERT INTO route_legs
                   (route_id, leg_order, kind, transport, line, from_stop, to_stop,
                    duration_min, wait_min, departures)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(route_id, i, l["kind"], l["transport"], l.get("line"), l["from_stop"],
              l["to_stop"], l["duration_min"], l.get("wait_min", 0),
              ",".join(l.get("departures") or []) or None)
             for i, l in enumerate(legs, start=1)],
        )
        return route_id


def get_route(user_id: int, building_code: Optional[str] = None,
              db_path: str = DB_PATH) -> Optional[dict]:
    """
    Маршрут для корпусу; якщо для нього окремого немає — маршрут за замовчуванням.
    Повертає {'id', 'building_code', 'title', 'legs': [...]} або None.
    """
    code = building_code or ""
    with get_conn(db_path) as conn:
        row = conn.execute(
            """SELECT * FROM routes
               WHERE user_id = ? AND building_code IN (?, '')
               ORDER BY building_code DESC LIMIT 1""",
            (user_id, code),
        ).fetchone()
        if not row:
            return None
        route = dict(row)
        route["legs"] = _legs_for_route(conn, route["id"])
        return route


def list_routes(user_id: int, db_path: str = DB_PATH) -> list[dict]:
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM routes WHERE user_id = ? ORDER BY building_code", (user_id,)
        ).fetchall()
        routes = []
        for row in rows:
            route = dict(row)
            route["legs"] = _legs_for_route(conn, route["id"])
            routes.append(route)
        return routes


def delete_route(route_id: int, user_id: int, db_path: str = DB_PATH) -> bool:
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM routes WHERE id = ? AND user_id = ?", (route_id, user_id)
        )
        return cur.rowcount > 0


# ───────────────────────── trip_cache ─────────────────────────

def get_trip_cache(user_id: int, day: str, db_path: str = DB_PATH) -> Optional[dict]:
    """Кеш маршруту на день + age_sec (скільки секунд тому записано)."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            """SELECT *, CAST((julianday('now') - julianday(fetched_at)) * 86400 AS INTEGER)
                         AS age_sec
               FROM trip_cache WHERE user_id = ? AND day = ?""",
            (user_id, day),
        ).fetchone()
        return dict(row) if row else None


def save_trip_cache(user_id: int, day: str, cache_key: str, legs_json: Optional[str],
                    error: Optional[str], db_path: str = DB_PATH) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            """INSERT INTO trip_cache (user_id, day, cache_key, legs_json, error)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, day) DO UPDATE SET
                   cache_key = excluded.cache_key, legs_json = excluded.legs_json,
                   error = excluded.error, fetched_at = datetime('now')""",
            (user_id, day, cache_key, legs_json, error),
        )
        conn.execute("DELETE FROM trip_cache WHERE day < date('now', '-3 day')")


# ───────────────────────── notification_log ─────────────────────────

def claim_notification(user_id: int, kind: str, day: str,
                       db_path: str = DB_PATH) -> bool:
    """
    Атомарно «бронює» сповіщення. True — можна надсилати (ще не надсилали),
    False — вже надіслано/заброньовано. Захищає від дублів після перезапуску бота.
    """
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO notification_log (user_id, kind, day) VALUES (?, ?, ?)",
            (user_id, kind, day),
        )
        return cur.rowcount == 1


def release_notification(user_id: int, kind: str, day: str,
                         db_path: str = DB_PATH) -> None:
    """Знімає бронь, якщо надсилання не вдалося (щоб спробувати ще раз на наступній хвилині)."""
    with get_conn(db_path) as conn:
        conn.execute(
            "DELETE FROM notification_log WHERE user_id = ? AND kind = ? AND day = ?",
            (user_id, kind, day),
        )


# ───────────────────────── settings ─────────────────────────

def get_setting(key: str, db_path: str = DB_PATH) -> Optional[str]:
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: Optional[str], db_path: str = DB_PATH) -> None:
    """value=None видаляє налаштування."""
    with get_conn(db_path) as conn:
        if value is None:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
