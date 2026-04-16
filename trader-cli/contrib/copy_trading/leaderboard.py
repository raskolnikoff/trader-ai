#!/usr/bin/env python3
"""
Polymarket leaderboard fetcher.

Fetches top wallets by PnL from the Polymarket Data API (no auth required)
and returns structured WalletEntry objects for downstream scoring.

Usage (standalone):
    python leaderboard.py
    python leaderboard.py --period week --limit 50
    python leaderboard.py --period month --limit 20 --json
"""

import argparse
import json
import urllib.request
from dataclasses import dataclass
from typing import Optional

# -- Constants -----------------------------------------------------------------

DATA_API_BASE = "https://data-api.polymarket.com"
REQUEST_TIMEOUT = 10

# Valid period values accepted by the Data API
VALID_PERIODS = ("1d", "7d", "30d", "all")
PERIOD_ALIASES = {"day": "1d", "week": "7d", "month": "30d"}

DEFAULT_PERIOD = "7d"
DEFAULT_LIMIT  = 100


# -- Data container ------------------------------------------------------------

@dataclass
class WalletEntry:
    proxy_address: str       # Polymarket proxy wallet (use this for tracking)
    username: str            # display name or truncated address
    pnl: float               # profit/loss in USDC
    volume: float            # total volume traded in USDC
    trades_count: int        # number of trades
    win_rate: Optional[float]  # fraction 0-1, if available from API
    period: str              # period this entry covers

    @property
    def avg_trade_size(self) -> float:
        if self.trades_count == 0:
            return 0.0
        return round(self.volume / self.trades_count, 2)

    def to_dict(self) -> dict:
        return {
            "proxy_address": self.proxy_address,
            "username":      self.username,
            "pnl":           self.pnl,
            "volume":        self.volume,
            "trades_count":  self.trades_count,
            "win_rate":      self.win_rate,
            "avg_trade_size": self.avg_trade_size,
            "period":        self.period,
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
        print(f"  [http] {url} -> {exc}")
        return None


# -- Leaderboard fetch ---------------------------------------------------------

def _resolve_period(period: str) -> str:
    """Normalize period aliases to API values."""
    return PERIOD_ALIASES.get(period.lower(), period.lower())


def fetch_leaderboard(
    period: str = DEFAULT_PERIOD,
    limit: int  = DEFAULT_LIMIT,
    order_by: str = "pnl",
) -> list[WalletEntry]:
    """
    Fetch top wallets from the Polymarket Data API leaderboard endpoint.

    Args:
        period:   One of: 1d, 7d, 30d, all  (also accepts day/week/month)
        limit:    Max number of wallets to return (server max ~500)
        order_by: "pnl" or "volume"

    Returns:
        List of WalletEntry sorted by PnL descending.
    """
    period = _resolve_period(period)
    url = (
        f"{DATA_API_BASE}/leaderboard"
        f"?period={period}&limit={limit}&order_by={order_by}"
    )
    data = _fetch_json(url)
    if data is None:
        return []

    # API may return a list directly or {"data": [...]}
    rows = data if isinstance(data, list) else data.get("data", [])

    entries = []
    for row in rows:
        if not isinstance(row, dict):
            continue

        proxy = row.get("proxyAddress") or row.get("proxy_address") or row.get("address", "")
        if not proxy:
            continue

        try:
            pnl    = float(row.get("pnl", 0) or 0)
            volume = float(row.get("volume", 0) or 0)
            trades = int(row.get("tradesCount") or row.get("trades_count") or 0)
        except (TypeError, ValueError):
            continue

        # win_rate is not always present in the leaderboard response
        wr_raw   = row.get("winRate") or row.get("win_rate")
        win_rate = float(wr_raw) if wr_raw is not None else None

        username = (
            row.get("name")
            or row.get("username")
            or proxy[:10] + "..."
        )

        entries.append(WalletEntry(
            proxy_address=proxy,
            username=username,
            pnl=round(pnl, 2),
            volume=round(volume, 2),
            trades_count=trades,
            win_rate=win_rate,
            period=period,
        ))

    # Sort by PnL descending (API usually returns sorted, but ensure it)
    entries.sort(key=lambda e: e.pnl, reverse=True)
    return entries


# -- Formatting ----------------------------------------------------------------

def format_table(entries: list[WalletEntry]) -> str:
    if not entries:
        return "  No entries found."

    lines = [
        f"{'#':<4} {'Username':<22} {'PnL':>10} {'Volume':>12} "
        f"{'Trades':>7} {'Avg':>8} {'WinRate':>8}",
        "-" * 76,
    ]
    for i, e in enumerate(entries, 1):
        wr = f"{e.win_rate*100:.1f}%" if e.win_rate is not None else "  --"
        lines.append(
            f"{i:<4} {e.username[:22]:<22} "
            f"${e.pnl:>9,.0f} "
            f"${e.volume:>11,.0f} "
            f"{e.trades_count:>7} "
            f"${e.avg_trade_size:>7,.0f} "
            f"{wr:>8}"
        )
    return "\n".join(lines)


# -- Entry point ---------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch Polymarket leaderboard (no auth required)"
    )
    parser.add_argument(
        "--period", default=DEFAULT_PERIOD,
        help="Time window: 1d, 7d, 30d, all  (or day/week/month)  [default: 7d]",
    )
    parser.add_argument(
        "--limit", type=int, default=50,
        help="Number of wallets to fetch [default: 50]",
    )
    parser.add_argument(
        "--order-by", dest="order_by", default="pnl",
        choices=["pnl", "volume"],
        help="Sort field [default: pnl]",
    )
    parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="Output machine-readable JSON",
    )
    args = parser.parse_args()

    entries = fetch_leaderboard(
        period=args.period,
        limit=args.limit,
        order_by=args.order_by,
    )

    if args.as_json:
        print(json.dumps([e.to_dict() for e in entries], indent=2))
    else:
        print(f"\n[leaderboard] period={args.period}  top {len(entries)} wallets\n")
        print(format_table(entries))


if __name__ == "__main__":
    main()
