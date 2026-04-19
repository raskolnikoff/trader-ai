"""
Retrieval-Augmented Generation (RAG) helper.

Retrieval strategy:
  1. Collect candidates from three sources, tagged with their origin.
  2. De-duplicate by message id.
  3. Score each candidate — source priority + keyword match + recency bonus.
  4. Return the top N highest-scoring unique candidates.

Source priority:
  analysis_results  -> +3.0  (distilled past conclusions)
  fts_hits          -> +2.0  (keyword-matched raw messages)
  recent            -> +1.5  (recency pool, no keyword guarantee)

Keyword scoring (per matching keyword):
  +2.0 for exact token match
  +1.0 for substring match

Recency bonus: +0.0 to +0.5 linearly distributed across the pool.

FTS5 query sanitization:
  SQLite FTS5 treats many characters as special (?, *, ", (, ), ^, ~, -).
  We sanitize the query before passing to search_messages() to avoid
  "fts5: syntax error near X" crashes. Characters outside [a-zA-Z0-9 ] are
  stripped from the FTS query; original query is still used for keyword scoring.
"""

import re

from db import get_recent_messages, get_recent_analysis_results, search_messages

# -- Constants -----------------------------------------------------------------

_PRIORITY_ANALYSIS = 3.0
_PRIORITY_FTS      = 2.0
_PRIORITY_RECENT   = 1.5

# FTS5 safe: keep only alphanumeric and spaces
_FTS_SAFE_RE = re.compile(r"[^a-zA-Z0-9 ]")


# -- Helpers -------------------------------------------------------------------

def _sanitize_fts_query(query: str) -> str:
    """
    Strip FTS5 special characters to avoid syntax errors.
    Returns empty string if nothing remains (caller should skip FTS).
    """
    safe = _FTS_SAFE_RE.sub(" ", query)
    # Collapse multiple spaces and strip
    safe = " ".join(safe.split())
    return safe


def _score(
    candidate: dict,
    keywords: list[str],
    source_priority: float,
    recency_rank: int,
    total: int,
) -> float:
    content = candidate.get("content", "").lower()
    score   = source_priority

    for keyword in keywords:
        kw = keyword.lower()
        if kw in content:
            score += 2.0 if f" {kw} " in f" {content} " else 1.0

    recency_bonus = ((total - recency_rank) / total * 0.5) if total > 0 else 0.0
    score += recency_bonus
    return score


# -- Public API ----------------------------------------------------------------

def retrieve_relevant_messages(query: str, limit: int = 3) -> list[dict]:
    """
    Return the top-limit most relevant past messages for the given query.

    Candidate pool:
      - analysis_results summaries (up to 3)  -- priority +3.0
      - FTS5 keyword matches (up to 5)         -- priority +2.0
      - Recent messages (last 5)               -- priority +1.5

    FTS query is sanitized to prevent SQLite FTS5 syntax errors.
    If the sanitized query is empty, FTS lookup is skipped gracefully.
    """
    keywords    = [w for w in query.split() if len(w) >= 2]
    fts_query   = _sanitize_fts_query(query)

    # analysis_results -> pseudo-message dicts
    analysis_candidates: list[tuple[dict, float]] = [
        (
            {
                "id":         f"ar_{row['id']}",
                "role":       "assistant",
                "content":    row["summary"],
                "symbol":     row.get("symbol"),
                "timeframe":  row.get("timeframe"),
                "created_at": row["created_at"],
            },
            _PRIORITY_ANALYSIS,
        )
        for row in get_recent_analysis_results(limit=3)
    ]

    # FTS5 search (only if sanitized query is non-empty)
    fts_hits: list[dict] = []
    if fts_query and keywords:
        try:
            fts_hits = search_messages(fts_query, limit=5)
        except Exception:
            # FTS errors are non-fatal -- degrade to recency-only
            fts_hits = []

    fts_candidates: list[tuple[dict, float]] = [
        (msg, _PRIORITY_FTS) for msg in fts_hits
    ]

    recent_candidates: list[tuple[dict, float]] = [
        (msg, _PRIORITY_RECENT) for msg in get_recent_messages(limit=5)
    ]

    # Merge and dedup by id
    seen_ids:   set                     = set()
    candidates: list[tuple[dict, float]] = []
    for msg, priority in analysis_candidates + fts_candidates + recent_candidates:
        msg_id = msg.get("id")
        if msg_id not in seen_ids:
            seen_ids.add(msg_id)
            candidates.append((msg, priority))

    if not candidates:
        return []

    # Score and rank
    total  = len(candidates)
    scored = [
        (msg, _score(msg, keywords, priority, rank, total))
        for rank, (msg, priority) in enumerate(candidates)
    ]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return [msg for msg, _ in scored[:limit]]


# -- Symbol-aware retrieval (for tv_polymarket integration) --------------------

def retrieve_relevant_messages_for_symbol(
    query: str,
    symbol: str,
    limit: int = 3,
) -> list[dict]:
    """
    Variant that boosts messages matching the given symbol.
    Falls back to retrieve_relevant_messages if no symbol-specific results.
    """
    results = retrieve_relevant_messages(query, limit=limit * 2)

    # Boost symbol-matching messages
    symbol_upper = symbol.upper()
    boosted = [m for m in results if (m.get("symbol") or "").upper() == symbol_upper]
    others  = [m for m in results if (m.get("symbol") or "").upper() != symbol_upper]

    combined = (boosted + others)[:limit]
    return combined if combined else retrieve_relevant_messages(query, limit=limit)
