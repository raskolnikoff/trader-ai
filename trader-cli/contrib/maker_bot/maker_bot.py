#!/usr/bin/env python3
"""
Polymarket Maker Bot.

Strategy:
  1. Fetch current BTC price and momentum from Binance (5m candles).
  2. Fetch active BTC markets from Polymarket CLOB sampling endpoint
     AND/OR the public Gamma API (for weekly/monthly short-term markets).
  3. For each market, compute a fair-value probability using Binance price
     relative to the market's resolution threshold (strike price).
  4. Compare fair value against current Polymarket odds to detect mispricing.
  5. Place a limit (maker) order on the underpriced side if edge >= threshold.
  6. Repeat on a configurable interval.

Market sourcing:
  --source clob    : CLOB sampling endpoint only (original behavior).
  --source gamma   : Gamma API only (short-term markets, public, no auth).
  --source all     : Merge both sources, deduplicated by condition_id. DEFAULT.

Horizon filter (Gamma only):
  --horizon weekly  : markets resolving within 7 days.
  --horizon monthly : markets resolving in 7-31 days.
  --horizon all     : any resolution window. DEFAULT.

Balance management:
  - Fetches live USDC.e balance from Polygon via web3 before each cycle.
  - Caps total orders per cycle so spending never exceeds available balance.
  - Skips cycle entirely if balance < MIN_ORDER_SIZE_USD.
  - Fail-safe: on any balance check error, returns 0.0 (halts cycle).

Maker orders pay zero fees and earn a rebate on Polymarket CLOB.
This bot NEVER places taker orders.

Safety:
  - --dry-run is the DEFAULT. Pass --live to place real orders.
  - Max position size is capped by MAX_ORDER_SIZE_USD.
  - Minimum edge required is MIN_EDGE_THRESHOLD.
  - All exceptions are caught per cycle; the loop never crashes.

Usage:
    python maker_bot.py --once --verbose                    # dry-run, one cycle, all sources
    python maker_bot.py --verbose                           # dry-run, continuous
    python maker_bot.py --live --max-size 3.0               # live trading
    python maker_bot.py --source gamma --horizon weekly     # weekly-only scan
    python maker_bot.py --source clob                       # CLOB-only (legacy)

Requirements:
    pip install py-clob-client python-dotenv web3
"""

import argparse
import logging
import math
import os
import re
import sys
import time
import urllib.request
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# -- Path setup ----------------------------------------------------------------

_ROOT = Path(__file__).parent.parent.parent.parent
_CLI  = Path(__file__).parent.parent.parent

_dotenv_path = _ROOT / ".env"
if _dotenv_path.exists():
    from dotenv import load_dotenv
    load_dotenv(_dotenv_path, override=True)

if str(_CLI) not in sys.path:
    sys.path.insert(0, str(_CLI))

# Local import (after path setup so this works when run as a script)
from contrib.maker_bot.gamma_markets import (  # noqa: E402
    fetch_btc_gamma_markets,
    GammaMarket,
    DEFAULT_MIN_LIQUIDITY_USD,
)

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
CHAIN_ID: int  = 137  # Polygon

# USDC.e (bridged USDC) contract on Polygon -- required by Polymarket CLOB
USDC_E_ADDRESS: str = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
POLYGON_RPC: str    = "https://polygon-bor-rpc.publicnode.com"

DEFAULT_INTERVAL_SEC: int  = 60
MAX_ORDER_SIZE_USD: float  = 5.0
MIN_ORDER_SIZE_USD: float  = 5.0   # Polymarket sampling market minimum
MIN_EDGE_THRESHOLD: float  = 0.04  # 4% minimum mispricing to place order
MAKER_PRICE_OFFSET: float  = 0.01  # post maker 1 cent below fair value
HTTP_TIMEOUT_SEC: int      = 8

# Reserve fraction of balance -- never spend 100% to leave room for gas
BALANCE_RESERVE_RATIO: float = 0.90  # use at most 90% of available balance

