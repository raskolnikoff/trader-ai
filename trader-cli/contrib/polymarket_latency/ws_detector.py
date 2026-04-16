#!/usr/bin/env python3
"""
Phase 1: WebSocket-based Binance -> Polymarket latency detector.

Replaces the 2-second REST polling loop in detector.py with:
  - Binance trade stream via WebSocket  (ms-level price feed)
  - Async event loop so Polymarket polling does not block Binance ingestion
  - Output log: data/ws_latency.jsonl (separate from REST log)

Usage:
    python ws_detector.py
    python ws_detector.py --threshold 0.15 --window 30 --log-all

Requires:
    pip install 'websockets>=12.0'
"""

import argparse
import asyncio
import json
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import websockets
except ImportError:
    raise SystemExit("websockets not installed. Run: pip install 'websockets>=12.0'")

# -- Constants -----------------------------------------------------------------

DEFAULT_THRESHOLD_PCT    = 0.15   # tighter than detector.py (0.20) -> more events
DEFAULT_WINDOW_SECONDS   = 45.0   # shorter window -> faster iteration
POLYMARKET_POLL_INTERVAL = 1.5    # seconds between Polymarket REST polls during tracking
REQUEST_TIMEOUT          = 8
TOP_N_MARKETS            = 5

BINANCE_WS_URL   = "wss://stream.binance.com:9443/ws/btcusdt@trade"
POLYMARKET_URL   = "https://clob.polymarket.com/markets"
BTC_KEYWORDS     = ["bitcoin", "btc"]

_PROJECT_ROOT    = Path(__file__).parent.parent.parent.parent
LATENCY_LOG_PATH = _PROJECT_ROOT / "data" / "ws_latency.jsonl"


# -- Data containers -----------------------------------------------------------

@dataclass
class MarketSnapshot:
    market_id: str
    question: str
    mid_price: float
    ts: float  # time.time() at snapshot


@dataclass
class LatencyRecord:
    market_id: str
    question: str
    latency_ms: float  # ms precision (vs seconds in detector.py)
    direction: str     # "up" | "down" | "unclear"


@dataclass
class ScanState:
    """Shared mutable state between Binance WS consumer and tracking tasks."""
    last_price: float = 0.0
    active_tracking: bool = False
    event_count: int = 0


# -- HTTP helpers --------------------------------------------------------------

def _fetch_json_sync(url: str) -> Optional[dict | list]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; trader-ai/1.0)",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"  [http] {exc}")
        return None


