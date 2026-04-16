#!/usr/bin/env python3
"""
Phase 1: Paper trading backtest from ws_latency.jsonl.

Simulates 'what if we had bet $1 on every lagging market that reacted
within a given latency threshold' and reports:
  - Win rate        (direction matched Binance move)
  - Expected value  per dollar wagered
  - P&L curve       (ASCII sparkline)
  - Per-market breakdown

NOTE: SIMULATION ONLY. No real money involved.
Assumptions:
  - Binary market: YES/NO token resolves at $1.00.
  - We buy the 'correct direction' token at observed mid price.
  - Binance price direction = ground truth for resolution.
  - No gas beyond configured fee/slippage params.

Usage:
    python backtest.py
    python backtest.py --max-latency 5000 --stake 10 --fee 0.02
    python backtest.py --json

No extra dependencies (stdlib only).
"""

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Optional

# -- Config --------------------------------------------------------------------

_PROJECT_ROOT       = Path(__file__).parent.parent.parent.parent
WS_LOG_PATH         = _PROJECT_ROOT / "data" / "ws_latency.jsonl"

DEFAULT_STAKE       = 1.0       # USDC per trade
DEFAULT_FEE_PCT     = 0.02      # 2% round-trip (conservative)
DEFAULT_SLIPPAGE    = 0.005     # 0.5% entry slippage
DEFAULT_MAX_LATENCY = 10_000    # ms cutoff
SPARKLINE_WIDTH     = 40


# -- Data loading --------------------------------------------------------------

def load_records(path: Path = WS_LOG_PATH) -> list[dict]:
    if not path.exists():
        return []
    records = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    records.append(obj)
            except json.JSONDecodeError:
                pass
    return records


# -- Trade simulation ----------------------------------------------------------

def simulate_trade(
    mid_price: float,
    direction_signal: str,   # "up"|"down" -- what Polymarket moved
    binance_direction: str,  # "up"|"down" -- ground truth
    stake: float,
    fee_pct: float,
    slippage: float,
) -> dict:
    """
    Simulate a binary-outcome bet on Polymarket.
    WIN  -> payout = contracts * $1.00
    LOSS -> lose stake
    """
    entry_price     = min(mid_price + slippage, 0.99)
    effective_stake = stake * (1.0 - fee_pct)
    contracts       = effective_stake / entry_price
    is_win          = direction_signal == binance_direction
    pnl             = (contracts * 1.0 - stake) if is_win else -stake

    return {
        "win":         is_win,
        "pnl":         round(pnl, 4),
        "entry_price": round(entry_price, 4),
        "contracts":   round(contracts, 4),
    }


# -- Backtest ------------------------------------------------------------------

def run_backtest(
    records: list[dict],
    max_latency_ms: float = DEFAULT_MAX_LATENCY,
    stake: float          = DEFAULT_STAKE,
    fee_pct: float        = DEFAULT_FEE_PCT,
    slippage: float       = DEFAULT_SLIPPAGE,
) -> dict:
    trades     = []
    pnl_curve  = []
    per_market = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    skipped    = 0
    cumulative = 0.0

    for rec in records:
        latency   = rec.get("latency_ms")
        direction = rec.get("direction", "")
        if latency is None or direction not in ("up", "down"):
            skipped += 1
            continue
        if float(latency) > max_latency_ms:
            skipped += 1
            continue

        pct         = rec.get("pct_change", 0.0)
        binance_dir = "up" if pct > 0 else ("down" if pct < 0 else direction)
        mid         = float(rec.get("mid_price", 0.55))  # fallback: conservative

        result = simulate_trade(
            mid_price=mid,
            direction_signal=direction,
            binance_direction=binance_dir,
            stake=stake,
            fee_pct=fee_pct,
            slippage=slippage,
        )
        trades.append(result)
        cumulative += result["pnl"]
        pnl_curve.append(round(cumulative, 4))

        mkt = rec.get("market_id", "unknown")[:20]
        per_market[mkt]["trades"] += 1
        per_market[mkt]["pnl"]    = round(per_market[mkt]["pnl"] + result["pnl"], 4)
        if result["win"]:
            per_market[mkt]["wins"] += 1

    n = len(trades)
    if n == 0:
        return {
            "trades": 0, "skipped": skipped,
            "note": "No qualifying trades. Run ws_detector.py to collect data first.",
        }

    wins      = sum(1 for t in trades if t["win"])
    total_pnl = round(sum(t["pnl"] for t in trades), 4)
    ev        = round(total_pnl / (n * stake), 4)

    market_list = sorted(
        [
            {
                "market_id": k,
                "trades":    v["trades"],
                "wins":      v["wins"],
                "win_rate":  round(v["wins"] / v["trades"], 3),
                "pnl":       v["pnl"],
            }
            for k, v in per_market.items()
        ],
        key=lambda m: m["pnl"],
        reverse=True,
    )

    return {
        "trades":        n,
        "skipped":       skipped,
        "wins":          wins,
        "win_rate":      round(wins / n, 4),
        "total_pnl":     total_pnl,
        "avg_pnl":       round(statistics.mean(t["pnl"] for t in trades), 4),
        "ev_per_dollar": ev,
        "pnl_curve":     pnl_curve,
        "per_market":    market_list,
        "config": {
            "max_latency_ms": max_latency_ms,
            "stake":          stake,
            "fee_pct":        fee_pct,
            "slippage":       slippage,
        },
    }


