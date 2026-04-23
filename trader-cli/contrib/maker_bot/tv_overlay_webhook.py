#!/usr/bin/env python3
"""
TradingView Overlay Webhook Server.

Receives Pine Script alerts from TradingView via webhook (HTTP POST) and
overlays Polymarket position data, optionally augmented with read-only
TradingView chart state fetched via the `tv` CLI (tradingview-mcp).

Architecture:
    TradingView alert (Pro+ plan)  --> POST /webhook      --> SQLite (tv_alerts)
    Polymarket CLOB                --> GET  /positions    --> JSON feed
    TradingView Desktop (any plan) --> GET  /tv/chart     --> JSON (via tv CLI)
    Dashboard client               --> GET  /feed         --> combined JSON

Security:
    - Optional shared-secret auth on POST /webhook.
      Set TV_WEBHOOK_SECRET env var to require a matching `secret` field in
      the JSON body (TradingView webhooks cannot send custom headers, so the
      secret must travel in the body).
    - Optional per-IP rate limiting (WEBHOOK_RATE_LIMIT_PER_MIN, default 60).
    - When TV_WEBHOOK_SECRET is unset, the server logs a WARNING on startup
      and accepts unauthenticated alerts (backward compatible). DO NOT expose
      the webhook endpoint publicly in that mode.

Usage:
    # Start server (default port 8765)
    python tv_overlay_webhook.py

    # Custom port
    python tv_overlay_webhook.py --port 9000

    # With secret required
    export TV_WEBHOOK_SECRET="some-random-string-32-chars-min"
    python tv_overlay_webhook.py

    # With ngrok for public exposure
    ./scripts/start_ngrok.sh
    # (then configure TradingView alert URL to the ngrok HTTPS URL)

Pine Script alert message template (put in the Alert Message field in TV,
NOT committed to any repo):
    {
      "secret": "YOUR_TV_WEBHOOK_SECRET",
      "symbol": "{{ticker}}",
      "price":  {{close}},
      "action": "{{strategy.order.action}}",
      "time":   "{{timenow}}"
    }

Requirements:
    pip install py-clob-client python-dotenv
    (optional) tradingview-mcp submodule for /tv/chart endpoint
"""

import argparse
import collections
import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Optional

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
TV_CHART_TTL_SEC: int = 10   # refresh TV chart snapshot every 10 seconds
CLOB_HOST: str        = "https://clob.polymarket.com"
CHAIN_ID: int         = 137

# Security / rate limiting
WEBHOOK_SECRET: Optional[str] = os.environ.get("TV_WEBHOOK_SECRET") or None
RATE_LIMIT_PER_MIN: int = int(os.environ.get("WEBHOOK_RATE_LIMIT_PER_MIN", "60"))

# Path to `tv` CLI from tradingview-mcp submodule.
# Falls back to whatever is on PATH (works if user ran `npm link` globally).
_TV_CLI_FROM_SUBMODULE = _ROOT / "tradingview-mcp" / "src" / "cli" / "index.js"
TV_CLI_CMD: list[str] = (
    ["node", str(_TV_CLI_FROM_SUBMODULE)]
    if _TV_CLI_FROM_SUBMODULE.exists()
    else ["tv"]
)
TV_CLI_TIMEOUT_SEC: int = 5


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


# -- TradingView chart fetcher (via tv CLI, read-only) -------------------------