# Keywords indicating this is a BTC price prediction market
_PRICE_KEYWORDS: list[str] = [
    "reach", "hit", "dip to", "above", "below", "between", "exceed"
]

# Keywords indicating the market resolves YES if price goes DOWN
_DIP_KEYWORDS: list[str] = ["dip", "below", "drop", "fall"]

# Sigmoid scaling factor for distance->probability conversion
_SIGMOID_SCALE: float = 3.0


# -- Dataclasses ---------------------------------------------------------------

@dataclass
class BinanceSnapshot:
    """Current BTC price and momentum from Binance."""
    price: float
    change_5m: Optional[float]
    change_30m: Optional[float]
    direction: str
    confidence: float


@dataclass
class ClobMarket:
    """A Polymarket market fetched from the CLOB API with real-time token prices."""
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
    """A detected mispricing between our fair-value model and Polymarket odds."""
    market: ClobMarket
    signal: BinanceSnapshot
    target_side: str
    target_token_id: str
    fair_value: float
    market_price: float
    edge: float
    order_price: float
    order_size: float


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
    Fetch JSON from a URL synchronously. Returns None on any error.

    Args:
        url: Full URL to fetch.

    Returns:
        Parsed JSON, or None on failure.
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


# -- Balance check (on-chain) --------------------------------------------------

def fetch_usdc_e_balance(wallet_address: str) -> float:
    """
    Fetch the USDC.e (bridged USDC on Polygon) balance of a wallet via web3.

    Polymarket CLOB only accepts USDC.e as collateral. This check prevents
    over-ordering when the wallet has insufficient on-chain balance.

    Fail-safe behavior:
      On ANY error (missing web3, RPC failure, bad address, etc.), returns 0.0.
      The downstream logic in run_once() skips the cycle when balance is below
      MIN_ORDER_SIZE_USD, so returning 0.0 halts trading safely rather than
      allowing uncapped orders via float('inf').

    Args:
        wallet_address: Ethereum-compatible wallet address (0x...).

    Returns:
        Balance in USD (float). Returns 0.0 on any error (fail-safe).
    """
    try:
        from web3 import Web3
    except ImportError:
        logger.error(
            "web3 not installed -- HALTING cycle. Run: pip install web3"
        )
        return 0.0  # fail-safe: treat as insufficient balance

    try:
        w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
        abi = [
            {
                "inputs": [{"name": "account", "type": "address"}],
                "name": "balanceOf",
                "outputs": [{"type": "uint256"}],
                "stateMutability": "view",
                "type": "function",
            }
        ]
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(USDC_E_ADDRESS), abi=abi
        )
        raw = contract.functions.balanceOf(
            Web3.to_checksum_address(wallet_address)
        ).call()
        balance_usd = raw / 1e6  # USDC.e has 6 decimals
        logger.info("USDC.e balance: $%.4f", balance_usd)
        return balance_usd
    except Exception as exc:
        logger.error(
            "Balance check failed: %s -- HALTING cycle for safety", exc
        )
        return 0.0  # fail-safe: treat as insufficient balance


# -- CLOB client ---------------------------------------------------------------

