#!/usr/bin/env python3
"""
Polymarket top-trader finder.

The Data API has no /leaderboard endpoint. Instead, this module:
  1. Fetches active high-volume markets from the Gamma API
  2. For each market, fetches the top holders via Data API /holders
  3. Deduplicates and ranks wallets by total position value

This gives us a list of large, active traders worth watching -- the
closest equivalent to a "leaderboard" available without auth.

Alternatively, pass known wallet addresses directly to wallet_scorer.py
and monitor.py via --add 0x... if you already have targets.

Usage (standalone):
    python leaderboard.py
    python leaderboard.py --markets 10 --holders 5
    python leaderboard.py --json
"""

import argparse
import json
import urllib.request
from dataclasses import dataclass
from typing import Optional

# -- Constants -----------------------------------------------------------------

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
DATA_API_BASE  = "https://data-api.polymarket.com"
REQUEST_TIMEOUT = 10

DEFAULT_MARKET_LIMIT  = 20   # top active markets to scan
DEFAULT_HOLDERS_LIMIT = 10   # top holders per market
DEFAULT_TOP_N         = 30   # max wallets to return after dedup


# -- Data container ------------------------------------------------------------

@dataclass
class WalletEntry:
    proxy_address: str
    username: str
    total_position: float   # sum of position sizes across all found markets
    markets_count: int      # number of distinct markets this wallet appears in
    source: str             # "holders" - how this wallet was discovered

    def to_dict(self) -> dict:
        return {
            "proxy_address":   self.proxy_address,
            "username":        self.username,
            "total_position":  round(self.total_position, 2),
            "markets_count":   self.markets_count,
            "source":          self.source,
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


# -- Market fetcher ------------------------------------------------------------

def fetch_top_markets(limit: int = DEFAULT_MARKET_LIMIT) -> list[dict]:
    """
    Fetch active markets sorted by volume descending from the Gamma API.
    Returns raw market dicts with at minimum: conditionId, question, volume.
    """
    url = (
        f"{GAMMA_API_BASE}/markets"
        f"?active=true&closed=false&limit={limit}&order=volume&ascending=false"
    )
    data = _fetch_json(url)
    if data is None:
        return []
    return data if isinstance(data, list) else data.get("data", [])


# -- Holder fetcher ------------------------------------------------------------

def fetch_holders(condition_id: str, limit: int = DEFAULT_HOLDERS_LIMIT) -> list[dict]:
    """
    Fetch top holders for a market from the Data API /holders endpoint.
    Returns list of holder dicts: proxyWallet, name, amount.
    """
    url = f"{DATA_API_BASE}/holders?market={condition_id}&limit={limit}"
    data = _fetch_json(url)
    if data is None:
        return []

    # Response is a list of token groups: [{token: ..., holders: [...]}, ...]
    holders = []
    rows = data if isinstance(data, list) else data.get("data", [])
    for token_group in rows:
        if not isinstance(token_group, dict):
            continue
        for h in token_group.get("holders", []):
            if isinstance(h, dict):
                holders.append(h)
    return holders


# -- Top-trader discovery ------------------------------------------------------

def fetch_top_traders(
    market_limit: int  = DEFAULT_MARKET_LIMIT,
    holders_limit: int = DEFAULT_HOLDERS_LIMIT,
    top_n: int         = DEFAULT_TOP_N,
) -> list[WalletEntry]:
    """
    Scan top markets, collect holders, aggregate into ranked wallet list.
    Wallets appearing in multiple markets are ranked higher.
    """
    markets = fetch_top_markets(limit=market_limit)
    if not markets:
        print("  [leaderboard] No markets returned from Gamma API.")
        return []

    # Accumulate: proxy_address -> {total_position, markets_count, username}
    wallet_data: dict[str, dict] = {}

    for market in markets:
        cond_id = market.get("conditionId") or market.get("condition_id", "")
        if not cond_id:
            continue

        holders = fetch_holders(cond_id, limit=holders_limit)
        for h in holders:
            proxy = h.get("proxyWallet") or h.get("proxy_wallet", "")
            if not proxy:
                continue
            amount = float(h.get("amount", 0) or 0)
            name   = h.get("name") or h.get("pseudonym") or proxy[:10] + "..."

            if proxy not in wallet_data:
                wallet_data[proxy] = {
                    "username":        name,
                    "total_position":  0.0,
                    "markets_count":   0,
                }
            wallet_data[proxy]["total_position"] += amount
            wallet_data[proxy]["markets_count"]  += 1

    # Build WalletEntry list, sort by markets_count then total_position
    entries = [
        WalletEntry(
            proxy_address=addr,
            username=data["username"],
            total_position=round(data["total_position"], 2),
            markets_count=data["markets_count"],
            source="holders",
        )
        for addr, data in wallet_data.items()
    ]
    entries.sort(key=lambda e: (e.markets_count, e.total_position), reverse=True)
    return entries[:top_n]


# -- Formatting ----------------------------------------------------------------

def format_table(entries: list[WalletEntry]) -> str:
    if not entries:
        return "  No wallets found."

    lines = [
        f"{'#':<4} {'Username':<22} {'Markets':>8} {'Total Position':>16}",
        "-" * 55,
    ]
    for i, e in enumerate(entries, 1):
        lines.append(
            f"{i:<4} {e.username[:22]:<22} "
            f"{e.markets_count:>8} "
            f"${e.total_position:>15,.0f}"
        )
    return "\n".join(lines)


# -- Entry point ---------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Find top Polymarket traders via market holders (no auth required)"
    )
    parser.add_argument(
        "--markets", type=int, default=DEFAULT_MARKET_LIMIT,
        help=f"Number of top markets to scan [default: {DEFAULT_MARKET_LIMIT}]",
    )
    parser.add_argument(
        "--holders", type=int, default=DEFAULT_HOLDERS_LIMIT,
        help=f"Top holders to fetch per market [default: {DEFAULT_HOLDERS_LIMIT}]",
    )
    parser.add_argument(
        "--top", type=int, default=DEFAULT_TOP_N,
        help=f"Max wallets to display [default: {DEFAULT_TOP_N}]",
    )
    parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="Output machine-readable JSON",
    )
    args = parser.parse_args()

    print(f"[leaderboard] Scanning top {args.markets} markets for holders...")
    entries = fetch_top_traders(
        market_limit=args.markets,
        holders_limit=args.holders,
        top_n=args.top,
    )

    if args.as_json:
        print(json.dumps([e.to_dict() for e in entries], indent=2))
    else:
        print(f"\n[leaderboard] Top {len(entries)} traders found\n")
        print(format_table(entries))
        print(f"\nTip: copy a proxy_address and run:")
        print(f"     python wallet_scorer.py --address 0x...")


if __name__ == "__main__":
    main()
