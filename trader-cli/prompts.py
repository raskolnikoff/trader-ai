"""
Prompt construction for the analyze command.

Intent routing:
  analyze — default market analysis (📊💡🧠⚠️🎯 format)
  recall  — user is asking about past observations or prior conversation
  explain — user wants deeper reasoning behind a movement or decision

Each intent receives a tailored system prompt while keeping the same
four-section data layout: [Current Data] / [Relevant Memory] /
[Recent Conversation] / [User Query].
"""

import json
from typing import Any, Literal

Intent = Literal["analyze", "recall", "explain"]

# ── Keywords used for intent detection ────────────────────────────────────────

_RECALL_KEYWORDS = [
    "last time", "previously", "history", "remember", "before",
    "previous", "past", "earlier", "last analysis",
]
_EXPLAIN_KEYWORDS = [
    "why", "explain", "because", "reason", "cause",
]

# ── System instructions per intent ────────────────────────────────────────────

_ANALYZE_INSTRUCTIONS = """\
You are a professional trading assistant.
Using the provided data and conversation history, respond strictly in the following format.
Do not change the section headings. Keep explanations accessible to beginner traders.
Do not give definitive investment advice.

Important: Do NOT use any tools or external APIs. Analyze only the text data provided below.

📊 Status
(Objective description of the current market condition)

💡 Assessment
(Buy / Sell / Neutral — one sentence summary)

🧠 Rationale (up to 3 points)
(Specific technical or fundamental reasons, in bullet points)

⚠️ Caution
(Key risks and caveats, concisely stated)

🎯 Key price levels to watch
(Support / resistance zones and target price ranges)\
"""

_RECALL_INSTRUCTIONS = """\
You are a trading assistant.
The user is asking about past analyses or previous conversation history.
Focus primarily on the [Relevant Memory] and [Recent Conversation] sections.
Do not give definitive investment advice.

Important: Do NOT use any tools or external APIs. Reference only the text data provided below.

Summarize past observations and analyses in plain language.
If information is insufficient, say so explicitly.\
"""

_EXPLAIN_INSTRUCTIONS = """\
You are a trading assistant.
The user wants a deeper understanding of price movements or the reasoning behind an assessment.
Refer to [Current Data] and [Relevant Memory] to explain the background, reasons, and mechanisms.
Use plain language — avoid unexplained jargon.
Do not give definitive investment advice.

Important: Do NOT use any tools or external APIs. Analyze only the text data provided below.\
"""

_INSTRUCTIONS_BY_INTENT: dict[Intent, str] = {
    "analyze": _ANALYZE_INSTRUCTIONS,
    "recall": _RECALL_INSTRUCTIONS,
    "explain": _EXPLAIN_INSTRUCTIONS,
}

# ── Intent detection ──────────────────────────────────────────────────────────

def detect_intent(query: str) -> Intent:
    """
    Classify the user's query into one of three intents.

    Rules (checked in priority order):
      1. recall  — query mentions past events, history, or prior conversation.
      2. explain — query asks for reasons or explanations.
      3. analyze — default intent for all other queries.
    """
    query_lower = query.lower()

    if any(keyword in query_lower for keyword in _RECALL_KEYWORDS):
        return "recall"
    if any(keyword in query_lower for keyword in _EXPLAIN_KEYWORDS):
        return "explain"
    return "analyze"


# ── Formatters ────────────────────────────────────────────────────────────────

