#!/usr/bin/env python3
"""
Polymarket CLOB WebSocket subscriber.

Subscribes to real-time order book updates for specific market tokens
directly from Polymarket's CLOB WebSocket endpoint.

Use this as a drop-in replacement for REST polling inside ws_detector.py
once you have identified which markets to watch via 'trader latency candidates'.
Sub-second reaction detection vs ~1.5s REST polling.

Usage (standalone exploration):
    python polymarket_ws.py --token-id <TOKEN_ID>
    python polymarket_ws.py --token-id <TOKEN_ID> --duration 60

How to get a token_id:
    1. trader latency candidates           # find lagging market_ids
    2. curl 'https://clob.polymarket.com/markets/<market_id>'
    3. copy a tokenID from the response tokens array

Requires:
    pip install 'websockets>=12.0'
"""

import argparse
import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

try:
    import websockets
except ImportError:
    raise SystemExit("websockets not installed. Run: pip install 'websockets>=12.0'")

# Polymarket CLOB WebSocket -- docs: https://docs.polymarket.com/#websocket-channels
CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


# -- Data container ------------------------------------------------------------

@dataclass
class BookUpdate:
    token_id: str
    ts: float             # local time.time() when received
    ts_server: str        # server-side timestamp string (if present)
    best_bid: Optional[float]
    best_ask: Optional[float]
    mid: Optional[float]
    event_type: str       # "book" | "price_change" | "tick_size_change" | ...

    @classmethod
    def from_msg(cls, msg: dict, token_id: str) -> "BookUpdate":
        now = time.time()
        bids = msg.get("bids", [])
        asks = msg.get("asks", [])
        best_bid = max(
            (float(b["price"]) for b in bids if "price" in b), default=None
        )
        best_ask = min(
            (float(a["price"]) for a in asks if "price" in a), default=None
        )
        mid = (
            (best_bid + best_ask) / 2.0
            if best_bid is not None and best_ask is not None
            else None
        )
        return cls(
            token_id=token_id,
            ts=now,
            ts_server=msg.get("timestamp", ""),
            best_bid=best_bid,
            best_ask=best_ask,
            mid=mid,
            event_type=msg.get("event_type", "unknown"),
        )


# -- Subscription helpers ------------------------------------------------------

def build_subscribe_msg(token_ids: list[str]) -> str:
    """Build the Polymarket CLOB subscription payload."""
    return json.dumps({"assets_ids": token_ids, "type": "market"})


# -- Main subscriber -----------------------------------------------------------

async def subscribe(
    token_ids: list[str],
    on_update,                    # async callable(BookUpdate)
    duration: Optional[float] = None,
) -> None:
    """
    Connect to Polymarket CLOB WebSocket and stream order book updates.

    Args:
        token_ids: Polymarket token IDs to subscribe to.
        on_update: Async callback invoked with each BookUpdate.
        duration:  Stop after N seconds. None = run until Ctrl+C.
    """
    sub_msg  = build_subscribe_msg(token_ids)
    start_ts = time.time()

    print(f"[poly-ws] Connecting to {CLOB_WS_URL}")
    print(f"[poly-ws] Tokens: {token_ids}")

    async for ws in websockets.connect(
        CLOB_WS_URL,
        ping_interval=20,
        additional_headers={"User-Agent": "trader-ai/1.0"},
    ):
        try:
            await ws.send(sub_msg)
            print("[poly-ws] Subscribed. Streaming...\n")

            async for raw in ws:
                if duration and (time.time() - start_ts) > duration:
                    print("[poly-ws] Duration reached.")
                    return

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                # Server may send a list or a single object
                events = msg if isinstance(msg, list) else [msg]
                for event in events:
                    if not isinstance(event, dict):
                        continue
                    asset_id = event.get("asset_id", "")
                    if asset_id not in token_ids:
                        continue
                    await on_update(BookUpdate.from_msg(event, token_id=asset_id))

        except websockets.ConnectionClosed as exc:
            print(f"[poly-ws] closed ({exc.code}), reconnecting...")
            await asyncio.sleep(2)
        except Exception as exc:
            print(f"[poly-ws] error: {exc}")
            await asyncio.sleep(2)


# -- Default CLI handler -------------------------------------------------------

async def _print_update(update: BookUpdate) -> None:
    ts_str  = datetime.fromtimestamp(update.ts, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]
    mid_str = f"{update.mid:.4f}" if update.mid is not None else "--"
    bid_str = f"{update.best_bid:.4f}" if update.best_bid is not None else "--"
    ask_str = f"{update.best_ask:.4f}" if update.best_ask is not None else "--"
    print(f"[{ts_str}]  {update.event_type:<18}  bid={bid_str}  ask={ask_str}  mid={mid_str}")


# -- Entry point ---------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stream Polymarket CLOB order book via WebSocket"
    )
    parser.add_argument(
        "--token-id", dest="token_ids", action="append", required=True,
        metavar="TOKEN_ID",
        help="Token ID to subscribe to (repeatable for multiple markets)",
    )
    parser.add_argument(
        "--duration", type=float, default=None,
        help="Stop after N seconds (default: run until Ctrl+C)",
    )
    args = parser.parse_args()

    try:
        asyncio.run(subscribe(
            token_ids=args.token_ids,
            on_update=_print_update,
            duration=args.duration,
        ))
    except KeyboardInterrupt:
        print("\n[stopped]")


if __name__ == "__main__":
    main()
