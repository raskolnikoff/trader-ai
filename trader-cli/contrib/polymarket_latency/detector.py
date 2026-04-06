#!/usr/bin/env python3
"""
Polymarket vs Binance latency detector.

Experimental / observation tool only.
This module measures how quickly Polymarket prediction markets react to
Binance BTC price movements. It is NOT a trading strategy.

Usage:
    python detector.py [--threshold 0.1]
"""

import os
import time
import urllib.request
import urllib.error
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Default constants (all overridable at call-site or via env) ────────────────
DEFAULT_THRESHOLD_PCT = 0.20    # % move that counts as a "significant event"
POLL_INTERVAL_SECONDS = 2.0     # how often to poll Binance during tracking
TRACKING_WINDOW_SECONDS = 60.0  # how long to watch for Polymarket reaction
TOP_N_MARKETS = 5               # lagging markets to display
REQUEST_TIMEOUT = 10            # HTTP timeout in seconds

BINANCE_URL = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
POLYMARKET_URL = "https://clob.polymarket.com/markets"

# Loose keyword filter — lowercase, matched against the market question field.
# Keeping this broad ensures we don't silently drop real BTC markets.
BTC_KEYWORDS = ["bitcoin", "btc"]

# JSONL log lives at <project-root>/data/latency.jsonl
_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
LATENCY_LOG_PATH = _PROJECT_ROOT / "data" / "latency.jsonl"


# ── Data containers ────────────────────────────────────────────────────────────

@dataclass
class BinanceSnapshot:
    price: float
    timestamp: float


@dataclass
class MarketSnapshot:
    market_id: str
    question: str
    mid_price: float
    timestamp: float


@dataclass
class LatencyRecord:
    market_id: str
    question: str
    latency_seconds: float
    direction: str   # "up" | "down" | "unclear"


# ── HTTP helpers ───────────────────────────────────────────────────────────────

def _fetch_json(url: str) -> Optional[dict | list]:
    """
    Fetch JSON from a URL. Returns None on any error so callers degrade gracefully.

    Sends browser-like headers so APIs that block bare urllib requests (e.g.
    Polymarket returning 403 Forbidden) respond normally.
    """
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; trader-ai/1.0)",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        print(f"  [http]    HTTP {exc.code} {exc.reason} — {url}")
    except urllib.error.URLError as exc:
        print(f"  [network] Cannot reach {url}: {exc.reason}")
    except json.JSONDecodeError as exc:
        print(f"  [parse]   JSON decode failed for {url}: {exc}")
    except Exception as exc:
        print(f"  [error]   Unexpected error fetching {url}: {exc}")
    return None


# ── Binance helpers ────────────────────────────────────────────────────────────

def fetch_binance_price() -> Optional[float]:
    """Return the current BTCUSDT spot price from Binance, or None on failure."""
    data = _fetch_json(BINANCE_URL)
    if data is None:
        return None
    try:
        return float(data["price"])
    except (KeyError, ValueError, TypeError) as exc:
        print(f"  [binance] Unexpected price format: {exc}")
        return None


def compute_pct_change(old_price: float, new_price: float) -> float:
    """Calculate percentage change from old to new price."""
    if old_price == 0:
        return 0.0
    return ((new_price - old_price) / old_price) * 100.0


# ── Polymarket helpers ─────────────────────────────────────────────────────────

def fetch_bitcoin_markets(verbose: bool = False) -> list[MarketSnapshot]:
    """
    Fetch active Polymarket markets whose question mentions 'bitcoin' or 'btc'.

    Args:
        verbose: When True, print diagnostic info about the raw API response
                 and show up to 3 sample markets. Use this for the initial
                 baseline fetch; leave False during polling to avoid noise.

    Mid price is computed from bestBid/bestAsk when available.
    Markets where mid price cannot be computed fall back to 0.5 (neutral
    probability) so they still appear in the tracking window rather than
    being silently dropped.

    Returns an empty list only on network/parse failure.
    """
    data = _fetch_json(POLYMARKET_URL)
    if data is None:
        return []

    # The CLOB API returns either a list directly or {"data": [...], "next_cursor": ...}
    if isinstance(data, dict):
        markets_raw = data.get("data", [])
    elif isinstance(data, list):
        markets_raw = data
    else:
        print(f"  [polymarket] Unexpected response type: {type(data).__name__}")
        return []

    if verbose:
        print(f"  [polymarket] Total markets in API response: {len(markets_raw)}")
        if markets_raw:
            print("  [polymarket] Sample markets (first 3):")
            for sample in markets_raw[:3]:
                if isinstance(sample, dict):
                    raw_id = sample.get("condition_id") or sample.get("id", "?")
                    question = sample.get("question", "")[:70]
                    print(f"    id={str(raw_id)[:14]}  q={question!r}")

    snapshots: list[MarketSnapshot] = []
    now = time.time()

    for market in markets_raw:
        if not isinstance(market, dict):
            continue

        question = market.get("question", "")

        # Loose filter: match "bitcoin" OR "btc" (case-insensitive)
        if not any(kw in question.lower() for kw in BTC_KEYWORDS):
            continue

        # Try to compute mid price; fall back to neutral 0.5 so the market
        # stays in the tracking window even when bid/ask data is absent.
        mid_price = _compute_mid_price(market)
        if mid_price is None:
            mid_price = 0.5

        market_id = market.get("condition_id") or market.get("id") or question[:40]
        snapshots.append(MarketSnapshot(
            market_id=str(market_id),
            question=question,
            mid_price=mid_price,
            timestamp=now,
        ))

    if verbose:
        if snapshots:
            print(f"  [polymarket] BTC/Bitcoin markets matched: {len(snapshots)}")
        else:
            print("  [polymarket] ⚠️  No BTC/Bitcoin markets found after filtering.")
            print("  [polymarket] Full response preview (first 5 items):")
            try:
                preview = json.dumps(markets_raw[:5], ensure_ascii=False, indent=2)
                # Cap at 2000 chars so the terminal stays readable
                print(preview[:2000])
                if len(preview) > 2000:
                    print("  ... (truncated)")
            except Exception as exc:
                print(f"  (could not serialize response: {exc})")

    return snapshots


