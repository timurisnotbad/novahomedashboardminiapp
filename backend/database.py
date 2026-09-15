"""SQLite persistence layer for the Nova Home Dashboard.

Uses the standard-library ``sqlite3`` module with a thin helper API. Access is
synchronous; FastAPI runs sync path operations in a threadpool, which is more
than enough for a single-property operations dashboard.
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Iterable

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY,
    apartment_id INTEGER NOT NULL,
    apartment_name TEXT,
    begin_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    status TEXT,
    days_count INTEGER,
    amount REAL,
    debt REAL DEFAULT 0,
    prepayment REAL DEFAULT 0,
    prepayment_progress REAL,
    is_external BOOLEAN,
    source_id INTEGER,
    client_name TEXT,
    client_phone TEXT,
    arrival_time TEXT,
    departure_time TEXT,
    short_notes TEXT,
    is_delete BOOLEAN DEFAULT 0,
    created_at TIMESTAMP,
    synced_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS payments_cash (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id INTEGER,
    apartment_name TEXT,
    amount REAL,
    currency TEXT DEFAULT 'UZS',
    method TEXT DEFAULT 'наличные',
    paid_at DATE,
    note TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cleaning_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    apartment_name TEXT,
    cleaning_date DATE,
    status TEXT DEFAULT 'pending',
    updated_at TIMESTAMP,
    UNIQUE(apartment_name, cleaning_date)
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    bookings_count INTEGER,
    success BOOLEAN
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    apartment_name TEXT,            -- NULL = общая задача (не привязана к квартире)
    title TEXT NOT NULL,
    deadline DATE,                  -- NULL = без срока
    deadline_time TEXT,             -- HH:MM, NULL = без времени
    status TEXT DEFAULT 'open',     -- open | done
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    done_at TIMESTAMP
);

-- de-dup for reminder notifications: one row per (task, kind) already sent
CREATE TABLE IF NOT EXISTS task_notifications (
    task_id INTEGER,
    kind TEXT,
    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(task_id, kind)
);

-- booking ids we've already announced, so we only notify about genuinely new ones
CREATE TABLE IF NOT EXISTS notified_bookings (
    booking_id INTEGER PRIMARY KEY,
    seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- last-seen state of each booking, to detect edits (e.g. moved to another flat)
CREATE TABLE IF NOT EXISTS booking_snapshots (
    booking_id INTEGER PRIMARY KEY,
    apartment_name TEXT,
    begin_date TEXT,
    end_date TEXT,
    amount REAL,
    status TEXT,
    client_name TEXT,
    client_phone TEXT
);

-- staff arrivals (first live-location inside the work zone each day)
CREATE TABLE IF NOT EXISTS attendance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_id INTEGER,
    staff_name TEXT,
    work_date DATE,
    arrived_at TIMESTAMP,
    lat REAL,
    lng REAL,
    on_time BOOLEAN,
    late_minutes INTEGER,
    UNIQUE(staff_id, work_date)
);

-- staff auto-registry: everyone who ever talked to the bot (except owners).
-- Drives attendance reminders and the roll call without editing .env.
CREATE TABLE IF NOT EXISTS staff_registry (
    staff_id INTEGER PRIMARY KEY,
    name TEXT,
    username TEXT,
    first_seen TIMESTAMP,
    active INTEGER DEFAULT 1
);

-- forum topics in the team group: which thread each kind of message goes to
CREATE TABLE IF NOT EXISTS chat_topics (
    chat_id INTEGER,
    role TEXT,
    thread_id INTEGER,
    name TEXT,
    PRIMARY KEY (chat_id, role)
);

-- payments channel reader: parsed posts, matched to bookings when possible
CREATE TABLE IF NOT EXISTS channel_payments (
    chat_id INTEGER,
    msg_id INTEGER,
    at TIMESTAMP,
    raw_text TEXT,
    amount REAL,
    currency TEXT,
    method TEXT,
    apartment TEXT,
    checkin DATE,
    checkout DATE,
    rc_ref TEXT,
    booking_id INTEGER,
    match_score INTEGER DEFAULT 0,
    PRIMARY KEY (chat_id, msg_id)
);

-- payroll (owner-only): per-staff terms and payment history
CREATE TABLE IF NOT EXISTS staff_terms (
    staff TEXT PRIMARY KEY,
    salary REAL DEFAULT 0,
    pay_note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS salary_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    staff TEXT,
    amount REAL,
    note TEXT,
    at TIMESTAMP
);

-- discipline log: fines and bonuses per staff member (owner-only feature)
CREATE TABLE IF NOT EXISTS penalties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT CHECK(kind IN ('fine','bonus')),
    staff TEXT,
    amount REAL,
    reason TEXT,
    at TIMESTAMP
);

-- cleaning sessions: "до" (start) / "после" (finish) reports per apartment,
-- locked to the cleaner who started; duration + travel time from the previous
-- apartment (or from the morning arrival) are computed at finish/start time
CREATE TABLE IF NOT EXISTS cleaning_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    apartment TEXT,
    staff_id INTEGER,
    staff_name TEXT,
    work_date DATE,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    duration_min INTEGER,
    travel_min INTEGER,
    no_before INTEGER DEFAULT 0,   -- finished without a "до" report
    forced INTEGER DEFAULT 0       -- closed by the owner / at night, not by the cleaner
);

-- daily jobs that already ran (roll call, cleaning control): lets the bot
-- catch up after a restart without running a job twice
CREATE TABLE IF NOT EXISTS job_runs (
    job TEXT,
    day DATE,
    ran_at TIMESTAMP,
    PRIMARY KEY (job, day)
);

-- bot messages that get edited/replaced instead of re-sent (the evening plan
-- and its after-hours refreshes): one row per (kind, chat)
CREATE TABLE IF NOT EXISTS bot_messages (
    key TEXT,
    chat_id INTEGER,
    message_id INTEGER,
    text_hash TEXT,
    sent_at TIMESTAMP,
    PRIMARY KEY (key, chat_id)
);

-- shopping list: what the cleaners ask to buy ("нужно 103 полотенца 2, шампунь")
CREATE TABLE IF NOT EXISTS supplies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    apartment TEXT,
    item TEXT NOT NULL,
    qty INTEGER DEFAULT 1,
    staff_name TEXT,
    created_at TIMESTAMP,
    bought_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_bookings_begin ON bookings(begin_date);
CREATE INDEX IF NOT EXISTS idx_bookings_end ON bookings(end_date);
"""


