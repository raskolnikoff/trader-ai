#!/usr/bin/env python3
"""
Polymarket vs Binance latency detector.

Experimental / observation tool only.
This module measures how quickly Polymarket prediction markets react to
Binance BTC price movements. It is NOT a trading strategy.

Usage:
    python detector.py
"""

import time
import urllib.request
import urllib.error
import json
from dataclasses import dataclass, field
from typing import Optional

# ── Configurable thresholds ────────────────────────────────────────────────────
BINANCE_MOVE_THRESHOLD_PCT = 0.20   # % move that counts as a "significant event"
POLL_INTERVAL_SECONDS = 2.0         # how often to poll Binance during tracking
TRACKING_WINDOW_SECONDS = 60.0      # how long to watch for Polymarket reaction
TOP_N_MARKETS = 5                   # lagging markets to display
REQUEST_TIMEOUT = 10                # HTTP timeout in seconds

BINANCE_URL = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
POLYMARKET_URL = "https://clob.polymarket.com/markets"


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
    question: str
    latency_seconds: float
    direction: str   # "up" | "down" | "unclear"


# ── HTTP helpers ───────────────────────────────────────────────────────────────

def _fetch_json(url: str) -> Optional[dict | list]:
    """Fetch JSON from a URL. Returns None on any error so callers degrade gracefully."""
    try:
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw)
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

def fetch_bitcoin_markets() -> list[MarketSnapshot]:
    """
    Fetch active Polymarket markets that mention 'bitcoin' in their question.
    Computes mid price from bestBid / bestAsk when available.
    Returns an empty list on any failure.
    """
    data = _fetch_json(POLYMARKET_URL)
    if data is None:
        return []

    # The CLOB API returns either a list directly or a dict with a "data" key
    if isinstance(data, dict):
        markets_raw = data.get("data", [])
    elif isinstance(data, list):
        markets_raw = data
    else:
        return []

    snapshots: list[MarketSnapshot] = []
    now = time.time()

    for market in markets_raw:
        if not isinstance(market, dict):
            continue

        question = market.get("question", "")
        if "bitcoin" not in question.lower():
            continue

        mid_price = _compute_mid_price(market)
        if mid_price is None:
            continue

        market_id = market.get("condition_id") or market.get("id") or question[:40]
        snapshots.append(MarketSnapshot(
            market_id=str(market_id),
            question=question,
            mid_price=mid_price,
            timestamp=now,
        ))

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
        print("  (観測ウィンドウ内に反応したマーケットはありません)")
        return

    print("⚡ Lagging markets:")
    for record in top:
        dir_icon = "📈" if record.direction == "up" else "📉" if record.direction == "down" else "➡️"
        question = record.question[:80]
        print(f"  {dir_icon}  {question} → {record.latency_seconds:.2f}s")


# ── Main scan loop ─────────────────────────────────────────────────────────────

def run_scan() -> None:
    """
    Continuously poll Binance. When a significant price move is detected,
    record a baseline Polymarket snapshot and measure how long each market
    takes to reflect the move.
    """
    print("🔍 Binance BTC 価格を監視中...")
    print(f"   移動閾値: {BINANCE_MOVE_THRESHOLD_PCT:.2f}%  / "
          f"追跡ウィンドウ: {TRACKING_WINDOW_SECONDS:.0f}s\n")
    print("   Ctrl+C で終了\n")

    previous = fetch_binance_price()
    if previous is None:
        print("❌ Binance に接続できません。ネットワークを確認してください。")
        return

    print(f"  初期 BTC 価格: ${previous:,.2f}")

    while True:
        time.sleep(POLL_INTERVAL_SECONDS)

        current_price = fetch_binance_price()
        if current_price is None:
            continue   # transient failure — keep going

        pct_change = compute_pct_change(previous, current_price)

        if abs(pct_change) >= BINANCE_MOVE_THRESHOLD_PCT:
            event_time = time.time()
            binance_direction = "up" if pct_change > 0 else "down"
            print_event(pct_change)

            print("  Polymarket のベースラインを取得中...")
            baseline_markets = fetch_bitcoin_markets()

            if not baseline_markets:
                print("  ⚠️  Bitcoin マーケットが見つかりません。次のイベントを待ちます。\n")
                previous = current_price
                continue

            print(f"  {len(baseline_markets)} マーケットを追跡中 "
                  f"（最大 {TRACKING_WINDOW_SECONDS:.0f}s）...\n")

            lagging = measure_market_latency(
                baseline_markets=baseline_markets,
                event_time=event_time,
                binance_direction=binance_direction,
            )

            print_lagging_markets(lagging)
            print()

            # Reset baseline to current price after tracking completes
            previous = fetch_binance_price() or current_price
        else:
            # No event — update reference silently
            previous = current_price


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        run_scan()
    except KeyboardInterrupt:
        print("\n\n👋 終了しました。")

