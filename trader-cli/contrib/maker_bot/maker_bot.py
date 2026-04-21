#!/usr/bin/env python3
"""
Polymarket Maker Bot.

Strategy:
  1. Fetch current BTC price and momentum from Binance (5m candles).
  2. Find the nearest active BTC Up/Down 5-minute market on Polymarket.
  3. Compare Binance momentum with Polymarket odds to detect mispricing.
  4. Place a limit (maker) order on the mispriced side if edge >= threshold.
  5. Repeat on a configurable interval.

Maker orders pay zero fees and earn a rebate on Polymarket CLOB.
This bot NEVER places taker orders.

Safety:
  - --dry-run is the DEFAULT. Pass --live to place real orders.
  - Max position size is capped by MAX_ORDER_SIZE_USD.
  - Minimum edge required before placing is MIN_EDGE_THRESHOLD.

Usage:
    # Preview only (default dry-run)
    python maker_bot.py

    # Dry-run with verbose signal output
    python maker_bot.py --verbose

    # Live trading (requires POLY_* env vars)
    python maker_bot.py --live

    # Custom interval and size
    python maker_bot.py --live --interval 60 --max-size 5.0

Requirements:
    pip install py-clob-client python-dotenv websockets
"""

import argparse
import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Load .env from repo root
_ROOT = Path(__file__).parent.parent.parent.parent
_dotenv_path = _ROOT / ".env"
if _dotenv_path.exists():
    from dotenv import load_dotenv
    load_dotenv(_dotenv_path)

# Add trader-cli to path
_CLI = Path(__file__).parent.parent.parent
if str(_CLI) not in sys.path:
    sys.path.insert(0, str(_CLI))

from contrib.tv_polymarket.polymarket_markets import (
    find_markets_for_symbol,
    PolymarketMarket,
)

# -- Logging setup -------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("maker_bot")

# -- Constants -----------------------------------------------------------------

# Binance REST endpoints
BINANCE_PRICE_URL: str = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
BINANCE_KLINE_URL: str = (
    "https://api.binance.com/api/v3/klines"
    "?symbol=BTCUSDT&interval=5m&limit=3"
)

# Polymarket CLOB endpoint
CLOB_HOST: str = "https://clob.polymarket.com"

# Bot parameters
DEFAULT_INTERVAL_SEC: int   = 30    # how often to check for opportunities
MAX_ORDER_SIZE_USD: float   = 5.0   # maximum single order size in USD
MIN_ORDER_SIZE_USD: float   = 1.0   # minimum order size (Polymarket minimum)
MIN_EDGE_THRESHOLD: float   = 0.04  # minimum mispricing to place order (4%)
MAKER_PRICE_OFFSET: float   = 0.02  # place maker 2 cents below fair value
HTTP_TIMEOUT_SEC: int       = 8

# Polymarket signature type for API-key auth (non-browser wallet)
SIGNATURE_TYPE: int = 0

# -- Dataclasses ---------------------------------------------------------------

@dataclass
class BinanceSignal:
    """Current BTC price snapshot from Binance."""
    price: float
    change_5m: Optional[float]   # % change last 5m candle
    direction: str               # "UP", "DOWN", or "FLAT"
    confidence: float            # 0.0 – 1.0 (based on magnitude)


@dataclass
class MarketOpportunity:
    """A detected mispricing between Binance signal and Polymarket odds."""
    market: PolymarketMarket
    binance_signal: BinanceSignal
    target_side: str             # "YES" or "NO"
    fair_value: float            # our estimated fair probability
    market_price: float          # current Polymarket price for target_side
    edge: float                  # fair_value - market_price (positive = underpriced)
    suggested_price: float       # maker limit price to post
    suggested_size: float        # order size in USD


@dataclass
class OrderResult:
    """Result of an order placement attempt."""
    success: bool
    order_id: Optional[str]
    dry_run: bool
    opportunity: MarketOpportunity
    error: Optional[str] = None


# -- HTTP helpers --------------------------------------------------------------

def _fetch_json_sync(url: str) -> Optional[dict | list]:
    """Fetch JSON from a URL synchronously. Returns None on any error."""
    import urllib.request
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; trader-ai-maker/1.0)",
            "Accept":     "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            import json
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.warning("HTTP fetch failed for %s: %s", url, exc)
        return None


# -- Binance signal ------------------------------------------------------------

