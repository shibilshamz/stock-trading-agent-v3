"""Reusable SQLite connection module for the trading agent."""

import os
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from init_db import initialize_database, DB_PATH as DEFAULT_DB_PATH  # noqa: E402


def get_db_path() -> Path:
    """Resolve the SQLite database file path, honoring the DB_PATH env var."""
    return Path(os.environ.get("DB_PATH", DEFAULT_DB_PATH))


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a new SQLite connection with foreign keys enforced and Row access."""
    path = Path(db_path) if db_path else get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_db(db_path: Path | str | None = None):
    """Context manager yielding a connection; commits on success, rolls back on error."""
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_database(db_path: Path | str | None = None) -> Path:
    """Create all tables and indexes from schema.sql. Idempotent - safe to call
    multiple times (schema.sql uses CREATE ... IF NOT EXISTS throughout)."""
    path = Path(db_path) if db_path else get_db_path()
    return initialize_database(db_path=path)


if __name__ == "__main__":
    path = init_database()
    print(f"Database initialized at {path}")
