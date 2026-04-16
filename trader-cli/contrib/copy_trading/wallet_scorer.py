#!/usr/bin/env python3
"""
Wallet scorer for copy trading candidate selection.

The Polymarket /trades endpoint does NOT return resolved/won fields.
Instead, this module uses two endpoints:
  - /trades  -> all trades (BUY + SELL) for volume, recency, activity
  - /activity?type=REDEEM -> redemptions = winning positions paid out

Win rate approximation:
  win_rate = redeem_count / buy_count
  (each REDEEM corresponds to a resolved winning position)

This is an approximation because:
  - A wallet may have open positions that haven't resolved yet
  - One REDEEM can cover multiple token buys on the same market
  But it's the best signal available from the public API without auth.

Scoring:
  composite = win_rate * log(buy_count + 1) * (recency + 0.1)

Usage:
    python wallet_scorer.py --address 0xABC...
    python wallet_scorer.py --address 0xABC... --json
    python wallet_scorer.py --address 0xABC... --min-trades 5 --min-win-rate 0.4
"""

import argparse
import json
import math
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

# -- Constants -----------------------------------------------------------------

DATA_API_BASE   = "https://data-api.polymarket.com"
REQUEST_TIMEOUT = 10
FETCH_LIMIT     = 500

# Default hard filters
MIN_TRADES     = 10     # minimum BUY trades required
MIN_WIN_RATE   = 0.40   # relaxed from 0.55 -- REDEEM-based is conservative
MAX_AVG_TRADE  = 5_000  # skip whales
MIN_AVG_TRADE  = 5      # skip dust
RECENCY_DAYS   = 7


# -- Data containers -----------------------------------------------------------

@dataclass
class WalletScore:
    address: str
    buy_count: int          # number of BUY trades
    sell_count: int         # number of SELL trades
    redeem_count: int       # number of REDEEM events (winning positions)
    win_rate: float         # redeem_count / buy_count
    avg_trade_size: float   # mean BUY size in USDC
    recency_score: float    # fraction of buys in last RECENCY_DAYS
    composite: float
    disqualified: bool = False
    disqualify_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "address":        self.address,
            "buy_count":      self.buy_count,
            "sell_count":     self.sell_count,
            "redeem_count":   self.redeem_count,
            "win_rate":       round(self.win_rate, 4),
            "avg_trade_size": round(self.avg_trade_size, 2),
            "recency_score":  round(self.recency_score, 4),
            "composite":      round(self.composite, 4),
            "disqualified":   self.disqualified,
            "reason":         self.disqualify_reason,
        }


# -- HTTP helper ---------------------------------------------------------------

def _fetch_json(url: str) -> Optional[dict | list]:
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
    except Exception as exc:
        print(f"  [http] {exc}")
        return None


# -- Data fetching -------------------------------------------------------------

def fetch_trades(address: str) -> list[dict]:
    """Fetch all trades (BUY + SELL) for a wallet."""
    url  = f"{DATA_API_BASE}/trades?user={address}&limit={FETCH_LIMIT}&takerOnly=false"
    data = _fetch_json(url)
    if data is None:
        return []
    return data if isinstance(data, list) else data.get("data", [])


def fetch_redeems(address: str) -> list[dict]:
    """
    Fetch REDEEM activity for a wallet.
    Each REDEEM = a winning position was claimed (market resolved YES for the held token).
    """
    url  = f"{DATA_API_BASE}/activity?user={address}&type=REDEEM&limit={FETCH_LIMIT}"
    data = _fetch_json(url)
    if data is None:
        return []
    return data if isinstance(data, list) else data.get("data", [])


# -- Scoring -------------------------------------------------------------------

