#!/usr/bin/env python3
"""
Latency log analyzer.

Reads data/latency.jsonl (written by detector.py) and prints summary statistics.
This is a read-only, offline tool — it never fetches live data.

Usage:
    python analyze.py
    trader latency analyze
"""

import json
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Resolves to <project-root>/data/latency.jsonl, same anchor as detector.py
_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
LATENCY_LOG_PATH = _PROJECT_ROOT / "data" / "latency.jsonl"


# ── Data loading ───────────────────────────────────────────────────────────────

def load_records(log_path: Path = LATENCY_LOG_PATH) -> list[dict]:
    """
    Read all valid JSON lines from the log file.
    Silently skips missing file, empty lines, and malformed JSON.
    Returns a list of raw dicts.
    """
    if not log_path.exists():
        return []

    records = []
    with log_path.open(encoding="utf-8") as log_file:
        for line_number, raw_line in enumerate(log_file, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                if isinstance(record, dict):
                    records.append(record)
            except json.JSONDecodeError:
                # Corrupted line — skip silently
                pass

    return records


# ── Statistic helpers ──────────────────────────────────────────────────────────

def _safe_float(value) -> Optional[float]:
    """Return float or None — never raises."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _percentile(sorted_values: list[float], pct: float) -> float:
    """
    Return the value at the given percentile (0–100) from a pre-sorted list.
    Uses the nearest-rank method so no interpolation is needed.
    """
    if not sorted_values:
        return 0.0
    index = max(0, int(len(sorted_values) * pct / 100) - 1)
    return sorted_values[index]


def _parse_ts(ts_string: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp string into an aware datetime, or None."""
    try:
        # Python 3.11+ handles Z; for 3.10 compatibility replace it
        return datetime.fromisoformat(ts_string.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


# ── Core metrics ───────────────────────────────────────────────────────────────

def compute_basic_stats(latencies: list[float]) -> dict:
    """
    Return count, mean, p50, p95, and max for a list of latency values.
    All float results are rounded to 2 decimal places.
    """
    if not latencies:
        return {"count": 0, "mean": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}

    sorted_lat = sorted(latencies)
    return {
        "count": len(sorted_lat),
        "mean": round(statistics.mean(sorted_lat), 2),
        "p50": round(statistics.median(sorted_lat), 2),
        "p95": round(_percentile(sorted_lat, 95), 2),
        "max": round(max(sorted_lat), 2),
    }


def compute_direction_means(records: list[dict]) -> dict:
    """
    Compute mean latency grouped by direction ("up" / "down").
    Unknown directions are collected under "other".
    Returns a dict mapping direction → mean latency (or None if no data).
    """
    buckets: dict[str, list[float]] = defaultdict(list)

    for record in records:
        latency = _safe_float(record.get("latency"))
        direction = record.get("direction", "other")
        if latency is not None and isinstance(direction, str):
            buckets[direction].append(latency)

    result = {}
    for direction in ("up", "down"):
        values = buckets.get(direction, [])
        result[direction] = round(statistics.mean(values), 2) if values else None

    return result


def compute_top_markets(records: list[dict], top_n: int = 5) -> list[dict]:
    """
    Group records by market_id and compute average latency per market.
    Returns the top_n markets sorted by average latency descending.
    Each entry: {"market_id": str, "question": str, "avg_latency": float, "count": int}
    """
    latencies_by_id: dict[str, list[float]] = defaultdict(list)
    question_by_id: dict[str, str] = {}

    for record in records:
        market_id = record.get("market_id")
        latency = _safe_float(record.get("latency"))
        if not market_id or latency is None:
            continue

        latencies_by_id[market_id].append(latency)
        # Keep the most recent question seen for this market_id
        if market_id not in question_by_id:
            question_by_id[market_id] = record.get("question", "")

    markets = []
    for market_id, values in latencies_by_id.items():
        markets.append({
            "market_id": market_id,
            "question": question_by_id.get(market_id, ""),
            "avg_latency": round(statistics.mean(values), 2),
            "count": len(values),
        })

    markets.sort(key=lambda m: m["avg_latency"], reverse=True)
    return markets[:top_n]


def compute_events_per_hour(records: list[dict]) -> Optional[float]:
    """
    Estimate how many events occur per hour based on the timestamp span.
    Returns None if there are fewer than 2 distinct timestamps.
    """
    timestamps = []
    for record in records:
        ts_string = record.get("ts")
        if ts_string:
            parsed = _parse_ts(ts_string)
            if parsed:
                timestamps.append(parsed)

    if len(timestamps) < 2:
        return None

    earliest = min(timestamps)
    latest = max(timestamps)
    span_seconds = (latest - earliest).total_seconds()

    if span_seconds <= 0:
        return None

    span_hours = span_seconds / 3600.0
    return round(len(records) / span_hours, 1)


# ── Formatting / output ────────────────────────────────────────────────────────

def format_report(
    basic: dict,
    direction_means: dict,
    top_markets: list[dict],
    events_per_hour: Optional[float],
) -> str:
    """
    Build the full report string.
    Returns a single multi-line string ready to be printed.
    """
    lines = []

    # ── Basic stats ────────────────────────────────────────────────────────────
    lines.append("📊 Latency Summary")
    if basic["count"] == 0:
        lines.append("  No events recorded yet.")
        lines.append("  Run:  trader latency scan")
        return "\n".join(lines)

    lines.append(f"Events: {basic['count']}")
    lines.append(f"Mean:   {basic['mean']}s")
    lines.append(f"p50:    {basic['p50']}s")
    lines.append(f"p95:    {basic['p95']}s")
    lines.append(f"Max:    {basic['max']}s")

    # ── Direction breakdown ────────────────────────────────────────────────────
    lines.append("")
    lines.append("📈 Direction")
    mean_up = direction_means.get("up")
    mean_down = direction_means.get("down")
    lines.append(f"UP:    {mean_up}s" if mean_up is not None else "UP:    —")
    lines.append(f"DOWN:  {mean_down}s" if mean_down is not None else "DOWN:  —")

    # ── Top lagging markets ────────────────────────────────────────────────────
    lines.append("")
    lines.append("⚡ Top Lagging Markets")
    if top_markets:
        for market in top_markets:
            short_id = market["market_id"][:10]
            avg = market["avg_latency"]
            count = market["count"]
            lines.append(f"  [{short_id}]  avg {avg}s  ({count} events)")
    else:
        lines.append("  No market data available.")

    # ── Event frequency ────────────────────────────────────────────────────────
    lines.append("")
    lines.append("⏱ Frequency")
    if events_per_hour is not None:
        lines.append(f"Events/hour: {events_per_hour}")
    else:
        lines.append("Events/hour: — (need at least 2 events with different timestamps)")

    return "\n".join(lines)


# ── Public entry point ─────────────────────────────────────────────────────────

def run_analysis(log_path: Path = LATENCY_LOG_PATH) -> str:
    """
    Load the JSONL log, compute all metrics, and return the formatted report.
    Never raises — any internal error produces a graceful message.
    """
    try:
        records = load_records(log_path)
    except Exception as exc:
        return f"❌ ログファイルの読み込みに失敗しました: {exc}"

    latencies = [
        v for record in records
        if (v := _safe_float(record.get("latency"))) is not None
    ]

    basic = compute_basic_stats(latencies)
    direction_means = compute_direction_means(records)
    top_markets = compute_top_markets(records)
    freq = compute_events_per_hour(records)

    return format_report(basic, direction_means, top_markets, freq)


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(run_analysis())

