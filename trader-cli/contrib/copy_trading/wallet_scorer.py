#!/usr/bin/env python3
"""
Wallet scorer for copy trading candidate selection.

Fetches detailed trade history for a wallet from the Polymarket Data API
and computes a composite score to identify bots / consistent winners worth
copying. Filters out wallets that are too large to copy (whale size) or
too inactive to trust.

Scoring factors:
    win_rate        -- fraction of trades that resolved profitably
    pnl             -- absolute profit (USDC)
    trade_count     -- number of resolved trades (proxy for reliability)
    recency_score   -- fraction of trades in the last 7 days (still active?)
    avg_trade_size  -- median trade size in USDC (must be copy-able)

Composite score = win_rate * log(trade_count+1) * recency_score
                  (PnL and avg_trade_size used as hard filters only)

Usage (standalone):
    python wallet_scorer.py --address 0xABC...
    python wallet_scorer.py --address 0xABC... --json
"""

import argparse
import json
import math
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# -- Constants -----------------------------------------------------------------

DATA_API_BASE   = "https://data-api.polymarket.com"
REQUEST_TIMEOUT = 10
TRADES_LIMIT    = 500    # max trades to fetch per wallet

# Hard filter defaults (override via CLI)
MIN_TRADES       = 20     # ignore wallets with fewer resolved trades
MIN_WIN_RATE     = 0.55   # must win >55% of resolved trades
MAX_AVG_TRADE    = 5_000  # skip whales above this avg trade size (USDC)
MIN_AVG_TRADE    = 5      # skip dust traders
RECENCY_DAYS     = 7      # days window for recency score


# -- Data containers -----------------------------------------------------------

@dataclass
class TradeRecord:
    market_id: str
    side: str           # "BUY" | "SELL"
    outcome: str        # "YES" | "NO"
    size: float         # USDC value
    price: float        # entry price (0-1)
    timestamp: float    # unix timestamp
    resolved: bool
    won: Optional[bool] # None if unresolved


@dataclass
class WalletScore:
    address: str
    win_rate: float
    trade_count: int
    resolved_count: int
    avg_trade_size: float
    recency_score: float   # 0-1: fraction of trades in last RECENCY_DAYS days
    composite: float       # final ranking score
    trades: list[TradeRecord] = field(default_factory=list, repr=False)
    disqualified: bool = False
    disqualify_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "address":        self.address,
            "win_rate":       round(self.win_rate, 4),
            "trade_count":    self.trade_count,
            "resolved_count": self.resolved_count,
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


# -- Trade fetching ------------------------------------------------------------

def fetch_trades(address: str, limit: int = TRADES_LIMIT) -> list[dict]:
    """
    Fetch raw trade records for a wallet from the Data API.
    Returns raw dicts; parsing is handled by parse_trades().

    Note: Polymarket users have a PROXY wallet address separate from their
    EOA. Always pass the proxy address (visible in the profile URL).
    """
    url = (
        f"{DATA_API_BASE}/trades"
        f"?maker={address}&limit={limit}&offset=0"
    )
    data = _fetch_json(url)
    if data is None:
        return []
    return data if isinstance(data, list) else data.get("data", [])


def parse_trades(raw: list[dict]) -> list[TradeRecord]:
    """Convert raw API dicts to typed TradeRecord objects."""
    records = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        try:
            size      = float(row.get("size", 0) or 0)
            price     = float(row.get("price", 0) or 0)
            ts_raw    = row.get("timestamp") or row.get("created_at") or 0
            timestamp = float(ts_raw) if ts_raw else 0.0

            # Determine resolution
            outcome_index = row.get("outcomeIndex")
            resolved_val  = row.get("resolved")
            resolved      = bool(resolved_val) if resolved_val is not None else False

            # Won: the trade was on the correct outcome side
            won_raw = row.get("won")
            won     = bool(won_raw) if won_raw is not None else None

            records.append(TradeRecord(
                market_id=row.get("conditionId") or row.get("market_id", ""),
                side=row.get("side", "").upper(),
                outcome=row.get("outcome", ""),
                size=size,
                price=price,
                timestamp=timestamp,
                resolved=resolved,
                won=won,
            ))
        except (TypeError, ValueError):
            continue
    return records


