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

Demo mode:
    Set DEMO_MODE=1 (or pass --demo) to make /feed return deterministic
    synthetic data instead of querying the Polymarket CLOB and the tv CLI.
    Useful for:
      - Screenshots / README images.
      - Dashboard development without funded accounts.
      - Onboarding (clone the repo, see the dashboard populated, no setup).
    No real orders or balances are ever read in demo mode. The webhook POST
    endpoint is disabled; demo_mode=true is surfaced in /health and /feed.

Usage:
    # Start server (default port 8765)
    python tv_overlay_webhook.py

    # Custom port
    python tv_overlay_webhook.py --port 9000

    # With secret required
    export TV_WEBHOOK_SECRET="some-random-string-32-chars-min"
    python tv_overlay_webhook.py

    # Demo mode (no real API calls)
    DEMO_MODE=1 python tv_overlay_webhook.py

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

# Try trader-ai/.env first (correct), fall back to one level up for legacy.
_dotenv_path = _CLI.parent / ".env"
_fallback_dotenv_path = _ROOT / ".env"
if _dotenv_path.exists():
    from dotenv import load_dotenv
    load_dotenv(_dotenv_path, override=True)
elif _fallback_dotenv_path.exists():
    from dotenv import load_dotenv
    load_dotenv(_fallback_dotenv_path, override=True)

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
ACCOUNT_TTL_SEC: int  = 30   # refresh balance + open orders every 30 seconds
CLOB_HOST: str        = "https://clob.polymarket.com"
CHAIN_ID: int         = 137

# Security / rate limiting
WEBHOOK_SECRET: Optional[str] = os.environ.get("TV_WEBHOOK_SECRET") or None
RATE_LIMIT_PER_MIN: int = int(os.environ.get("WEBHOOK_RATE_LIMIT_PER_MIN", "60"))

# Demo mode (see module docstring).
DEMO_MODE: bool = os.environ.get("DEMO_MODE", "").strip() not in ("", "0", "false", "False")

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


# -- Account snapshot (balance + open orders) ---------------------------------

def fetch_account_snapshot() -> dict[str, Any]:
    """
    Return a lightweight account snapshot for the dashboard:
      - usdce_balance (float | None)   USDC.e balance on Polygon
      - open_orders   (int | None)     count of live CLOB orders

    Both values are None on any error (fail-safe; the dashboard renders '—').
    """
    result: dict[str, Any] = {"usdce_balance": None, "open_orders": None}

    wallet = os.environ.get("POLY_WALLET_ADDRESS", "").strip()
    if wallet:
        try:
            from web3 import Web3
            w3 = Web3(Web3.HTTPProvider("https://polygon-bor-rpc.publicnode.com"))
            abi = [{
                "inputs": [{"name": "account", "type": "address"}],
                "name": "balanceOf",
                "outputs": [{"type": "uint256"}],
                "stateMutability": "view",
                "type": "function",
            }]
            contract = w3.eth.contract(
                address=Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"),
                abi=abi,
            )
            raw = contract.functions.balanceOf(Web3.to_checksum_address(wallet)).call()
            result["usdce_balance"] = raw / 1e6
        except Exception as exc:
            logger.debug("usdce balance fetch failed: %s", exc)

    try:
        client = _build_clob_client()
        orders = client.get_orders() or []
        result["open_orders"] = len(orders)
    except Exception as exc:
        logger.debug("open orders fetch failed: %s", exc)

    return result


# -- Demo data (see module docstring) ------------------------------------------
#
# Kept as deterministic Python literals so README screenshots stay identical
# across runs. Numbers roughly mirror a realistic BTC prediction-markets
# snapshot at ~$77k spot. No live API is called in demo mode.

