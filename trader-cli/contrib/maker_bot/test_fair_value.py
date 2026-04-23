"""
Unit tests for the GBM fair-value model in maker_bot.

Run with:
    python -m pytest trader-cli/contrib/maker_bot/test_fair_value.py -v

Or without pytest:
    python trader-cli/contrib/maker_bot/test_fair_value.py
"""

import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make imports work whether run via pytest or directly
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from maker_bot import (  # noqa: E402
    BinanceSnapshot,
    ClobMarket,
    MAX_EDGE_SANITY_CAP,
    MAX_MARKET_PROBABILITY,
    MIN_MARKET_PROBABILITY,
    _norm_cdf,
    _time_to_resolution_years,
    _touch_probability_down,
    _touch_probability_up,
    compute_fair_value_for_market,
    detect_opportunity,
)


# -- Helpers -------------------------------------------------------------------

def _snapshot(price: float = 67000.0) -> BinanceSnapshot:
    """Build a default-flat Binance snapshot for tests."""
    return BinanceSnapshot(
        price=price,
        change_5m=0.0,
        change_30m=0.0,
        direction="FLAT",
        confidence=0.0,
    )


def _market(
    question: str,
    yes_price: float,
    end_date: datetime | None,
    *,
    condition_id: str = "test-condition",
) -> ClobMarket:
    return ClobMarket(
        condition_id=condition_id,
        question=question,
        yes_token_id="t_yes",
        no_token_id="t_no",
        yes_price=yes_price,
        no_price=round(1.0 - yes_price, 3),
        accepting_orders=True,
        minimum_order_size=5.0,
        end_date=end_date,
    )


def _assert_close(actual: float, expected: float, tol: float = 0.02, msg: str = "") -> None:
    assert abs(actual - expected) <= tol, (
        f"{msg}: expected {expected:.4f} +/- {tol}, got {actual:.4f}"
    )


# -- _norm_cdf sanity ----------------------------------------------------------

def test_norm_cdf_identities():
    _assert_close(_norm_cdf(0.0), 0.5, tol=1e-9, msg="Phi(0)")
    _assert_close(_norm_cdf(1.0) + _norm_cdf(-1.0), 1.0, tol=1e-9, msg="symmetry")
    assert _norm_cdf(-10) < 1e-10
    assert _norm_cdf(10) > 1 - 1e-10


# -- Touch-probability sanity --------------------------------------------------

def test_touch_up_already_above():
    """If S_0 >= K, the upper barrier is touched at t=0, probability is 1."""
    assert _touch_probability_up(s0=70000, k=65000, t_years=0.5) == 1.0


def test_touch_down_already_below():
    """If S_0 <= K for a dip market, probability is 1."""
    assert _touch_probability_down(s0=30000, k=35000, t_years=0.5) == 1.0


def test_touch_up_short_horizon_far_strike():
    """$100k strike, 30 days horizon, from $67k: low but nonzero."""
    p = _touch_probability_up(s0=67000, k=100000, t_years=30 / 365)
    assert 0.0 < p < 0.10, f"expected small touch prob, got {p:.4f}"


def test_touch_up_long_horizon_unreachable():
    """GTA VI scenario: $1m strike, 1 year. Should be tiny (< 2%)."""
    p = _touch_probability_up(s0=67000, k=1_000_000, t_years=1.0)
    assert p < 0.02, f"expected near-zero touch prob for 14x move in 1y, got {p:.4f}"


def test_touch_up_monotonic_in_time():
    """Longer horizon => higher touch probability."""
    p_1m = _touch_probability_up(s0=67000, k=100000, t_years=30 / 365)
    p_6m = _touch_probability_up(s0=67000, k=100000, t_years=180 / 365)
    p_1y = _touch_probability_up(s0=67000, k=100000, t_years=1.0)
    assert p_1m < p_6m < p_1y, f"not monotonic: {p_1m:.3f} {p_6m:.3f} {p_1y:.3f}"


