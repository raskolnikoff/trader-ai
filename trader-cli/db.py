"""
SQLite storage layer for conversation history, session state, and analysis results.
Uses FTS5 trigram tokenizer for full-text search over mixed Latin/CJK content.
"""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_default_db_dir = Path(__file__).parent.parent / "data"
DB_DIR  = Path(os.environ.get("TRADER_DB_DIR", str(_default_db_dir)))
DB_PATH = DB_DIR / "trader.db"


def get_connection() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def initialize_db() -> None:
    """Create all tables and FTS5 index if they do not already exist."""
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                role        TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content     TEXT NOT NULL,
                symbol      TEXT,
                timeframe   TEXT,
                created_at  TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
            USING fts5(
                content,
                role,
                symbol,
                content=messages,
                content_rowid=id,
                tokenize='trigram'
            )
        """)

        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS messages_ai
            AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content, role, symbol)
                VALUES (new.id, new.content, new.role, new.symbol);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS messages_ad
            AFTER DELETE ON messages BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, content, role, symbol)
                VALUES ('delete', old.id, old.content, old.role, old.symbol);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS messages_au
            AFTER UPDATE ON messages BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, content, role, symbol)
                VALUES ('delete', old.id, old.content, old.role, old.symbol);
                INSERT INTO messages_fts(rowid, content, role, symbol)
                VALUES (new.id, new.content, new.role, new.symbol);
            END
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS session (
                id         INTEGER PRIMARY KEY CHECK(id = 1),
                symbol     TEXT,
                timeframe  TEXT,
                updated_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS analysis_results (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol     TEXT,
                timeframe  TEXT,
                summary    TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        conn.commit()


# -- messages ------------------------------------------------------------------

def save_message(
    role: str,
    content: str,
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
) -> int:
    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO messages (role, content, symbol, timeframe, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (role, content, symbol, timeframe, timestamp),
        )
        conn.commit()
        return cursor.lastrowid


def get_recent_messages(limit: int = 5) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, role, content, symbol, timeframe, created_at
            FROM messages
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def get_recent_messages_by_symbol(symbol: str, limit: int = 5) -> list[dict]:
    """Return recent messages filtered by symbol (case-insensitive)."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, role, content, symbol, timeframe, created_at
            FROM messages
            WHERE upper(symbol) = upper(?)
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (symbol, limit),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def search_messages(query: str, limit: int = 5) -> list[dict]:
    """Full-text search using FTS5 trigram tokenizer."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT m.id, m.role, m.content, m.symbol, m.timeframe, m.created_at
            FROM messages_fts
            JOIN messages m ON messages_fts.rowid = m.id
            WHERE messages_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def search_messages_by_symbol(query: str, symbol: str, limit: int = 5) -> list[dict]:
    """
    Full-text search filtered by symbol.
    Combines FTS5 relevance ranking with SQL symbol filter.
    Falls back to unfiltered search if no results.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT m.id, m.role, m.content, m.symbol, m.timeframe, m.created_at
            FROM messages_fts
            JOIN messages m ON messages_fts.rowid = m.id
            WHERE messages_fts MATCH ?
              AND upper(m.symbol) = upper(?)
            ORDER BY rank
            LIMIT ?
            """,
            (query, symbol, limit),
        ).fetchall()
    results = [dict(row) for row in rows]
    if not results:
        # fallback to unfiltered search
        return search_messages(query, limit=limit)
    return results


# -- session -------------------------------------------------------------------

def get_session() -> dict:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT symbol, timeframe FROM session WHERE id = 1"
        ).fetchone()
    return dict(row) if row else {"symbol": None, "timeframe": None}


def update_session(
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT symbol, timeframe FROM session WHERE id = 1"
        ).fetchone()
        if existing:
            new_symbol    = symbol    if symbol    is not None else existing["symbol"]
            new_timeframe = timeframe if timeframe is not None else existing["timeframe"]
            conn.execute(
                "UPDATE session SET symbol = ?, timeframe = ?, updated_at = ? WHERE id = 1",
                (new_symbol, new_timeframe, timestamp),
            )
        else:
            conn.execute(
                "INSERT INTO session (id, symbol, timeframe, updated_at) VALUES (1, ?, ?, ?)",
                (symbol, timeframe, timestamp),
            )
        conn.commit()


# -- analysis_results ----------------------------------------------------------

def save_analysis_result(
    summary: str,
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
) -> int:
    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO analysis_results (symbol, timeframe, summary, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (symbol, timeframe, summary, timestamp),
        )
        conn.commit()
        return cursor.lastrowid


def get_recent_analysis_results(limit: int = 5) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, symbol, timeframe, summary, created_at
            FROM analysis_results
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def get_analysis_results_by_symbol(symbol: str, limit: int = 5) -> list[dict]:
    """Return analysis_results filtered by symbol (case-insensitive)."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, symbol, timeframe, summary, created_at
            FROM analysis_results
            WHERE upper(symbol) = upper(?)
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (symbol, limit),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]
