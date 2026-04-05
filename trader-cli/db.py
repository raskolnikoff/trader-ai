"""
SQLite storage layer for conversation history, session state, and analysis results.
Uses FTS5 trigram tokenizer for full-text search over mixed Latin/CJK content.
"""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Allow tests or callers to override the DB location via environment variable.
_default_db_dir = Path(__file__).parent.parent / "data"
DB_DIR = Path(os.environ.get("TRADER_DB_DIR", str(_default_db_dir)))
DB_PATH = DB_DIR / "trader.db"


def get_connection() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def initialize_db() -> None:
    """Create all tables and FTS5 index if they do not already exist."""
    with get_connection() as conn:
        # ── messages ──────────────────────────────────────────────────────────
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

        # FTS5 virtual table using the trigram tokenizer so that mixed
        # Latin/CJK text (e.g. "BTCどう？") is matched correctly without
        # depending on word-boundary tokenisation.
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

        # Keep FTS index in sync via triggers
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

        # ── session — single-row table for persisted UI state ─────────────────
        # Constrained to id=1 so there is always exactly one session record.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session (
                id         INTEGER PRIMARY KEY CHECK(id = 1),
                symbol     TEXT,
                timeframe  TEXT,
                updated_at TEXT NOT NULL
            )
        """)

        # ── analysis_results — compact summaries used for RAG retrieval ───────
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


# ── messages ──────────────────────────────────────────────────────────────────

def save_message(
    role: str,
    content: str,
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
) -> int:
    """Persist a user or assistant message. Returns the new row id."""
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
    """Return the most recent messages, oldest first (chronological order)."""
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


def search_messages(query: str, limit: int = 5) -> list[dict]:
    """
    Full-text search over message content using FTS5 trigram tokenizer.
    Returns up to `limit` ranked results.
    """
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


# ── session ───────────────────────────────────────────────────────────────────

def get_session() -> dict:
    """
    Return the persisted session state.
    Always returns a dict with 'symbol' and 'timeframe' keys (may be None).
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT symbol, timeframe FROM session WHERE id = 1"
        ).fetchone()
    return dict(row) if row else {"symbol": None, "timeframe": None}


def update_session(
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
) -> None:
    """Upsert the session row. Only non-None values overwrite existing fields."""
    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT symbol, timeframe FROM session WHERE id = 1"
        ).fetchone()

        if existing:
            new_symbol = symbol if symbol is not None else existing["symbol"]
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


# ── analysis_results ──────────────────────────────────────────────────────────

def save_analysis_result(
    summary: str,
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
) -> int:
    """Persist a compact analysis summary. Returns the new row id."""
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
    """Return recent analysis summaries, oldest first."""
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