async def fetch_json(url: str) -> Optional[dict | list]:
    """Non-blocking wrapper: runs sync fetch in a thread executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_json_sync, url)


# -- Polymarket helpers --------------------------------------------------------

def _safe_float(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _compute_mid(market: dict) -> Optional[float]:
    bid = _safe_float(market.get("bestBid") or market.get("best_bid"))
    ask = _safe_float(market.get("bestAsk") or market.get("best_ask"))
    if bid is not None and ask is not None and ask > 0:
        return (bid + ask) / 2.0
    tokens = market.get("tokens")
    if isinstance(tokens, list):
        prices = [_safe_float(t.get("price")) for t in tokens if isinstance(t, dict)]
        prices = [p for p in prices if p is not None]
        if prices:
            return sum(prices) / len(prices)
    return None


async def fetch_btc_markets(verbose: bool = False) -> list[MarketSnapshot]:
    """Fetch active Polymarket BTC markets. Returns [] on failure."""
    data = await fetch_json(POLYMARKET_URL)
    if data is None:
        return []
    markets_raw = data.get("data", data) if isinstance(data, dict) else data
    if not isinstance(markets_raw, list):
        return []

    now = time.time()
    snapshots = []
    for m in markets_raw:
        if not isinstance(m, dict):
            continue
        q = m.get("question", "")
        if not any(kw in q.lower() for kw in BTC_KEYWORDS):
            continue
        mid = _compute_mid(m) or 0.5
        market_id = str(m.get("condition_id") or m.get("id") or q[:40])
        snapshots.append(MarketSnapshot(
            market_id=market_id, question=q, mid_price=mid, ts=now
        ))

    if verbose:
        print(f"  [poly] {len(snapshots)} BTC markets fetched")
    return snapshots


# -- Latency measurement -------------------------------------------------------

async def measure_latency(
    baseline: list[MarketSnapshot],
    event_ts: float,
    window: float,
) -> list[LatencyRecord]:
    """
    Poll Polymarket every POLYMARKET_POLL_INTERVAL seconds.
    Record when each market first shows a price delta >= 0.001.
    Returns LatencyRecord list sorted slowest-first.
    """
    baseline_by_id = {m.market_id: m for m in baseline}
    reacted: dict[str, float] = {}      # market_id -> latency_ms
    direction_map: dict[str, str] = {}
    deadline = event_ts + window

    while time.time() < deadline:
        await asyncio.sleep(POLYMARKET_POLL_INTERVAL)
        current = await fetch_btc_markets()

        for cur in current:
            if cur.market_id in reacted:
                continue
            base = baseline_by_id.get(cur.market_id)
            if base is None:
                continue
            delta = cur.mid_price - base.mid_price
            if abs(delta) >= 0.001:
                reacted[cur.market_id] = (cur.ts - event_ts) * 1000.0
                direction_map[cur.market_id] = "up" if delta > 0 else "down"

    records = [
        LatencyRecord(
            market_id=m.market_id,
            question=m.question,
            latency_ms=round(reacted[m.market_id], 1),
            direction=direction_map.get(m.market_id, "unclear"),
        )
        for m in baseline
        if m.market_id in reacted
    ]
    records.sort(key=lambda r: r.latency_ms, reverse=True)
    return records


# -- Persistence ---------------------------------------------------------------

def append_log(
    records: list[LatencyRecord], binance_price: float, pct_change: float
) -> None:
    """Append latency records to ws_latency.jsonl. Fails silently."""
    if not records:
        return
    try:
        LATENCY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        with LATENCY_LOG_PATH.open("a", encoding="utf-8") as f:
            for r in records:
                entry = {
                    "ts": ts,
                    "binance_price": round(binance_price, 2),
                    "pct_change": round(pct_change, 4),
                    "market_id": r.market_id,
                    "question": r.question,
                    "latency_ms": r.latency_ms,
                    "direction": r.direction,
                }
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"  [log] write failed: {exc}")


def append_zero_event(
    n_markets: int, binance_price: float, pct_change: float
) -> None:
    """Sentinel line: event fired but no market reacted within window."""
    try:
        LATENCY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "binance_price": round(binance_price, 2),
            "pct_change": round(pct_change, 4),
            "event": True,
            "reacted": 0,
            "markets_tracked": n_markets,
        }
        with LATENCY_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"  [log] zero-event write failed: {exc}")


# -- Display -------------------------------------------------------------------

def print_event(price: float, pct: float) -> None:
    sign = "+" if pct >= 0 else ""
    print(f"\n[EVENT] ${price:,.2f}  ({sign}{pct:.3f}%)")


def print_results(records: list[LatencyRecord]) -> None:
    if not records:
        print("  (no markets reacted within window)")
        return
    print(f"[REACTED] {len(records)} markets:")
    for r in records[:TOP_N_MARKETS]:
        icon = "^" if r.direction == "up" else "v" if r.direction == "down" else "-"
        q = r.question[:60] + ("..." if len(r.question) > 60 else "")
        print(f"  [{icon}] {r.latency_ms:.0f}ms  {q}")


# -- Main scan loop ------------------------------------------------------------

async def run_scan(
    threshold: float = DEFAULT_THRESHOLD_PCT,
    window: float    = DEFAULT_WINDOW_SECONDS,
    log_all: bool    = False,
) -> None:
    """
    Connect to Binance trade stream via WebSocket.
    On a significant move, spawn a concurrent tracking task.
    Only one tracking task runs at a time (active_tracking guard).
    """
    state = ScanState()

    print("[ws_detector] Connecting to Binance WebSocket...")
    print(f"   threshold={threshold:.2f}%  window={window:.0f}s  log_all={log_all}")
    print(f"   output -> {LATENCY_LOG_PATH}")
    print("   Ctrl+C to stop\n")

    async def tracking_task(event_price: float, pct: float, event_ts: float) -> None:
        state.active_tracking = True
        try:
            baseline = await fetch_btc_markets(verbose=True)
            if not baseline:
                print("  [warn] no BTC markets found")
                return
            print(f"  tracking {len(baseline)} markets for {window:.0f}s...")
            records = await measure_latency(baseline, event_ts, window)
            print_results(records)
            append_log(records, event_price, pct)
            if not records and log_all:
                append_zero_event(len(baseline), event_price, pct)
        finally:
            state.active_tracking = False

    async for ws in websockets.connect(BINANCE_WS_URL, ping_interval=20):
        try:
            async for raw_msg in ws:
                msg   = json.loads(raw_msg)
                price = float(msg["p"])  # trade price
                ts_ms = int(msg["T"])    # trade timestamp ms

                if state.last_price == 0.0:
                    state.last_price = price
                    print(f"  Initial BTC: ${price:,.2f}")
                    continue

                pct = ((price - state.last_price) / state.last_price) * 100.0

                if abs(pct) >= threshold and not state.active_tracking:
                    state.event_count += 1
                    print_event(price, pct)
                    # Spawn tracking concurrently so WS stream keeps reading
                    asyncio.create_task(
                        tracking_task(price, pct, ts_ms / 1000.0)
                    )
                    state.last_price = price  # reset reference immediately

        except websockets.ConnectionClosed:
            print("  [ws] disconnected, reconnecting...")
            await asyncio.sleep(2)
        except Exception as exc:
            print(f"  [ws] error: {exc}")
            await asyncio.sleep(2)


# -- Entry point ---------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="WebSocket-based Binance -> Polymarket latency detector"
    )
    parser.add_argument(
        "--threshold", type=float, default=DEFAULT_THRESHOLD_PCT,
        help=f"BTC move %% to trigger tracking (default: {DEFAULT_THRESHOLD_PCT})",
    )
    parser.add_argument(
        "--window", type=float, default=DEFAULT_WINDOW_SECONDS,
        help=f"Tracking window seconds (default: {DEFAULT_WINDOW_SECONDS})",
    )
    parser.add_argument(
        "--log-all", action="store_true",
        help="Also log sentinel lines for zero-reaction events",
    )
    args = parser.parse_args()

    try:
        asyncio.run(run_scan(
            threshold=args.threshold,
            window=args.window,
            log_all=args.log_all,
        ))
    except KeyboardInterrupt:
        print("\n[stopped]")


if __name__ == "__main__":
    main()
