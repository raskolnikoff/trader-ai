#!/usr/bin/env python3
"""
Unified analysis CLI: TradingView + Binance + Polymarket -> Claude.

Usage:
    python analyze_unified.py "What is BTC doing?"
    python analyze_unified.py "Is SPX overextended?" --symbol SPX
    python analyze_unified.py "Analyze" --verbose
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
from claude_client import ask_claude
# db.py is function-based (no class) -- import functions directly
from db import initialize_db, save_message, get_recent_messages
# rag.py function name is retrieve_relevant_messages
from rag import retrieve_relevant_messages


def run_analysis(
    query: str,
    symbol: str   = "BTCUSDT",
    use_rag: bool = True,
    verbose: bool = False,
) -> str:
    """Full pipeline: collect -> prompt -> Claude -> store."""

    print(f"[unified] Collecting signals for {symbol}...")
    ctx = asyncio.run(collect_signal(symbol))

    if verbose:
        print(format_signal_context(ctx))
        print()

    relevant_messages = []
    recent_messages   = []
    if use_rag:
        try:
            initialize_db()
            relevant_messages = retrieve_relevant_messages(query, limit=3)
            recent_messages   = get_recent_messages(limit=4)
        except Exception as exc:
            print(f"  [rag] skipped: {exc}")

    prompt = build_unified_prompt(
        ctx,
        query=query,
        relevant_messages=relevant_messages,
        recent_messages=recent_messages,
    )

    print("[unified] Sending to Claude...")
    try:
        response = ask_claude(prompt)
    except RuntimeError as exc:
        return f"[unified] Claude error: {exc}"

    if not response:
        return "[unified] No response from Claude."

    # Store in DB for future RAG
    try:
        initialize_db()
        save_message(role="user",      content=query,    symbol=symbol)
        save_message(role="assistant", content=response, symbol=symbol)
    except Exception:
        pass

    return response


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Unified TV + Binance + Polymarket analysis via Claude"
    )
    parser.add_argument("query", nargs="?",
                        default="Analyze the current market situation.")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--no-rag", dest="use_rag", action="store_false", default=True)
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
