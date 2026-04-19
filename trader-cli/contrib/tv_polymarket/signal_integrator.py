#!/usr/bin/env python3
"""
Signal integrator: TradingView + Binance + Polymarket -> unified context.

Collects data from all three sources concurrently (asyncio) and assembles
a SignalContext object ready for Claude analysis.

Usage (standalone):
    python signal_integrator.py
    python signal_integrator.py --symbol BTCUSDT
    python signal_integrator.py --symbol SPX --json
"""

import argparse
import asyncio
import json
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_HERE = Path(__file__).parent
_CLI  = _HERE.parent.parent
_ROOT = _CLI.parent
for _p in [str(_CLI), str(_ROOT)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tv_client import collect_tv_context, tv_binary_available, check_tv_reachable
from contrib.tv_polymarket.polymarket_markets import (
    find_markets_for_symbol, format_markets, PolymarketMarket
)

# -- Constants -----------------------------------------------------------------

BINANCE_PRICE_URL = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
BINANCE_KLINE_URL = (
    "https://api.binance.com/api/v3/klines"
    "?symbol=BTCUSDT&interval=5m&limit=6"
)
REQUEST_TIMEOUT  = 8
DEFAULT_SYMBOL   = "BTCUSDT"


# -- Data containers -----------------------------------------------------------

@dataclass
class BinanceSnapshot:
    price: float
    change_5m: Optional[float]
    change_30m: Optional[float]
    timestamp: float = field(default_factory=time.time)


@dataclass
class SignalContext:
    symbol: str
    tv: Optional[dict[str, Any]]
    binance: Optional[BinanceSnapshot]
    polymarket_markets: list[PolymarketMarket]
    wallet_alerts: list[dict]
    collected_at: float = field(default_factory=time.time)

    @property
    def tv_available(self) -> bool:
        return self.tv is not None and self.tv.get("quote") is not None

    @property
    def binance_available(self) -> bool:
        return self.binance is not None

    @property
    def polymarket_available(self) -> bool:
        return len(self.polymarket_markets) > 0


# -- HTTP helpers --------------------------------------------------------------

def _fetch_json_sync(url: str) -> Optional[dict | list]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; trader-ai/1.0)",
            "Accept":     "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


async def _fetch_json(url: str) -> Optional[dict | list]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_json_sync, url)


# -- Binance -------------------------------------------------------------------

async def collect_binance() -> Optional[BinanceSnapshot]:
    price_data, kline_data = await asyncio.gather(
        _fetch_json(BINANCE_PRICE_URL),
        _fetch_json(BINANCE_KLINE_URL),
    )
    if price_data is None:
        return None
    try:
        price = float(price_data["price"])
    except (KeyError, TypeError, ValueError):
        return None

    change_5m = change_30m = None
    if isinstance(kline_data, list) and len(kline_data) >= 2:
        try:
            latest_close = float(kline_data[-1][4])
            prev_close   = float(kline_data[-2][4])
            oldest_close = float(kline_data[0][4])
            if prev_close > 0:
                change_5m  = round((latest_close - prev_close) / prev_close * 100, 3)
            if oldest_close > 0:
                change_30m = round((latest_close - oldest_close) / oldest_close * 100, 3)
        except (IndexError, TypeError, ValueError):
            pass

    return BinanceSnapshot(price=price, change_5m=change_5m, change_30m=change_30m)


# -- TradingView ---------------------------------------------------------------

async def collect_tv(symbol: Optional[str] = None) -> Optional[dict[str, Any]]:
    if not tv_binary_available():
        return None
    loop = asyncio.get_event_loop()
    reachable = await loop.run_in_executor(None, check_tv_reachable)
    if not reachable:
        return None
    return await loop.run_in_executor(None, collect_tv_context, symbol)


# -- Polymarket (sync wrapper, called via run_in_executor) ---------------------

def _find_markets_sync(symbol: str) -> list[PolymarketMarket]:
    """Sync wrapper so it can be called via run_in_executor."""
    return find_markets_for_symbol(symbol, limit=5)


# -- Wallet alert log ----------------------------------------------------------

def load_recent_alerts(max_alerts: int = 5) -> list[dict]:
    log_path = _ROOT / "data" / "ws_latency.jsonl"
    if not log_path.exists():
        return []
    records = []
    try:
        with log_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict) and "market_id" in obj:
                        records.append(obj)
                except json.JSONDecodeError:
                    pass
    except Exception:
        return []
    return records[-max_alerts:]


