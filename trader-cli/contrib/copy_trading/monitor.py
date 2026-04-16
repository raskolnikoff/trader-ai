#!/usr/bin/env python3
"""
Wallet trade monitor for copy trading.

Polls a list of watched wallets at a configurable interval and prints
an alert whenever a new trade is detected. Designed for manual copy
trading: you see the alert and place the same trade yourself on Polymarket.

Watched wallets are loaded from a JSON file (default: data/watched_wallets.json).
New trades are deduplicated via a seen-txhash set persisted in data/seen_trades.json.

Usage:
    python monitor.py
    python monitor.py --wallets data/watched_wallets.json --interval 30
    python monitor.py --add 0xABC...          # add a wallet and exit
    python monitor.py --scan-leaderboard      # auto-populate from leaderboard

Alert format (printed to stdout):
    [ALERT] <username> (<address[:8]>) opened a NEW position
      Market : <question>
      Side   : YES / NO
      Size   : $<USDC>
      Price  : <entry price>
      Link   : https://polymarket.com/event/<slug>

Requires:
    No extra dependencies (stdlib only).
    leaderboard.py and wallet_scorer.py in the same package for --scan-leaderboard.
"""

import argparse
import json
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# -- Constants -----------------------------------------------------------------

DATA_API_BASE  = "https://data-api.polymarket.com"
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
REQUEST_TIMEOUT = 10

_PROJECT_ROOT      = Path(__file__).parent.parent.parent.parent
WATCHED_PATH       = _PROJECT_ROOT / "data" / "watched_wallets.json"
SEEN_TRADES_PATH   = _PROJECT_ROOT / "data" / "seen_trades.json"

DEFAULT_INTERVAL   = 60    # seconds between polls
DEFAULT_LIMIT      = 20    # trades to fetch per wallet per poll
LEADERBOARD_TOP_N  = 30    # wallets to pull when --scan-leaderboard


# -- Data containers -----------------------------------------------------------

@dataclass
class WatchedWallet:
    address: str
    label: str = ""     # human-readable name / tag


@dataclass
class TradeAlert:
    wallet_address: str
    wallet_label: str
    tx_hash: str
    market_question: str
    market_slug: str
    side: str           # "YES" | "NO"
    size: float
    price: float
    timestamp: float


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
        print(f"  [http] {exc}")
        return None


# -- Persistence helpers -------------------------------------------------------

def load_watched(path: Path = WATCHED_PATH) -> list[WatchedWallet]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [
            WatchedWallet(address=w["address"], label=w.get("label", ""))
            for w in raw
            if isinstance(w, dict) and w.get("address")
        ]
    except Exception:
        return []


def save_watched(wallets: list[WatchedWallet], path: Path = WATCHED_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([{"address": w.address, "label": w.label} for w in wallets], indent=2),
        encoding="utf-8",
    )


def load_seen(path: Path = SEEN_TRADES_PATH) -> set[str]:
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return set()


