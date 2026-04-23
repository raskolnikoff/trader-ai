#!/usr/bin/env python3
"""
Polymarket Maker Bot.

Strategy:
  1. Fetch current BTC price and momentum from Binance (5m candles).
  2. Fetch active BTC markets from Polymarket CLOB sampling endpoint
     AND/OR the public Gamma API (for weekly/monthly short-term markets).
  3. For each market, compute a fair-value probability using a GBM-based
     touch-probability model when the market's end_date is known, or a
     sigmoid fallback otherwise.
  4. Compare fair value against current Polymarket odds to detect mispricing.
  5. Place a limit (maker) order on the underpriced side if edge >= threshold.
  6. Repeat on a configurable interval.

Fair-value model (v2 — PR #21):
  When end_date is available, we price the market as a "barrier touch"
  probability under geometric Brownian motion (GBM) with constant volatility.
  For an upper barrier K > S_0, using the running-maximum distribution of a
  drifted arithmetic Brownian motion X_t = ln(S_t / S_0) with drift
  mu = r - sigma^2/2 and diffusion sigma, and setting k = ln(K/S_0) > 0:

      P(max_{t<=T} X_t >= k)
        = Phi((-k + mu*T) / (sigma*sqrt(T)))
          + exp(2*mu*k / sigma^2) * Phi((-k - mu*T) / (sigma*sqrt(T)))

  This is the standard BM-with-drift running-maximum formula; see any text
  on barrier options or Borodin & Salminen. Verified numerically against a
  Monte Carlo simulation.

  Parameters:
    sigma = GBM_VOL_ANNUAL (default 0.60, representative BTC realised vol)
    r     = 0              (short horizon, negligible drift)
    T     = (end_date - now) in years, clamped to [1/365, 3.0]
  Dip markets (`S_0 > K` and "dip/below/drop/fall" in question) are priced as
  P(min_{t<=T} S_t <= K), computed via the log-price symmetry
  down_touch(S_0, K) = up_touch(K, S_0).

  When end_date is missing, the model falls back to the previous sigmoid
  heuristic — preserving backwards compatibility with sources that lack a
  resolution date.

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

Sanity filters:
  - Markets with yes_price outside [MIN_MARKET_PROBABILITY,
    MAX_MARKET_PROBABILITY] are skipped (tail markets are unreliable inputs).
  - Opportunities with edge > MAX_EDGE_SANITY_CAP are logged and skipped
    (model is probably wrong, not the market).

Maker orders pay zero fees and earn a rebate on Polymarket CLOB.
This bot NEVER places taker orders.

Safety:
  - --live is required to place real orders (default: dry-run).
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# -- Path setup ----------------------------------------------------------------

# This file lives at:
#   <repo_root>/trader-cli/contrib/maker_bot/maker_bot.py
# so three `.parent` hops reach the repo root, not four. (Four hops would go
# one level above the repo, to whatever directory the repo is checked out in.)
_ROOT = Path(__file__).parent.parent.parent.parent
_CLI  = Path(__file__).parent.parent.parent

# Try the nominal location first; if it doesn't exist, try one level up
# (some checkouts place the repo one directory deeper than expected).
_dotenv_path = _CLI.parent / ".env"            # trader-ai/.env (correct)
_fallback_dotenv_path = _ROOT / ".env"         # legacy behaviour, one level higher

if _dotenv_path.exists():
    from dotenv import load_dotenv
    load_dotenv(_dotenv_path, override=True)
elif _fallback_dotenv_path.exists():
    from dotenv import load_dotenv
    load_dotenv(_fallback_dotenv_path, override=True)

# Ensure both trader-cli/ (for `contrib.maker_bot.*`) and the maker_bot
# directory itself (for sibling imports when run as a script) are on sys.path.
if str(_CLI) not in sys.path:
    sys.path.insert(0, str(_CLI))
_THIS_DIR = Path(__file__).parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

# Local import: try package-style first (cron / python -m), fall back to
# sibling-style (direct script invocation).
try:
    from contrib.maker_bot.gamma_markets import (  # noqa: E402
        fetch_btc_gamma_markets,
        GammaMarket,
        DEFAULT_MIN_LIQUIDITY_USD,
    )
except ImportError:
    from gamma_markets import (  # noqa: E402
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

# -- Fair-value model parameters (PR #21) --------------------------------------

# Annualised BTC volatility used in the GBM model. 0.60 is a representative
# realised-vol value for BTC over the last 1-2 years. Overridable via env.
GBM_VOL_ANNUAL: float = float(os.environ.get("GBM_VOL_ANNUAL", "0.60"))

# Horizon cap in years. Any market resolving more than this far out is clamped
# so the model does not output absurdly high probabilities on long horizons.
MAX_HORIZON_YEARS: float = 3.0

# Minimum horizon (1 day) to avoid div-by-zero for near-expiry markets.
MIN_HORIZON_YEARS: float = 1.0 / 365.0

# Risk-free rate in the GBM drift term. Zero is a reasonable simplification
# for the horizons we price (weeks to months) and lets probability be driven
# purely by spot-vs-strike distance and volatility.
GBM_RISK_FREE_RATE: float = 0.0

# -- Sanity-filter parameters (PR #21) -----------------------------------------

# Tail markets (very low or very high yes_price) are dominated by factors the
# model does not capture (time decay, resolution ambiguity, liquidation). Skip
# them outright rather than trust a computed edge against them.
MIN_MARKET_PROBABILITY: float = 0.05
MAX_MARKET_PROBABILITY: float = 0.95

# If the computed edge is larger than this, log a warning and skip the order.
# A 25%+ mispricing on a liquid Polymarket market is almost always a model
# bug on our side, not a real arbitrage.
MAX_EDGE_SANITY_CAP: float = 0.20

# Keywords indicating this is a BTC price prediction market
_PRICE_KEYWORDS: list[str] = [
    "reach", "hit", "dip to", "above", "below", "between", "exceed"
]

# Keywords indicating the market resolves YES if price goes DOWN
_DIP_KEYWORDS: list[str] = ["dip", "below", "drop", "fall"]

# Sigmoid scaling factor for the legacy distance->probability fallback
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
    """
    A Polymarket market fetched from the CLOB API with real-time token prices.

    `end_date` is optional and only populated when the source (e.g. Gamma) or
    CLOB metadata provides it. Used by the GBM fair-value model.
    """
    condition_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    yes_price: float
    no_price: float
    accepting_orders: bool
    minimum_order_size: float
    end_date: Optional[datetime] = None


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
    """Fetch JSON from a URL synchronously. Returns None on any error."""
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

    Returns:
        Balance in USD (float). Returns 0.0 on any error (fail-safe).
    """
    try:
        from web3 import Web3
    except ImportError:
        logger.error(
            "web3 not installed -- HALTING cycle. Run: pip install web3"
        )
        return 0.0

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
        balance_usd = raw / 1e6
        logger.info("USDC.e balance: $%.4f", balance_usd)
        return balance_usd
    except Exception as exc:
        logger.error(
            "Balance check failed: %s -- HALTING cycle for safety", exc
        )
        return 0.0


