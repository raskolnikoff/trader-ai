"""
Polymarket Gamma API client for short-term BTC markets.

The CLOB sampling endpoint used by maker_bot returns primarily long-duration
featured markets. For weekly/monthly BTC prediction markets (which have
smaller liquidity but shorter resolution windows and tighter tradable edges),
we use the public Gamma API instead.

The Gamma API is read-only, requires no authentication, and supports rich
filtering by end_date, volume, liquidity, and tags.

Reference: https://gamma-api.polymarket.com/markets

Filter parameters used here:
  - active=true         : market is currently trading
  - closed=false        : market has not resolved
  - archived=false      : market has not been archived
  - liquidity_min=N     : minimum on-book liquidity in USD
  - end_date_min/max    : resolution date window

Rate limits:
  - /markets: 300 req / 10s (well under our 30 req/hour cadence)
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

logger = logging.getLogger("gamma_markets")

GAMMA_URL: str = "https://gamma-api.polymarket.com"
GAMMA_MARKETS_ENDPOINT: str = f"{GAMMA_URL}/markets"
HTTP_TIMEOUT_SEC: int = 8

# Default minimum liquidity threshold ($USD). Raise to $50,000 for AdiiX-style
# latency arb. $1,000 is permissive for dry-run market exploration.
DEFAULT_MIN_LIQUIDITY_USD: float = float(
    os.environ.get("MIN_LIQUIDITY_USD", "1000")
)

# Horizon -> (end_date_min_delta, end_date_max_delta) from now
Horizon = Literal["weekly", "monthly", "all"]
HORIZON_WINDOWS: dict[str, tuple[Optional[timedelta], Optional[timedelta]]] = {
    "weekly":  (timedelta(hours=0),  timedelta(days=7)),
    "monthly": (timedelta(days=7),   timedelta(days=31)),
    "all":     (None,                None),
}


@dataclass
class GammaMarket:
    """
    A Polymarket market fetched from the Gamma API.

    Matches the shape consumed by maker_bot.ClobMarket, but adds Gamma-specific
    fields (liquidity, volume, end_date) for filtering.
    """
    condition_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    yes_price: float
    no_price: float
    accepting_orders: bool
    minimum_order_size: float
    liquidity_usd: float
    volume_24h_usd: float
    end_date: Optional[datetime]

    @property
    def hours_to_resolution(self) -> Optional[float]:
        """Hours until market resolves. None if end_date unknown."""
        if self.end_date is None:
            return None
        delta = self.end_date - datetime.now(timezone.utc)
        return delta.total_seconds() / 3600


# -- Internal helpers ----------------------------------------------------------

def _fetch_json(url: str) -> Optional[list | dict]:
    """Fetch JSON from a URL. Returns None on any error."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; trader-ai-gamma/1.0)",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.warning("Gamma HTTP fetch failed [%s]: %s", url[:80], exc)
        return None


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 datetime string, returning None on failure."""
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _parse_stringified_json_list(value) -> list:
    """Gamma returns outcomePrices and clobTokenIds as stringified lists."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []
    return []


def _build_query_string(
    horizon: Horizon,
    min_liquidity_usd: float,
    limit: int,
    offset: int,
) -> str:
    """Build a Gamma /markets query string with the desired filters."""
    params: dict[str, str] = {
        "active":   "true",
        "closed":   "false",
        "archived": "false",
        "limit":    str(limit),
        "offset":   str(offset),
        # Sort by end_date ascending so nearest-resolution markets come first.
        # This makes pagination stop early once we're past the horizon window.
        "order":       "endDate",
        "ascending":   "true",
    }
    if min_liquidity_usd > 0:
        params["liquidity_num_min"] = str(min_liquidity_usd)

    start_delta, end_delta = HORIZON_WINDOWS[horizon]
    now = datetime.now(timezone.utc)
    if start_delta is not None:
        params["end_date_min"] = (now + start_delta).strftime("%Y-%m-%dT%H:%M:%SZ")
    if end_delta is not None:
        params["end_date_max"] = (now + end_delta).strftime("%Y-%m-%dT%H:%M:%SZ")

    return urllib.parse.urlencode(params)


def _is_btc_market(question: str, slug: str = "") -> bool:
    """Return True if the market question/slug references Bitcoin price."""
    text = (question + " " + slug).lower()
    if "bitcoin" not in text and "btc" not in text:
        return False
    # Exclude non-price BTC markets (ETF flows, exchange events, etc.)
    price_keywords = [
        "reach", "hit", "dip", "above", "below", "between", "exceed",
        "up or down", "up/down", "close", "price"
    ]
    return any(kw in text for kw in price_keywords)