@contextmanager
def get_conn():
    # timeout lets the bot and the server share the DB without "database is locked"
    conn = sqlite3.connect(config.DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        # WAL + relaxed sync = snappier concurrent reads for the dashboard.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        # Migrations. The server and the bot start at the same moment and both
        # run this; the loser of the race gets "duplicate column name" — harmless.
        def _add_column(table: str, column: str, decl: str) -> bool:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if column in cols:
                return False
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                return True
            except sqlite3.OperationalError as exc:
                if "duplicate column" in str(exc).lower():
                    return False
                raise

        _add_column("tasks", "deadline_time", "TEXT")
        # items the bot pulled out of the «Поломки» topic: who wrote it, and
        # the chat-line id so an edited message is not recorded twice
        _add_column("tasks", "created_by", "TEXT")
        _add_column("tasks", "src_key", "TEXT")
        _add_column("supplies", "src_key", "TEXT")
        # attendance log: explicit status (ok / late / absent) — no-shows are
        # recorded too, so the monthly statistics are complete
        if _add_column("attendance", "status", "TEXT"):
            conn.execute(
                "UPDATE attendance SET status = CASE WHEN on_time THEN 'ok' ELSE 'late' END "
                "WHERE status IS NULL AND arrived_at IS NOT NULL"
            )


BOOKING_COLUMNS = [
    "id", "apartment_id", "apartment_name", "begin_date", "end_date", "status",
    "days_count", "amount", "debt", "prepayment", "prepayment_progress",
    "is_external", "source_id", "client_name", "client_phone", "arrival_time",
    "departure_time", "short_notes", "is_delete", "created_at", "synced_at",
]


def _booking_rows(bookings: Iterable[dict]) -> list[tuple]:
    now = datetime.now().isoformat(timespec="seconds")  # local time, like every other timestamp
    rows = []
    for b in bookings:
        client = b.get("client") or {}
        rows.append((
            b.get("id"),
            b.get("apartment_id"),
            b.get("apartment_name"),
            b.get("begin_date"),
            b.get("end_date"),
            b.get("status"),
            b.get("days_count"),
            _num(b.get("amount")),
            _num(b.get("debt")),
            _num(b.get("prepayment")),
            _num(b.get("prepayment_progress")),
            1 if b.get("is_external") else 0,
            b.get("source_id"),
            b.get("client_name") or client.get("fio"),
            b.get("client_phone") or client.get("phone"),
            b.get("arrival_time"),
            b.get("departure_time"),
            b.get("short_notes"),
            1 if b.get("is_delete") else 0,
            b.get("created_at"),
            now,
        ))
    return rows


def _insert_sql() -> str:
    placeholders = ",".join(["?"] * len(BOOKING_COLUMNS))
    return (
        f"INSERT OR REPLACE INTO bookings ({','.join(BOOKING_COLUMNS)}) "
        f"VALUES ({placeholders})"
    )


def upsert_bookings(bookings: Iterable[dict]) -> int:
    """Insert or replace bookings by primary key. Returns the count written."""
    rows = _booking_rows(bookings)
    with get_conn() as conn:
        conn.executemany(_insert_sql(), rows)
    return len(rows)


def _num(value):
    if value is None:
        return 0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0


def replace_all_bookings(bookings: list[dict]) -> int:
    """Full refresh: clear the table then insert, in a single transaction.

    Used on every sync so the table stays an exact mirror of the source and
    never mixes stale rows (e.g. demo data) with a fresh fetch.
    """
    rows = _booking_rows(bookings)
    with get_conn() as conn:
        conn.execute("DELETE FROM bookings")
        conn.executemany(_insert_sql(), rows)
    return len(rows)


def get_bookings(
    date_from: str | None = None,
    date_to: str | None = None,
    apartment: str | None = None,
    status: str | None = None,
    has_debt: bool | None = None,
) -> list[dict]:
    clauses = ["is_delete = 0"]
    params: list = []
    if date_from:
        clauses.append("end_date >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("begin_date <= ?")
        params.append(date_to)
    if apartment:
        clauses.append("apartment_name = ?")
        params.append(apartment)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if has_debt is True:
        clauses.append("debt > 0")
    elif has_debt is False:
        clauses.append("debt <= 0")
    where = " AND ".join(clauses)
    with get_conn() as conn:
        cur = conn.execute(
            f"SELECT * FROM bookings WHERE {where} ORDER BY begin_date, apartment_name",
            params,
        )
        return [dict(r) for r in cur.fetchall()]


def all_active_bookings() -> list[dict]:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM bookings WHERE is_delete = 0 ORDER BY begin_date"
        )
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Cleaning status
# ---------------------------------------------------------------------------
def get_cleaning_status(apartment_name: str, cleaning_date: str) -> str:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT status FROM cleaning_status WHERE apartment_name = ? AND cleaning_date = ?",
            (apartment_name, cleaning_date),
        )
        row = cur.fetchone()
        return row["status"] if row else "pending"


