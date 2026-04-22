#!/usr/bin/env python3
"""
TradingView Overlay Webhook Server.

Receives Pine Script alerts from TradingView via webhook (HTTP POST)
and overlays Polymarket position data onto TradingView charts by:
  1. Storing incoming TV alerts in SQLite.
  2. Fetching open Polymarket CLOB trades for the configured wallet.
  3. Serving a combined JSON feed that a TradingView indicator can poll.

Architecture:
    TradingView alert --> POST /webhook  --> SQLite (tv_alerts)
    Polymarket CLOB   --> GET  /positions --> JSON feed
    TradingView UDI   --> GET  /feed      --> combined JSON

Usage:
    # Start server (default port 8765)
    python tv_overlay_webhook.py

    # Custom port
    python tv_overlay_webhook.py --port 9000

    # TradingView Pine Script alert URL:
    #   http://<your-ip>:8765/webhook
    # Body (JSON message in alert):
    #   {"symbol": "BTCUSDT", "price": {{close}}, "action": "{{strategy.order.action}}"}

Pine Script snippet to display Polymarket positions as labels:
    // indicator("Polymarket Overlay", overlay=true)
    // var table t = table.new(position.top_right, 2, 10)
    // // Poll /feed endpoint and render positions as labels on chart

Requirements:
    pip install py-clob-client python-dotenv
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

# -- Path setup ----------------------------------------------------------------

_ROOT = Path(__file__).parent.parent.parent.parent
_CLI  = Path(__file__).parent.parent.parent

_dotenv_path = _ROOT / ".env"
if _dotenv_path.exists():
    from dotenv import load_dotenv
    load_dotenv(_dotenv_path, override=True)

if str(_CLI) not in sys.path:
    sys.path.insert(0, str(_CLI))

# -- Logging -------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("tv_overlay")

# -- Constants -----------------------------------------------------------------

DB_PATH: Path         = _ROOT / "trader-cli" / "tv_overlay.db"
DEFAULT_PORT: int     = 8765
POSITION_TTL_SEC: int = 300  # refresh Polymarket positions every 5 minutes
CLOB_HOST: str        = "https://clob.polymarket.com"
CHAIN_ID: int         = 137


# -- Database ------------------------------------------------------------------

def init_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """
    Initialize SQLite database for TV alerts and position cache.

    Args:
        db_path: Path to the SQLite file.

    Returns:
        Open database connection.
    """
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tv_alerts (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ts        INTEGER NOT NULL,
            symbol    TEXT,
            price     REAL,
            action    TEXT,
            raw       TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS position_cache (
            id          INTEGER PRIMARY KEY CHECK (id = 1),
            updated_at  INTEGER NOT NULL,
            payload     TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


# -- Polymarket position fetcher -----------------------------------------------

def _build_clob_client():
    """Build authenticated ClobClient from env vars."""
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    creds = ApiCreds(
        api_key=os.environ["POLY_API_KEY"],
        api_secret=os.environ["POLY_SECRET"],
        api_passphrase=os.environ["POLY_PASSPHRASE"],
    )
    return ClobClient(
        host=CLOB_HOST,
        chain_id=CHAIN_ID,
        key=os.environ["POLY_PRIVATE_KEY"],
        creds=creds,
        signature_type=0,
    )


def fetch_polymarket_positions() -> list[dict[str, Any]]:
    """
    Fetch recent confirmed trades from Polymarket CLOB.

    Returns:
        List of position dicts: market, outcome, side, size, price, status, ts.
    """
    try:
        client = _build_clob_client()
        trades = client.get_trades()
    except Exception as exc:
        logger.error("Failed to fetch Polymarket trades: %s", exc)
        return []

    positions = []
    for t in trades:
        if t.get("status") != "CONFIRMED":
            continue
        positions.append({
            "market":  t.get("market", "")[:20],
            "outcome": t.get("outcome", ""),
            "side":    t.get("side", ""),
            "size":    float(t.get("size", 0)),
            "price":   float(t.get("price", 0)),
            "status":  t.get("status", ""),
            "ts":      int(t.get("match_time", 0)),
        })
    return positions


# -- Position cache (background refresh) --------------------------------------

class PositionCache:
    """
    Thread-safe cache for Polymarket positions.
    Refreshes every POSITION_TTL_SEC seconds on demand.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn    = conn
        self._lock    = threading.Lock()
        self._data:   list[dict] = []
        self._updated = 0

    def get(self) -> list[dict]:
        """Return cached positions, refreshing if stale."""
        now = int(time.time())
        with self._lock:
            if now - self._updated > POSITION_TTL_SEC:
                self._refresh()
        return self._data

    def _refresh(self) -> None:
        """Fetch fresh positions and update cache. Must hold self._lock."""
        logger.info("Refreshing Polymarket position cache...")
        positions     = fetch_polymarket_positions()
        self._data    = positions
        self._updated = int(time.time())
        payload = json.dumps(positions)
        self._conn.execute(
            "INSERT OR REPLACE INTO position_cache (id, updated_at, payload) VALUES (1, ?, ?)",
            (self._updated, payload),
        )
        self._conn.commit()
        logger.info("Position cache updated: %d positions", len(positions))