def _compute_mid_price(market: dict) -> Optional[float]:
    """
    Derive a single price from a market dict.
    Tries bestBid/bestAsk → tokens → outcomePrices → falls back to None.
    """
    best_bid = _safe_float(market.get("bestBid") or market.get("best_bid"))
    best_ask = _safe_float(market.get("bestAsk") or market.get("best_ask"))

    if best_bid is not None and best_ask is not None and best_ask > 0:
        return (best_bid + best_ask) / 2.0

    # Some endpoints expose per-outcome token prices
    tokens = market.get("tokens")
    if isinstance(tokens, list) and tokens:
        prices = [_safe_float(t.get("price")) for t in tokens if isinstance(t, dict)]
        prices = [p for p in prices if p is not None]
        if prices:
            return sum(prices) / len(prices)

    outcome_prices = market.get("outcomePrices")
    if isinstance(outcome_prices, list) and outcome_prices:
        prices = [_safe_float(p) for p in outcome_prices]
        prices = [p for p in prices if p is not None]
        if prices:
            return sum(prices) / len(prices)

    return None


def _safe_float(value) -> Optional[float]:
    """Convert a value to float safely, returning None if not possible."""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


# ── Persistence ────────────────────────────────────────────────────────────────

def append_latency_log(records: list[LatencyRecord]) -> None:
    """
    Append latency records to data/latency.jsonl in the project root.
    Each record becomes one JSON line. Fails silently so the scan loop is never interrupted.
    """
    if not records:
        return

    try:
        LATENCY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()

        with LATENCY_LOG_PATH.open("a", encoding="utf-8") as log_file:
            for record in records:
                entry = {
                    "ts": ts,
                    "market_id": record.market_id,
                    "question": record.question,
                    "latency": round(record.latency_seconds, 4),
                    "direction": record.direction,
                }
                log_file.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        # Log persistence must never crash the main scan loop
        print(f"  [log] Failed to write latency log: {exc}")


# ── Latency detection logic ────────────────────────────────────────────────────

def detect_direction_change(
    baseline: MarketSnapshot,
    current: MarketSnapshot,
    binance_direction: str,
) -> Optional[str]:
    """
    Check whether a market's price moved in the expected direction.

    A Binance "up" event should eventually push probability markets for
    high-price outcomes upward and low-price outcomes downward.
    This is a simple heuristic — it just checks whether the mid changed at all.
    """
    delta = current.mid_price - baseline.mid_price
    if abs(delta) < 0.001:   # not enough movement to classify
        return None

    if delta > 0:
        return "up"
    return "down"


def measure_market_latency(
    baseline_markets: list[MarketSnapshot],
    event_time: float,
    binance_direction: str,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    window: float = TRACKING_WINDOW_SECONDS,
) -> list[LatencyRecord]:
    """
    Poll Polymarket at regular intervals until each bitcoin market shows
    a direction change, recording when it first reacts.

    Returns LatencyRecord per market that reacted within the window.
    """
    baseline_by_id = {m.market_id: m for m in baseline_markets}
    first_reaction: dict[str, float] = {}
    direction_by_id: dict[str, str] = {}

    deadline = event_time + window

    while time.time() < deadline:
        time.sleep(poll_interval)
        current_markets = fetch_bitcoin_markets()

        for current in current_markets:
            if current.market_id in first_reaction:
                continue   # already recorded

            baseline = baseline_by_id.get(current.market_id)
            if baseline is None:
                continue

            direction = detect_direction_change(baseline, current, binance_direction)
            if direction is not None:
                latency = current.timestamp - event_time
                first_reaction[current.market_id] = latency
                direction_by_id[current.market_id] = direction

    results: list[LatencyRecord] = []
    for market in baseline_markets:
        if market.market_id in first_reaction:
            results.append(LatencyRecord(
                market_id=market.market_id,
                question=market.question,
                latency_seconds=first_reaction[market.market_id],
                direction=direction_by_id.get(market.market_id, "unclear"),
            ))

    # Sort so the slowest-to-react (most lagging) markets are at the top
    results.sort(key=lambda r: r.latency_seconds, reverse=True)
    return results