_DEMO_NOW = int(time.time())
_DEMO_POSITIONS = [
    {"market": "Will Bitcoin reach $150,", "outcome": "No",  "side": "BUY", "size": 10.0, "price": 0.900, "status": "CONFIRMED", "ts": _DEMO_NOW - 1200},
    {"market": "Will Bitcoin reach $100,", "outcome": "No",  "side": "BUY", "size": 10.0, "price": 0.585, "status": "CONFIRMED", "ts": _DEMO_NOW - 3200},
    {"market": "Will Bitcoin dip to $45,0", "outcome": "No", "side": "BUY", "size":  5.0, "price": 0.680, "status": "CONFIRMED", "ts": _DEMO_NOW - 4800},
    {"market": "Will Bitcoin reach $110,", "outcome": "Yes", "side": "BUY", "size":  5.0, "price": 0.250, "status": "CONFIRMED", "ts": _DEMO_NOW - 6100},
    {"market": "Will Bitcoin reach $120,", "outcome": "Yes", "side": "BUY", "size":  5.0, "price": 0.200, "status": "CONFIRMED", "ts": _DEMO_NOW - 7900},
]
_DEMO_MARKETS = [
    {"question": "Will Bitcoin reach $100,000 by December 31, 2026?", "market_price": 0.440, "fair_value": 0.436, "liquidity_usd": 182_000},
    {"question": "Will Bitcoin reach $110,000 by December 31, 2026?", "market_price": 0.295, "fair_value": 0.293, "liquidity_usd": 145_000},
    {"question": "Will Bitcoin reach $120,000 by December 31, 2026?", "market_price": 0.210, "fair_value": 0.195, "liquidity_usd": 121_000},
    {"question": "Will Bitcoin reach $130,000 by December 31, 2026?", "market_price": 0.145, "fair_value": 0.129, "liquidity_usd":  98_000},
    {"question": "Will Bitcoin reach $140,000 by December 31, 2026?", "market_price": 0.115, "fair_value": 0.085, "liquidity_usd":  76_000},
    {"question": "Will Bitcoin reach $150,000 by December 31, 2026?", "market_price": 0.105, "fair_value": 0.056, "liquidity_usd":  63_000},
    {"question": "Will Bitcoin reach $180,000 by December 31, 2026?", "market_price": 0.065, "fair_value": 0.050, "liquidity_usd":  41_000},
    {"question": "Will Bitcoin dip to $55,000 by December 31, 2026?",  "market_price": 0.540, "fair_value": 0.298, "liquidity_usd":  58_000},
]
_DEMO_ALERTS = [
    {"ts": _DEMO_NOW -   60, "symbol": "BTCUSDT", "price": 77_411.0, "action": "edge:NO@150k"},
    {"ts": _DEMO_NOW -  140, "symbol": "BTCUSDT", "price": 77_402.5, "action": "order_placed"},
    {"ts": _DEMO_NOW -  620, "symbol": "BTCUSDT", "price": 77_394.0, "action": "cycle_ok"},
    {"ts": _DEMO_NOW -  870, "symbol": "BTCUSDT", "price": 77_388.2, "action": "edge:NO@35k"},
    {"ts": _DEMO_NOW - 1220, "symbol": "BTCUSDT", "price": 77_365.1, "action": "cycle_ok"},
]
_DEMO_TV_CHART = {
    "connected": True,
    "symbol": "BITSTAMP:BTCUSD",
    "timeframe": "5",
    "price": 77_411.0,
    "volume": 128.4,
    "change": 0.02,
}
_DEMO_ACCOUNT = {
    "usdce_balance": 10.61,
    "open_orders":   9,
}


def build_demo_feed() -> dict[str, Any]:
    """Return a fully populated /feed payload using deterministic demo data."""
    return {
        "updated_at":    datetime.now(timezone.utc).isoformat(),
        "demo_mode":     True,
        "alerts":        _DEMO_ALERTS,
        "positions":     _DEMO_POSITIONS,
        "markets":       _DEMO_MARKETS,
        "tv_chart":      _DEMO_TV_CHART,
        "usdce_balance": _DEMO_ACCOUNT["usdce_balance"],
        "open_orders":   _DEMO_ACCOUNT["open_orders"],
    }


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