def _format_tv_data(tv_data: dict[str, Any]) -> str:
    if not tv_data:
        return "N/A (TradingView not connected)"

    lines: list[str] = []

    symbol = tv_data.get("symbol") or "N/A"
    timeframe = tv_data.get("timeframe") or "N/A"
    lines.append(f"Symbol: {symbol}  |  Timeframe: {timeframe}")

    # Real-time quote (bid/ask/last price)
    quote = tv_data.get("quote")
    if isinstance(quote, dict):
        lines.append(f"Quote: {json.dumps(quote, ensure_ascii=False)}")

    # OHLCV bar statistics
    ohlcv = tv_data.get("ohlcv_summary")
    if isinstance(ohlcv, dict):
        lines.append(f"OHLCV Summary: {json.dumps(ohlcv, ensure_ascii=False)}")

    # Active indicator values
    indicators = tv_data.get("indicators")
    if isinstance(indicators, dict):
        lines.append(f"Indicators: {json.dumps(indicators, ensure_ascii=False)}")

    # Pine Script drawn price levels
    pine_lines = tv_data.get("pine_lines")
    if isinstance(pine_lines, dict):
        lines.append(f"Pine Lines: {json.dumps(pine_lines, ensure_ascii=False)}")

    if len(lines) == 1:
        # Only the header line was added — no real data came back
        lines.append("(No live data — chart may not be connected)")

    return "\n".join(lines)


def _format_message_list(messages: list[dict]) -> str:
    if not messages:
        return "None"

    parts: list[str] = []
    for msg in messages:
        role_label = "User" if msg.get("role") == "user" else "Assistant"
        timestamp = msg.get("created_at", "")[:19]
        content_preview = msg.get("content", "")[:400]
        symbol_tag = f" [{msg['symbol']}]" if msg.get("symbol") else ""
        parts.append(f"[{timestamp}]{symbol_tag} {role_label}: {content_preview}")

    return "\n".join(parts)


def _build_body(
    query: str,
    tv_data: dict[str, Any],
    relevant_messages: list[dict],
    recent_messages: list[dict],
) -> str:
    """Assemble the shared four-section data block."""
    return (
        f"[Current Data]\n{_format_tv_data(tv_data)}\n\n"
        f"---\n\n"
        f"[Relevant Memory]\n{_format_message_list(relevant_messages)}\n\n"
        f"---\n\n"
        f"[Recent Conversation]\n{_format_message_list(recent_messages)}\n\n"
        f"---\n\n"
        f"[User Query]\n{query}"
    )


# ── Public API ────────────────────────────────────────────────────────────────

def build_prompt(
    intent: Intent,
    query: str,
    tv_data: dict[str, Any],
    relevant_messages: list[dict],
    recent_messages: list[dict],
) -> str:
    """
    Build the complete prompt for Claude, routed by intent.

    Each intent receives different system instructions while sharing the
    same structured data body.
    """
    instructions = _INSTRUCTIONS_BY_INTENT.get(intent, _ANALYZE_INSTRUCTIONS)
    body = _build_body(query, tv_data, relevant_messages, recent_messages)
    return f"{instructions}\n\n---\n\n{body}"


def build_analyze_prompt(
    query: str,
    tv_data: dict[str, Any],
    relevant_messages: list[dict],
    recent_messages: list[dict],
) -> str:
    """Backward-compatible wrapper — always uses the 'analyze' intent."""
    return build_prompt("analyze", query, tv_data, relevant_messages, recent_messages)


def extract_summary(response: str) -> str:
    """
    Extract a compact summary from a Claude response for storage in
    analysis_results. Targets the 📊 Status and 💡 Assessment sections.

    Guaranteed to never raise — always returns a non-empty string.
    """
    try:
        if not response or not response.strip():
            return "(empty response)"

        capturing = False
        stop_markers = {"🧠", "⚠️", "🎯"}
        summary_lines: list[str] = []

        for line in response.splitlines():
            stripped = line.strip()
            if "📊" in stripped or "💡" in stripped:
                capturing = True
            if capturing and any(marker in stripped for marker in stop_markers):
                break
            if capturing and stripped:
                summary_lines.append(stripped)

        if summary_lines:
            return " ".join(summary_lines)[:500]

        # Fallback: first 500 characters of raw response
        return response.strip()[:500]

    except Exception:
        # Last-resort fallback — never let this function crash the caller
        return (response or "")[:500]