def fetch_binance_signal() -> Optional[BinanceSignal]:
    """
    Fetch current BTC price and 5m momentum from Binance REST API.

    Returns:
        BinanceSignal with direction and confidence, or None if unreachable.
    """
    price_data = _fetch_json_sync(BINANCE_PRICE_URL)
    kline_data = _fetch_json_sync(BINANCE_KLINE_URL)

    if price_data is None:
        logger.error("Binance price endpoint unreachable")
        return None

    try:
        price = float(price_data["price"])
    except (KeyError, TypeError, ValueError) as exc:
        logger.error("Failed to parse Binance price: %s", exc)
        return None

    change_5m: Optional[float] = None
    if isinstance(kline_data, list) and len(kline_data) >= 2:
        try:
            # kline format: [open_time, open, high, low, close, volume, ...]
            latest_close = float(kline_data[-1][4])
            prev_close   = float(kline_data[-2][4])
            if prev_close > 0:
                change_5m = (latest_close - prev_close) / prev_close * 100
        except (IndexError, TypeError, ValueError) as exc:
            logger.warning("Failed to compute 5m change: %s", exc)

    direction, confidence = _classify_direction(change_5m)

    return BinanceSignal(
        price=price,
        change_5m=change_5m,
        direction=direction,
        confidence=confidence,
    )


def _classify_direction(change_pct: Optional[float]) -> tuple[str, float]:
    """
    Classify price movement direction and confidence.

    Args:
        change_pct: Percentage price change. None treated as flat.

    Returns:
        (direction, confidence) where direction is "UP"/"DOWN"/"FLAT"
        and confidence is 0.0–1.0 scaled by magnitude.
    """
    if change_pct is None:
        return "FLAT", 0.0

    abs_change = abs(change_pct)

    if abs_change < 0.05:
        return "FLAT", 0.0
    elif abs_change < 0.10:
        confidence = 0.3
    elif abs_change < 0.20:
        confidence = 0.6
    else:
        confidence = min(1.0, abs_change / 0.30)

    direction = "UP" if change_pct > 0 else "DOWN"
    return direction, confidence


# -- Opportunity detection -----------------------------------------------------

def find_btc_5m_markets(limit: int = 5) -> list[PolymarketMarket]:
    """
    Find active Polymarket BTC Up/Down 5-minute markets.

    Returns:
        List of matching markets sorted by volume descending.
    """
    markets = find_markets_for_symbol("BTCUSDT", limit=limit * 3)
    # Filter to 5-minute directional markets only
    filtered = [
        m for m in markets
        if _is_5m_btc_market(m.question)
    ]
    return filtered[:limit]


def _is_5m_btc_market(question: str) -> bool:
    """
    Check if a market question describes a BTC 5-minute Up/Down market.

    Args:
        question: Market question string.

    Returns:
        True if this looks like a 5-minute BTC directional market.
    """
    q = question.lower()
    has_btc = "bitcoin" in q or "btc" in q
    has_direction = "up or down" in q or "up/down" in q
    has_5m = "5m" in q or "5-min" in q or "5 min" in q
    return has_btc and (has_direction or has_5m)


def compute_fair_value(signal: BinanceSignal) -> tuple[float, float]:
    """
    Estimate fair probability for YES (price goes up) and NO (price goes down).

    Uses Binance momentum as the primary signal. Base is 50/50; adjusted
    by direction and confidence.

    Args:
        signal: BinanceSignal from Binance.

    Returns:
        (yes_fair_value, no_fair_value) both in range 0.0–1.0, sum to 1.0.
    """
    base = 0.50
    adjustment = signal.confidence * 0.20   # max 20% shift from momentum

    if signal.direction == "UP":
        yes_fair = base + adjustment
    elif signal.direction == "DOWN":
        yes_fair = base - adjustment
    else:
        yes_fair = base

    yes_fair = max(0.05, min(0.95, yes_fair))
    no_fair  = 1.0 - yes_fair
    return yes_fair, no_fair


def detect_opportunity(
    market: PolymarketMarket,
    signal: BinanceSignal,
    min_edge: float = MIN_EDGE_THRESHOLD,
    max_size: float = MAX_ORDER_SIZE_USD,
) -> Optional[MarketOpportunity]:
    """
    Detect if there is a maker opportunity in a given market.

    Compares Binance-derived fair value against current Polymarket odds.
    Returns an opportunity only if edge >= min_edge.

    Args:
        market: The Polymarket market to evaluate.
        signal: Current Binance price signal.
        min_edge: Minimum required edge to consider the opportunity.
        max_size: Maximum order size in USD.

    Returns:
        MarketOpportunity if edge found, None otherwise.
    """
    if market.yes_price is None or market.no_price is None:
        logger.debug("Skipping market with missing odds: %s", market.question[:50])
        return None

    yes_fair, no_fair = compute_fair_value(signal)

    # Check YES side edge
    yes_edge = yes_fair - market.yes_price
    # Check NO side edge
    no_edge  = no_fair  - market.no_price

    if yes_edge >= min_edge:
        target_side    = "YES"
        fair_value     = yes_fair
        market_price   = market.yes_price
        edge           = yes_edge
    elif no_edge >= min_edge:
        target_side    = "NO"
        fair_value     = no_fair
        market_price   = market.no_price
        edge           = no_edge
    else:
        logger.debug(
            "No edge found: YES edge=%.3f, NO edge=%.3f (min=%.3f)",
            yes_edge, no_edge, min_edge,
        )
        return None

    # Place maker order slightly below fair value to ensure it rests as maker
    suggested_price = round(max(0.01, fair_value - MAKER_PRICE_OFFSET), 2)
    suggested_size  = min(max_size, max(MIN_ORDER_SIZE_USD, max_size))

    return MarketOpportunity(
        market=market,
        binance_signal=signal,
        target_side=target_side,
        fair_value=fair_value,
        market_price=market_price,
        edge=edge,
        suggested_price=suggested_price,
        suggested_size=suggested_size,
    )