def set_cleaning_status(apartment_name: str, cleaning_date: str, status: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO cleaning_status (apartment_name, cleaning_date, status, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(apartment_name, cleaning_date)
            DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at
            """,
            (apartment_name, cleaning_date, status, now),
        )


# ---------------------------------------------------------------------------
# Tasks (per-apartment and general to-do list)
# ---------------------------------------------------------------------------
def add_task(apartment_name, title: str, deadline=None, deadline_time=None,
             created_by=None, src_key=None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (apartment_name, title, deadline, deadline_time, created_by, src_key) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (apartment_name or None, title, deadline or None, deadline_time or None,
             created_by or None, src_key or None),
        )
        return cur.lastrowid


def src_recorded(table: str, src_key: str) -> bool:
    """Was this chat line already turned into a task / supply?"""
    if table not in ("tasks", "supplies"):
        raise ValueError(table)
    with get_conn() as conn:
        return conn.execute(
            f"SELECT 1 FROM {table} WHERE src_key = ? LIMIT 1", (src_key,)
        ).fetchone() is not None


def get_task(task_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None


def open_tasks_with_deadline() -> list[dict]:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM tasks WHERE status = 'open' AND deadline IS NOT NULL"
        )
        return [dict(r) for r in cur.fetchall()]


def task_notif_sent(task_id: int, kind: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM task_notifications WHERE task_id = ? AND kind = ?",
            (task_id, kind),
        ).fetchone()
        return row is not None


def mark_task_notif(task_id: int, kind: str) -> bool:
    """Record a reminder as sent. Returns False if it was already recorded."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO task_notifications (task_id, kind) VALUES (?, ?)",
            (task_id, kind),
        )
        return cur.rowcount > 0


def record_arrival(staff_id, staff_name, work_date, arrived_at, lat, lng, on_time, late_minutes) -> bool:
    """Record the first arrival of the day for a staff member. Returns False if
    an arrival was already recorded today (so we notify only once)."""
    with get_conn() as conn:
        # A no-show row written by the roll call must not block a check-in that
        # lands in the same minute: upgrade it instead of ignoring the arrival.
        cur = conn.execute(
            "INSERT INTO attendance "
            "(staff_id, staff_name, work_date, arrived_at, lat, lng, on_time, late_minutes, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(staff_id, work_date) DO UPDATE SET "
            "staff_name=excluded.staff_name, arrived_at=excluded.arrived_at, lat=excluded.lat, "
            "lng=excluded.lng, on_time=excluded.on_time, late_minutes=excluded.late_minutes, "
            "status=excluded.status WHERE attendance.arrived_at IS NULL",
            (staff_id, staff_name, work_date, arrived_at, lat, lng,
             1 if on_time else 0, int(late_minutes), "ok" if on_time else "late"),
        )
        return cur.rowcount > 0


def record_absent(staff_id, staff_name, work_date) -> bool:
    """Log a no-show at the roll call (once per staff per day) so the monthly
    attendance statistics include absences, not only arrivals."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO attendance "
            "(staff_id, staff_name, work_date, arrived_at, on_time, late_minutes, status) "
            "VALUES (?, ?, ?, NULL, 0, 0, 'absent')",
            (staff_id, staff_name, work_date),
        )
        return cur.rowcount > 0


def arrival_for(staff_id, work_date) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM attendance WHERE staff_id = ? AND work_date = ? AND arrived_at IS NOT NULL",
            (staff_id, work_date),
        ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Cleaning sessions (до / после)
# ---------------------------------------------------------------------------
def open_session(apartment: str) -> dict | None:
    """The unfinished session for this apartment, whatever day it started on
    (a cleaning that started at 23:40 is still open after midnight)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM cleaning_sessions WHERE apartment = ? "
            "AND finished_at IS NULL ORDER BY id DESC LIMIT 1",
            (apartment,),
        ).fetchone()
        return dict(row) if row else None