# -- ASCII sparkline -----------------------------------------------------------

def sparkline(values: list[float], width: int = SPARKLINE_WIDTH) -> str:
    if not values:
        return "(no data)"
    step    = max(1, len(values) // width)
    sampled = values[::step][:width]
    lo, hi  = min(sampled), max(sampled)
    span    = hi - lo or 1.0
    bars    = " \u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
    return "".join(bars[int((v - lo) / span * (len(bars) - 1))] for v in sampled)


# -- Report formatting ---------------------------------------------------------

def format_report(result: dict, as_json: bool = False) -> str:
    if as_json:
        out = {k: v for k, v in result.items() if k != "pnl_curve"}
        return json.dumps(out, ensure_ascii=False, indent=2)

    if "note" in result:
        return f"[backtest] {result['note']}"

    ev = result["ev_per_dollar"]
    if ev > 0.05:
        verdict = "[PASS] POSITIVE EDGE -- proceed to Phase 2 (real execution)"
    elif ev > 0:
        verdict = "[MARGINAL] Weak edge -- collect more data"
    else:
        verdict = "[FAIL] No edge -- latency arb may be priced out"

    lines = [
        "[backtest] Paper Trading Simulation (ws_latency.jsonl)",
        "",
        f"Trades:        {result['trades']}  (skipped: {result['skipped']})",
        f"Win rate:      {result['win_rate']*100:.1f}%  ({result['wins']}/{result['trades']})",
        f"Total P&L:     ${result['total_pnl']:+.4f}",
        f"Avg P&L/trade: ${result['avg_pnl']:+.4f}",
        f"EV/dollar:     {result['ev_per_dollar']:+.4f}",
        "",
        "P&L curve:",
        f"  {sparkline(result['pnl_curve'])}",
        f"  [{result['pnl_curve'][0]:+.3f}] -> [{result['pnl_curve'][-1]:+.3f}]",
        "",
        "Config:",
        f"  max_latency={result['config']['max_latency_ms']}ms  "
        f"stake=${result['config']['stake']}  "
        f"fee={result['config']['fee_pct']*100:.0f}%  "
        f"slippage={result['config']['slippage']*100:.1f}%",
        "",
        "Top markets by P&L:",
    ]
    for m in result["per_market"][:5]:
        lines.append(
            f"  {m['market_id']:<22}  "
            f"trades={m['trades']}  wr={m['win_rate']*100:.0f}%  pnl=${m['pnl']:+.4f}"
        )
    lines += ["", verdict]
    return "\n".join(lines)


# -- Entry point ---------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Paper trading backtest from ws_latency.jsonl"
    )
    parser.add_argument(
        "--max-latency", type=float, default=DEFAULT_MAX_LATENCY,
        help="Only trades where market reacted within N ms (default: 10000)",
    )
    parser.add_argument(
        "--stake", type=float, default=DEFAULT_STAKE,
        help="USDC per trade (default: 1.0)",
    )
    parser.add_argument(
        "--fee", type=float, default=DEFAULT_FEE_PCT,
        help="Round-trip fee as decimal (default: 0.02)",
    )
    parser.add_argument(
        "--slippage", type=float, default=DEFAULT_SLIPPAGE,
        help="Entry slippage as decimal (default: 0.005)",
    )
    parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="Output machine-readable JSON",
    )
    args = parser.parse_args()

    records = load_records()
    result  = run_backtest(
        records,
        max_latency_ms=args.max_latency,
        stake=args.stake,
        fee_pct=args.fee,
        slippage=args.slippage,
    )
    print(format_report(result, as_json=args.as_json))


if __name__ == "__main__":
    main()