# -- Main integrator -----------------------------------------------------------

async def collect_signal(symbol: str = DEFAULT_SYMBOL) -> SignalContext:
    """
    Collect all three data sources concurrently.
    Uses run_in_executor for sync functions; never nests create_task around
    a Future (which broke on Python 3.14).
    """
    loop = asyncio.get_event_loop()

    # Run all three sources concurrently using gather
    # Note: run_in_executor returns a Future, not a coroutine.
    # We pass them directly to gather() -- no create_task() wrapping needed.
    tv_coro      = collect_tv(symbol)
    binance_coro = collect_binance()
    poly_future  = loop.run_in_executor(None, _find_markets_sync, symbol)

    tv_data, binance_data, poly_markets = await asyncio.gather(
        tv_coro,
        binance_coro,
        poly_future,
        return_exceptions=True,
    )

    if isinstance(tv_data, Exception):
        tv_data = None
    if isinstance(binance_data, Exception):
        binance_data = None
    if isinstance(poly_markets, Exception):
        poly_markets = []

    wallet_alerts = load_recent_alerts()

    return SignalContext(
        symbol=symbol,
        tv=tv_data,
        binance=binance_data,
        polymarket_markets=poly_markets or [],
        wallet_alerts=wallet_alerts,
    )


# -- Formatting ----------------------------------------------------------------

def format_signal_context(ctx: SignalContext) -> str:
    lines = [
        f"[Signal Context]  {ctx.symbol}  "
        f"@ {time.strftime('%H:%M:%S UTC', time.gmtime(ctx.collected_at))}"
    ]

    lines += ["", "=== TradingView ==="]
    if ctx.tv_available:
        tv = ctx.tv
        lines.append(f"  Symbol: {tv.get('symbol')}  TF: {tv.get('timeframe')}")
        lines.append(f"  Quote:  {json.dumps(tv.get('quote'), ensure_ascii=False)}")
        if tv.get("ohlcv_summary"):
            lines.append(f"  OHLCV:  {json.dumps(tv.get('ohlcv_summary'), ensure_ascii=False)}")
        if tv.get("indicators"):
            lines.append(f"  Ind:    {json.dumps(tv.get('indicators'), ensure_ascii=False)}")
    else:
        lines.append("  (TradingView not connected)")

    lines += ["", "=== Binance ==="]
    if ctx.binance_available:
        b = ctx.binance
        lines.append(f"  BTC: ${b.price:,.2f}")
        if b.change_5m is not None:
            lines.append(f"  5m:  {b.change_5m:+.3f}%")
        if b.change_30m is not None:
            lines.append(f"  30m: {b.change_30m:+.3f}%")
    else:
        lines.append("  (Binance not reachable)")

    lines += ["", "=== Polymarket ==="]
    if ctx.polymarket_available:
        for m in ctx.polymarket_markets:
            prob = f"{m.implied_probability}%" if m.implied_probability is not None else "??"
            lines.append(f"  [{prob:>6} YES]  {m.question[:70]}")
    else:
        lines.append("  (No relevant Polymarket markets found)")

    if ctx.wallet_alerts:
        lines += ["", "=== Recent Wallet Alerts ==="]
        for a in ctx.wallet_alerts[-3:]:
            lines.append(
                f"  {a.get('direction','?'):4}  "
                f"{a.get('latency_ms', a.get('latency','?'))}  "
                f"{a.get('question', a.get('market_id',''))[:60]}"
            )

    return "\n".join(lines)


# -- Entry point ---------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collect unified TV + Binance + Polymarket signal"
    )
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument("--json", dest="as_json", action="store_true")
    args = parser.parse_args()

    ctx = asyncio.run(collect_signal(args.symbol))

    if args.as_json:
        out = {
            "symbol":   ctx.symbol,
            "tv":       ctx.tv,
            "binance": {
                "price":      ctx.binance.price if ctx.binance else None,
                "change_5m":  ctx.binance.change_5m if ctx.binance else None,
                "change_30m": ctx.binance.change_30m if ctx.binance else None,
            } if ctx.binance else None,
            "polymarket":    [m.to_dict() for m in ctx.polymarket_markets],
            "wallet_alerts": ctx.wallet_alerts,
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(format_signal_context(ctx))


if __name__ == "__main__":
    main()