def open_session_for_staff(staff_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM cleaning_sessions WHERE staff_id = ? "
            "AND finished_at IS NULL ORDER BY id DESC LIMIT 1",
            (staff_id,),
        ).fetchone()
        return dict(row) if row else None


def open_sessions() -> list[dict]:
    """All unfinished sessions, oldest first."""
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM cleaning_sessions WHERE finished_at IS NULL ORDER BY started_at"
        )
        return [dict(r) for r in cur.fetchall()]


def stale_open_sessions(started_before: str) -> list[dict]:
    """Unfinished sessions that started before the given timestamp — the
    cleaner never sent the «после» report."""
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM cleaning_sessions WHERE finished_at IS NULL AND started_at < ? "
            "ORDER BY started_at", (started_before,),
        )
        return [dict(r) for r in cur.fetchall()]


def start_session(apartment, staff_id, staff_name, work_date, started_at, travel_min) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO cleaning_sessions (apartment, staff_id, staff_name, work_date, "
            "started_at, travel_min) VALUES (?, ?, ?, ?, ?, ?)",
            (apartment, staff_id, staff_name, work_date, started_at, travel_min),
        )
        return cur.lastrowid


def finish_session(session_id: int, finished_at: str, duration_min, forced: bool = False) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE cleaning_sessions SET finished_at = ?, duration_min = ?, forced = ? WHERE id = ?",
            (finished_at, duration_min, 1 if forced else 0, session_id),
        )


