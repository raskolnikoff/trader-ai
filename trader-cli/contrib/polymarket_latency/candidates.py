#!/usr/bin/env python3
"""
Latency candidate selector.

Reads data/latency.jsonl and identifies markets that show consistent,
repeatable latency — meaning they lag behind Binance moves reliably.

This is a filtering tool for observation only. It does NOT recommend trades.

Usage:
    python candidates.py
    trader latency candidates
"""

import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Optional

# Reuse the shared log-reading logic from the sibling analyze module
from contrib.polymarket_latency.analyze import load_records, LATENCY_LOG_PATH

# ── Filter thresholds ──────────────────────────────────────────────────────────
MIN_AVG_LATENCY = 2.0   # seconds — below this the lag is probably noise
MIN_EVENT_COUNT = 5     # need enough observations to trust the average
MIN_P95_LATENCY = 3.0   # tail must also be elevated (not just occasional spikes)
TOP_N_CANDIDATES = 10   # how many markets to show


# ── Data containers ────────────────────────────────────────────────────────────

class MarketStats:
    """Aggregated latency statistics for a single market."""

    def __init__(self, market_id: str, question: str, latencies: list[float]) -> None:
        self.market_id = market_id
        self.question = question
        self.latencies = sorted(latencies)

    @property
    def count(self) -> int:
        return len(self.latencies)

    @property
    def avg_latency(self) -> float:
        return round(statistics.mean(self.latencies), 2)

    @property
    def p95(self) -> float:
        """Nearest-rank 95th percentile."""
        index = max(0, int(len(self.latencies) * 0.95) - 1)
        return round(self.latencies[index], 2)

    @property
    def score(self) -> float:
        """
        Composite score that rewards both high average latency and
        a larger observation count. log(count + 1) prevents a market
        seen once from outranking one seen many times.
        """
        return round(self.avg_latency * math.log(self.count + 1), 4)

    def passes_filter(self) -> bool:
        return (
            self.avg_latency >= MIN_AVG_LATENCY
            and self.count >= MIN_EVENT_COUNT
            and self.p95 >= MIN_P95_LATENCY
        )


# ── Aggregation ────────────────────────────────────────────────────────────────

def aggregate_by_market(records: list[dict]) -> list[MarketStats]:
    """
    Group raw records by market_id and build a MarketStats object for each.
    Records missing market_id or a valid latency float are skipped.
    """
    latencies_by_id: dict[str, list[float]] = defaultdict(list)
    question_by_id: dict[str, str] = {}

    for record in records:
        market_id = record.get("market_id")
        if not market_id:
            continue

        try:
            latency = float(record["latency"])
        except (KeyError, ValueError, TypeError):
            continue

        latencies_by_id[market_id].append(latency)
        # Keep the first question seen for this market_id
        if market_id not in question_by_id:
            question_by_id[market_id] = record.get("question", "")

    return [
        MarketStats(
            market_id=market_id,
            question=question_by_id.get(market_id, ""),
            latencies=latencies,
        )
        for market_id, latencies in latencies_by_id.items()
    ]


# ── Selection ──────────────────────────────────────────────────────────────────

def select_candidates(
    market_stats: list[MarketStats],
    top_n: int = TOP_N_CANDIDATES,
) -> list[MarketStats]:
    """
    Filter markets that meet all three thresholds, then sort by score descending.
    Returns the top_n results.
    """
    qualified = [market for market in market_stats if market.passes_filter()]
    qualified.sort(key=lambda m: m.score, reverse=True)
    return qualified[:top_n]


# ── Formatting ─────────────────────────────────────────────────────────────────

def format_candidates(candidates: list[MarketStats]) -> str:
    """Build a human-readable report string from the selected candidates."""
    lines = ["🎯 Candidate Markets"]

    if not candidates:
        lines.append("")
        lines.append("  該当するマーケットがありません。")
        lines.append(f"  フィルター基準: avg ≥ {MIN_AVG_LATENCY}s, "
                     f"events ≥ {MIN_EVENT_COUNT}, p95 ≥ {MIN_P95_LATENCY}s")
        lines.append("")
        lines.append("  Tip: 'trader latency scan' を実行してデータを増やしてください。")
        return "\n".join(lines)

    for market in candidates:
        short_id = market.market_id[:10]
        question = market.question[:70]
        if len(market.question) > 70:
            question += "…"

        lines.append("")
        lines.append(f"[{short_id}]")
        if question:
            lines.append(f"  {question}")
        lines.append(
            f"  score: {market.score}  |  "
            f"avg: {market.avg_latency}s  |  "
            f"events: {market.count}  |  "
            f"p95: {market.p95}s"
        )

    return "\n".join(lines)


# ── Public entry point ─────────────────────────────────────────────────────────

def run_candidates(log_path: Path = LATENCY_LOG_PATH) -> str:
    """
    Load the JSONL log, apply filters and scoring, return the formatted report.
    Never raises — any internal error produces a graceful message.
    """
    try:
        records = load_records(log_path)
    except Exception as exc:
        return f"❌ ログファイルの読み込みに失敗しました: {exc}"

    market_stats = aggregate_by_market(records)
    candidates = select_candidates(market_stats)
    return format_candidates(candidates)


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(run_candidates())

