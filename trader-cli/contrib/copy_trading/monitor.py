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
    leaderboard.py and wallet_scorer.py must be in the same directory.
"""

import argparse
import json
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# -- Constants -----------------------------------------------------------------

DATA_API_BASE  = "https://data-api.polymarket.com"
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
REQUEST_TIMEOUT = 10

_PROJECT_ROOT    = Path(__file__).parent.parent.parent.parent
WATCHED_PATH     = _PROJECT_ROOT / "data" / "watched_wallets.json"
SEEN_TRADES_PATH = _PROJECT_ROOT / "data" / "seen_trades.json"

DEFAULT_INTERVAL  = 60    # seconds between polls
DEFAULT_LIMIT     = 20    # trades to fetch per wallet per poll
LEADERBOARD_TOP_N = 30    # wallets to pull when --scan-leaderboard


# -- Data containers -----------------------------------------------------------

@dataclass
class WatchedWallet:
    address: str
    label: str = ""


@dataclass
class TradeAlert:
    wallet_address: str
    wallet_label: str
    tx_hash: str
    market_question: str
    market_slug: str
    side: str
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


# -- Persistence ---------------------------------------------------------------

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
    path.write_text(json.dumps(list(seen)[-10_000:]), encoding="utf-8")


# -- Market metadata cache -----------------------------------------------------

_market_cache: dict[str, dict] = {}


def fetch_market(condition_id: str) -> dict:
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
    """Fetch most recent trades for a wallet via Data API /trades."""
    url  = f"{DATA_API_BASE}/trades?user={address}&limit={limit}"
    data = _fetch_json(url)
    if data is None:
        return []
    return data if isinstance(data, list) else data.get("data", [])


def detect_new_trades(
    wallet: WatchedWallet,
    seen: set[str],
) -> tuple[list[TradeAlert], set[str]]:
    raw_trades = poll_wallet(wallet.address)
    new_alerts: list[TradeAlert] = []

    for trade in raw_trades:
        if not isinstance(trade, dict):
            continue

        tx = (
            trade.get("transactionHash")
            or trade.get("txHash")
            or f"{wallet.address}:{trade.get('timestamp','')}:{trade.get('size','')}"
        )
        if tx in seen:
            continue
        seen.add(tx)

        try:
            size  = float(trade.get("size", 0) or 0)
            price = float(trade.get("price", 0) or 0)
            ts    = float(trade.get("timestamp") or 0)
        except (TypeError, ValueError):
            continue

        if size <= 0:
            continue

        outcome  = trade.get("outcome") or trade.get("side", "")
        cond_id  = trade.get("conditionId") or ""
        market   = fetch_market(cond_id) if cond_id else {}
        question = market.get("question") or trade.get("title") or cond_id[:30] or "Unknown"
        slug     = market.get("slug") or trade.get("eventSlug") or trade.get("slug") or ""

        new_alerts.append(TradeAlert(
            wallet_address=wallet.address,
            wallet_label=wallet.label or wallet.address[:10],
            tx_hash=tx,
            market_question=question,
            market_slug=slug,
            side=outcome,
            size=size,
            price=price,
            timestamp=ts,
        ))

    return new_alerts, seen


# -- Alert formatting ----------------------------------------------------------

def format_alert(alert: TradeAlert) -> str:
    ts_str = (
        datetime.fromtimestamp(alert.timestamp, tz=timezone.utc).strftime("%H:%M:%S UTC")
        if alert.timestamp else "unknown time"
    )
    link  = f"https://polymarket.com/event/{alert.market_slug}" if alert.market_slug else ""
    lines = [
        f"\n[ALERT] {alert.wallet_label} ({alert.wallet_address[:10]}...)  @ {ts_str}",
        f"  Market : {alert.market_question[:80]}",
        f"  Side   : {alert.side}",
        f"  Size   : ${alert.size:,.2f}  @  {alert.price:.3f}",
    ]
    if link:
        lines.append(f"  Link   : {link}")
    return "\n".join(lines)


# -- Monitor loop --------------------------------------------------------------

def run_monitor(wallets: list[WatchedWallet], interval: int = DEFAULT_INTERVAL) -> None:
    if not wallets:
        print("[monitor] No wallets to watch. Use --add 0x... to add wallets.")
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
        description="Monitor Polymarket wallets for new trades"
    )
    parser.add_argument("--wallets", default=str(WATCHED_PATH))
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    parser.add_argument("--add", metavar="ADDRESS",
                        help="Add a wallet to the watch list and exit")
    parser.add_argument("--label", default="",
                        help="Label for --add")
    parser.add_argument("--scan-leaderboard", action="store_true",
                        help="Auto-populate from top traders (leaderboard.py)")
    args = parser.parse_args()

    watched_path = Path(args.wallets)

    if args.add:
        wallets  = load_watched(watched_path)
        existing = {w.address for w in wallets}
        if args.add in existing:
            print(f"[monitor] {args.add} is already in the watch list.")
        else:
            wallets.append(WatchedWallet(address=args.add, label=args.label))
            save_watched(wallets, watched_path)
            print(f"[monitor] Added {args.add} (label: {args.label or 'none'}).")
            print(f"          Watch list: {len(wallets)} wallet(s).")
        return

    if args.scan_leaderboard:
        # Add this file's directory to sys.path for sibling imports
        _here = Path(__file__).parent
        if str(_here) not in sys.path:
            sys.path.insert(0, str(_here))

        from leaderboard import fetch_top_traders
        from wallet_scorer import evaluate_wallet

        print(f"[monitor] Scanning leaderboard for top {LEADERBOARD_TOP_N} traders...")
        entries  = fetch_top_traders(market_limit=20, holders_limit=10, top_n=LEADERBOARD_TOP_N)
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

        print(f"\n[monitor] Added {added} candidate wallet(s).")
        return

    wallets = load_watched(watched_path)
    run_monitor(wallets, interval=args.interval)


if __name__ == "__main__":
    main()