def add_closed_session(apartment, staff_id, staff_name, work_date, finished_at) -> int:
    """A "после" report with no "до" before it: record the cleaning as done
    without timing."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO cleaning_sessions (apartment, staff_id, staff_name, work_date, "
            "finished_at, no_before) VALUES (?, ?, ?, ?, ?, 1)",
            (apartment, staff_id, staff_name, work_date, finished_at),
        )
        return cur.lastrowid


def last_finished_session(staff_id: int, work_date: str) -> dict | None:
    """The cleaner's last real «после» today — the reference point for the
    travel time to the next apartment. Force-closed rows are excluded (their
    finished_at is the moment of the auto-close, not when the person left)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM cleaning_sessions WHERE staff_id = ? AND work_date = ? "
            "AND finished_at IS NOT NULL AND forced = 0 ORDER BY finished_at DESC LIMIT 1",
            (staff_id, work_date),
        ).fetchone()
        return dict(row) if row else None


def session_for(apartment: str, work_date: str) -> dict | None:
    """Latest session (open or closed) of this apartment on this date — what
    the dashboard card shows."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM cleaning_sessions WHERE apartment = ? AND work_date = ? "
            "ORDER BY id DESC LIMIT 1", (apartment, work_date),
        ).fetchone()
        return dict(row) if row else None


def sessions_for_date(work_date: str) -> list[dict]:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM cleaning_sessions WHERE work_date = ? ORDER BY id", (work_date,)
        )
        return [dict(r) for r in cur.fetchall()]


def cleaning_sessions_month(ym: str) -> list[dict]:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM cleaning_sessions WHERE work_date LIKE ? ORDER BY work_date DESC, id DESC",
            (ym + "%",),
        )
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Supplies (shopping list)
# ---------------------------------------------------------------------------
def add_supply(apartment, item: str, qty: int, staff_name: str, created_at: str,
               src_key=None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO supplies (apartment, item, qty, staff_name, created_at, src_key) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (apartment or None, item, max(1, int(qty or 1)), staff_name or "", created_at,
             src_key or None),
        )
        return cur.lastrowid


def open_supplies() -> list[dict]:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM supplies WHERE bought_at IS NULL ORDER BY created_at, id"
        )
        return [dict(r) for r in cur.fetchall()]


def bought_supplies(limit: int = 60) -> list[dict]:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM supplies WHERE bought_at IS NOT NULL ORDER BY bought_at DESC, id DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]


def set_supply_bought(supply_id: int, bought: bool) -> None:
    at = datetime.now().isoformat(timespec="minutes") if bought else None
    with get_conn() as conn:
        conn.execute("UPDATE supplies SET bought_at = ? WHERE id = ?", (at, supply_id))


def delete_supply(supply_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM supplies WHERE id = ?", (supply_id,))


def set_topic(chat_id: int, role: str, thread_id, name: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO chat_topics (chat_id, role, thread_id, name) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id, role) DO UPDATE SET thread_id=excluded.thread_id, name=excluded.name",
            (chat_id, role, thread_id, name),
        )


def get_topic(chat_id: int, role: str):
    """Thread id for a message role in this chat, falling back to the 'general'
    mapping; None means the chat's default (General) topic."""
    with get_conn() as conn:
        for r in (role, "general"):
            row = conn.execute(
                "SELECT thread_id FROM chat_topics WHERE chat_id = ? AND role = ?",
                (chat_id, r),
            ).fetchone()
            if row is not None:
                return row[0]
    return None


