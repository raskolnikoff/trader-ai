#!/usr/bin/env python3
"""
Polymarket market finder for TradingView symbols.

Maps a TradingView symbol (e.g. BTCUSDT, SPX, AAPL) to relevant
Polymarket prediction markets using the Gamma API.

Strategy: fetch top markets by volume and filter by keyword client-side.
The Gamma API keyword param is unreliable; client-side filtering is more accurate.

Usage (standalone):
    python polymarket_markets.py --symbol BTCUSDT
    python polymarket_markets.py --symbol SPX --limit 5
    python polymarket_markets.py --symbol BTCUSDT --json
"""

import argparse
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

# -- Constants -----------------------------------------------------------------

GAMMA_API_BASE  = "https://gamma-api.polymarket.com"
REQUEST_TIMEOUT = 10
DEFAULT_LIMIT   = 8
FETCH_BATCH     = 100   # fetch more and filter client-side

# Symbol -> keywords to match against market question (case-insensitive)
SYMBOL_KEYWORDS: dict[str, list[str]] = {
    "BTCUSDT":  ["bitcoin", "btc"],
    "ETHUSDT":  ["ethereum", "eth"],
    "SOLUSDT":  ["solana", "sol"],
    "SPX":      ["s&p 500", "spx", "s&p500"],
    "SPXUSD":   ["s&p 500", "spx", "s&p500"],
    "NAS100":   ["nasdaq", "qqq", "nas100"],
    "XAUUSD":   ["gold"],
    "EURUSD":   ["euro", "eur/usd", "eurusd"],
    "USDJPY":   ["yen", "usd/jpy", "usdjpy", "japan"],
    "DXY":      ["dollar index", "dxy"],
    "AAPL":     ["apple", "aapl"],
    "TSLA":     ["tesla", "tsla"],
    "NVDA":     ["nvidia", "nvda"],
    "MSFT":     ["microsoft", "msft"],
}

_STRIP_SUFFIXES = ["USDT", "USD", "PERP", "SPOT"]


# -- Data container ------------------------------------------------------------

@dataclass
class PolymarketMarket:
    condition_id: str
    event_slug: str
    question: str
    yes_price: Optional[float]
    no_price: Optional[float]
    volume: float
    active: bool

    @property
    def implied_probability(self) -> Optional[float]:
        if self.yes_price is not None:
            return round(self.yes_price * 100, 1)
        return None

    @property
    def link(self) -> str:
        return f"https://polymarket.com/event/{self.event_slug}"

    def to_dict(self) -> dict:
        return {
            "condition_id":        self.condition_id,
            "event_slug":          self.event_slug,
            "question":            self.question,
            "yes_price":           self.yes_price,
            "no_price":            self.no_price,
            "implied_probability": self.implied_probability,
            "volume":              self.volume,
            "active":              self.active,
            "link":                self.link,
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


# -- Symbol normalization ------------------------------------------------------

def normalize_symbol(symbol: str) -> str:
    if ":" in symbol:
        symbol = symbol.split(":", 1)[1]
    return symbol.upper().strip()


def get_keywords(symbol: str) -> list[str]:
    norm = normalize_symbol(symbol)
    if norm in SYMBOL_KEYWORDS:
        return SYMBOL_KEYWORDS[norm]
    for suffix in _STRIP_SUFFIXES:
        if norm.endswith(suffix):
            base = norm[: -len(suffix)]
            if base in SYMBOL_KEYWORDS:
                return SYMBOL_KEYWORDS[base]
            return [base.lower()]
    return [norm.lower()]


# -- Gamma API -----------------------------------------------------------------

def _safe_float(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_market(m: dict) -> Optional[PolymarketMarket]:
    if not isinstance(m, dict):
        return None

    cond_id    = m.get("conditionId") or m.get("condition_id", "")
    event_slug = m.get("eventSlug") or m.get("event_slug") or m.get("slug", "")
    question   = m.get("question", "")
    active     = bool(m.get("active", False))
    volume     = _safe_float(m.get("volume") or m.get("volumeNum")) or 0.0

    yes_price = no_price = None
    outcome_prices = m.get("outcomePrices")
    if isinstance(outcome_prices, list) and len(outcome_prices) >= 2:
        yes_price = _safe_float(outcome_prices[0])
        no_price  = _safe_float(outcome_prices[1])

    tokens = m.get("tokens")
    if yes_price is None and isinstance(tokens, list):
        prices = [_safe_float(t.get("price")) for t in tokens if isinstance(t, dict)]
        prices = [p for p in prices if p is not None]
        if len(prices) >= 2:
            yes_price, no_price = prices[0], prices[1]

    if not question or not cond_id:
        return None

    return PolymarketMarket(
        condition_id=cond_id,
        event_slug=event_slug,
        question=question,
        yes_price=yes_price,
        no_price=no_price,
        volume=volume,
        active=active,
    )


def fetch_top_markets(batch: int = FETCH_BATCH) -> list[dict]:
    """Fetch top active markets by volume from Gamma API."""
    url = (
        f"{GAMMA_API_BASE}/markets"
        f"?active=true&closed=false"
        f"&limit={batch}"
        f"&order=volume&ascending=false"
    )
    data = _fetch_json(url)
    if data is None:
        return []
    return data if isinstance(data, list) else data.get("data", [])


# -- Public API ----------------------------------------------------------------

def find_markets_for_symbol(
    symbol: str,
    limit: int = DEFAULT_LIMIT,
) -> list[PolymarketMarket]:
    """
    Find Polymarket markets relevant to a TradingView symbol.

    Fetches top markets by volume and filters client-side by keyword match
    against the market question. More reliable than Gamma API keyword param.
    """
    keywords = get_keywords(symbol)
    raw      = fetch_top_markets(batch=FETCH_BATCH)

    matched = []
    for row in raw:
        question = row.get("question", "").lower()
        if any(kw in question for kw in keywords):
            m = _parse_market(row)
            if m:
                matched.append(m)

    # Sort by volume descending
    matched.sort(key=lambda m: m.volume, reverse=True)
    return matched[:limit]


# -- Formatting ----------------------------------------------------------------

def format_markets(markets: list[PolymarketMarket], symbol: str) -> str:
    if not markets:
        return f"  No active Polymarket markets found for {symbol}."

    lines = [f"Polymarket markets for {symbol} (top {len(markets)} by volume):"]
    for i, m in enumerate(markets, 1):
        prob = f"{m.implied_probability}% YES" if m.implied_probability is not None else "??%"
        vol  = f"${m.volume:,.0f}" if m.volume else "n/a"
        q    = m.question[:72] + ("..." if len(m.question) > 72 else "")
        lines.append(f"  {i}. [{prob:>10}]  vol={vol:>10}  {q}")
        lines.append(f"     {m.link}")
    return "\n".join(lines)


# -- Entry point ---------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Find Polymarket markets relevant to a TradingView symbol"
    )
    parser.add_argument("--symbol", required=True,
                        help="TradingView symbol (e.g. BTCUSDT, SPX, AAPL)")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"Max markets to return [default: {DEFAULT_LIMIT}]")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="Output machine-readable JSON")
    args = parser.parse_args()

    markets = find_markets_for_symbol(args.symbol, limit=args.limit)

    if args.as_json:
        print(json.dumps([m.to_dict() for m in markets], indent=2))
    else:
        print(format_markets(markets, args.symbol))


if __name__ == "__main__":
    main()
