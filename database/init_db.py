"""Standalone script: initialize the SQLite database from schema.sql."""

import sqlite3
from pathlib import Path

DATABASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = DATABASE_DIR.parent
SCHEMA_PATH = DATABASE_DIR / "schema.sql"
DB_PATH = PROJECT_ROOT / "data" / "trading_agent.db"


def initialize_database(db_path: Path = DB_PATH, schema_path: Path = SCHEMA_PATH) -> Path:
    """Create the database file and apply schema.sql. Safe to run repeatedly
    since every statement in schema.sql is CREATE ... IF NOT EXISTS."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = Path(schema_path).read_text()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(schema_sql)
        conn.commit()
    finally:
        conn.close()

    return db_path


if __name__ == "__main__":
    path = initialize_database()
    print(f"Database initialized at {path}")
