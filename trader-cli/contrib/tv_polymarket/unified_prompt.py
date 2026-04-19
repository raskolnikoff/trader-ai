#!/usr/bin/env python3
"""
Unified prompt builder for TradingView + Binance + Polymarket integration.

Takes a SignalContext and assembles a structured prompt for Claude.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Optional

_HERE = Path(__file__).parent
_CLI  = _HERE.parent.parent
_ROOT = _CLI.parent
for _p in [str(_CLI), str(_ROOT)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from contrib.tv_polymarket.signal_integrator import SignalContext, collect_signal


# -- System instructions -------------------------------------------------------

_UNIFIED_INSTRUCTIONS = """\
You are a professional trading assistant with access to three synchronized data sources:
  1. TradingView chart data (technical analysis)
  2. Binance real-time price feed (market momentum)
  3. Polymarket prediction markets (crowd probability consensus)

Analyze ALL three sources together. Look for:
  - ALIGNMENT: when technicals, momentum, and prediction market odds all agree -> stronger signal
  - DIVERGENCE: when they disagree -> flag explicitly and explain why
  - POLYMARKET EDGE: if prediction market odds seem mispriced vs technical reality -> note it

Respond strictly in this format. Do not add sections.
Do NOT use any tools or external APIs. Analyze only the text data provided below.

📊 Status
(Objective description combining TV chart + Binance momentum)

🎲 Polymarket Consensus
(What the prediction markets are pricing, and whether it aligns with technicals)

💡 Assessment
(Buy / Sell / Neutral — one sentence)

🧠 Rationale (up to 3 points)
(Specific reasons referencing all three sources where relevant)

⚠️ Caution
(Key risks, especially if the three sources diverge)

🎯 Key levels & opportunities
(Technical levels + any Polymarket markets worth watching)\
"""


# -- Formatters ----------------------------------------------------------------

def _fmt_tv(tv: Optional[dict[str, Any]]) -> str:
    if not tv:
        return "TradingView: not connected"
    lines = []
    symbol    = tv.get("symbol") or "?"
    timeframe = tv.get("timeframe") or "?"
    lines.append(f"Symbol: {symbol}  |  Timeframe: {timeframe}")
    if tv.get("quote"):
        lines.append(f"Quote: {json.dumps(tv['quote'], ensure_ascii=False)}")
    if tv.get("ohlcv_summary"):
        lines.append(f"OHLCV: {json.dumps(tv['ohlcv_summary'], ensure_ascii=False)}")
    if tv.get("indicators"):
        lines.append(f"Indicators: {json.dumps(tv['indicators'], ensure_ascii=False)}")
    if tv.get("pine_lines"):
        lines.append(f"Pine Lines: {json.dumps(tv['pine_lines'], ensure_ascii=False)}")
    if len(lines) == 1:
        lines.append("(No live data returned)")
    return "\n".join(lines)


def _fmt_binance(binance) -> str:
    if not binance:
        return "Binance: not reachable"
    lines = [f"BTC Price: ${binance.price:,.2f}"]
    if binance.change_5m is not None:
        d = "▲" if binance.change_5m > 0 else "▼"
        lines.append(f"5m:  {d} {binance.change_5m:+.3f}%")
    if binance.change_30m is not None:
        d = "▲" if binance.change_30m > 0 else "▼"
        lines.append(f"30m: {d} {binance.change_30m:+.3f}%")
    return "\n".join(lines)


def _fmt_polymarket(markets) -> str:
    if not markets:
        return "No relevant Polymarket markets found."
    lines = []
    for m in markets:
        if m.implied_probability is not None:
            prob = f"{m.implied_probability}% YES"
        else:
            # odds not available in this feed -- note it but don't hide the market
            prob = "odds N/A"
        vol  = f"${m.volume:,.0f}" if m.volume else "n/a"
        lines.append(f"  [{prob:>12}]  vol={vol:>10}  {m.question[:75]}")
        lines.append(f"               {m.link}")
    return "\n".join(lines)


def _fmt_wallet_alerts(alerts: list[dict]) -> str:
    if not alerts:
        return "None"
    lines = []
    for a in alerts[-5:]:
        lines.append(
            f"  {a.get('direction','?'):4}  "
            f"latency={a.get('latency_ms', a.get('latency','?'))}  "
            f"{a.get('question', a.get('market_id',''))[:60]}"
        )
    return "\n".join(lines)


def _fmt_messages(msgs: list[dict]) -> str:
    if not msgs:
        return "None"
    parts = []
    for m in msgs:
        role    = "User" if m.get("role") == "user" else "Assistant"
        ts      = m.get("created_at", "")[:19]
        content = m.get("content", "")[:400]
        sym     = f" [{m['symbol']}]" if m.get("symbol") else ""
        parts.append(f"[{ts}]{sym} {role}: {content}")
    return "\n".join(parts)


# -- Public API ----------------------------------------------------------------

def build_unified_prompt(
    ctx: SignalContext,
    query: str = "Analyze the current market situation.",
    relevant_messages: Optional[list[dict]] = None,
    recent_messages: Optional[list[dict]] = None,
) -> str:
    relevant_messages = relevant_messages or []
    recent_messages   = recent_messages or []

    body = (
        f"[TradingView Data]\n{_fmt_tv(ctx.tv)}\n\n"
        f"---\n\n"
        f"[Binance Feed]\n{_fmt_binance(ctx.binance)}\n\n"
        f"---\n\n"
        f"[Polymarket Odds]\n{_fmt_polymarket(ctx.polymarket_markets)}\n\n"
        f"---\n\n"
        f"[Wallet Alerts (recent)]\n{_fmt_wallet_alerts(ctx.wallet_alerts)}\n\n"
        f"---\n\n"
        f"[Relevant Memory]\n{_fmt_messages(relevant_messages)}\n\n"
        f"---\n\n"
        f"[Recent Conversation]\n{_fmt_messages(recent_messages)}\n\n"
        f"---\n\n"
        f"[User Query]\n{query}"
    )
    return f"{_UNIFIED_INSTRUCTIONS}\n\n---\n\n{body}"


# -- Entry point ---------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build unified TV + Binance + Polymarket prompt (preview)"
    )
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--query", default="Analyze the current market situation.")
    args = parser.parse_args()

    ctx    = asyncio.run(collect_signal(args.symbol))
    prompt = build_unified_prompt(ctx, query=args.query)
    print(prompt)


if __name__ == "__main__":
    main()
