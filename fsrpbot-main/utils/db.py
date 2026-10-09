"""
utils/db.py — SQLite helpers for infractions, session state, and session log.
"""
import sqlite3, os, threading
from datetime import datetime

_BASE_DIRS = []
for candidate in [
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")),
    "/home/container/fsrpbot",
    "/home/container",
]:
    if candidate and candidate not in _BASE_DIRS:
        _BASE_DIRS.append(candidate)

DB_PATH = None
for base_dir in _BASE_DIRS:
    candidate = os.path.join(base_dir, "data", "bot.db")
    if os.path.isdir(base_dir):
        try:
            parent_dir = os.path.dirname(candidate)
            os.makedirs(parent_dir, exist_ok=True)
            with open(candidate, "a"):
                pass
            DB_PATH = candidate
            break
        except Exception:
            continue

if DB_PATH is None:
    DB_PATH = os.path.join(_BASE_DIRS[0], "data", "bot.db")

_lock   = threading.Lock()


def _connect():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist."""
    with _lock, _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS infractions (
                case_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id        INTEGER NOT NULL,
                user_id         INTEGER NOT NULL,
                issuer_id       INTEGER NOT NULL,
                punishment      TEXT    NOT NULL,
                reason          TEXT    NOT NULL,
                status          TEXT    NOT NULL DEFAULT 'Active',
                log_channel_id  INTEGER,
                log_message_id  INTEGER,
                dm_channel_id   INTEGER,
                dm_message_id   INTEGER,
                created_at      TEXT    NOT NULL,
                updated_at      TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS session_state (
                guild_id        INTEGER PRIMARY KEY,
                active          INTEGER NOT NULL DEFAULT 0,
                type            TEXT,
                started_at      TEXT,
                host_id         INTEGER,
                full_msg_id     INTEGER,
                full_channel_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS session_log (
                session_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id      INTEGER NOT NULL,
                type          TEXT    NOT NULL,
                host_id       INTEGER NOT NULL,
                started_at    TEXT    NOT NULL,
                ended_at      TEXT,
                duration_secs INTEGER,
                -- Session boosts are disabled.
                -- boost_count   INTEGER NOT NULL DEFAULT 0,
                peak_players  INTEGER,
                ended_by_id   INTEGER,
                notes         TEXT
            );

            CREATE TABLE IF NOT EXISTS suspension_snapshots (
                guild_id  INTEGER NOT NULL,
                user_id   INTEGER NOT NULL,
                role_ids  TEXT    NOT NULL,
                suspended_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );
        """)


# ── Infractions ───────────────────────────────────────────────

def create_infraction(guild_id, user_id, issuer_id, punishment, reason):
    now = datetime.utcnow().isoformat()
    with _lock, _connect() as conn:
        cur = conn.execute(
            """INSERT INTO infractions
               (guild_id, user_id, issuer_id, punishment, reason, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?)""",
            (guild_id, user_id, issuer_id, punishment, reason, now, now)
        )
        return cur.lastrowid


def update_infraction_log(case_id, log_channel_id, log_message_id,
                          dm_channel_id=None, dm_message_id=None):
    now = datetime.utcnow().isoformat()
    with _lock, _connect() as conn:
        conn.execute(
            """UPDATE infractions SET
               log_channel_id=?, log_message_id=?, dm_channel_id=?, dm_message_id=?,
               updated_at=?
               WHERE case_id=?""",
            (log_channel_id, log_message_id, dm_channel_id, dm_message_id, now, case_id)
        )


def edit_infraction(case_id, guild_id, *, punishment=None, reason=None, status=None):
    now = datetime.utcnow().isoformat()
    row = get_infraction(case_id, guild_id)
    if not row:
        return None
    punishment = punishment or row["punishment"]
    reason     = reason     or row["reason"]
    status     = status     or row["status"]
    with _lock, _connect() as conn:
        conn.execute(
            """UPDATE infractions SET punishment=?, reason=?, status=?, updated_at=?
               WHERE case_id=? AND guild_id=?""",
            (punishment, reason, status, now, case_id, guild_id)
        )
    return get_infraction(case_id, guild_id)


def get_infraction(case_id, guild_id):
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM infractions WHERE case_id=? AND guild_id=?",
            (case_id, guild_id)
        ).fetchone()


def get_active_infractions(guild_id, user_id):
    with _connect() as conn:
        return conn.execute(
            """SELECT * FROM infractions
               WHERE guild_id=? AND user_id=? AND status='Active'
               ORDER BY case_id""",
            (guild_id, user_id)
        ).fetchall()


def get_all_infractions(guild_id, user_id):
    with _connect() as conn:
        return conn.execute(
            """SELECT * FROM infractions
               WHERE guild_id=? AND user_id=?
               ORDER BY case_id""",
            (guild_id, user_id)
        ).fetchall()