def fetch_tv_chart_snapshot() -> dict[str, Any]:
    """
    Fetch a read-only snapshot of the current TradingView chart via tv CLI.

    This calls `tv quote` and `tv status` to capture symbol, timeframe, and
    latest price. All data stays on the local machine; no network calls
    to TradingView servers are made by this process.

    The function is read-only by design: this server never sends signals back
    to TradingView or places trades based on TV data. TV is treated as a
    visualization source only.

    Returns:
        Dict with symbol, timeframe, price, and connected flag.
        On any error returns {"connected": false, "error": "..."}.
    """
    def _run(args: list[str]) -> Optional[dict]:
        try:
            result = subprocess.run(
                TV_CLI_CMD + args,
                capture_output=True, text=True, timeout=TV_CLI_TIMEOUT_SEC,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logger.debug("tv CLI unavailable: %s", exc)
            return None
        if result.returncode != 0:
            logger.debug("tv CLI non-zero exit: %s", result.stderr[:200])
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return None

    status = _run(["status"])
    quote  = _run(["quote"])

    if status is None and quote is None:
        return {"connected": False, "error": "tv CLI not reachable"}

    snapshot = {"connected": True}
    if isinstance(status, dict):
        snapshot["symbol"]    = status.get("symbol") or status.get("ticker")
        snapshot["timeframe"] = status.get("timeframe") or status.get("resolution")
    if isinstance(quote, dict):
        snapshot["price"]  = quote.get("close") or quote.get("price")
        snapshot["volume"] = quote.get("volume")
        snapshot["change"] = quote.get("change")

    return snapshot


# -- Position cache (thread-safe) ----------------------------------------------

class _TTLCache:
    """Generic thread-safe TTL cache for a single JSON-ish payload."""

    def __init__(self, fetcher, ttl_sec: int, label: str) -> None:
        self._fetcher  = fetcher
        self._ttl      = ttl_sec
        self._label    = label
        self._lock     = threading.Lock()
        self._data: Any = None
        self._updated  = 0

    def get(self) -> Any:
        now = int(time.time())
        with self._lock:
            if now - self._updated > self._ttl:
                self._refresh()
        return self._data

    def _refresh(self) -> None:
        try:
            self._data = self._fetcher()
            self._updated = int(time.time())
        except Exception as exc:
            logger.error("%s cache refresh failed: %s", self._label, exc)


class PositionCache(_TTLCache):
    """Thread-safe cache for Polymarket positions, persisted to SQLite."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__(
            fetcher=fetch_polymarket_positions,
            ttl_sec=POSITION_TTL_SEC,
            label="positions",
        )
        self._conn = conn
        self._data = []

    def _refresh(self) -> None:
        logger.info("Refreshing Polymarket position cache...")
        super()._refresh()
        if isinstance(self._data, list):
            payload = json.dumps(self._data)
            self._conn.execute(
                "INSERT OR REPLACE INTO position_cache (id, updated_at, payload) VALUES (1, ?, ?)",
                (self._updated, payload),
            )
            self._conn.commit()
            logger.info("Position cache updated: %d positions", len(self._data))


class TvChartCache(_TTLCache):
    """Thread-safe cache for TradingView chart snapshot (in-memory only)."""

    def __init__(self) -> None:
        super().__init__(
            fetcher=fetch_tv_chart_snapshot,
            ttl_sec=TV_CHART_TTL_SEC,
            label="tv_chart",
        )
        self._data = {"connected": False, "error": "not yet fetched"}


# -- Rate limiter --------------------------------------------------------------

class RateLimiter:
    """Sliding-window per-IP rate limiter, per minute."""

    def __init__(self, limit_per_min: int) -> None:
        self._limit = max(1, limit_per_min)
        self._lock  = threading.Lock()
        self._hits: dict[str, collections.deque] = {}

    def allow(self, key: str) -> bool:
        now = time.time()
        cutoff = now - 60.0
        with self._lock:
            dq = self._hits.setdefault(key, collections.deque())
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= self._limit:
                return False
            dq.append(now)
            return True


# -- HTTP request handler ------------------------------------------------------

def make_handler(
    conn: sqlite3.Connection,
    pos_cache: PositionCache,
    tv_cache: TvChartCache,
    rate_limiter: RateLimiter,
    require_secret: bool,
):
    """
    Factory that creates a request handler class with injected dependencies.

    Args:
        conn: SQLite connection.
        pos_cache: PositionCache instance.
        tv_cache: TvChartCache instance.
        rate_limiter: RateLimiter instance.
        require_secret: If True, POST /webhook requires a matching `secret`
            field in the JSON body (compared against TV_WEBHOOK_SECRET env).

    Returns:
        BaseHTTPRequestHandler subclass.
    """

    class Handler(BaseHTTPRequestHandler):
        """Handles webhook and feed endpoints."""

        def log_message(self, fmt, *args):  # suppress default access log
            logger.debug("HTTP %s", fmt % args)

        def _client_ip(self) -> str:
            return self.client_address[0] if self.client_address else "unknown"

        # -- POST /webhook -----------------------------------------------------

        def _handle_webhook(self) -> None:
            """
            Accept a TradingView alert and store it in SQLite.

            Expected JSON body:
              {
                "secret": "shared-secret-if-required",
                "symbol": "BTCUSDT",
                "price":  78000,
                "action": "buy",
                "time":   "2026-04-23T01:23:45Z"
              }
            """
            if not rate_limiter.allow(self._client_ip()):
                logger.warning("Rate limit exceeded for %s", self._client_ip())
                self._respond(429, {"error": "rate_limit"})
                return

            length = int(self.headers.get("Content-Length", 0))
            if length > 65536:
                self._respond(413, {"error": "payload_too_large"})
                return

            body = self.rfile.read(length).decode("utf-8", errors="replace")
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = {"raw": body}

            # Auth check (only if a secret is configured)
            if require_secret:
                provided = data.get("secret") if isinstance(data, dict) else None
                if provided != WEBHOOK_SECRET:
                    logger.warning(
                        "Webhook auth failed from %s (provided=%r)",
                        self._client_ip(),
                        (provided[:4] + "***") if isinstance(provided, str) else provided,
                    )
                    self._respond(401, {"error": "unauthorized"})
                    return

            # Extract fields (safe if data is dict-ish)
            d      = data if isinstance(data, dict) else {}
            ts     = int(time.time())
            symbol = d.get("symbol", "")
            price  = d.get("price")
            action = d.get("action", "")

            # Strip secret before storing (never persist credentials)
            sanitized = {k: v for k, v in d.items() if k != "secret"}
            raw_for_storage = json.dumps(sanitized) if d else body

            conn.execute(
                "INSERT INTO tv_alerts (ts, symbol, price, action, raw) VALUES (?, ?, ?, ?, ?)",
                (ts, symbol, price, action, raw_for_storage),
            )
            conn.commit()

            logger.info(
                "TV alert received: symbol=%s price=%s action=%s ip=%s",
                symbol, price, action, self._client_ip(),
            )
            self._respond(200, {"ok": True, "ts": ts})

        # -- GET /positions ----------------------------------------------------

        def _handle_positions(self) -> None:
            """Return current Polymarket positions as JSON."""
            positions = pos_cache.get() or []
            self._respond(200, {"positions": positions, "count": len(positions)})

        # -- GET /tv/chart -----------------------------------------------------

        def _handle_tv_chart(self) -> None:
            """
            Return read-only TradingView chart snapshot.

            Data comes from the locally running TradingView Desktop app via
            the `tv` CLI. Nothing is sent to TradingView's servers. Nothing
            from this endpoint feeds back into the order placement logic --
            this is visualization only.
            """
            snapshot = tv_cache.get() or {"connected": False}
            self._respond(200, snapshot)

        # -- GET /feed ---------------------------------------------------------

        def _handle_feed(self) -> None:
            """
            Return combined feed: last 10 TV alerts + positions + chart state.
            Consumed by the local dashboard.
            """
            cursor = conn.execute(
                "SELECT ts, symbol, price, action FROM tv_alerts ORDER BY ts DESC LIMIT 10"
            )
            alerts = [
                {"ts": r[0], "symbol": r[1], "price": r[2], "action": r[3]}
                for r in cursor.fetchall()
            ]
            positions = pos_cache.get() or []
            tv_chart  = tv_cache.get() or {"connected": False}
            self._respond(200, {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "alerts":     alerts,
                "positions":  positions,
                "tv_chart":   tv_chart,
            })

        # -- Router ------------------------------------------------------------

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/webhook":
                self._handle_webhook()
            else:
                self._respond(404, {"error": "not_found"})

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/positions":
                self._handle_positions()
            elif self.path.startswith("/tv/chart"):
                self._handle_tv_chart()
            elif self.path.startswith("/feed"):
                self._handle_feed()
            elif self.path == "/health":
                self._respond(200, {
                    "ok": True,
                    "auth": "required" if require_secret else "disabled",
                    "rate_limit_per_min": RATE_LIMIT_PER_MIN,
                })
            else:
                self._respond(404, {"error": "not_found"})

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

    conn = init_db()
    pos_cache = PositionCache(conn)
    tv_cache  = TvChartCache()
    rate_limiter = RateLimiter(RATE_LIMIT_PER_MIN)

    # Warm up both caches (non-blocking on failure)
    try:
        pos_cache.get()
    except Exception as exc:
        logger.warning("Initial position cache warm-up failed: %s", exc)
    try:
        tv_cache.get()
    except Exception as exc:
        logger.debug("Initial tv cache warm-up failed (OK if TV not running): %s", exc)

    require_secret = WEBHOOK_SECRET is not None
    if not require_secret:
        logger.warning(
            "TV_WEBHOOK_SECRET is not set. POST /webhook is UNAUTHENTICATED. "
            "DO NOT expose this server publicly (ngrok, port-forward) without "
            "setting a secret in your .env file."
        )

    handler_class = make_handler(
        conn, pos_cache, tv_cache, rate_limiter, require_secret
    )
    server = HTTPServer(("0.0.0.0", args.port), handler_class)

    logger.info("TV Overlay Webhook Server running on http://0.0.0.0:%d", args.port)
    logger.info("  auth:       %s", "REQUIRED (TV_WEBHOOK_SECRET)" if require_secret else "DISABLED")
    logger.info("  rate limit: %d req/min per IP", RATE_LIMIT_PER_MIN)
    logger.info("  tv CLI:     %s", " ".join(TV_CLI_CMD))
    logger.info("Endpoints:")
    logger.info("  POST /webhook   <- TradingView alerts (Pro+ plan)")
    logger.info("  GET  /positions <- Polymarket positions (5m cache)")
    logger.info("  GET  /tv/chart  <- TV chart snapshot (10s cache, read-only)")
    logger.info("  GET  /feed      <- Combined feed (for dashboard)")
    logger.info("  GET  /health    <- Health + config check")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
