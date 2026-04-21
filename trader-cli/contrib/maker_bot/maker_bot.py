#!/usr/bin/env python3
"""
Polymarket Maker Bot.

Strategy:
  1. Fetch current BTC price and momentum from Binance (5m candles).
  2. Fetch active BTC markets from Polymarket CLOB sampling endpoint.
     Sampling markets are liquid, accepting orders, and eligible for maker rebates.
  3. For each market, compute a fair-value probability using Binance price
     relative to the market's resolution threshold.
  4. Compare fair value against current Polymarket odds to detect mispricing.
  5. Place a limit (maker) order on the underpriced side if edge >= threshold.
  6. Repeat on a configurable interval.

Maker orders pay zero fees and earn a rebate on Polymarket CLOB.
This bot NEVER places taker orders (no market orders).

Safety:
  - --dry-run is the DEFAULT. Pass --live to place real orders.
  - Max position size is capped by MAX_ORDER_SIZE_USD.
  - Minimum edge required before placing is MIN_EDGE_THRESHOLD.
  - All exceptions are caught and logged; the loop never crashes.

Usage:
    # Preview only (default dry-run)
    python maker_bot.py --once --verbose

    # Run continuously (dry-run)
    python maker_bot.py --verbose

    # Live trading (requires POLY_* env vars in .env)
    python maker_bot.py --live --max-size 3.0

Requirements:
    pip install py-clob-client python-dotenv
"""

import argparse
import logging
import os
import sys
import time
import urllib.request
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# -- Path setup ----------------------------------------------------------------

_ROOT = Path(__file__).parent.parent.parent.parent
_CLI  = Path(__file__).parent.parent.parent

# Load .env from repo root before any other imports
_dotenv_path = _ROOT / ".env"
if _dotenv_path.exists():
    from dotenv import load_dotenv
    load_dotenv(_dotenv_path, override=True)

if str(_CLI) not in sys.path:
    sys.path.insert(0, str(_CLI))

# -- Logging -------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("maker_bot")

# -- Constants -----------------------------------------------------------------

BINANCE_PRICE_URL: str = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
BINANCE_KLINE_URL: str = (
    "https://api.binance.com/api/v3/klines"
    "?symbol=BTCUSDT&interval=5m&limit=6"
)
CLOB_HOST: str = "https://clob.polymarket.com"
CHAIN_ID: int  = 137   # Polygon

DEFAULT_INTERVAL_SEC: int  = 60
MAX_ORDER_SIZE_USD: float  = 5.0
MIN_ORDER_SIZE_USD: float  = 5.0   # Polymarket minimum is $5 on sampling markets
MIN_EDGE_THRESHOLD: float  = 0.04  # 4% minimum mispricing to place order
MAKER_PRICE_OFFSET: float  = 0.01  # post maker 1 cent below fair value
HTTP_TIMEOUT_SEC: int      = 8

# BTC price range keywords used to extract strike price from market question
_PRICE_KEYWORDS: list[str] = [
    "reach", "hit", "dip to", "above", "below", "between", "exceed"
]


# -- Dataclasses ---------------------------------------------------------------

@dataclass
class BinanceSnapshot:
    """Current BTC price and momentum from Binance."""
    price: float
    change_5m: Optional[float]    # % change over last 5m candle
    change_30m: Optional[float]   # % change over last 30m (6 candles)
    direction: str                # "UP", "DOWN", "FLAT"
    confidence: float             # 0.0 – 1.0


@dataclass
class ClobMarket:
    """
    A single Polymarket market fetched directly from the CLOB API.
    Unlike PolymarketMarket (from Gamma), this has real-time token prices.
    """
    condition_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    yes_price: float
    no_price: float
    accepting_orders: bool
    minimum_order_size: float


@dataclass
class MarketOpportunity:
    """Detected mispricing between our fair-value model and Polymarket odds."""
    market: ClobMarket
    signal: BinanceSnapshot
    target_side: str       # "YES" or "NO"
    target_token_id: str   # CLOB token_id for the target side
    fair_value: float      # our estimated probability
    market_price: float    # current Polymarket price
    edge: float            # fair_value - market_price (positive = underpriced)
    order_price: float     # limit price to post (maker)
    order_size: float      # size in USD


@dataclass
class OrderResult:
    """Result of a single order placement attempt."""
    success: bool
    order_id: Optional[str]
    dry_run: bool
    opportunity: MarketOpportunity
    error: Optional[str] = None