def save_seen(seen: set[str], path: Path = SEEN_TRADES_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep only the last 10,000 hashes to avoid unbounded growth
    trimmed = list(seen)[-10_000:]
    path.write_text(json.dumps(trimmed), encoding="utf-8")


# -- Market metadata -----------------------------------------------------------

_market_cache: dict[str, dict] = {}


def fetch_market(condition_id: str) -> dict:
    """Fetch market metadata from Gamma API. Cached per condition_id."""
    if condition_id in _market_cache:
        return _market_cache[condition_id]
    url  = f"{GAMMA_API_BASE}/markets?conditionId={condition_id}"
    data = _fetch_json(url)
    if data is None:
        return {}
    rows = data if isinstance(data, list) else data.get("data", [])
    meta = rows[0] if rows else {}
    _market_cache[condition_id] = meta
    return meta


# -- Trade polling -------------------------------------------------------------

def poll_wallet(address: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
    """Fetch the most recent trades for a wallet."""
    url  = f"{DATA_API_BASE}/trades?maker={address}&limit={limit}"
    data = _fetch_json(url)
    if data is None:
        return []
    return data if isinstance(data, list) else data.get("data", [])


def detect_new_trades(
    wallet: WatchedWallet,
    seen: set[str],
) -> tuple[list[TradeAlert], set[str]]:
    """
    Poll wallet, compare against seen set, return new TradeAlerts.
    Returns (new_alerts, updated_seen_set).
    """
    raw_trades = poll_wallet(wallet.address)
    new_alerts: list[TradeAlert] = []

    for trade in raw_trades:
        if not isinstance(trade, dict):
            continue

        # Deduplicate by transaction hash or a composite key
        tx = (
            trade.get("transactionHash")
            or trade.get("txHash")
            or f"{wallet.address}:{trade.get('timestamp','')}:{trade.get('size','')}"
        )
        if tx in seen:
            continue
        seen.add(tx)

        # Parse fields
        try:
            size  = float(trade.get("size", 0) or 0)
            price = float(trade.get("price", 0) or 0)
            ts    = float(trade.get("timestamp") or trade.get("created_at") or 0)
        except (TypeError, ValueError):
            continue

        if size <= 0:
            continue

        # Only alert on BUY-side entries (opening positions)
        side_raw = trade.get("side", "").upper()
        outcome  = trade.get("outcome", trade.get("side", ""))

        cond_id  = trade.get("conditionId") or trade.get("market_id", "")
        market   = fetch_market(cond_id) if cond_id else {}
        question = market.get("question") or cond_id[:30] or "Unknown market"
        slug     = market.get("slug") or ""

        new_alerts.append(TradeAlert(
            wallet_address=wallet.address,
            wallet_label=wallet.label or wallet.address[:10],
            tx_hash=tx,
            market_question=question,
            market_slug=slug,
            side=outcome or side_raw,
            size=size,
            price=price,
            timestamp=ts,
        ))

    return new_alerts, seen


# -- Alert formatting ----------------------------------------------------------

def format_alert(alert: TradeAlert) -> str:
    ts_str = datetime.fromtimestamp(alert.timestamp, tz=timezone.utc).strftime("%H:%M:%S UTC") \
             if alert.timestamp else "unknown time"
    link   = f"https://polymarket.com/event/{alert.market_slug}" if alert.market_slug else ""
    lines  = [
        f"\n[ALERT] {alert.wallet_label} ({alert.wallet_address[:10]}...)  @ {ts_str}",
        f"  Market : {alert.market_question[:80]}",
        f"  Side   : {alert.side}",
        f"  Size   : ${alert.size:,.2f}  @  {alert.price:.3f}",
    ]
    if link:
        lines.append(f"  Link   : {link}")
    return "\n".join(lines)


# -- Main monitor loop ---------------------------------------------------------

def run_monitor(
    wallets: list[WatchedWallet],
    interval: int = DEFAULT_INTERVAL,
) -> None:
    if not wallets:
        print("[monitor] No wallets to watch. Add wallets with --add 0x...")
        return

    seen = load_seen()

    print(f"[monitor] Watching {len(wallets)} wallets  (poll every {interval}s)")
    for w in wallets:
        print(f"  {w.label or '(no label)':20}  {w.address}")
    print("  Ctrl+C to stop\n")

    while True:
        for wallet in wallets:
            alerts, seen = detect_new_trades(wallet, seen)
            for alert in alerts:
                print(format_alert(alert))

        save_seen(seen)
        time.sleep(interval)


# -- Entry point ---------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Monitor Polymarket wallets for new trades (copy trading alerts)"
    )
    parser.add_argument(
        "--wallets", default=str(WATCHED_PATH),
        help=f"Path to watched_wallets.json [default: {WATCHED_PATH}]",
    )
    parser.add_argument(
        "--interval", type=int, default=DEFAULT_INTERVAL,
        help=f"Poll interval in seconds [default: {DEFAULT_INTERVAL}]",
    )
    parser.add_argument(
        "--add", metavar="ADDRESS",
        help="Add a wallet address to the watch list and exit",
    )
    parser.add_argument(
        "--label", default="",
        help="Label for --add (e.g. 'bot_A')",
    )
    parser.add_argument(
        "--scan-leaderboard", action="store_true",
        help=f"Auto-populate watch list from top {LEADERBOARD_TOP_N} leaderboard wallets",
    )
    args = parser.parse_args()

    watched_path = Path(args.wallets)

    # -- Add a wallet and exit -------------------------------------------------
    if args.add:
        wallets = load_watched(watched_path)
        existing = {w.address for w in wallets}
        if args.add in existing:
            print(f"[monitor] {args.add} is already in the watch list.")
        else:
            wallets.append(WatchedWallet(address=args.add, label=args.label))
            save_watched(wallets, watched_path)
            print(f"[monitor] Added {args.add} ({args.label or 'no label'}).")
            print(f"          Watch list now has {len(wallets)} wallets.")
        return

    # -- Scan leaderboard and populate -----------------------------------------
    if args.scan_leaderboard:
        from contrib.copy_trading.leaderboard import fetch_leaderboard
        from contrib.copy_trading.wallet_scorer import evaluate_wallet

        print(f"[monitor] Fetching top {LEADERBOARD_TOP_N} wallets from leaderboard...")
        entries  = fetch_leaderboard(period="7d", limit=LEADERBOARD_TOP_N)
        existing = {w.address for w in load_watched(watched_path)}
        added    = 0

        for entry in entries:
            if entry.proxy_address in existing:
                continue
            score = evaluate_wallet(entry.proxy_address)
            if not score.disqualified:
                existing.add(entry.proxy_address)
                wallets = load_watched(watched_path)
                wallets.append(WatchedWallet(
                    address=entry.proxy_address,
                    label=entry.username[:20],
                ))
                save_watched(wallets, watched_path)
                added += 1
                print(f"  [+] {entry.username[:20]:<20}  score={score.composite:.3f}")
            else:
                print(f"  [-] {entry.username[:20]:<20}  {score.disqualify_reason}")

        print(f"\n[monitor] Added {added} candidate wallets.")
        return

    # -- Normal monitor loop ---------------------------------------------------
    wallets = load_watched(watched_path)
    run_monitor(wallets, interval=args.interval)


if __name__ == "__main__":
    main()