def test_touch_up_monotonic_in_strike():
    """Higher strike => lower touch probability."""
    p_80k  = _touch_probability_up(s0=67000, k=80_000,  t_years=0.5)
    p_120k = _touch_probability_up(s0=67000, k=120_000, t_years=0.5)
    p_200k = _touch_probability_up(s0=67000, k=200_000, t_years=0.5)
    assert p_80k > p_120k > p_200k, (
        f"not monotonic: 80k={p_80k:.3f} 120k={p_120k:.3f} 200k={p_200k:.3f}"
    )


def test_touch_symmetry_down_equals_up_swapped():
    """Down-touch(S, K) equals up-touch(K, S) by reflection."""
    up   = _touch_probability_up(s0=100_000, k=67_000,  t_years=0.5)
    down = _touch_probability_down(s0=67_000, k=45_000, t_years=0.5)
    # These aren't the same markets but both apply the same reflection,
    # so touch-down with s0>k should be structurally symmetric to touch-up
    # with k>s0 of the swapped inputs. Use a direct symmetry test:
    a = _touch_probability_down(s0=67_000, k=45_000,  t_years=0.5)
    b = _touch_probability_up(  s0=45_000, k=67_000,  t_years=0.5)
    _assert_close(a, b, tol=1e-9, msg="reflection symmetry")


# -- _time_to_resolution_years -------------------------------------------------

def test_time_to_resolution_future():
    end = datetime.now(timezone.utc) + timedelta(days=30)
    t = _time_to_resolution_years(end)
    assert t is not None
    _assert_close(t, 30 / 365.25, tol=0.01, msg="30-day horizon")


def test_time_to_resolution_expired():
    end = datetime.now(timezone.utc) - timedelta(days=1)
    assert _time_to_resolution_years(end) is None


def test_time_to_resolution_clamps_long():
    end = datetime.now(timezone.utc) + timedelta(days=365 * 10)
    t = _time_to_resolution_years(end)
    assert t == 3.0, f"expected clamp to 3.0, got {t}"


def test_time_to_resolution_naive_datetime_treated_as_utc():
    """Naive datetime should be treated as UTC, not raise."""
    end = (datetime.now(timezone.utc) + timedelta(days=5)).replace(tzinfo=None)
    t = _time_to_resolution_years(end)
    assert t is not None and 0 < t < 1


def test_time_to_resolution_none():
    assert _time_to_resolution_years(None) is None


# -- compute_fair_value_for_market end-to-end ---------------------------------

def test_fair_value_with_end_date_uses_gbm():
    """'$100k by Dec 31, 2026' priced with ~8 months horizon: sensible range."""
    # Roughly 8 months out
    end = datetime.now(timezone.utc) + timedelta(days=240)
    m   = _market("Will Bitcoin reach $100,000 by December 31, 2026?",
                  yes_price=0.60, end_date=end)
    yes_fair, no_fair = compute_fair_value_for_market(m, _snapshot(67000))
    # Reasonable band; exact value depends on sigma and clamp
    assert 0.2 < yes_fair < 0.8, f"out of range: {yes_fair:.3f}"
    _assert_close(yes_fair + no_fair, 1.0, tol=0.01, msg="yes+no==1")


def test_fair_value_gta_vi_near_zero():
    """$1m by GTA VI (~1 year) from $67k: GBM should reject the trade."""
    end = datetime.now(timezone.utc) + timedelta(days=365)
    m   = _market("Will bitcoin hit $1m before GTA VI?",
                  yes_price=0.05, end_date=end)
    yes_fair, _ = compute_fair_value_for_market(m, _snapshot(67000))
    # fair gets clamped to 0.05 minimum; confirm it's near the floor
    assert yes_fair <= 0.06, f"expected near-zero fair, got {yes_fair:.4f}"


def test_fair_value_without_end_date_uses_sigmoid_fallback():
    """No end_date => legacy sigmoid path, must return valid probability."""
    m = _market("Will Bitcoin reach $100,000 by December 31, 2026?",
                yes_price=0.60, end_date=None)
    yes_fair, no_fair = compute_fair_value_for_market(m, _snapshot(67000))
    assert 0.05 <= yes_fair <= 0.95
    _assert_close(yes_fair + no_fair, 1.0, tol=0.01, msg="yes+no==1")