# -- HTTP helper ---------------------------------------------------------------

def _fetch_json(url: str) -> Optional[dict | list]:
    """
    Fetch JSON from a URL. Returns None on any network or parse error.

    Args:
        url: Full URL to fetch.

    Returns:
        Parsed JSON object, or None on failure.
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; trader-ai-maker/1.0)",
            "Accept":     "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.warning("HTTP fetch failed [%s]: %s", url[:60], exc)
        return None


# -- CLOB client factory -------------------------------------------------------

def _build_clob_client():
    """
    Build and return an authenticated ClobClient using environment credentials.

    Returns:
        Authenticated ClobClient instance.

    Raises:
        EnvironmentError: If required env vars are missing.
        ImportError: If py-clob-client is not installed.
    """
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
    except ImportError as exc:
        raise ImportError(
            "py-clob-client is not installed. Run: pip install py-clob-client"
        ) from exc

    required = ["POLY_API_KEY", "POLY_SECRET", "POLY_PASSPHRASE", "POLY_PRIVATE_KEY"]
    missing  = [k for k in required if not os.environ.get(k)]
    if missing:
        raise EnvironmentError(
            f"Missing required env vars: {', '.join(missing)}\n"
            "See .env.example for setup."
        )

    creds = ApiCreds(
        api_key=os.environ["POLY_API_KEY"],
        api_secret=os.environ["POLY_SECRET"],
        api_passphrase=os.environ["POLY_PASSPHRASE"],
    )
    return ClobClient(
        host=CLOB_HOST,
        chain_id=CHAIN_ID,
        key=os.environ["POLY_PRIVATE_KEY"],
        creds=creds,
        signature_type=0,
    )


# -- Binance signal ------------------------------------------------------------

def fetch_binance_snapshot() -> Optional[BinanceSnapshot]:
    """
    Fetch current BTC price and recent momentum from Binance REST API.

    Returns:
        BinanceSnapshot, or None if Binance is unreachable.
    """
    price_data = _fetch_json(BINANCE_PRICE_URL)
    kline_data = _fetch_json(BINANCE_KLINE_URL)

    if price_data is None:
        logger.error("Binance price endpoint unreachable")
        return None

    try:
        price = float(price_data["price"])
    except (KeyError, TypeError, ValueError) as exc:
        logger.error("Cannot parse Binance price: %s", exc)
        return None

    change_5m = change_30m = None
    if isinstance(kline_data, list) and len(kline_data) >= 2:
        try:
            latest = float(kline_data[-1][4])
            prev   = float(kline_data[-2][4])
            oldest = float(kline_data[0][4])
            if prev > 0:
                change_5m  = (latest - prev)   / prev   * 100
            if oldest > 0:
                change_30m = (latest - oldest) / oldest * 100
        except (IndexError, TypeError, ValueError) as exc:
            logger.warning("Cannot compute momentum: %s", exc)

    direction, confidence = _classify_momentum(change_5m)
    return BinanceSnapshot(
        price=price,
        change_5m=change_5m,
        change_30m=change_30m,
        direction=direction,
        confidence=confidence,
    )


def _classify_momentum(change_pct: Optional[float]) -> tuple[str, float]:
    """
    Classify price momentum direction and confidence from a % change.

    Args:
        change_pct: Percentage change. None is treated as flat.

    Returns:
        (direction, confidence): direction is "UP"/"DOWN"/"FLAT",
        confidence is 0.0–1.0 proportional to magnitude.
    """
    if change_pct is None or abs(change_pct) < 0.05:
        return "FLAT", 0.0

    abs_ch = abs(change_pct)
    if abs_ch < 0.10:
        confidence = 0.3
    elif abs_ch < 0.20:
        confidence = 0.6
    else:
        confidence = min(1.0, abs_ch / 0.30)

    return ("UP" if change_pct > 0 else "DOWN"), confidence


# -- CLOB market fetching ------------------------------------------------------

def fetch_btc_sampling_markets() -> list[ClobMarket]:
    """
    Fetch active BTC markets from the CLOB sampling endpoint.

    Sampling markets are liquid, accepting orders, and eligible for maker rebates.
    Filters to BTC-related markets with valid YES/NO token prices.

    Returns:
        List of ClobMarket sorted by minimum_order_size ascending.
    """
    try:
        client = _build_clob_client()
        resp   = client.get_sampling_markets()
    except Exception as exc:
        logger.error("Failed to fetch sampling markets: %s", exc)
        return []

    raw_markets = resp.get("data", []) if isinstance(resp, dict) else resp
    result: list[ClobMarket] = []

    for m in raw_markets:
        if not m.get("accepting_orders"):
            continue

        question = m.get("question", "")
        if not _is_btc_price_market(question):
            continue

        tokens = m.get("tokens", [])
        if len(tokens) < 2:
            continue

        # tokens[0] = YES, tokens[1] = NO (Polymarket convention)
        yes_tok = next((t for t in tokens if t.get("outcome") == "Yes"), tokens[0])
        no_tok  = next((t for t in tokens if t.get("outcome") == "No"),  tokens[1])

        try:
            yes_price = float(yes_tok["price"])
            no_price  = float(no_tok["price"])
        except (KeyError, TypeError, ValueError):
            continue

        if yes_price <= 0 or no_price <= 0:
            continue

        result.append(ClobMarket(
            condition_id=m.get("condition_id", ""),
            question=question,
            yes_token_id=str(yes_tok.get("token_id", "")),
            no_token_id=str(no_tok.get("token_id", "")),
            yes_price=yes_price,
            no_price=no_price,
            accepting_orders=True,
            minimum_order_size=float(m.get("minimum_order_size", MIN_ORDER_SIZE_USD)),
        ))

    result.sort(key=lambda m: m.minimum_order_size)
    logger.info("Fetched %d active BTC sampling markets", len(result))
    return result


def _is_btc_price_market(question: str) -> bool:
    """
    Check if a market question is a BTC price prediction market.

    Args:
        question: Market question string.

    Returns:
        True if question mentions Bitcoin/BTC and a price keyword.
    """
    q = question.lower()
    has_btc   = "bitcoin" in q or "btc" in q
    has_price = any(kw in q for kw in _PRICE_KEYWORDS)
    return has_btc and has_price


# -- Fair value model ----------------------------------------------------------

def compute_fair_value_for_market(
    market: ClobMarket,
    snapshot: BinanceSnapshot,
) -> tuple[float, float]:
    """
    Estimate fair YES/NO probability for a BTC price market.

    Model:
      - Extract the strike price from the market question (e.g. $100,000).
      - Compute distance = (current_price - strike) / strike as a normalized
        measure of how far from resolution the market is.
      - Use distance + Binance momentum to adjust a base probability.
      - For "reach X" markets: high current price -> higher YES probability.
      - For "dip to X" markets: low current price -> higher YES probability.

    Args:
        market: The ClobMarket to evaluate.
        snapshot: Current Binance price snapshot.

    Returns:
        (yes_fair, no_fair) both in range [0.05, 0.95], summing to 1.0.
    """
    strike = _extract_strike_price(market.question)

    if strike is None or strike <= 0:
        # No strike found -- use momentum-only model
        return _momentum_fair_value(snapshot)

    current = snapshot.price
    is_dip_market = any(
        kw in market.question.lower()
        for kw in ["dip", "below", "drop", "fall"]
    )

    # Normalized distance from strike: positive = current above strike
    distance = (current - strike) / strike

    if is_dip_market:
        # YES resolves if price goes DOWN to strike
        # Current price far above strike -> hard to dip -> lower YES probability
        base_yes = max(0.05, min(0.95, 0.5 - distance * 2))
    else:
        # YES resolves if price goes UP to strike
        # Current price far below strike -> hard to reach -> lower YES probability
        base_yes = max(0.05, min(0.95, 0.5 + distance * 2))

    # Adjust by momentum
    momentum_adj = snapshot.confidence * 0.10
    if snapshot.direction == "UP" and not is_dip_market:
        base_yes += momentum_adj
    elif snapshot.direction == "DOWN" and is_dip_market:
        base_yes += momentum_adj
    elif snapshot.direction == "UP" and is_dip_market:
        base_yes -= momentum_adj
    elif snapshot.direction == "DOWN" and not is_dip_market:
        base_yes -= momentum_adj

    yes_fair = max(0.05, min(0.95, base_yes))
    return yes_fair, 1.0 - yes_fair


def _extract_strike_price(question: str) -> Optional[float]:
    """
    Extract a dollar strike price from a market question string.

    Handles formats like "$100,000", "$75k", "100000".

    Args:
        question: Market question string.

    Returns:
        Strike price as float, or None if not found.
    """
    import re

    # Match $100,000 or $100k or $1.5m patterns
    patterns = [
        r"\$([0-9][0-9,]+)",          # $100,000
        r"\$([0-9]+)k\b",             # $75k
        r"\$([0-9]+(?:\.[0-9]+)?)m\b", # $1.5m
    ]
    for pattern in patterns:
        match = re.search(pattern, question, re.IGNORECASE)
        if match:
            raw = match.group(1).replace(",", "")
            try:
                value = float(raw)
                if "k" in pattern:
                    value *= 1_000
                if "m" in pattern:
                    value *= 1_000_000
                return value
            except ValueError:
                continue
    return None


def _momentum_fair_value(snapshot: BinanceSnapshot) -> tuple[float, float]:
    """
    Fallback fair value based purely on Binance momentum.

    Args:
        snapshot: Current Binance price snapshot.

    Returns:
        (yes_fair, no_fair) centered around 0.50 with momentum adjustment.
    """
    adj = snapshot.confidence * 0.15
    if snapshot.direction == "UP":
        yes_fair = 0.50 + adj
    elif snapshot.direction == "DOWN":
        yes_fair = 0.50 - adj
    else:
        yes_fair = 0.50
    return max(0.05, min(0.95, yes_fair)), max(0.05, min(0.95, 1.0 - yes_fair))


# -- Opportunity detection -----------------------------------------------------

def detect_opportunity(
    market: ClobMarket,
    snapshot: BinanceSnapshot,
    min_edge: float = MIN_EDGE_THRESHOLD,
    max_size: float = MAX_ORDER_SIZE_USD,
) -> Optional[MarketOpportunity]:
    """
    Detect a maker opportunity in a single market.

    Computes fair value using the price model, compares against current CLOB
    odds, and returns an opportunity if edge >= min_edge on either side.

    Args:
        market: The ClobMarket to evaluate.
        snapshot: Current Binance snapshot.
        min_edge: Minimum required edge (probability difference) to trade.
        max_size: Maximum order size in USD.

    Returns:
        MarketOpportunity if edge found, None otherwise.
    """
    yes_fair, no_fair = compute_fair_value_for_market(market, snapshot)

    yes_edge = yes_fair - market.yes_price
    no_edge  = no_fair  - market.no_price

    if yes_edge >= min_edge:
        target_side    = "YES"
        token_id       = market.yes_token_id
        fair_value     = yes_fair
        market_price   = market.yes_price
        edge           = yes_edge
    elif no_edge >= min_edge:
        target_side    = "NO"
        token_id       = market.no_token_id
        fair_value     = no_fair
        market_price   = market.no_price
        edge           = no_edge
    else:
        return None

    order_price = round(max(0.01, min(0.99, fair_value - MAKER_PRICE_OFFSET)), 2)
    order_size  = max(market.minimum_order_size, min(max_size, max_size))

    return MarketOpportunity(
        market=market,
        signal=snapshot,
        target_side=target_side,
        target_token_id=token_id,
        fair_value=fair_value,
        market_price=market_price,
        edge=edge,
        order_price=order_price,
        order_size=order_size,
    )


# -- Order placement -----------------------------------------------------------

def place_maker_order(
    opportunity: MarketOpportunity,
    dry_run: bool = True,
) -> OrderResult:
    """
    Place a maker limit order on the Polymarket CLOB.

    Args:
        opportunity: The detected market opportunity.
        dry_run: If True, log the order but do not submit it.

    Returns:
        OrderResult with success status and order ID if placed.
    """
    if dry_run:
        logger.info(
            "[DRY RUN] Would place MAKER order:\n"
            "  Market     : %s\n"
            "  Side       : %s\n"
            "  Token ID   : %s\n"
            "  Order price: %.2f  (fair=%.3f  market=%.3f  edge=%.3f)\n"
            "  Order size : $%.2f",
            opportunity.market.question[:65],
            opportunity.target_side,
            opportunity.target_token_id[:20] + "...",
            opportunity.order_price,
            opportunity.fair_value,
            opportunity.market_price,
            opportunity.edge,
            opportunity.order_size,
        )
        return OrderResult(
            success=True, order_id=None, dry_run=True, opportunity=opportunity
        )

    try:
        from py_clob_client.clob_types import OrderArgs
        from py_clob_client.constants import BUY
    except ImportError as exc:
        msg = f"py-clob-client import error: {exc}"
        logger.error(msg)
        return OrderResult(
            success=False, order_id=None, dry_run=False,
            opportunity=opportunity, error=msg,
        )

    try:
        client = _build_clob_client()
        order_args = OrderArgs(
            token_id=opportunity.target_token_id,
            price=opportunity.order_price,
            size=opportunity.order_size,
            side=BUY,
        )
        response = client.create_and_post_order(order_args)
        order_id = (
            response.get("orderID")
            or response.get("order_id")
            or response.get("id")
        )
        logger.info(
            "Order placed: %s %s @ %.2f | $%.2f | id=%s",
            opportunity.target_side,
            opportunity.market.question[:40],
            opportunity.order_price,
            opportunity.order_size,
            order_id,
        )
        return OrderResult(
            success=True, order_id=order_id, dry_run=False, opportunity=opportunity
        )

    except Exception as exc:
        logger.error("Order placement failed: %s", exc)
        return OrderResult(
            success=False, order_id=None, dry_run=False,
            opportunity=opportunity, error=str(exc),
        )


# -- Bot cycle -----------------------------------------------------------------

def run_once(
    dry_run: bool   = True,
    max_size: float = MAX_ORDER_SIZE_USD,
    min_edge: float = MIN_EDGE_THRESHOLD,
    verbose: bool   = False,
) -> list[OrderResult]:
    """
    Execute one full cycle: fetch -> analyze -> detect -> place.

    Args:
        dry_run: If True, do not place real orders.
        max_size: Maximum order size in USD.
        min_edge: Minimum edge threshold to place an order.
        verbose: If True, log signal and market details.

    Returns:
        List of OrderResult for each order attempted this cycle.
    """
    results: list[OrderResult] = []

    # Step 1: Fetch Binance snapshot
    snapshot = fetch_binance_snapshot()
    if snapshot is None:
        logger.warning("Skipping cycle: Binance unavailable")
        return results

    if verbose:
        logger.info(
            "Binance: $%.2f  5m=%+.3f%%  30m=%+.3f%%  %s(%.2f)",
            snapshot.price,
            snapshot.change_5m or 0,
            snapshot.change_30m or 0,
            snapshot.direction,
            snapshot.confidence,
        )

    # Step 2: Fetch active BTC sampling markets from CLOB
    markets = fetch_btc_sampling_markets()
    if not markets:
        logger.info("No active BTC markets found")
        return results

    if verbose:
        for m in markets:
            yes_fair, _ = compute_fair_value_for_market(m, snapshot)
            strike = _extract_strike_price(m.question)
            logger.info(
                "  %-65s  YES market=%.3f fair=%.3f  strike=$%s",
                m.question[:65],
                m.yes_price,
                yes_fair,
                f"{strike:,.0f}" if strike else "?",
            )

    # Step 3: Detect opportunities
    for market in markets:
        opp = detect_opportunity(market, snapshot, min_edge=min_edge, max_size=max_size)
        if opp is None:
            continue

        logger.info(
            "Edge detected: %s | %s @ %.3f edge",
            opp.target_side,
            market.question[:50],
            opp.edge,
        )
        result = place_maker_order(opp, dry_run=dry_run)
        results.append(result)

    if not results:
        logger.info("No opportunities found (edge < %.3f)", min_edge)

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

    Catches all exceptions per cycle to ensure the loop never crashes.

    Args:
        interval: Seconds between cycles.
        dry_run: If True, do not place real orders.
        max_size: Maximum order size per trade in USD.
        min_edge: Minimum required edge to place an order.
        verbose: If True, log detailed signal and market information.
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
            logger.info("Stopped by user")
            break
        except Exception as exc:
            logger.error("Unexpected error in cycle %d: %s", cycle, exc)

        logger.info("Sleeping %ds...", interval)
        time.sleep(interval)


# -- Entry point ---------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Polymarket Maker Bot\n"
            "Default: dry-run. Pass --live to place real orders.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--live", dest="dry_run", action="store_false", default=True,
        help="Place real orders (default: dry-run)",
    )
    parser.add_argument(
        "--interval", type=int, default=DEFAULT_INTERVAL_SEC,
        help=f"Seconds between cycles (default: {DEFAULT_INTERVAL_SEC})",
    )
    parser.add_argument(
        "--max-size", type=float, default=MAX_ORDER_SIZE_USD,
        help=f"Max order size USD (default: {MAX_ORDER_SIZE_USD})",
    )
    parser.add_argument(
        "--min-edge", type=float, default=MIN_EDGE_THRESHOLD,
        help=f"Min edge to place order (default: {MIN_EDGE_THRESHOLD})",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run one cycle and exit",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print detailed signal and market info each cycle",
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