# ── CLI presentation ───────────────────────────────────────────────────────────

def print_event(pct_change: float) -> None:
    sign = "+" if pct_change >= 0 else ""
    print(f"\n📡 EVENT: {sign}{pct_change:.2f}%\n")


def print_lagging_markets(records: list[LatencyRecord]) -> None:
    top = records[:TOP_N_MARKETS]
    if not top:
        print("  (No markets reacted within the observation window)")
        return

    print("⚡ Lagging markets:")
    for record in top:
        dir_icon = "📈" if record.direction == "up" else "📉" if record.direction == "down" else "➡️"
        # Truncate the question so the line stays readable
        question = record.question[:60]
        if len(record.question) > 60:
            question += "…"
        # Show a short prefix of the market_id (first 10 chars) for reproducibility
        short_id = record.market_id[:10]
        print(f"  {dir_icon}  [{short_id}] {question} → {record.latency_seconds:.2f}s")


# ── Threshold resolution ───────────────────────────────────────────────────────

def resolve_threshold(cli_value: Optional[float]) -> float:
    """
    Return the effective threshold in this priority order:
      1. Value passed explicitly via CLI (cli_value)
      2. TRADER_LATENCY_THRESHOLD environment variable
      3. DEFAULT_THRESHOLD_PCT module constant
    """
    if cli_value is not None:
        return cli_value

    env_value = os.environ.get("TRADER_LATENCY_THRESHOLD")
    if env_value is not None:
        try:
            return float(env_value)
        except ValueError:
            print(f"  [config] Invalid TRADER_LATENCY_THRESHOLD='{env_value}', using default.")

    return DEFAULT_THRESHOLD_PCT


# ── Main scan loop ─────────────────────────────────────────────────────────────

def run_scan(threshold: Optional[float] = None) -> None:
    """
    Continuously poll Binance. When a significant price move is detected,
    record a baseline Polymarket snapshot and measure how long each market
    takes to reflect the move.

    Args:
        threshold: % move that triggers tracking. Falls back to the
                   TRADER_LATENCY_THRESHOLD env var, then DEFAULT_THRESHOLD_PCT.
    """
    effective_threshold = resolve_threshold(threshold)

    print("🔍 Monitoring Binance BTC price...")
    print(f"   Trigger threshold: {effective_threshold:.2f}%  / "
          f"Tracking window: {TRACKING_WINDOW_SECONDS:.0f}s")
    print(f"   Log output: {LATENCY_LOG_PATH}")
    print("   Press Ctrl+C to stop\n")

    previous = fetch_binance_price()
    if previous is None:
        print("❌ Cannot connect to Binance. Check your network connection.")
        return

    print(f"  Initial BTC price: ${previous:,.2f}")

    while True:
        time.sleep(POLL_INTERVAL_SECONDS)

        current_price = fetch_binance_price()
        if current_price is None:
            continue   # transient failure — keep going

        pct_change = compute_pct_change(previous, current_price)

        if abs(pct_change) >= effective_threshold:
            event_time = time.time()
            binance_direction = "up" if pct_change > 0 else "down"
            print_event(pct_change)

            print("  Fetching Polymarket baseline...")
            baseline_markets = fetch_bitcoin_markets(verbose=True)

            if not baseline_markets:
                print("  ⚠️  No Bitcoin markets found. Waiting for next event.\n")
                previous = current_price
                continue

            print(f"  Tracking {len(baseline_markets)} markets "
                  f"(up to {TRACKING_WINDOW_SECONDS:.0f}s)...\n")

            lagging = measure_market_latency(
                baseline_markets=baseline_markets,
                event_time=event_time,
                binance_direction=binance_direction,
            )

            print_lagging_markets(lagging)
            append_latency_log(lagging)
            print()

            # Reset baseline to current price after tracking completes
            previous = fetch_binance_price() or current_price
        else:
            # No event — update reference silently
            previous = current_price


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Polymarket vs Binance latency detector")
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help=(
            f"BTC move %% that triggers tracking "
            f"(default: {DEFAULT_THRESHOLD_PCT}, env: TRADER_LATENCY_THRESHOLD)"
        ),
    )
    args = parser.parse_args()

    try:
        run_scan(threshold=args.threshold)
    except KeyboardInterrupt:
        print("\n\n👋 Stopped.")