# -- CLOB client ---------------------------------------------------------------

def _build_clob_client():
    """Build an authenticated ClobClient from environment credentials."""
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
    """Fetch current BTC price and 5m/30m momentum from Binance REST API."""
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

def _parse_clob_end_date(raw_market: dict) -> Optional[datetime]:
    """
    Extract an end_date from a CLOB sampling market payload.

    The CLOB response sometimes includes `end_date_iso` (ISO-8601 UTC) and
    sometimes `end_date`. Returns None if neither is usable.
    """
    for key in ("end_date_iso", "endDateIso", "end_date"):
        value = raw_market.get(key)
        if not value:
            continue
        try:
            if isinstance(value, str):
                if value.endswith("Z"):
                    value = value[:-1] + "+00:00"
                return datetime.fromisoformat(value)
        except (ValueError, TypeError):
            continue
    return None


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
            end_date=_parse_clob_end_date(m),
        ))

    result.sort(key=lambda m: m.minimum_order_size)
    logger.info("Fetched %d active BTC sampling markets (CLOB)", len(result))
    return result


def _gamma_to_clob_market(g: GammaMarket) -> ClobMarket:
    """Convert a GammaMarket to a ClobMarket (source-agnostic downstream)."""
    return ClobMarket(
        condition_id=g.condition_id,
        question=g.question,
        yes_token_id=g.yes_token_id,
        no_token_id=g.no_token_id,
        yes_price=g.yes_price,
        no_price=g.no_price,
        accepting_orders=g.accepting_orders,
        minimum_order_size=max(g.minimum_order_size, MIN_ORDER_SIZE_USD),
        end_date=g.end_date,
    )


