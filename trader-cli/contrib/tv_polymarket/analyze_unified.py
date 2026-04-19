#!/usr/bin/env python3
"""
Unified analysis CLI: TradingView + Binance + Polymarket -> Claude.

Collects data from all three sources, builds a unified prompt,
and sends it to Claude CLI for analysis.

Usage:
    python analyze_unified.py \"What is BTC doing?\"
    python analyze_unified.py \"Is SPX overextended?\" --symbol SPX
    python analyze_unified.py \"Analyze\" --verbose

Or via trader.sh:
    ./scripts/trader.sh unified \"What is BTC doing?\"

Requires:
    - TradingView running with CDP port 9222 (optional, degrades gracefully)
    - Claude CLI installed and authenticated
    - Internet for Binance + Polymarket APIs
"""

import argparse
import asyncio
import sys
from pathlib import Path

_HERE = Path(__file__).parent
_CLI  = _HERE.parent.parent
_ROOT = _CLI.parent
for _p in [str(_CLI), str(_ROOT)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from contrib.tv_polymarket.signal_integrator import collect_signal, format_signal_context
from contrib.tv_polymarket.unified_prompt import build_unified_prompt
from claude_client import query_claude
from db import Database
from rag import retrieve_relevant


def run_analysis(
    query: str,
    symbol: str  = "BTCUSDT",
    use_rag: bool = True,
    verbose: bool = False,
) -> str:
    """Full pipeline: collect -> prompt -> Claude -> store."""

    print(f"[unified] Collecting signals for {symbol}...")
    ctx = asyncio.run(collect_signal(symbol))

    if verbose:
        print(format_signal_context(ctx))
        print()

    # RAG: retrieve relevant past analyses
    relevant_messages = []
    recent_messages   = []
    if use_rag:
        try:
            db = Database()
            relevant_messages = retrieve_relevant(db, query, symbol=symbol, limit=3)
            recent_messages   = db.get_recent_messages(limit=4)
        except Exception as exc:
            print(f"  [rag] skipped: {exc}")

    # Build unified prompt
    prompt = build_unified_prompt(
        ctx,
        query=query,
        relevant_messages=relevant_messages,
        recent_messages=recent_messages,
    )

    # Send to Claude CLI
    print("[unified] Sending to Claude...")
    response = query_claude(prompt)

    if not response:
        return "[unified] No response from Claude."

    # Store in DB for future RAG
    try:
        db = Database()
        db.save_message(role="user",      content=query,    symbol=symbol)
        db.save_message(role="assistant", content=response, symbol=symbol)
    except Exception:
        pass

    return response


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Unified TV + Binance + Polymarket analysis via Claude"
    )
    parser.add_argument("query", nargs="?",
                        default="Analyze the current market situation.",
                        help="Question or analysis request")
    parser.add_argument("--symbol", default="BTCUSDT",
                        help="Symbol to analyze (default: BTCUSDT)")
    parser.add_argument("--no-rag", dest="use_rag", action="store_false",
                        default=True,
                        help="Skip RAG memory retrieval")
    parser.add_argument("--verbose", action="store_true",
                        help="Print collected signal context before analysis")
    args = parser.parse_args()

    response = run_analysis(
        query=args.query,
        symbol=args.symbol,
        use_rag=args.use_rag,
        verbose=args.verbose,
    )
    print(response)


if __name__ == "__main__":
    main()