def topic_binding(chat_id: int, role: str):
    """The binding row for exactly this role (no fallback to 'general'), or
    None when the chat has no such binding."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM chat_topics WHERE chat_id = ? AND role = ?", (chat_id, role)
        ).fetchone()
        return dict(row) if row is not None else None


def chat_topics(chat_id: int) -> list[dict]:
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM chat_topics WHERE chat_id = ? ORDER BY role", (chat_id,))
        return [dict(r) for r in cur.fetchall()]


def all_topics() -> list[dict]:
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM chat_topics ORDER BY chat_id, role")
        return [dict(r) for r in cur.fetchall()]


def get_bot_message(key: str, chat_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM bot_messages WHERE key = ? AND chat_id = ?", (key, chat_id)
        ).fetchone()
        return dict(row) if row is not None else None


def set_bot_message(key: str, chat_id: int, message_id: int, text_hash: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO bot_messages (key, chat_id, message_id, text_hash, sent_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(key, chat_id) DO UPDATE SET "
            "message_id=excluded.message_id, text_hash=excluded.text_hash, sent_at=excluded.sent_at",
            (key, chat_id, message_id, text_hash, datetime.now().isoformat(timespec="seconds")),
        )


def prune_bot_messages(before_iso: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM bot_messages WHERE sent_at < ?", (before_iso,))


def distinct_apartments() -> list[str]:
    """Apartment names present in the synced bookings — lets the bot process
    see units auto-discovered by the backend (they run as separate processes,
    so the in-memory map refresh doesn't reach the bot)."""
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT DISTINCT apartment_name FROM bookings WHERE apartment_name IS NOT NULL"
        )
        return [r[0] for r in cur.fetchall()]


def upsert_staff(staff_id: int, name: str, username: str) -> None:
    """Auto-register a staff member on any bot interaction; refresh name and
    reactivate (a returning person starts being tracked again)."""
    from datetime import datetime
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO staff_registry (staff_id, name, username, first_seen, active) "
            "VALUES (?, ?, ?, ?, 1) "
            "ON CONFLICT(staff_id) DO UPDATE SET name=excluded.name, "
            "username=excluded.username, active=1",
            (staff_id, name or "", username or "", datetime.now().isoformat(timespec="seconds")),
        )


def all_staff(active_only: bool = True) -> list[dict]:
    with get_conn() as conn:
        q = "SELECT * FROM staff_registry"
        if active_only:
            q += " WHERE active = 1"
        cur = conn.execute(q + " ORDER BY name")
        return [dict(r) for r in cur.fetchall()]


def set_staff_active(staff_id: int, active: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE staff_registry SET active = ? WHERE staff_id = ?",
            (1 if active else 0, staff_id),
        )


def add_penalty(kind: str, staff: str, amount: float, reason: str, at: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO penalties (kind, staff, amount, reason, at) VALUES (?, ?, ?, ?, ?)",
            (kind, staff, amount, reason, at),
        )
        return cur.lastrowid