def score_wallet(
    address: str,
    trades: list[dict],
    redeems: list[dict],
    min_trades: int     = MIN_TRADES,
    min_win_rate: float = MIN_WIN_RATE,
    max_avg_trade: float = MAX_AVG_TRADE,
    min_avg_trade: float = MIN_AVG_TRADE,
    recency_days: int   = RECENCY_DAYS,
) -> WalletScore:
    # Split trades into BUY / SELL
    buys  = [t for t in trades if str(t.get("side", "")).upper() == "BUY"]
    sells = [t for t in trades if str(t.get("side", "")).upper() == "SELL"]

    buy_count    = len(buys)
    sell_count   = len(sells)
    redeem_count = len(redeems)

    # Win rate: redeems / buys (capped at 1.0)
    win_rate = min(redeem_count / buy_count, 1.0) if buy_count > 0 else 0.0

    # Avg trade size from BUY side
    buy_sizes = [float(t.get("size", 0) or 0) for t in buys if float(t.get("size", 0) or 0) > 0]
    avg_trade = sum(buy_sizes) / len(buy_sizes) if buy_sizes else 0.0

    # Recency: fraction of buys within last RECENCY_DAYS
    cutoff  = time.time() - recency_days * 86400
    recent  = [t for t in buys if float(t.get("timestamp", 0) or 0) >= cutoff]
    recency = len(recent) / buy_count if buy_count > 0 else 0.0

    # Composite score
    composite = win_rate * math.log(buy_count + 1) * (recency + 0.1)

    score = WalletScore(
        address=address,
        buy_count=buy_count,
        sell_count=sell_count,
        redeem_count=redeem_count,
        win_rate=round(win_rate, 4),
        avg_trade_size=round(avg_trade, 2),
        recency_score=round(recency, 4),
        composite=round(composite, 4),
    )

    # Hard filters
    if buy_count < min_trades:
        score.disqualified     = True
        score.disqualify_reason = f"too few buys ({buy_count} < {min_trades})"
    elif win_rate < min_win_rate:
        score.disqualified     = True
        score.disqualify_reason = f"win rate too low ({win_rate*100:.1f}% < {min_win_rate*100:.0f}%)"
    elif avg_trade > max_avg_trade:
        score.disqualified     = True
        score.disqualify_reason = f"avg trade too large (${avg_trade:,.0f} > ${max_avg_trade:,.0f})"
    elif avg_trade < min_avg_trade:
        score.disqualified     = True
        score.disqualify_reason = f"avg trade too small (${avg_trade:.2f} < ${min_avg_trade})"

    return score


# -- Public API ----------------------------------------------------------------

def evaluate_wallet(
    address: str,
    min_trades: int     = MIN_TRADES,
    min_win_rate: float = MIN_WIN_RATE,
    max_avg_trade: float = MAX_AVG_TRADE,
) -> WalletScore:
    """Fetch trades + redeems and score a wallet. Main entry point."""
    trades  = fetch_trades(address)
    redeems = fetch_redeems(address)
    return score_wallet(
        address, trades, redeems,
        min_trades=min_trades,
        min_win_rate=min_win_rate,
        max_avg_trade=max_avg_trade,
    )


# -- Entry point ---------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Score a Polymarket wallet for copy trading (uses /trades + /activity REDEEM)"
    )
    parser.add_argument("--address", required=True,
                        help="Polymarket proxy wallet address (0x...)")
    parser.add_argument("--min-trades", type=int, default=MIN_TRADES,
                        help=f"Minimum BUY trades [default: {MIN_TRADES}]")
    parser.add_argument("--min-win-rate", type=float, default=MIN_WIN_RATE,
                        help=f"Minimum win rate 0-1 [default: {MIN_WIN_RATE}]")
    parser.add_argument("--max-avg-trade", type=float, default=MAX_AVG_TRADE,
                        help=f"Max avg BUY size USDC [default: {MAX_AVG_TRADE}]")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="Output machine-readable JSON")
    args = parser.parse_args()

    score = evaluate_wallet(
        address=args.address,
        min_trades=args.min_trades,
        min_win_rate=args.min_win_rate,
        max_avg_trade=args.max_avg_trade,
    )

    if args.as_json:
        print(json.dumps(score.to_dict(), indent=2))
        return

    status = "[DISQUALIFIED]" if score.disqualified else "[CANDIDATE]"
    print(f"\n{status}  {score.address}")
    print(f"  buys:          {score.buy_count}  sells: {score.sell_count}  redeems: {score.redeem_count}")
    print(f"  win_rate:      {score.win_rate*100:.1f}%  (redeems / buys)")
    print(f"  avg_trade:     ${score.avg_trade_size:,.2f}")
    print(f"  recency:       {score.recency_score*100:.0f}% buys in last {RECENCY_DAYS}d")
    print(f"  composite:     {score.composite:.4f}")
    if score.disqualified:
        print(f"  reason:        {score.disqualify_reason}")


if __name__ == "__main__":
    main()