# -- Order placement -----------------------------------------------------------

def _load_clob_credentials() -> dict:
    """
    Load Polymarket CLOB credentials from environment variables.

    Returns:
        Dict with api_key, secret, passphrase, wallet_address.

    Raises:
        EnvironmentError: If any required credential is missing.
    """
    required = {
        "api_key":        "POLY_API_KEY",
        "secret":         "POLY_SECRET",
        "passphrase":     "POLY_PASSPHRASE",
        "wallet_address": "POLY_WALLET_ADDRESS",
    }
    creds = {}
    missing = []
    for field, env_var in required.items():
        value = os.environ.get(env_var)
        if not value:
            missing.append(env_var)
        else:
            creds[field] = value

    if missing:
        raise EnvironmentError(
            f"Missing required environment variables: {', '.join(missing)}\n"
            f"See .env.example for setup instructions."
        )
    return creds


def place_maker_order(
    opportunity: MarketOpportunity,
    dry_run: bool = True,
) -> OrderResult:
    """
    Place a maker limit order on Polymarket CLOB.

    Args:
        opportunity: The detected market opportunity.
        dry_run: If True, log the order details but do not submit.

    Returns:
        OrderResult indicating success/failure and order ID.
    """
    if dry_run:
        logger.info(
            "[DRY RUN] Would place MAKER order:\n"
            "  Market   : %s\n"
            "  Side     : %s\n"
            "  Price    : %.2f\n"
            "  Size     : $%.2f\n"
            "  Edge     : %.3f\n"
            "  Fair val : %.3f  Market: %.3f\n"
            "  Link     : %s",
            opportunity.market.question[:60],
            opportunity.target_side,
            opportunity.suggested_price,
            opportunity.suggested_size,
            opportunity.edge,
            opportunity.fair_value,
            opportunity.market_price,
            opportunity.market.link,
        )
        return OrderResult(
            success=True,
            order_id=None,
            dry_run=True,
            opportunity=opportunity,
        )

    # Live order placement via py-clob-client
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY
    except ImportError:
        msg = (
            "py-clob-client is not installed.\n"
            "Run: pip install py-clob-client"
        )
        logger.error(msg)
        return OrderResult(
            success=False,
            order_id=None,
            dry_run=False,
            opportunity=opportunity,
            error=msg,
        )

    try:
        creds = _load_clob_credentials()
    except EnvironmentError as exc:
        logger.error("Credential error: %s", exc)
        return OrderResult(
            success=False,
            order_id=None,
            dry_run=False,
            opportunity=opportunity,
            error=str(exc),
        )

    try:
        client = ClobClient(
            host=CLOB_HOST,
            key=creds["api_key"],
            secret=creds["secret"],
            passphrase=creds["passphrase"],
            signature_type=SIGNATURE_TYPE,
        )

        order_args = OrderArgs(
            token_id=opportunity.market.condition_id,
            price=opportunity.suggested_price,
            size=opportunity.suggested_size,
            side=BUY,
        )

        response = client.create_and_post_order(order_args)
        order_id = response.get("orderID") or response.get("id")

        logger.info(
            "Order placed: %s | %s @ %.2f | size=$%.2f | id=%s",
            opportunity.target_side,
            opportunity.market.question[:40],
            opportunity.suggested_price,
            opportunity.suggested_size,
            order_id,
        )

        return OrderResult(
            success=True,
            order_id=order_id,
            dry_run=False,
            opportunity=opportunity,
        )

    except Exception as exc:
        logger.error("Order placement failed: %s", exc)
        return OrderResult(
            success=False,
            order_id=None,
            dry_run=False,
            opportunity=opportunity,
            error=str(exc),
        )


# -- Main bot loop -------------------------------------------------------------