def _build_clob_client():
    """
    Build an authenticated ClobClient from environment credentials.

    Returns:
        Authenticated ClobClient instance.

    Raises:
        EnvironmentError: If any required env var is missing.
        ImportError: If py-clob-client is not installed.
    """
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
    except ImportError as exc:
        raise ImportError(
            "py-clob-client not installed. Run: pip install py-clob-client"
        ) from exc

    required = ["POLY_API_KEY", "POLY_SECRET", "POLY_PASSPHRASE", "POLY_PRIVATE_KEY"]
    missing  = [k for k in required if not os.environ.get(k)]
    if missing:
        raise EnvironmentError(
            f"Missing env vars: {', '.join(missing)}\nSee .env.example."
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


# -- Binance -------------------------------------------------------------------

def fetch_binance_snapshot() -> Optional[BinanceSnapshot]:
    """
    Fetch current BTC price and 5m/30m momentum from Binance REST API.

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
    Classify price momentum direction and confidence.

    Args:
        change_pct: % price change. None treated as flat.

    Returns:
        (direction, confidence) where direction is UP/DOWN/FLAT.
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


# -- Market fetching -----------------------------------------------------------

def fetch_btc_sampling_markets() -> list[ClobMarket]:
    """
    Fetch active BTC price markets from the CLOB sampling endpoint.

    Returns:
        List of ClobMarket sorted by minimum_order_size ascending.
    """
    try:
        client = _build_clob_client()
        resp   = client.get_sampling_markets()
    except Exception as exc:
        logger.error("Failed to fetch sampling markets: %s", exc)
        return []

    raw = resp.get("data", []) if isinstance(resp, dict) else resp
    result: list[ClobMarket] = []

    for m in raw:
        if not m.get("accepting_orders"):
            continue
        question = m.get("question", "")
        if not _is_btc_price_market(question):
            continue

        tokens = m.get("tokens", [])
        if len(tokens) < 2:
            continue

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
    logger.info("Fetched %d active BTC sampling markets (CLOB)", len(result))
    return result


def _gamma_to_clob_market(g: GammaMarket) -> ClobMarket:
    """Convert a GammaMarket to a ClobMarket so downstream code is source-agnostic."""
    return ClobMarket(
        condition_id=g.condition_id,
        question=g.question,
        yes_token_id=g.yes_token_id,
        no_token_id=g.no_token_id,
        yes_price=g.yes_price,
        no_price=g.no_price,
        accepting_orders=g.accepting_orders,
        minimum_order_size=max(g.minimum_order_size, MIN_ORDER_SIZE_USD),
    )


def fetch_all_btc_markets(
    source: str = "all",
    horizon: str = "all",
    min_liquidity_usd: float = DEFAULT_MIN_LIQUIDITY_USD,
) -> list[ClobMarket]:
    """
    Fetch BTC markets from the requested source(s), deduplicated by condition_id.

    Args:
        source: 'clob' | 'gamma' | 'all' (default).
        horizon: 'weekly' | 'monthly' | 'all' (Gamma only).
        min_liquidity_usd: Minimum liquidity filter for Gamma results.

    Returns:
        Deduplicated list of ClobMarket, sorted by minimum_order_size ascending.
    """
    by_condition: dict[str, ClobMarket] = {}

    if source in ("clob", "all"):
        for m in fetch_btc_sampling_markets():
            if m.condition_id:
                by_condition[m.condition_id] = m

    if source in ("gamma", "all"):
        try:
            gamma_markets = fetch_btc_gamma_markets(
                horizon=horizon,
                min_liquidity_usd=min_liquidity_usd,
            )
        except Exception as exc:
            logger.error("Gamma fetch failed: %s", exc)
            gamma_markets = []

        for g in gamma_markets:
            if not g.condition_id or g.condition_id in by_condition:
                continue  # CLOB takes priority; it has accurate order book
            by_condition[g.condition_id] = _gamma_to_clob_market(g)

    result = list(by_condition.values())
    result.sort(key=lambda m: m.minimum_order_size)

    logger.info(
        "Total unique BTC markets: %d (source=%s, horizon=%s, min_liq=$%.0f)",
        len(result), source, horizon, min_liquidity_usd,
    )
    return result


def _is_btc_price_market(question: str) -> bool:
    """Return True if question is a BTC price prediction market."""
    q = question.lower()
    return ("bitcoin" in q or "btc" in q) and any(kw in q for kw in _PRICE_KEYWORDS)


# -- Strike price extraction ---------------------------------------------------

def _extract_strike_price(question: str) -> Optional[float]:
    """
    Extract the dollar strike price from a market question string.

    Supports: $100,000  $150k  $1.5m

    Args:
        question: Market question string.

    Returns:
        Strike price as float (USD), or None if not found.
    """
    patterns: list[tuple[str, float]] = [
        (r"\$([0-9]+(?:\.[0-9]+)?)\s*m\b", 1_000_000),
        (r"\$([0-9]+(?:\.[0-9]+)?)\s*k\b", 1_000),
        (r"\$([0-9][0-9,]{2,})",           1),
    ]
    for pattern, multiplier in patterns:
        match = re.search(pattern, question, re.IGNORECASE)
        if match:
            try:
                return float(match.group(1).replace(",", "")) * multiplier
            except ValueError:
                continue
    return None


# -- Fair value model ----------------------------------------------------------

def compute_fair_value_for_market(
    market: ClobMarket,
    snapshot: BinanceSnapshot,
) -> tuple[float, float]:
    """
    Estimate fair YES/NO probability for a BTC price market.

    Uses sigmoid function on normalized distance from strike price,
    with a small momentum adjustment.

    Args:
        market: ClobMarket to evaluate.
        snapshot: Current Binance price snapshot.

    Returns:
        (yes_fair, no_fair) both in [0.05, 0.95], summing to 1.0.
    """
    strike = _extract_strike_price(market.question)
    if strike is None or strike <= 0:
        return _momentum_fair_value(snapshot)

    current     = snapshot.price
    is_dip      = any(kw in market.question.lower() for kw in _DIP_KEYWORDS)
    distance    = (current - strike) / strike
    signed_dist = distance if not is_dip else -distance
    yes_fair_raw = 1.0 / (1.0 + math.exp(-_SIGMOID_SCALE * signed_dist))

    momentum_adj = snapshot.confidence * 0.05
    if snapshot.direction == "UP" and not is_dip:
        yes_fair_raw += momentum_adj
    elif snapshot.direction == "DOWN" and is_dip:
        yes_fair_raw += momentum_adj
    elif snapshot.direction == "UP" and is_dip:
        yes_fair_raw -= momentum_adj
    elif snapshot.direction == "DOWN" and not is_dip:
        yes_fair_raw -= momentum_adj

    yes_fair = max(0.05, min(0.95, yes_fair_raw))
    return yes_fair, round(1.0 - yes_fair, 4)


def _momentum_fair_value(snapshot: BinanceSnapshot) -> tuple[float, float]:
    """Fallback fair value based purely on Binance momentum."""
    adj = snapshot.confidence * 0.10
    if snapshot.direction == "UP":
        yes_fair = 0.50 + adj
    elif snapshot.direction == "DOWN":
        yes_fair = 0.50 - adj
    else:
        yes_fair = 0.50
    yes_fair = max(0.05, min(0.95, yes_fair))
    return yes_fair, round(1.0 - yes_fair, 4)


# -- Opportunity detection -----------------------------------------------------

def detect_opportunity(
    market: ClobMarket,
    snapshot: BinanceSnapshot,
    min_edge: float = MIN_EDGE_THRESHOLD,
    max_size: float = MAX_ORDER_SIZE_USD,
) -> Optional[MarketOpportunity]:
    """
    Detect a maker opportunity in a single market.

    Args:
        market: The ClobMarket to evaluate.
        snapshot: Current Binance snapshot.
        min_edge: Minimum probability difference required to trade.
        max_size: Maximum order size in USD.

    Returns:
        MarketOpportunity if edge found, None otherwise.
    """
    yes_fair, no_fair = compute_fair_value_for_market(market, snapshot)

    yes_edge = yes_fair - market.yes_price
    no_edge  = no_fair  - market.no_price

    if yes_edge >= min_edge:
        target_side  = "YES"
        token_id     = market.yes_token_id
        fair_value   = yes_fair
        market_price = market.yes_price
        edge         = yes_edge
    elif no_edge >= min_edge:
        target_side  = "NO"
        token_id     = market.no_token_id
        fair_value   = no_fair
        market_price = market.no_price
        edge         = no_edge
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
            "  Token ID   : %s...\n"
            "  Order price: %.2f  (fair=%.3f  market=%.3f  edge=%.3f)\n"
            "  Order size : $%.2f",
            opportunity.market.question[:65],
            opportunity.target_side,
            opportunity.target_token_id[:20],
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
    except ImportError as exc:
        msg = f"py-clob-client import error: {exc}"
        logger.error(msg)
        return OrderResult(
            success=False, order_id=None, dry_run=False,
            opportunity=opportunity, error=msg,
        )

    try:
        client     = _build_clob_client()
        order_args = OrderArgs(
            token_id=opportunity.target_token_id,
            price=opportunity.order_price,
            size=opportunity.order_size,
            side="BUY",  # side is plain string in current py-clob-client
        )
        response = client.create_and_post_order(order_args)
        order_id = (
            response.get("orderID")
            or response.get("order_id")
            or response.get("id")
        )
        logger.info(
            "Order placed: %s | %s @ %.2f | $%.2f | id=%s",
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
    source:  str    = "all",
    horizon: str    = "all",
    min_liquidity_usd: float = DEFAULT_MIN_LIQUIDITY_USD,
) -> list[OrderResult]:
    """
    Execute one full cycle: balance check -> fetch -> analyze -> detect -> place.

    Balance management:
      - Reads live USDC.e balance from Polygon.
      - Caps number of orders so total spend <= balance * BALANCE_RESERVE_RATIO.
      - Skips cycle if balance < MIN_ORDER_SIZE_USD.

    Args:
        dry_run: If True, do not place real orders.
        max_size: Maximum order size in USD per trade.
        min_edge: Minimum edge threshold to place an order.
        verbose: If True, log per-market fair value details.
        source: 'clob' | 'gamma' | 'all'.
        horizon: 'weekly' | 'monthly' | 'all' (Gamma only).
        min_liquidity_usd: Minimum liquidity filter for Gamma results.

    Returns:
        List of OrderResult for each order attempted this cycle.
    """
    results: list[OrderResult] = []

    # -- Step 1: Balance check -------------------------------------------------
    wallet = os.environ.get("POLY_WALLET_ADDRESS", "")
    if wallet and not dry_run:
        balance   = fetch_usdc_e_balance(wallet)
        spendable = balance * BALANCE_RESERVE_RATIO
        if spendable < MIN_ORDER_SIZE_USD:
            logger.warning(
                "Balance too low to trade: $%.4f (need $%.2f). Skipping cycle.",
                balance, MIN_ORDER_SIZE_USD,
            )
            return results
        max_orders = int(spendable // max_size)
        logger.info(
            "Balance $%.4f  spendable $%.4f  max_orders=%d",
            balance, spendable, max_orders,
        )
    else:
        max_orders = 999  # dry-run: no limit
        if dry_run:
            logger.info("DRY RUN: skipping balance check")

    # -- Step 2: Binance snapshot ----------------------------------------------
    snapshot = fetch_binance_snapshot()
    if snapshot is None:
        logger.warning("Skipping cycle: Binance unavailable")
        return results

    if verbose:
        logger.info(
            "Binance: $%.2f  5m=%+.3f%%  30m=%+.3f%%  %s(conf=%.2f)",
            snapshot.price,
            snapshot.change_5m or 0,
            snapshot.change_30m or 0,
            snapshot.direction,
            snapshot.confidence,
        )

    # -- Step 3: Fetch markets (CLOB + Gamma) ----------------------------------
    markets = fetch_all_btc_markets(
        source=source,
        horizon=horizon,
        min_liquidity_usd=min_liquidity_usd,
    )
    if not markets:
        logger.info("No active BTC markets found")
        return results

    if verbose:
        for m in markets:
            yes_fair, _ = compute_fair_value_for_market(m, snapshot)
            strike      = _extract_strike_price(m.question)
            logger.info(
                "  %-65s  YES mkt=%.3f fair=%.3f  strike=$%s",
                m.question[:65],
                m.yes_price,
                yes_fair,
                f"{strike:,.0f}" if strike else "?",
            )

    # -- Step 4: Detect and place (respecting max_orders cap) ------------------
    orders_placed = 0
    for market in markets:
        if orders_placed >= max_orders:
            logger.info(
                "Order cap reached (%d). Remaining markets skipped.", max_orders
            )
            break

        opp = detect_opportunity(market, snapshot, min_edge=min_edge, max_size=max_size)
        if opp is None:
            continue

        logger.info(
            "Edge detected: %s | %s @ %.3f",
            opp.target_side,
            market.question[:50],
            opp.edge,
        )
        result = place_maker_order(opp, dry_run=dry_run)
        results.append(result)
        if result.success:
            orders_placed += 1

    if not results:
        logger.info("No opportunities found (edge < %.3f)", min_edge)

    return results


def run_loop(
    interval: int   = DEFAULT_INTERVAL_SEC,
    dry_run: bool   = True,
    max_size: float = MAX_ORDER_SIZE_USD,
    min_edge: float = MIN_EDGE_THRESHOLD,
    verbose: bool   = False,
    source:  str    = "all",
    horizon: str    = "all",
    min_liquidity_usd: float = DEFAULT_MIN_LIQUIDITY_USD,
) -> None:
    """
    Run the maker bot continuously on a fixed interval.

    Never crashes -- all per-cycle exceptions are caught and logged.

    Args:
        interval: Seconds between cycles.
        dry_run: If True, do not place real orders.
        max_size: Maximum order size per trade in USD.
        min_edge: Minimum required edge to place an order.
        verbose: If True, log detailed signal and market information.
        source: 'clob' | 'gamma' | 'all'.
        horizon: 'weekly' | 'monthly' | 'all' (Gamma only).
        min_liquidity_usd: Minimum liquidity filter for Gamma results.
    """
    logger.info(
        "Maker bot starting | mode=%s | interval=%ds | max_size=$%.2f | "
        "min_edge=%.3f | source=%s | horizon=%s | min_liq=$%.0f",
        "DRY RUN" if dry_run else "LIVE", interval, max_size, min_edge,
        source, horizon, min_liquidity_usd,
    )
    cycle = 0
    while True:
        cycle += 1
        logger.info("--- Cycle %d ---", cycle)
        try:
            run_once(
                dry_run=dry_run, max_size=max_size, min_edge=min_edge,
                verbose=verbose, source=source, horizon=horizon,
                min_liquidity_usd=min_liquidity_usd,
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
        description="Polymarket Maker Bot\nDefault: dry-run. Pass --live for real orders.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--live", dest="dry_run", action="store_false", default=True,
                        help="Place real orders (default: dry-run)")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SEC,
                        help=f"Seconds between cycles (default: {DEFAULT_INTERVAL_SEC})")
    parser.add_argument("--max-size", type=float, default=MAX_ORDER_SIZE_USD,
                        help=f"Max order size USD (default: {MAX_ORDER_SIZE_USD})")
    parser.add_argument("--min-edge", type=float, default=MIN_EDGE_THRESHOLD,
                        help=f"Min edge to place order (default: {MIN_EDGE_THRESHOLD})")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--verbose", action="store_true",
                        help="Print detailed signal and market info")
    parser.add_argument("--source", choices=["clob", "gamma", "all"], default="all",
                        help="Market source (default: all = CLOB + Gamma merged)")
    parser.add_argument("--horizon", choices=["weekly", "monthly", "all"], default="all",
                        help="Resolution window for Gamma markets (default: all)")
    parser.add_argument("--min-liquidity", type=float, default=DEFAULT_MIN_LIQUIDITY_USD,
                        help=f"Min liquidity USD for Gamma markets "
                             f"(default: ${DEFAULT_MIN_LIQUIDITY_USD:.0f}, "
                             f"set MIN_LIQUIDITY_USD env to override)")
    args = parser.parse_args()

    if args.once:
        results = run_once(
            dry_run=args.dry_run, max_size=args.max_size,
            min_edge=args.min_edge, verbose=args.verbose,
            source=args.source, horizon=args.horizon,
            min_liquidity_usd=args.min_liquidity,
        )
        logger.info("Cycle complete: %d order(s) attempted", len(results))
    else:
        run_loop(
            interval=args.interval, dry_run=args.dry_run,
            max_size=args.max_size, min_edge=args.min_edge, verbose=args.verbose,
            source=args.source, horizon=args.horizon,
            min_liquidity_usd=args.min_liquidity,
        )


if __name__ == "__main__":
    main()