# ── Session state (current live session) ──────────────────────

def get_session(guild_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM session_state WHERE guild_id=?", (guild_id,)
        ).fetchone()
    return dict(row) if row else None


def upsert_session(guild_id, **kwargs):
    existing = get_session(guild_id)
    if existing:
        sets = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [guild_id]
        with _lock, _connect() as conn:
            conn.execute(f"UPDATE session_state SET {sets} WHERE guild_id=?", vals)
    else:
        kwargs["guild_id"] = guild_id
        cols = ", ".join(kwargs.keys())
        qs   = ", ".join("?" * len(kwargs))
        with _lock, _connect() as conn:
            conn.execute(
                f"INSERT INTO session_state ({cols}) VALUES ({qs})",
                list(kwargs.values())
            )


# ── Session log (permanent history) ───────────────────────────

def create_session_log(guild_id, type_, host_id, started_at):
    """Insert a new session log entry. Returns the session_id."""
    with _lock, _connect() as conn:
        cur = conn.execute(
            """INSERT INTO session_log (guild_id, type, host_id, started_at)
               VALUES (?,?,?,?)""",
            (guild_id, type_, host_id, started_at)
        )
        return cur.lastrowid


def close_session_log(session_id, ended_by_id, peak_players=None, notes=None):
    """Mark a session as ended and calculate duration."""
    now = datetime.utcnow().isoformat()

    with _connect() as conn:
        row = conn.execute(
            "SELECT started_at FROM session_log WHERE session_id=?", (session_id,)
        ).fetchone()

    if not row:
        return

    try:
        started = datetime.fromisoformat(row["started_at"])
        ended   = datetime.fromisoformat(now)
        duration = int((ended - started).total_seconds())
    except Exception:
        duration = None

    with _lock, _connect() as conn:
        conn.execute(
            """UPDATE session_log SET
               ended_at=?, duration_secs=?, ended_by_id=?, peak_players=?, notes=?
               WHERE session_id=?""",
            (now, duration, ended_by_id, peak_players, notes, session_id)
        )


# Session boosts are disabled.
# def increment_boost(session_id):
#     """Increment the boost counter for a session."""
#     with _lock, _connect() as conn:
#         conn.execute(
#             "UPDATE session_log SET boost_count = boost_count + 1 WHERE session_id=?",
#             (session_id,)
#         )


def update_peak_players(session_id, count):
    """Update peak player count if the new count is higher."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT peak_players FROM session_log WHERE session_id=?", (session_id,)
        ).fetchone()
    if row and (row["peak_players"] is None or count > row["peak_players"]):
        with _lock, _connect() as conn:
            conn.execute(
                "UPDATE session_log SET peak_players=? WHERE session_id=?",
                (count, session_id)
            )


def get_session_log(session_id):
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM session_log WHERE session_id=?", (session_id,)
        ).fetchone()


def get_recent_sessions(guild_id, limit=10):
    """Return the most recent sessions for a guild."""
    with _connect() as conn:
        return conn.execute(
            """SELECT * FROM session_log WHERE guild_id=?
               ORDER BY session_id DESC LIMIT ?""",
            (guild_id, limit)
        ).fetchall()


# ── Migration (safe to run on existing DBs) ───────────────────

def migrate_db():
    """Add columns/tables that may not exist in older versions of the DB."""
    with _lock, _connect() as conn:
        # Add session_log_id to session_state if missing
        try:
            conn.execute("ALTER TABLE session_state ADD COLUMN session_log_id INTEGER")
        except Exception:
            pass
        # Create suspension_snapshots if missing (older installs)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS suspension_snapshots (
                guild_id     INTEGER NOT NULL,
                user_id      INTEGER NOT NULL,
                role_ids     TEXT    NOT NULL,
                suspended_at TEXT    NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )
        """)


# ── Suspension snapshots ──────────────────────────────────────

def save_suspension(guild_id: int, user_id: int, role_ids: list[int]):
    now = datetime.utcnow().isoformat()
    role_str = ",".join(str(r) for r in role_ids)
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO suspension_snapshots
               (guild_id, user_id, role_ids, suspended_at) VALUES (?,?,?,?)""",
            (guild_id, user_id, role_str, now)
        )


def load_suspension(guild_id: int, user_id: int) -> list[int]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT role_ids FROM suspension_snapshots WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)
        ).fetchone()
    if not row or not row["role_ids"]:
        return []
    return [int(r) for r in row["role_ids"].split(",") if r]


def clear_suspension(guild_id: int, user_id: int):
    with _lock, _connect() as conn:
        conn.execute(
            "DELETE FROM suspension_snapshots WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)
        )