class AccountCache(_TTLCache):
    """Thread-safe cache for usdce_balance + open_orders."""

    def __init__(self) -> None:
        super().__init__(
            fetcher=fetch_account_snapshot,
            ttl_sec=ACCOUNT_TTL_SEC,
            label="account",
        )
        self._data = {"usdce_balance": None, "open_orders": None}


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
    account_cache: AccountCache,
    rate_limiter: RateLimiter,
    require_secret: bool,
    demo_mode: bool,
):
    """Factory that creates a request handler class with injected dependencies."""

    class Handler(BaseHTTPRequestHandler):
        """Handles webhook and feed endpoints."""

        def log_message(self, fmt, *args):  # suppress default access log
            logger.debug("HTTP %s", fmt % args)

        def _client_ip(self) -> str:
            return self.client_address[0] if self.client_address else "unknown"

        # -- POST /webhook -----------------------------------------------------

        def _handle_webhook(self) -> None:
            """Accept a TradingView alert and store it in SQLite."""
            if demo_mode:
                # Refuse writes in demo mode so screenshots are reproducible.
                self._respond(503, {"error": "demo_mode_read_only"})
                return

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

            d      = data if isinstance(data, dict) else {}
            ts     = int(time.time())
            symbol = d.get("symbol", "")
            price  = d.get("price")
            action = d.get("action", "")

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
            if demo_mode:
                self._respond(200, {"positions": _DEMO_POSITIONS, "count": len(_DEMO_POSITIONS)})
                return
            positions = pos_cache.get() or []
            self._respond(200, {"positions": positions, "count": len(positions)})

        # -- GET /tv/chart -----------------------------------------------------

        def _handle_tv_chart(self) -> None:
            if demo_mode:
                self._respond(200, _DEMO_TV_CHART)
                return
            snapshot = tv_cache.get() or {"connected": False}
            self._respond(200, snapshot)

        # -- GET /feed ---------------------------------------------------------

        def _handle_feed(self) -> None:
            """Combined feed consumed by the dashboard."""
            if demo_mode:
                self._respond(200, build_demo_feed())
                return

            cursor = conn.execute(
                "SELECT ts, symbol, price, action FROM tv_alerts ORDER BY ts DESC LIMIT 10"
            )
            alerts = [
                {"ts": r[0], "symbol": r[1], "price": r[2], "action": r[3]}
                for r in cursor.fetchall()
            ]
            positions = pos_cache.get() or []
            tv_chart  = tv_cache.get() or {"connected": False}
            account   = account_cache.get() or {}

            self._respond(200, {
                "updated_at":    datetime.now(timezone.utc).isoformat(),
                "demo_mode":     False,
                "alerts":        alerts,
                "positions":     positions,
                "markets":       [],  # maker_bot-scan output, wired in a future PR
                "tv_chart":      tv_chart,
                "usdce_balance": account.get("usdce_balance"),
                "open_orders":   account.get("open_orders"),
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
                    "auth":              "required" if require_secret else "disabled",
                    "rate_limit_per_min": RATE_LIMIT_PER_MIN,
                    "demo_mode":          demo_mode,
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
    parser.add_argument(
        "--demo", action="store_true", default=None,
        help="Run in demo mode (synthetic data, no live API). "
             "Same as DEMO_MODE=1.",
    )
    args = parser.parse_args()

    global DEMO_MODE
    if args.demo is True:
        DEMO_MODE = True

    conn = init_db()
    pos_cache     = PositionCache(conn)
    tv_cache      = TvChartCache()
    account_cache = AccountCache()
    rate_limiter  = RateLimiter(RATE_LIMIT_PER_MIN)

    if not DEMO_MODE:
        try:
            pos_cache.get()
        except Exception as exc:
            logger.warning("Initial position cache warm-up failed: %s", exc)
        try:
            tv_cache.get()
        except Exception as exc:
            logger.debug("Initial tv cache warm-up failed (OK if TV not running): %s", exc)

    require_secret = WEBHOOK_SECRET is not None
    if not require_secret and not DEMO_MODE:
        logger.warning(
            "TV_WEBHOOK_SECRET is not set. POST /webhook is UNAUTHENTICATED. "
            "DO NOT expose this server publicly (ngrok, port-forward) without "
            "setting a secret in your .env file."
        )

    handler_class = make_handler(
        conn, pos_cache, tv_cache, account_cache,
        rate_limiter, require_secret, DEMO_MODE,
    )
    server = HTTPServer(("0.0.0.0", args.port), handler_class)

    logger.info("TV Overlay Webhook Server running on http://0.0.0.0:%d", args.port)
    logger.info("  mode:       %s", "DEMO (synthetic data)" if DEMO_MODE else "LIVE")
    if not DEMO_MODE:
        logger.info("  auth:       %s", "REQUIRED (TV_WEBHOOK_SECRET)" if require_secret else "DISABLED")
        logger.info("  rate limit: %d req/min per IP", RATE_LIMIT_PER_MIN)
        logger.info("  tv CLI:     %s", " ".join(TV_CLI_CMD))
    logger.info("Endpoints:")
    logger.info("  POST /webhook   <- TradingView alerts (Pro+ plan)%s",
                " [disabled in demo]" if DEMO_MODE else "")
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
