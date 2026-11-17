"""Open the database and bring it to the schema this build understands."""

import sqlite3
from pathlib import Path

from fusion_service.settings import Settings

SCHEMA_VERSION = 1
SQL_DIR = Path(__file__).parent / "sql"
INITIAL_SCHEMA = SQL_DIR / "001_initial.sql"


def _schema_version(db: sqlite3.Connection) -> int | None:
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'",
    ).fetchone():
        return None
    row = db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
    return None if row is None else row[0]


def open_database(settings: Settings) -> sqlite3.Connection:
    """Connect, enforce size and version limits, create tables and seed derived metadata."""
    settings.database.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(settings.database, check_same_thread=False, timeout=5)
    try:
        db.row_factory = sqlite3.Row
        existing = _schema_version(db)
        if existing is not None and existing != SCHEMA_VERSION:
            raise ValueError("unsupported database version")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        pages = settings.max_database_bytes // db.execute("PRAGMA page_size").fetchone()[0]
        actual = db.execute(f"PRAGMA max_page_count={pages}").fetchone()[0]
        if actual > pages:
            raise ValueError("existing database exceeds max_database_bytes")
        db.executescript(INITIAL_SCHEMA.read_text())
        if _schema_version(db) != SCHEMA_VERSION:
            raise ValueError("unsupported database version")
        db.execute(
            "INSERT OR IGNORE INTO metadata SELECT 'event_bytes', COALESCE(SUM(size),0) FROM events",
        )
        db.commit()
    except BaseException:
        db.close()
        raise
    return db
