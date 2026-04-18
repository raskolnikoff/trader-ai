#!/usr/bin/env python3
"""
Wallet trade monitor for copy trading.

Polls a list of watched wallets at a configurable interval and prints
an alert whenever a new trade is detected. Designed for manual copy
trading: you see the alert and place the same trade yourself on Polymarket.

Bot filter (new in this version):
    Detects and suppresses automated/bot trading patterns that are not
    useful for copy trading:

    1. Burst filter: 5+ trades from the same wallet at the exact same
       timestamp -> bot liquidation/redemption, skip the burst.

    2. Min size filter: trades below MIN_TRADE_SIZE (default $2) are
       dust or test trades, not worth copying.

    3. Near-certainty filter: trades at price >= 0.97 are already
       resolved markets being swept, not actionable signals.

    Filtered alerts are counted and summarized at the end of each poll
    cycle so you know how much noise was removed.

Usage:
    python monitor.py
    python monitor.py --interval 30
    python monitor.py --min-size 10        # only alert on $10+ trades
    python monitor.py --no-bot-filter      # disable all filters (debug)
    python monitor.py --add 0xABC...
    python monitor.py --scan-leaderboard

Requires:
    No extra dependencies (stdlib only).
    leaderboard.py and wallet_scorer.py must be in the same directory.
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import urllib.request

# -- Constants -----------------------------------------------------------------

DATA_API_BASE    = "https://data-api.polymarket.com"
REQUEST_TIMEOUT  = 10

_PROJECT_ROOT    = Path(__file__).parent.parent.parent.parent
WATCHED_PATH     = _PROJECT_ROOT / "data" / "watched_wallets.json"
SEEN_TRADES_PATH = _PROJECT_ROOT / "data" / "seen_trades.json"

DEFAULT_INTERVAL  = 60
DEFAULT_LIMIT     = 50     # fetch more trades per poll to catch bursts
LEADERBOARD_TOP_N = 30

# Bot filter defaults
BURST_THRESHOLD   = 5      # N+ trades at same timestamp = bot burst
MIN_TRADE_SIZE    = 2.0    # USD -- skip dust trades
MAX_CERTAIN_PRICE = 0.97   # skip near-resolved markets (price >= this)


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
    title: str
    event_slug: str
    outcome: str
    size: float
    price: float
    timestamp: float
    filtered: bool = False        # True if suppressed by bot filter
    filter_reason: str = ""

    @property
    def link(self) -> str:
        if self.event_slug:
            return f"https://polymarket.com/event/{self.event_slug}"
        return ""


@dataclass
class FilterStats:
    """Tracks how many alerts were filtered per poll cycle."""
    burst: int = 0
    min_size: int = 0
    near_certain: int = 0

    @property
    def total(self) -> int:
        return self.burst + self.min_size + self.near_certain

    def summary(self) -> str:
        if self.total == 0:
            return ""
        parts = []
        if self.burst:
            parts.append(f"burst={self.burst}")
        if self.min_size:
            parts.append(f"min_size={self.min_size}")
        if self.near_certain:
            parts.append(f"near_certain={self.near_certain}")
        return f"[filtered {self.total}: {', '.join(parts)}]"


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


# -- Bot filter ----------------------------------------------------------------

def apply_bot_filter(
    alerts: list[TradeAlert],
    burst_threshold: int   = BURST_THRESHOLD,
    min_size: float        = MIN_TRADE_SIZE,
    max_certain: float     = MAX_CERTAIN_PRICE,
) -> tuple[list[TradeAlert], FilterStats]:
    """
    Apply three bot-detection heuristics to a list of new alerts.

    Returns (clean_alerts, stats) where clean_alerts contains only
    human-actionable signals and stats summarises what was filtered.

    Filter 1 — Burst detection:
        Group alerts by (wallet_address, timestamp). If a wallet fires
        BURST_THRESHOLD or more trades at the exact same second, the
        entire group is marked as a bot burst and suppressed.
        Rationale: balthazar/my-macbook pattern (20x $50 @ 0.999 same ts).

    Filter 2 — Minimum size:
        Trades below min_size USD are dust or test trades.
        Rationale: asfdt7 pattern ($0.01, $0.03, $0.06 trades).

    Filter 3 — Near-certainty price:
        Trades at price >= max_certain are already resolved markets
        being swept by bots at near-zero risk. Not actionable.
        Rationale: balthazar No @ 0.999 pattern.
    """
    stats = FilterStats()

    # Pass 1: identify burst timestamps per wallet
    ts_count: dict[tuple[str, float], int] = defaultdict(int)
    for alert in alerts:
        ts_count[(alert.wallet_address, alert.timestamp)] += 1

    burst_keys = {
        key for key, count in ts_count.items()
        if count >= burst_threshold
    }

    clean: list[TradeAlert] = []
    for alert in alerts:
        # Burst check
        if (alert.wallet_address, alert.timestamp) in burst_keys:
            alert.filtered      = True
            alert.filter_reason = f"burst ({ts_count[(alert.wallet_address, alert.timestamp)]} trades @ same ts)"
            stats.burst += 1
            continue

        # Min size check
        if alert.size < min_size:
            alert.filtered      = True
            alert.filter_reason = f"size too small (${alert.size:.2f} < ${min_size})"
            stats.min_size += 1
            continue

        # Near-certainty check
        if alert.price >= max_certain:
            alert.filtered      = True
            alert.filter_reason = f"near-certain price ({alert.price:.3f} >= {max_certain})"
            stats.near_certain += 1
            continue

        clean.append(alert)

    return clean, stats


# -- Trade polling -------------------------------------------------------------

def poll_wallet(address: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
    url  = f"{DATA_API_BASE}/trades?user={address}&limit={limit}"
    data = _fetch_json(url)
    if data is None:
        return []
    return data if isinstance(data, list) else data.get("data", [])


def detect_new_trades(
    wallet: WatchedWallet,
    seen: set[str],
) -> tuple[list[TradeAlert], set[str]]:
    """Fetch trades, dedup against seen set, return new TradeAlert list."""
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

        title      = trade.get("title") or "Unknown market"
        event_slug = trade.get("eventSlug") or ""
        outcome    = trade.get("outcome") or trade.get("side", "")

        new_alerts.append(TradeAlert(
            wallet_address=wallet.address,
            wallet_label=wallet.label or wallet.address[:10],
            tx_hash=tx,
            title=title,
            event_slug=event_slug,
            outcome=outcome,
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
    lines = [
        f"\n[ALERT] {alert.wallet_label} ({alert.wallet_address[:10]}...)  @ {ts_str}",
        f"  Market : {alert.title[:80]}",
        f"  Side   : {alert.outcome}",
        f"  Size   : ${alert.size:,.2f}  @  {alert.price:.3f}",
    ]
    if alert.link:
        lines.append(f"  Link   : {alert.link}")
    return "\n".join(lines)


# -- Monitor loop --------------------------------------------------------------

def run_monitor(
    wallets: list[WatchedWallet],
    interval: int    = DEFAULT_INTERVAL,
    bot_filter: bool = True,
    min_size: float  = MIN_TRADE_SIZE,
) -> None:
    if not wallets:
        print("[monitor] No wallets to watch. Use --add 0x... to add wallets.")
        return

    seen = load_seen()
    print(f"[monitor] Watching {len(wallets)} wallets  (poll every {interval}s)")
    print(f"          bot_filter={bot_filter}  min_size=${min_size}")
    for w in wallets:
        print(f"  {w.label or '(no label)':20}  {w.address}")
    print("  Ctrl+C to stop\n")

    while True:
        all_new: list[TradeAlert] = []

        for wallet in wallets:
            alerts, seen = detect_new_trades(wallet, seen)
            all_new.extend(alerts)

        if bot_filter and all_new:
            clean, stats = apply_bot_filter(all_new, min_size=min_size)
            for alert in clean:
                print(format_alert(alert))
            summary = stats.summary()
            if summary:
                print(f"  {summary}")
        else:
            for alert in all_new:
                print(format_alert(alert))

        save_seen(seen)
        time.sleep(interval)


# -- Entry point ---------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Monitor Polymarket wallets for new trades (with bot filter)"
    )
    parser.add_argument("--wallets", default=str(WATCHED_PATH))
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help=f"Poll interval seconds (default: {DEFAULT_INTERVAL})")
    parser.add_argument("--min-size", type=float, default=MIN_TRADE_SIZE,
                        help=f"Min trade size USD to alert (default: {MIN_TRADE_SIZE})")
    parser.add_argument("--no-bot-filter", dest="bot_filter", action="store_false",
                        default=True,
                        help="Disable bot filter (show all trades including bursts)")
    parser.add_argument("--add", metavar="ADDRESS",
                        help="Add a wallet to the watch list and exit")
    parser.add_argument("--label", default="", help="Label for --add")
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
    run_monitor(
        wallets,
        interval=args.interval,
        bot_filter=args.bot_filter,
        min_size=args.min_size,
    )


if __name__ == "__main__":
    main()
