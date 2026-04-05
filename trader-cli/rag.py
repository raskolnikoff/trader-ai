"""
Retrieval-Augmented Generation (RAG) helper.

Retrieval strategy (Phase 1):
  1. Collect candidates from three sources, tagged with their origin.
  2. De-duplicate by message id.
  3. Score each candidate — source priority + keyword match + recency bonus.
  4. Return the top N highest-scoring unique candidates.

Source priority (flat base added before keyword/recency scoring):
  analysis_results  → +3.0  (highest signal — distilled past conclusions)
  fts_hits          → +2.0  (keyword-matched raw messages)
  recent            → +1.5  (recency pool, no keyword guarantee)

Keyword scoring (added per matching keyword):
  +2.0 for exact token match (word-boundary)
  +1.0 for substring (partial) match

Recency bonus: +0.0 to +0.5 linearly distributed across the pool.
"""

from db import get_recent_messages, get_recent_analysis_results, search_messages

# ── Source priority constants ─────────────────────────────────────────────────

_PRIORITY_ANALYSIS = 3.0
_PRIORITY_FTS = 2.0
_PRIORITY_RECENT = 1.5


# ── Scoring ───────────────────────────────────────────────────────────────────

def _score(
    candidate: dict,
    keywords: list[str],
    source_priority: float,
    recency_rank: int,
    total: int,
) -> float:
    """
    Compute a relevance score for a single candidate.

    Parameters
    ----------
    candidate:       message dict with at least a 'content' key.
    keywords:        whitespace-split tokens from the user query.
    source_priority: flat bonus applied before keyword/recency scoring.
    recency_rank:    0-based position in the pool (0 = most recent).
    total:           total number of candidates (for normalising recency).
    """
    content = candidate.get("content", "").lower()
    score = source_priority

    for keyword in keywords:
        keyword_lower = keyword.lower()
        if keyword_lower in content:
            # Exact token match (simple word-boundary via space padding)
            if f" {keyword_lower} " in f" {content} ":
                score += 2.0
            else:
                score += 1.0

    # Recency bonus: most recent candidate gets +0.5, oldest gets +0.0
    recency_bonus = ((total - recency_rank) / total * 0.5) if total > 0 else 0.0
    score += recency_bonus

    return score


# ── Public API ────────────────────────────────────────────────────────────────

def retrieve_relevant_messages(query: str, limit: int = 3) -> list[dict]:
    """
    Return the top-`limit` most relevant past messages for the given query.

    Candidate pool:
      - analysis_results summaries (up to 3) — source priority +3.0
      - FTS5 keyword matches (up to 5)        — source priority +2.0
      - Recent messages (last 5)              — source priority +1.5

    All sources are de-duplicated by id, scored, and sorted descending.
    """
    keywords = [w for w in query.split() if len(w) >= 2]

    # ── Build tagged candidate pool ───────────────────────────────────────────

    # analysis_results — convert to pseudo-message dicts, tag with highest priority
    analysis_candidates: list[tuple[dict, float]] = [
        (
            {
                "id": f"ar_{row['id']}",
                "role": "assistant",
                "content": row["summary"],
                "symbol": row.get("symbol"),
                "timeframe": row.get("timeframe"),
                "created_at": row["created_at"],
            },
            _PRIORITY_ANALYSIS,
        )
        for row in get_recent_analysis_results(limit=3)
    ]

    fts_hits = search_messages(query, limit=5) if keywords else []
    fts_candidates: list[tuple[dict, float]] = [
        (msg, _PRIORITY_FTS) for msg in fts_hits
    ]

    recent_candidates: list[tuple[dict, float]] = [
        (msg, _PRIORITY_RECENT) for msg in get_recent_messages(limit=5)
    ]

    # Merge — analysis first (highest priority), then FTS, then recency
    seen_ids: set = set()
    candidates: list[tuple[dict, float]] = []  # (message, source_priority)
    for msg, priority in analysis_candidates + fts_candidates + recent_candidates:
        msg_id = msg.get("id")
        if msg_id not in seen_ids:
            seen_ids.add(msg_id)
            candidates.append((msg, priority))

    if not candidates:
        return []

    # ── Score and rank ────────────────────────────────────────────────────────
    total = len(candidates)
    scored = [
        (
            msg,
            _score(msg, keywords, source_priority, rank, total),
        )
        for rank, (msg, source_priority) in enumerate(candidates)
    ]
    scored.sort(key=lambda pair: pair[1], reverse=True)

    return [msg for msg, _ in scored[:limit]]