def run_once(
    dry_run: bool = True,
    max_size: float = MAX_ORDER_SIZE_USD,
    min_edge: float = MIN_EDGE_THRESHOLD,
    verbose: bool = False,
) -> list[OrderResult]:
    """
    Execute one full cycle: fetch signal -> find markets -> detect edge -> place orders.

    Args:
        dry_run: If True, do not place real orders.
        max_size: Maximum order size in USD.
        min_edge: Minimum edge threshold to place an order.
        verbose: If True, log detailed signal information.

    Returns:
        List of OrderResult for each order attempted this cycle.
    """
    results: list[OrderResult] = []

    # Step 1: Fetch Binance signal
    signal = fetch_binance_signal()
    if signal is None:
        logger.warning("Skipping cycle: Binance signal unavailable")
        return results

    if verbose:
        logger.info(
            "Binance: BTC=$%.2f  5m=%s%.3f%%  direction=%s  confidence=%.2f",
            signal.price,
            "+" if (signal.change_5m or 0) >= 0 else "",
            signal.change_5m or 0.0,
            signal.direction,
            signal.confidence,
        )

    # Step 2: Find active BTC 5m markets
    markets = find_btc_5m_markets(limit=5)
    if not markets:
        logger.info("No active BTC 5m markets found on Polymarket")
        return results

    logger.info("Found %d BTC 5m market(s)", len(markets))

    # Step 3: Detect opportunities
    for market in markets:
        opp = detect_opportunity(market, signal, min_edge=min_edge, max_size=max_size)
        if opp is None:
            continue

        logger.info(
            "Edge detected: %s @ %.3f edge (market=%.3f, fair=%.3f)",
            opp.target_side,
            opp.edge,
            opp.market_price,
            opp.fair_value,
        )

        # Step 4: Place order
        result = place_maker_order(opp, dry_run=dry_run)
        results.append(result)

    if not results:
        logger.info("No opportunities found this cycle (edge < %.3f)", min_edge)

    return results


def run_loop(
    interval: int   = DEFAULT_INTERVAL_SEC,
    dry_run: bool   = True,
    max_size: float = MAX_ORDER_SIZE_USD,
    min_edge: float = MIN_EDGE_THRESHOLD,
    verbose: bool   = False,
) -> None:
    """
    Run the maker bot continuously on a fixed interval.

    Args:
        interval: Seconds between cycles.
        dry_run: If True, do not place real orders.
        max_size: Maximum order size per trade in USD.
        min_edge: Minimum required edge to place an order.
        verbose: If True, print detailed signal information each cycle.
    """
    mode = "DRY RUN" if dry_run else "LIVE"
    logger.info(
        "Maker bot starting | mode=%s | interval=%ds | max_size=$%.2f | min_edge=%.3f",
        mode, interval, max_size, min_edge,
    )

    cycle = 0
    while True:
        cycle += 1
        logger.info("--- Cycle %d ---", cycle)
        try:
            run_once(
                dry_run=dry_run,
                max_size=max_size,
                min_edge=min_edge,
                verbose=verbose,
            )
        except KeyboardInterrupt:
            logger.info("Maker bot stopped by user")
            break
        except Exception as exc:
            # Never crash the loop on unexpected errors -- log and continue
            logger.error("Unexpected error in cycle %d: %s", cycle, exc)

        logger.info("Sleeping %ds...", interval)
        time.sleep(interval)


# -- Entry point ---------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Polymarket Maker Bot\n"
            "Default mode is --dry-run. Pass --live for real orders.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--live",
        dest="dry_run",
        action="store_false",
        default=True,
        help="Place real orders (default: dry-run only)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_SEC,
        help=f"Seconds between cycles (default: {DEFAULT_INTERVAL_SEC})",
    )
    parser.add_argument(
        "--max-size",
        type=float,
        default=MAX_ORDER_SIZE_USD,
        help=f"Max order size in USD (default: {MAX_ORDER_SIZE_USD})",
    )
    parser.add_argument(
        "--min-edge",
        type=float,
        default=MIN_EDGE_THRESHOLD,
        help=f"Min edge to place order (default: {MIN_EDGE_THRESHOLD})",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one cycle and exit (useful for testing)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed signal output each cycle",
    )
    args = parser.parse_args()

    if args.once:
        results = run_once(
            dry_run=args.dry_run,
            max_size=args.max_size,
            min_edge=args.min_edge,
            verbose=args.verbose,
        )
        logger.info("Cycle complete: %d order(s) attempted", len(results))
    else:
        run_loop(
            interval=args.interval,
            dry_run=args.dry_run,
            max_size=args.max_size,
            min_edge=args.min_edge,
            verbose=args.verbose,
        )


if __name__ == "__main__":
    main()
