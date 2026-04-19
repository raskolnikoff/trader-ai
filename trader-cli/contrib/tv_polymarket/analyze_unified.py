#!/usr/bin/env python3
"""
Unified analysis CLI: TradingView + Binance + Polymarket -> Claude.

Usage:
    python analyze_unified.py "What is BTC doing?"
    python analyze_unified.py "Is SPX overextended?" --symbol SPX
    python analyze_unified.py "Analyze" --verbose
    python analyze_unified.py "Analyze" --no-rag
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
from db import initialize_db, save_message, get_recent_messages, save_analysis_result
from prompts import extract_summary
from rag import retrieve_relevant_messages_for_symbol


def run_analysis(
    query: str,
    symbol: str   = "BTCUSDT",
    use_rag: bool = True,
    verbose: bool = False,
) -> str:
    """Full pipeline: collect -> RAG -> prompt -> Claude -> store."""

    print(f"[unified] Collecting signals for {symbol}...")
    ctx = asyncio.run(collect_signal(symbol))

    if verbose:
        print(format_signal_context(ctx))
        print()

    # Infer timeframe from TV context if available
    timeframe = None
    if ctx.tv and ctx.tv.get("timeframe"):
        timeframe = ctx.tv["timeframe"]

    relevant_messages = []
    recent_messages   = []
    if use_rag:
        try:
            initialize_db()
            # Phase 2: symbol-aware retrieval
            relevant_messages = retrieve_relevant_messages_for_symbol(
                query,
                symbol=symbol,
                timeframe=timeframe,
                limit=4,
            )
            recent_messages = get_recent_messages(limit=4)
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

    # Store conversation in DB
    try:
        initialize_db()
        save_message(role="user",      content=query,    symbol=symbol, timeframe=timeframe)
        save_message(role="assistant", content=response, symbol=symbol, timeframe=timeframe)
    except Exception:
        pass

    # Store distilled summary in analysis_results for high-priority RAG recall
    try:
        summary = extract_summary(response)
        if summary and summary != "(empty response)":
            save_analysis_result(summary=summary, symbol=symbol, timeframe=timeframe)
    except Exception:
        pass

    return response


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Unified TV + Binance + Polymarket analysis via Claude"
    )
    parser.add_argument("query", nargs="?",
                        default="Analyze the current market situation.")
    parser.add_argument("--symbol", default="BTCUSDT",
                        help="Symbol to analyze (default: BTCUSDT)")
    parser.add_argument("--no-rag", dest="use_rag", action="store_false", default=True,
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