def _parse_gamma_market(raw: dict) -> Optional[GammaMarket]:
    """
    Convert a raw Gamma /markets item into a GammaMarket.

    Returns None if the market is missing required fields or has invalid prices.
    """
    try:
        question     = raw.get("question", "")
        slug         = raw.get("slug", "")
        condition_id = raw.get("conditionId", "") or raw.get("condition_id", "")

        outcome_prices = _parse_stringified_json_list(raw.get("outcomePrices"))
        token_ids      = _parse_stringified_json_list(raw.get("clobTokenIds"))

        if len(outcome_prices) < 2 or len(token_ids) < 2:
            return None

        try:
            yes_price = float(outcome_prices[0])
            no_price  = float(outcome_prices[1])
        except (TypeError, ValueError):
            return None

        if yes_price <= 0 or no_price <= 0:
            return None

        liquidity = float(raw.get("liquidityNum", raw.get("liquidity", 0)) or 0)
        volume_24 = float(raw.get("volume24hr",  raw.get("volume_24hr", 0)) or 0)
        end_date  = _parse_iso_datetime(raw.get("endDate") or raw.get("end_date"))

        # Gamma markets default to min order $1, but CLOB may enforce higher.
        # Use maker_bot's MIN_ORDER_SIZE_USD at call site; default to 1.0 here.
        min_order = float(raw.get("orderMinSize", 1.0) or 1.0)

        accepting = bool(
            raw.get("enableOrderBook", True)
            and raw.get("acceptingOrders", True)
            and not raw.get("closed", False)
            and not raw.get("archived", False)
        )

        return GammaMarket(
            condition_id=condition_id,
            question=question,
            yes_token_id=str(token_ids[0]),
            no_token_id=str(token_ids[1]),
            yes_price=yes_price,
            no_price=no_price,
            accepting_orders=accepting,
            minimum_order_size=min_order,
            liquidity_usd=liquidity,
            volume_24h_usd=volume_24,
            end_date=end_date,
        )
    except Exception as exc:
        logger.debug("Failed to parse Gamma market: %s", exc)
        return None


# -- Public API ----------------------------------------------------------------

def fetch_btc_gamma_markets(
    horizon: Horizon = "all",
    min_liquidity_usd: float = DEFAULT_MIN_LIQUIDITY_USD,
    page_limit: int = 100,
    max_pages: int = 5,
) -> list[GammaMarket]:
    """
    Fetch active BTC price markets from the Polymarket Gamma API.

    Args:
        horizon: 'weekly' (next 7 days), 'monthly' (7-31 days), or 'all'.
        min_liquidity_usd: Minimum on-book liquidity in USD.
        page_limit: Items per API page (max 100 per Gamma docs).
        max_pages: Safety cap on pagination.

    Returns:
        List of GammaMarket that pass BTC-price and liquidity filters,
        sorted by hours_to_resolution ascending (soonest-resolving first).
    """
    if horizon not in HORIZON_WINDOWS:
        raise ValueError(f"Invalid horizon: {horizon!r}. Use weekly/monthly/all.")

    collected: list[GammaMarket] = []
    offset: int = 0

    for _ in range(max_pages):
        qs  = _build_query_string(horizon, min_liquidity_usd, page_limit, offset)
        url = f"{GAMMA_MARKETS_ENDPOINT}?{qs}"
        raw = _fetch_json(url)

        if not isinstance(raw, list) or len(raw) == 0:
            break

        for item in raw:
            market = _parse_gamma_market(item)
            if market is None:
                continue
            if not market.accepting_orders:
                continue
            if market.liquidity_usd < min_liquidity_usd:
                continue
            if not _is_btc_market(market.question, item.get("slug", "")):
                continue
            collected.append(market)

        if len(raw) < page_limit:
            break
        offset += page_limit

    # Sort: soonest-resolving first; None end_dates last
    collected.sort(
        key=lambda m: (
            m.hours_to_resolution if m.hours_to_resolution is not None else float("inf")
        )
    )

    logger.info(
        "Gamma: fetched %d BTC markets (horizon=%s, min_liquidity=$%.0f)",
        len(collected), horizon, min_liquidity_usd,
    )
    return collected


if __name__ == "__main__":
    # Smoke test: print all BTC markets for each horizon.
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for h in ("weekly", "monthly", "all"):
        print(f"\n=== {h.upper()} ===")
        markets = fetch_btc_gamma_markets(horizon=h)
        for m in markets[:10]:
            hrs = m.hours_to_resolution
            print(
                f"  {m.question[:70]:70s} "
                f"Yes={m.yes_price:.3f} "
                f"liq=${m.liquidity_usd:>8.0f} "
                f"24h=${m.volume_24h_usd:>8.0f} "
                f"in={hrs:6.1f}h" if hrs is not None else "in=?h"
            )
