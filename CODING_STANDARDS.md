# Coding Standards

This document defines the coding conventions for trader-ai.
These rules apply to all Python code in this repository.
The goal: any engineer (or AI) can read, maintain, and extend the code
without asking the original author.

---

## 1. Single Responsibility

Each function does exactly one thing.
If you need "and" to describe what a function does, split it.

```python
# Bad
def fetch_and_process_and_save(symbol: str) -> None: ...

# Good
def fetch_market_data(symbol: str) -> MarketData: ...
def process_market_data(data: MarketData) -> Signal: ...
def save_signal(signal: Signal) -> None: ...
```

---

## 2. Type Hints — Required Everywhere

All function signatures must have type hints.
Use `Optional[X]` instead of `X | None` for Python < 3.10 compat.

```python
# Bad
def score(wallet, trades, min_size):
    ...

# Good
def score(wallet: str, trades: list[dict], min_size: float) -> float:
    ...
```

---

## 3. Docstrings — Required for Public Functions

Every public function needs a one-line summary.
Add Args/Returns only when the signature alone is not self-explanatory.

```python
def apply_bot_filter(
    alerts: list[TradeAlert],
    burst_threshold: int = BURST_THRESHOLD,
) -> tuple[list[TradeAlert], FilterStats]:
    """
    Remove bot-generated trade alerts using three heuristics.

    Args:
        alerts: Raw alerts from detect_new_trades().
        burst_threshold: Min trades at same timestamp to count as a burst.

    Returns:
        (clean_alerts, stats) where clean_alerts excludes bot patterns.
    """
```

---

## 4. No Magic Numbers

Every numeric constant must be named.

```python
# Bad
if trade["size"] < 2.0:
    skip()

# Good
MIN_TRADE_SIZE_USD: float = 2.0

if trade["size"] < MIN_TRADE_SIZE_USD:
    skip()
```

---

## 5. Pure Core, Side Effects at the Edges

Business logic functions must be pure (no I/O, no network, no DB).
I/O lives in the outermost layer only.

```python
# Bad — logic mixed with I/O
def score_wallet(address: str) -> float:
    trades = requests.get(f"/trades?user={address}").json()  # side effect inside logic
    return sum(t["size"] for t in trades)

# Good — separate concerns
def fetch_trades(address: str) -> list[dict]:        # I/O layer
    return requests.get(f"/trades?user={address}").json()

def score_trades(trades: list[dict]) -> float:       # pure logic
    return sum(t["size"] for t in trades)
```

---

## 6. Dry-Run Mode — Required for All Bots

Any script that writes data, places orders, or sends requests
must support `--dry-run`. Dry-run logs what would happen without doing it.

```python
def place_order(order: Order, dry_run: bool = True) -> Optional[str]:
    """
    Place a limit order on Polymarket CLOB.

    Args:
        order: The order to place.
        dry_run: If True, log the order but do not submit it.

    Returns:
        Order ID if placed, None if dry_run=True or on error.
    """
    if dry_run:
        logger.info("[DRY RUN] Would place order: %s", order)
        return None
    return _submit_order(order)
```

---

## 7. Logging — Use logging, Not print

All operational output uses `logging`. `print` is only for CLI help text.

```python
import logging

logger = logging.getLogger(__name__)

logger.info("Order placed: %s", order_id)
logger.warning("Skipping burst wallet: %s", address)
logger.error("CLOB API error: %s", exc)
```

---

## 8. Comments in English

All inline comments and docstrings are in English.
Variable names, function names, and log messages are in English.

---

## 9. Error Handling — Never Swallow Exceptions Silently

```python
# Bad
try:
    result = risky_call()
except Exception:
    pass  # silent failure

# Good
try:
    result = risky_call()
except Exception as exc:
    logger.error("risky_call failed: %s", exc)
    raise  # or return a safe default with a log
```

---

## 10. Dataclasses for Structured Data

Use `@dataclass` instead of raw dicts for any structured object
that is passed between functions.

```python
# Bad
order = {"symbol": "BTC", "side": "YES", "price": 0.45, "size": 10.0}

# Good
@dataclass
class Order:
    symbol: str
    side: str       # "YES" or "NO"
    price: float    # 0.0 – 1.0
    size: float     # USD
    market_id: str
```

---

## 11. File Structure Convention

```
module.py
├── Constants (ALL_CAPS)
├── Dataclasses / TypedDicts
├── Pure helper functions (prefixed with _)
├── Public API functions
└── main() / CLI entry point
```

---

## 12. .env for Secrets — Never Hardcode

```python
# Bad
API_KEY = "019dae89-..."

# Good
import os
API_KEY = os.environ["POLY_API_KEY"]
```

All required env vars must be documented in `.env.example`.