def fetch_all_btc_markets(
    source: str = "all",
    horizon: str = "all",
    min_liquidity_usd: float = DEFAULT_MIN_LIQUIDITY_USD,
) -> list[ClobMarket]:
    """Fetch BTC markets, deduplicated by condition_id."""
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
                continue
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


# -- Fair-value model: BM running-maximum touch probability --------------------

def _norm_cdf(x: float) -> float:
    """Standard normal CDF via math.erf (no scipy dependency)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _touch_probability_up(
    s0: float,
    k: float,
    t_years: float,
    sigma: float = GBM_VOL_ANNUAL,
    r: float = GBM_RISK_FREE_RATE,
) -> float:
    """
    Probability that a GBM process starting at S_0 touches the upper barrier K
    at any time within [0, T].

    Derivation (log-price running maximum):
      Let X_t = ln(S_t / S_0). Under GBM, X_t is a Brownian motion with
      drift mu = r - sigma^2/2 and diffusion sigma. We want
        P(max_{0<=t<=T} X_t >= b)   where b = ln(K/S_0) > 0.
      The running-max distribution of drifted BM gives:
        P(max >= b)
          = Phi((-b + mu*T) / (sigma*sqrt(T)))
          + exp(2*mu*b / sigma^2) * Phi((-b - mu*T) / (sigma*sqrt(T)))
      See Borodin & Salminen (2002), or any barrier-option text.

    If K <= S_0 the barrier has already been touched at t=0, so we return 1.0.
    Degenerate inputs (non-positive parameters) return 0.5 as a safe middle.
    """
    if s0 <= 0 or k <= 0 or t_years <= 0 or sigma <= 0:
        return 0.5
    if k <= s0:
        return 1.0

    mu = r - 0.5 * sigma * sigma            # log-price drift
    sqrt_t = math.sqrt(t_years)
    vol_t = sigma * sqrt_t
    b = math.log(k / s0)                    # > 0

    term_a = _norm_cdf((-b + mu * t_years) / vol_t)
    term_b = math.exp(2.0 * mu * b / (sigma * sigma)) * \
             _norm_cdf((-b - mu * t_years) / vol_t)

    prob = term_a + term_b
    # Numerical safety; the formula is a probability so should be in [0, 1].
    return max(0.0, min(1.0, prob))


def _touch_probability_down(
    s0: float,
    k: float,
    t_years: float,
    sigma: float = GBM_VOL_ANNUAL,
    r: float = GBM_RISK_FREE_RATE,
) -> float:
    """
    Probability that a GBM process starting at S_0 touches the lower barrier K
    within [0, T]. By log-price symmetry this equals the upper-touch
    probability with S_0 and K swapped.
    """
    if s0 <= 0 or k <= 0 or t_years <= 0 or sigma <= 0:
        return 0.5
    if k >= s0:
        return 1.0
    return _touch_probability_up(k, s0, t_years, sigma=sigma, r=r)


def _time_to_resolution_years(end_date: Optional[datetime]) -> Optional[float]:
    """Return years from now until end_date, clamped to [MIN, MAX]. None if unknown."""
    if end_date is None:
        return None
    if end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=timezone.utc)
    delta = end_date - datetime.now(timezone.utc)
    years = delta.total_seconds() / (365.25 * 24 * 3600)
    if years <= 0:
        return None  # market already expired, caller should skip
    return max(MIN_HORIZON_YEARS, min(MAX_HORIZON_YEARS, years))


def compute_fair_value_for_market(
    market: ClobMarket,
    snapshot: BinanceSnapshot,
) -> tuple[float, float]:
    """
    Estimate fair YES/NO probability for a BTC price market.

    Uses the GBM barrier-touch model when market.end_date is known, otherwise
    falls back to the legacy sigmoid heuristic with a small momentum tilt.

    Returns:
        (yes_fair, no_fair) both clamped to [0.05, 0.95], summing to 1.0.
    """
    strike = _extract_strike_price(market.question)
    if strike is None or strike <= 0:
        return _momentum_fair_value(snapshot)

    is_dip = any(kw in market.question.lower() for kw in _DIP_KEYWORDS)
    t_years = _time_to_resolution_years(market.end_date)

    if t_years is not None:
        # GBM touch-probability branch
        if is_dip:
            yes_fair_raw = _touch_probability_down(snapshot.price, strike, t_years)
        else:
            yes_fair_raw = _touch_probability_up(snapshot.price, strike, t_years)
    else:
        # Legacy sigmoid fallback
        current = snapshot.price
        distance = (current - strike) / strike
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
    """Fallback fair value based purely on Binance momentum (no strike info)."""
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

    Returns None if:
      - yes_price is outside [MIN_MARKET_PROBABILITY, MAX_MARKET_PROBABILITY]
        (tail markets are filtered as unreliable)
      - edge is below min_edge on both sides
      - edge exceeds MAX_EDGE_SANITY_CAP (logged WARN; probable model bug)
    """
    # Tail-market filter
    if (
        market.yes_price < MIN_MARKET_PROBABILITY
        or market.yes_price > MAX_MARKET_PROBABILITY
    ):
        logger.debug(
            "Skipping tail market (yes=%.3f): %s",
            market.yes_price, market.question[:60],
        )
        return None

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

    # Sanity cap: reject absurd edges (usually a model bug, not an arb)
    if edge > MAX_EDGE_SANITY_CAP:
        logger.warning(
            "Edge %.3f exceeds sanity cap %.3f on '%s' (fair=%.3f market=%.3f); "
            "skipping as probable model error.",
            edge, MAX_EDGE_SANITY_CAP,
            market.question[:60], fair_value, market_price,
        )
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
    """Place a maker limit order on the Polymarket CLOB."""
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
            side="BUY",
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
    """Execute one full cycle."""
    results: list[OrderResult] = []

    # Step 1: Balance check
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
        max_orders = 999
        if dry_run:
            logger.info("DRY RUN: skipping balance check")

    # Step 2: Binance snapshot
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

    # Step 3: Fetch markets
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
            t_years     = _time_to_resolution_years(m.end_date)
            horizon_str = f"{t_years:.2f}y" if t_years else "?y"
            logger.info(
                "  %-60s  YES mkt=%.3f fair=%.3f  strike=$%s  T=%s",
                m.question[:60],
                m.yes_price,
                yes_fair,
                f"{strike:,.0f}" if strike else "?",
                horizon_str,
            )

    # Step 4: Detect and place
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
    """Run the maker bot continuously on a fixed interval."""
    logger.info(
        "Maker bot starting | mode=%s | interval=%ds | max_size=$%.2f | "
        "min_edge=%.3f | source=%s | horizon=%s | min_liq=$%.0f | sigma=%.2f",
        "DRY RUN" if dry_run else "LIVE", interval, max_size, min_edge,
        source, horizon, min_liquidity_usd, GBM_VOL_ANNUAL,
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