def delete_penalty(penalty_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM penalties WHERE id = ?", (penalty_id,))


def upsert_channel_payment(chat_id: int, msg_id: int, at: str, raw_text: str,
                           parsed: dict, booking_id, match_score: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO channel_payments (chat_id, msg_id, at, raw_text, amount, currency, "
            "method, apartment, checkin, checkout, rc_ref, booking_id, match_score) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id, msg_id) DO UPDATE SET raw_text=excluded.raw_text, "
            "amount=excluded.amount, currency=excluded.currency, method=excluded.method, "
            "apartment=excluded.apartment, checkin=excluded.checkin, checkout=excluded.checkout, "
            "rc_ref=excluded.rc_ref, booking_id=excluded.booking_id, match_score=excluded.match_score",
            (chat_id, msg_id, at, raw_text, parsed.get("amount"), parsed.get("currency"),
             parsed.get("method"), parsed.get("apartment"), parsed.get("checkin"),
             parsed.get("checkout"), parsed.get("rc_ref"), booking_id, match_score),
        )


def channel_payments_month(ym: str) -> list[dict]:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM channel_payments WHERE at LIKE ? OR checkin LIKE ? "
            "ORDER BY at DESC", (ym + "%", ym + "%"),
        )
        return [dict(r) for r in cur.fetchall()]


def bookings_month(ym: str) -> list[dict]:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM bookings WHERE begin_date LIKE ? ORDER BY begin_date, apartment_name",
            (ym + "%",),
        )
        return [dict(r) for r in cur.fetchall()]


def set_staff_terms(staff: str, salary: float, pay_note: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO staff_terms (staff, salary, pay_note) VALUES (?, ?, ?) "
            "ON CONFLICT(staff) DO UPDATE SET salary=excluded.salary, pay_note=excluded.pay_note",
            (staff, salary, pay_note),
        )


def all_staff_terms() -> list[dict]:
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM staff_terms ORDER BY staff")
        return [dict(r) for r in cur.fetchall()]


def add_salary_payment(staff: str, amount: float, note: str, at: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO salary_payments (staff, amount, note, at) VALUES (?, ?, ?, ?)",
            (staff, amount, note, at),
        )
        return cur.lastrowid


def update_salary_payment(payment_id: int, staff: str, amount: float, note: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE salary_payments SET staff = ?, amount = ?, note = ? WHERE id = ?",
            (staff, amount, note, payment_id),
        )


def update_channel_payment_fields(chat_id: int, msg_id: int, amount, method) -> None:
    """Owner's manual completion of a parsed channel post (missing sum/method)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE channel_payments SET amount = COALESCE(?, amount), "
            "method = COALESCE(?, method) WHERE chat_id = ? AND msg_id = ?",
            (amount, method, chat_id, msg_id),
        )


def delete_salary_payment(payment_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM salary_payments WHERE id = ?", (payment_id,))


def salary_payments_month(ym: str) -> list[dict]:
    """Payments whose timestamp starts with 'YYYY-MM'."""
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM salary_payments WHERE at LIKE ? ORDER BY at DESC, id DESC",
            (ym + "%",),
        )
        return [dict(r) for r in cur.fetchall()]


def penalties_month(ym: str) -> list[dict]:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM penalties WHERE at LIKE ? ORDER BY at", (ym + "%",)
        )
        return [dict(r) for r in cur.fetchall()]


def attendance_month(ym: str) -> list[dict]:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM attendance WHERE work_date LIKE ? ORDER BY work_date", (ym + "%",)
        )
        return [dict(r) for r in cur.fetchall()]


def has_penalty_marker(staff: str, marker: str) -> bool:
    """True if a penalty whose reason starts with `marker` already exists for
    this staff member — used to make auto-fines idempotent."""
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT 1 FROM penalties WHERE staff = ? AND reason LIKE ? LIMIT 1",
            (staff, marker + "%"),
        )
        return cur.fetchone() is not None


def all_penalties() -> list[dict]:
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM penalties ORDER BY at DESC, id DESC")
        return [dict(r) for r in cur.fetchall()]


def attendance_for(work_date, arrived_only: bool = True) -> list[dict]:
    """Attendance rows for a day. By default only real arrivals — the roll
    call also stores no-show rows (status 'absent', arrived_at NULL) for the
    statistics, and those must not look like arrivals elsewhere."""
    q = "SELECT * FROM attendance WHERE work_date = ?"
    if arrived_only:
        q += " AND arrived_at IS NOT NULL"
    with get_conn() as conn:
        cur = conn.execute(q + " ORDER BY arrived_at", (work_date,))
        return [dict(r) for r in cur.fetchall()]


def job_done(job: str, day: str) -> bool:
    with get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM job_runs WHERE job = ? AND day = ?", (job, day)
        ).fetchone() is not None


def mark_job(job: str, day: str) -> bool:
    """Record a daily job as done. Returns False if it was already recorded."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO job_runs (job, day, ran_at) VALUES (?, ?, ?)",
            (job, day, datetime.now().isoformat(timespec="seconds")),
        )
        return cur.rowcount > 0