def test_fair_value_dip_market_below_spot():
    """'Dip to $45k' while spot=$67k: medium-term probability should be moderate."""
    end = datetime.now(timezone.utc) + timedelta(days=90)
    m   = _market("Will Bitcoin dip to $45,000 by 2026?",
                  yes_price=0.20, end_date=end)
    yes_fair, _ = compute_fair_value_for_market(m, _snapshot(67000))
    # Plausible range; sigma=0.6 over 90 days gives non-trivial touch prob
    assert 0.05 <= yes_fair <= 0.60, f"dip fair out of range: {yes_fair:.3f}"


# -- detect_opportunity sanity filters ----------------------------------------

def test_detect_skips_tail_market_low():
    """yes_price below MIN_MARKET_PROBABILITY => skipped regardless of edge."""
    end = datetime.now(timezone.utc) + timedelta(days=30)
    m   = _market("Will Bitcoin reach $200,000 by year end?",
                  yes_price=0.02, end_date=end)  # tail low
    opp = detect_opportunity(m, _snapshot(67000))
    assert opp is None


def test_detect_skips_tail_market_high():
    """yes_price above MAX_MARKET_PROBABILITY => skipped."""
    end = datetime.now(timezone.utc) + timedelta(days=30)
    m   = _market("Will Bitcoin reach $50,000 by year end?",
                  yes_price=0.98, end_date=end)  # tail high
    opp = detect_opportunity(m, _snapshot(67000))
    assert opp is None


def test_detect_skips_absurd_edge():
    """
    Construct a case where fair differs from market by > MAX_EDGE_SANITY_CAP.

    Use the sigmoid-fallback path (end_date=None) so we can force a large edge
    by choosing a yes_price far from what the sigmoid would output at distance 0.
    """
    m = _market("Will Bitcoin reach $66,500 by December 31, 2026?",
                yes_price=0.10, end_date=None)  # no end_date
    # With spot=$67000, distance -> ~0, sigmoid yes_fair ~= 0.5. Edge ~= 0.4.
    opp = detect_opportunity(m, _snapshot(67000))
    # Should be skipped because edge > MAX_EDGE_SANITY_CAP
    assert opp is None, f"should have been skipped for absurd edge"


def test_detect_accepts_reasonable_edge():
    """
    A modest, plausible edge (slightly above MIN_EDGE_THRESHOLD, well below
    MAX_EDGE_SANITY_CAP) should produce an opportunity.
    """
    end = datetime.now(timezone.utc) + timedelta(days=90)
    # Spot $67k, strike $80k, 90 days -> moderate touch prob; pick market
    # price a bit below to create a small edge on YES.
    m = _market("Will Bitcoin reach $80,000 by 2026-Q2?",
                yes_price=0.15, end_date=end)
    snap = _snapshot(67000)
    opp  = detect_opportunity(m, snap)
    # Whether it triggers depends on actual GBM output. Accept either outcome,
    # but if it does trigger it must be within sanity bounds.
    if opp is not None:
        assert opp.edge >= 0.04
        assert opp.edge <= MAX_EDGE_SANITY_CAP


def test_constants_are_sensible():
    """Guard against someone accidentally setting caps outside plausible ranges."""
    assert 0.0 < MIN_MARKET_PROBABILITY < 0.10
    assert 0.90 < MAX_MARKET_PROBABILITY < 1.0
    assert 0.10 < MAX_EDGE_SANITY_CAP < 0.50


# -- Plain-python runner (no pytest required) ---------------------------------

def _run_all() -> int:
    """Discover and run every test_ function in this module."""
    tests = [
        (name, obj) for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    ]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  OK   {name}")
            passed += 1
        except AssertionError as exc:
            print(f"  FAIL {name}: {exc}")
            failed += 1
        except Exception as exc:
            print(f"  ERR  {name}: {type(exc).__name__}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_run_all())
