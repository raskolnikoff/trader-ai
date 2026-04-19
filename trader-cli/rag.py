"""
Retrieval-Augmented Generation (RAG) helper.

Retrieval strategy (Phase 2 — symbol-aware):
  1. Collect candidates from four sources, tagged with origin + symbol match.
  2. De-duplicate by message id.
  3. Score each candidate with a multi-factor scorer.
  4. Return the top N highest-scoring unique candidates.

Scoring factors:
  Source priority base:
    analysis_results (symbol match)  -> +4.0
    analysis_results (any symbol)    -> +3.0
    fts_hits (symbol match)          -> +3.0
    fts_hits (any)                   -> +2.0
    recent (symbol match)            -> +2.0
    recent (any)                     -> +1.5

  Keyword scoring (per keyword, against content):
    exact token match  -> +2.0
    substring match    -> +1.0

  Symbol match bonus:  +1.5  (message.symbol == query symbol)
  Symbol mismatch:     -0.5  (message has a different symbol)
  Timeframe match:     +0.5  (message.timeframe == query timeframe)
  Recency bonus:       +0.0 to +0.5 (linear across candidate pool)

FTS5 sanitization:
  Special chars (?, *, ", (, ), ^, ~, -) are stripped before FTS query.
  Fallback to recency-only if sanitized query is empty.
"""

import re
from typing import Optional

from db import (
    get_recent_messages,
    get_recent_messages_by_symbol,
    get_recent_analysis_results,
    get_analysis_results_by_symbol,
    search_messages,
    search_messages_by_symbol,
)

# -- Constants -----------------------------------------------------------------

_PRIORITY_ANALYSIS_SYMBOL  = 4.0
_PRIORITY_ANALYSIS_ANY     = 3.0
_PRIORITY_FTS_SYMBOL       = 3.0
_PRIORITY_FTS_ANY          = 2.0
_PRIORITY_RECENT_SYMBOL    = 2.0
_PRIORITY_RECENT_ANY       = 1.5

_BONUS_SYMBOL_MATCH        = 1.5
_PENALTY_SYMBOL_MISMATCH   = 0.5
_BONUS_TIMEFRAME_MATCH     = 0.5
_MAX_RECENCY_BONUS         = 0.5

_FTS_SAFE_RE = re.compile(r"[^a-zA-Z0-9 ]")


# -- Helpers -------------------------------------------------------------------

def _sanitize_fts_query(query: str) -> str:
    safe = _FTS_SAFE_RE.sub(" ", query)
    return " ".join(safe.split())


def _score(
    candidate: dict,
    keywords: list[str],
    source_priority: float,
    recency_rank: int,
    total: int,
    target_symbol: Optional[str] = None,
    target_timeframe: Optional[str] = None,
) -> float:
    content   = candidate.get("content", "").lower()
    msg_sym   = (candidate.get("symbol") or "").upper()
    msg_tf    = (candidate.get("timeframe") or "").upper()
    score     = source_priority

    # Keyword scoring
    for kw in keywords:
        kw_lower = kw.lower()
        if kw_lower in content:
            score += 2.0 if f" {kw_lower} " in f" {content} " else 1.0

    # Symbol bonus / penalty
    if target_symbol:
        target_sym = target_symbol.upper()
        if msg_sym == target_sym:
            score += _BONUS_SYMBOL_MATCH
        elif msg_sym and msg_sym != target_sym:
            score -= _PENALTY_SYMBOL_MISMATCH

    # Timeframe bonus
    if target_timeframe and msg_tf and msg_tf == target_timeframe.upper():
        score += _BONUS_TIMEFRAME_MATCH

    # Recency bonus (most recent = max bonus)
    if total > 0:
        score += (total - recency_rank) / total * _MAX_RECENCY_BONUS

    return score


def _analysis_to_msg(row: dict) -> dict:
    """Convert analysis_result row to pseudo-message dict."""
    return {
        "id":         f"ar_{row['id']}",
        "role":       "assistant",
        "content":    row["summary"],
        "symbol":     row.get("symbol"),
        "timeframe":  row.get("timeframe"),
        "created_at": row["created_at"],
    }


# -- Public API ----------------------------------------------------------------

def retrieve_relevant_messages(query: str, limit: int = 3) -> list[dict]:
    """
    Symbol-agnostic retrieval. Uses global pool without symbol filtering.
    Compatible with existing callers.
    """
    return retrieve_relevant_messages_for_symbol(query, symbol=None, limit=limit)


def retrieve_relevant_messages_for_symbol(
    query: str,
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
    limit: int = 3,
) -> list[dict]:
    """
    Symbol-aware RAG retrieval (Phase 2).

    Builds a candidate pool from four sources:
      1. analysis_results filtered by symbol  (highest priority)
      2. analysis_results global              (fallback)
      3. FTS5 search filtered by symbol
      4. FTS5 search global
      5. Recent messages filtered by symbol
      6. Recent messages global

    Applies multi-factor scoring and returns top-limit candidates.
    """
    keywords  = [w for w in query.split() if len(w) >= 2]
    fts_query = _sanitize_fts_query(query)

    # ── Build tagged candidate pool ────────────────────────────────────────

    tagged: list[tuple[dict, float]] = []
    seen_ids: set = set()

    def _add(msg: dict, priority: float) -> None:
        mid = msg.get("id")
        if mid not in seen_ids:
            seen_ids.add(mid)
            tagged.append((msg, priority))

    # 1. analysis_results by symbol
    if symbol:
        for row in get_analysis_results_by_symbol(symbol, limit=5):
            _add(_analysis_to_msg(row), _PRIORITY_ANALYSIS_SYMBOL)

    # 2. analysis_results global (top by recency)
    for row in get_recent_analysis_results(limit=5):
        _add(_analysis_to_msg(row), _PRIORITY_ANALYSIS_ANY)

    # 3. FTS by symbol
    if fts_query and keywords and symbol:
        try:
            for msg in search_messages_by_symbol(fts_query, symbol, limit=8):
                _add(msg, _PRIORITY_FTS_SYMBOL)
        except Exception:
            pass

    # 4. FTS global
    if fts_query and keywords:
        try:
            for msg in search_messages(fts_query, limit=8):
                _add(msg, _PRIORITY_FTS_ANY)
        except Exception:
            pass

    # 5. Recent by symbol
    if symbol:
        for msg in get_recent_messages_by_symbol(symbol, limit=8):
            _add(msg, _PRIORITY_RECENT_SYMBOL)

    # 6. Recent global
    for msg in get_recent_messages(limit=8):
        _add(msg, _PRIORITY_RECENT_ANY)

    if not tagged:
        return []

    # ── Score and rank ─────────────────────────────────────────────────────
    total  = len(tagged)
    scored = [
        (
            msg,
            _score(msg, keywords, priority, rank, total,
                   target_symbol=symbol, target_timeframe=timeframe),
        )
        for rank, (msg, priority) in enumerate(tagged)
    ]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return [msg for msg, _ in scored[:limit]]