def salary_payment_recent(staff: str, amount: float, note: str, since: str) -> bool:
    """A second tap on «Записать выплату» must not create a second payment."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM salary_payments WHERE staff = ? AND amount = ? AND note = ? AND at >= ? LIMIT 1",
            (staff, amount, note, since),
        ).fetchone() is not None


def penalty_recent(kind: str, staff: str, amount: float, reason: str, since: str) -> bool:
    with get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM penalties WHERE kind = ? AND staff = ? AND amount = ? AND reason = ? "
            "AND at >= ? LIMIT 1",
            (kind, staff, amount, reason, since),
        ).fetchone() is not None


def count_bookings() -> int:
    with get_conn() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0])


def prune_booking_snapshots(end_before: str) -> int:
    """Forget snapshots of stays that ended long ago (they only exist to
    detect edits of upcoming/current bookings)."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM booking_snapshots WHERE end_date IS NOT NULL AND end_date < ?",
            (end_before,),
        )
        return cur.rowcount


def known_booking_ids() -> set:
    with get_conn() as conn:
        return {r[0] for r in conn.execute("SELECT booking_id FROM notified_bookings").fetchall()}


def add_notified_bookings(ids) -> None:
    ids = [(i,) for i in ids]
    if not ids:
        return
    with get_conn() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO notified_bookings (booking_id) VALUES (?)", ids
        )


def get_booking_snapshots() -> dict:
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM booking_snapshots")
        return {r["booking_id"]: dict(r) for r in cur.fetchall()}


def upsert_booking_snapshots(bookings) -> None:
    rows = [
        (b.get("id"), b.get("apartment_name"), b.get("begin_date"), b.get("end_date"),
         _num(b.get("amount")), b.get("status"), b.get("client_name"), b.get("client_phone"))
        for b in bookings if b.get("id") is not None
    ]
    if not rows:
        return
    with get_conn() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO booking_snapshots "
            "(booking_id, apartment_name, begin_date, end_date, amount, status, client_name, client_phone) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def list_tasks() -> list[dict]:
    with get_conn() as conn:
        cur = conn.execute(
            # open tasks first, soonest deadline first, undated last
            "SELECT * FROM tasks "
            "ORDER BY status = 'done', deadline IS NULL, deadline, id"
        )
        return [dict(r) for r in cur.fetchall()]


def set_task_status(task_id: int, status: str) -> None:
    done_at = datetime.now().isoformat(timespec="seconds") if status == "done" else None
    with get_conn() as conn:
        conn.execute(
            "UPDATE tasks SET status = ?, done_at = ? WHERE id = ?",
            (status, done_at, task_id),
        )


def delete_task(task_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------
def add_payment(payment: dict) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO payments_cash (booking_id, apartment_name, amount, currency, method, paid_at, note)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payment.get("booking_id"),
                payment.get("apartment_name"),
                payment.get("amount"),
                payment.get("currency", "UZS"),
                payment.get("method", "наличные"),
                payment.get("paid_at"),
                payment.get("note", ""),
            ),
        )
        return cur.lastrowid


# ---------------------------------------------------------------------------
# Sync log
# ---------------------------------------------------------------------------
def log_sync(bookings_count: int, success: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sync_log (bookings_count, success) VALUES (?, ?)",
            (bookings_count, 1 if success else 0),
        )


def last_sync() -> str | None:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT synced_at FROM sync_log WHERE success = 1 ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
        return row["synced_at"] if row else None
