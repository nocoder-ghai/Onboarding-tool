"""SQLite access. One connection per thread (the HTTP server is threaded)."""

import contextlib
import datetime
import os
import sqlite3
import threading

from . import config

_local = threading.local()


def now():
    """UTC timestamp string used for every *_at column."""
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat(sep=" ")


def parse_ts(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(str(value), fmt)
        except ValueError:
            continue
    return None


def days_since(value):
    ts = parse_ts(value)
    if ts is None:
        return None
    return (datetime.datetime.utcnow() - ts).days


def connect():
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
        conn = sqlite3.connect(config.DB_PATH, timeout=15,
                               detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 15000")
        _local.conn = conn
    return conn


def close():
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


def query(sql, args=()):
    return connect().execute(sql, args).fetchall()


def one(sql, args=()):
    return connect().execute(sql, args).fetchone()


def scalar(sql, args=(), default=None):
    row = one(sql, args)
    if row is None:
        return default
    value = row[0]
    return default if value is None else value


def execute(sql, args=()):
    conn = connect()
    with conn:
        cur = conn.execute(sql, args)
        return cur.lastrowid


def execute_many(sql, seq):
    conn = connect()
    with conn:
        conn.executemany(sql, seq)


@contextlib.contextmanager
def transaction():
    """Commit on success, roll back on any exception."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def insert(table, values):
    cols = list(values.keys())
    sql = "INSERT INTO %s (%s) VALUES (%s)" % (
        table, ", ".join(cols), ", ".join("?" * len(cols)))
    return execute(sql, [values[c] for c in cols])


def update(table, row_id, values):
    if not values:
        return 0
    cols = list(values.keys())
    sql = "UPDATE %s SET %s WHERE id = ?" % (
        table, ", ".join("%s = ?" % c for c in cols))
    return execute(sql, [values[c] for c in cols] + [row_id])


def init_db():
    """Create every table. Safe to run repeatedly."""
    config.ensure_dirs()
    with open(config.SCHEMA_PATH, "r", encoding="utf-8") as fh:
        script = fh.read()
    conn = connect()
    with conn:
        conn.executescript(script)
        _migrate(conn)
    return config.DB_PATH


def _migrate(conn):
    """Small additive migrations for columns added after a DB already exists.
    `executescript`'s CREATE TABLE IF NOT EXISTS can't add columns to a table
    that's already there, so new columns get an explicit ALTER TABLE here."""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "captain_id" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN captain_id INTEGER "
                     "REFERENCES users(id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_captain "
                 "ON users(captain_id)")
    if "grade_cohort_id" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN grade_cohort_id INTEGER "
                     "REFERENCES grade_cohorts(id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_grade_cohort "
                 "ON users(grade_cohort_id)")
    if "db_id" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN db_id TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_db_id "
                 "ON users(db_id) WHERE db_id IS NOT NULL AND db_id != ''")

    for table in ("components", "sub_items", "links", "documents"):
        table_cols = [row[1] for row in
                      conn.execute("PRAGMA table_info(%s)" % table).fetchall()]
        if "grade_cohort_id" not in table_cols:
            conn.execute("ALTER TABLE %s ADD COLUMN grade_cohort_id INTEGER "
                         "REFERENCES grade_cohorts(id)" % table)


def table_exists(name):
    return one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
               (name,)) is not None


def setting(key, default=""):
    return scalar("SELECT value FROM settings WHERE key = ?", (key,), default)


def set_setting(key, value, description=""):
    execute(
        "INSERT INTO settings (key, value, description, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, str(value), description, now()))


def setting_int(key, default=0):
    try:
        return int(str(setting(key, default)).strip())
    except (TypeError, ValueError):
        return default