# -- HTTP request handler ------------------------------------------------------

def make_handler(conn: sqlite3.Connection, cache: PositionCache):
    """
    Factory that creates a request handler class with injected dependencies.

    Args:
        conn: SQLite connection.
        cache: PositionCache instance.

    Returns:
        BaseHTTPRequestHandler subclass.
    """

    class Handler(BaseHTTPRequestHandler):
        """Handles webhook and feed endpoints."""

        def log_message(self, fmt, *args):  # suppress default access log
            logger.debug("HTTP %s", fmt % args)

        # -- POST /webhook -----------------------------------------------------

        def _handle_webhook(self) -> None:
            """
            Accept a TradingView alert and store it in SQLite.

            Expected JSON body:
              {"symbol": "BTCUSDT", "price": 78000, "action": "buy"}
            """
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length).decode("utf-8")
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = {"raw": body}

            ts     = int(time.time())
            symbol = data.get("symbol", "")
            price  = data.get("price")
            action = data.get("action", "")

            conn.execute(
                "INSERT INTO tv_alerts (ts, symbol, price, action, raw) VALUES (?, ?, ?, ?, ?)",
                (ts, symbol, price, action, body),
            )
            conn.commit()

            logger.info(
                "TV alert received: symbol=%s price=%s action=%s",
                symbol, price, action,
            )
            self._respond(200, {"ok": True, "ts": ts})

        # -- GET /positions ----------------------------------------------------

        def _handle_positions(self) -> None:
            """Return current Polymarket positions as JSON."""
            positions = cache.get()
            self._respond(200, {"positions": positions, "count": len(positions)})

        # -- GET /feed ---------------------------------------------------------

        def _handle_feed(self) -> None:
            """
            Return combined feed: last 10 TV alerts + current positions.
            Consumed by TradingView User-Defined Indicator (UDI) polling.
            """
            cursor = conn.execute(
                "SELECT ts, symbol, price, action FROM tv_alerts ORDER BY ts DESC LIMIT 10"
            )
            alerts = [
                {"ts": r[0], "symbol": r[1], "price": r[2], "action": r[3]}
                for r in cursor.fetchall()
            ]
            positions = cache.get()
            self._respond(200, {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "alerts":     alerts,
                "positions":  positions,
            })

        # -- Router ------------------------------------------------------------

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/webhook":
                self._handle_webhook()
            else:
                self._respond(404, {"error": "not found"})

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/positions":
                self._handle_positions()
            elif self.path.startswith("/feed"):
                self._handle_feed()
            elif self.path == "/health":
                self._respond(200, {"ok": True})
            else:
                self._respond(404, {"error": "not found"})

        # -- Response helper ---------------------------------------------------

        def _respond(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

    return Handler


# -- Entry point ---------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="TradingView Overlay Webhook Server"
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"Port to listen on (default: {DEFAULT_PORT})",
    )
    args = parser.parse_args()

    conn  = init_db()
    cache = PositionCache(conn)

    # Warm up the position cache on startup
    cache.get()

    handler_class = make_handler(conn, cache)
    server = HTTPServer(("0.0.0.0", args.port), handler_class)

    logger.info("TV Overlay Webhook Server running on http://0.0.0.0:%d", args.port)
    logger.info("Endpoints:")
    logger.info("  POST http://localhost:%d/webhook   <- TradingView alerts", args.port)
    logger.info("  GET  http://localhost:%d/positions <- Polymarket positions", args.port)
    logger.info("  GET  http://localhost:%d/feed      <- Combined feed (TV UDI)", args.port)
    logger.info("  GET  http://localhost:%d/health    <- Health check", args.port)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