# -- Scoring -------------------------------------------------------------------

def score_wallet(
    address: str,
    trades: list[TradeRecord],
    min_trades: int    = MIN_TRADES,
    min_win_rate: float = MIN_WIN_RATE,
    max_avg_trade: float = MAX_AVG_TRADE,
    min_avg_trade: float = MIN_AVG_TRADE,
    recency_days: int   = RECENCY_DAYS,
) -> WalletScore:
    """
    Compute composite score for a wallet.
    Returns a WalletScore with disqualified=True if hard filters fail.
    """
    resolved = [t for t in trades if t.resolved and t.won is not None]
    won      = [t for t in resolved if t.won]

    trade_count    = len(trades)
    resolved_count = len(resolved)
    win_rate       = len(won) / resolved_count if resolved_count > 0 else 0.0

    sizes         = [t.size for t in trades if t.size > 0]
    avg_trade     = sum(sizes) / len(sizes) if sizes else 0.0

    # Recency: fraction of trades within RECENCY_DAYS
    cutoff     = time.time() - recency_days * 86400
    recent     = [t for t in trades if t.timestamp >= cutoff]
    recency    = len(recent) / trade_count if trade_count > 0 else 0.0

    # Composite score
    composite = win_rate * math.log(resolved_count + 1) * (recency + 0.1)

    score = WalletScore(
        address=address,
        win_rate=win_rate,
        trade_count=trade_count,
        resolved_count=resolved_count,
        avg_trade_size=round(avg_trade, 2),
        recency_score=round(recency, 4),
        composite=round(composite, 4),
        trades=trades,
    )

    # Apply hard filters
    if resolved_count < min_trades:
        score.disqualified    = True
        score.disqualify_reason = f"too few resolved trades ({resolved_count} < {min_trades})"
    elif win_rate < min_win_rate:
        score.disqualified    = True
        score.disqualify_reason = f"win rate too low ({win_rate*100:.1f}% < {min_win_rate*100:.0f}%)"
    elif avg_trade > max_avg_trade:
        score.disqualified    = True
        score.disqualify_reason = f"avg trade too large (${avg_trade:,.0f} > ${max_avg_trade:,.0f})"
    elif avg_trade < min_avg_trade:
        score.disqualified    = True
        score.disqualify_reason = f"avg trade too small (${avg_trade:.2f} < ${min_avg_trade})"

    return score


# -- Public API ----------------------------------------------------------------

def evaluate_wallet(
    address: str,
    min_trades: int     = MIN_TRADES,
    min_win_rate: float = MIN_WIN_RATE,
    max_avg_trade: float = MAX_AVG_TRADE,
) -> WalletScore:
    """Fetch trades and score a single wallet. Main entry point."""
    raw    = fetch_trades(address)
    trades = parse_trades(raw)
    return score_wallet(
        address,
        trades,
        min_trades=min_trades,
        min_win_rate=min_win_rate,
        max_avg_trade=max_avg_trade,
    )


# -- Entry point ---------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Score a Polymarket wallet for copy trading suitability"
    )
    parser.add_argument("--address", required=True,
                        help="Polymarket proxy wallet address (0x...)")
    parser.add_argument("--min-trades", type=int, default=MIN_TRADES,
                        help=f"Minimum resolved trades [default: {MIN_TRADES}]")
    parser.add_argument("--min-win-rate", type=float, default=MIN_WIN_RATE,
                        help=f"Minimum win rate 0-1 [default: {MIN_WIN_RATE}]")
    parser.add_argument("--max-avg-trade", type=float, default=MAX_AVG_TRADE,
                        help=f"Max avg trade size USDC [default: {MAX_AVG_TRADE}]")
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
    print(f"  win_rate:      {score.win_rate*100:.1f}%")
    print(f"  trades:        {score.trade_count}  (resolved: {score.resolved_count})")
    print(f"  avg_trade:     ${score.avg_trade_size:,.2f}")
    print(f"  recency:       {score.recency_score*100:.0f}% trades in last 7d")
    print(f"  composite:     {score.composite:.4f}")
    if score.disqualified:
        print(f"  reason:        {score.disqualify_reason}")


if __name__ == "__main__":
    main()
