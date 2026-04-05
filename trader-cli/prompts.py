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
    "前", "過去", "以前", "前回", "先ほど", "さっき", "履歴",
    "last time", "previously", "history", "remember", "before",
]
_EXPLAIN_KEYWORDS = [
    "なぜ", "どうして", "理由", "原因", "根拠", "説明",
    "why", "explain", "because", "reason", "cause",
]

# ── System instructions per intent ────────────────────────────────────────────

_ANALYZE_INSTRUCTIONS = """\
あなたはプロのトレーディングアシスタントです。
提供されたデータと過去の会話履歴を参考にして、必ず以下のフォーマットで回答してください。
セクションの見出しは変えないでください。回答は日本語で行ってください。
専門用語は避け、初心者にも分かりやすい言葉を使ってください。
断定的な投資アドバイスは避けてください。

重要: ツールや外部APIは一切使用しないでください。以下に提供されたテキストデータだけを分析してください。

📊 状況
（現在のマーケット状況の客観的な説明）

💡 判断
（買い / 売り / 中立 — 一文で要約）

🧠 根拠（最大3つ）
（テクニカルまたはファンダメンタルの具体的な根拠を箇条書きで）

⚠️ 注意
（リスクや注意すべき点を簡潔に）

🎯 次に見る価格帯
（サポート・レジスタンスや目標価格帯）\
"""

_RECALL_INSTRUCTIONS = """\
あなたはトレーディングアシスタントです。
ユーザーは過去の分析や会話の内容について質問しています。
[Relevant Memory] と [Recent Conversation] の内容を中心に参照してください。
回答は日本語で行ってください。断定的な投資アドバイスは避けてください。

重要: ツールや外部APIは一切使用しないでください。以下に提供されたテキストデータだけを参照してください。

過去の観察・分析をわかりやすく要約して答えてください。
情報が不足している場合は、その旨を明示してください。\
"""

_EXPLAIN_INSTRUCTIONS = """\
あなたはトレーディングアシスタントです。
ユーザーは価格の動きや判断の理由について詳しく知りたがっています。
[Current Data] と [Relevant Memory] を参照しながら、背景・理由・メカニズムを説明してください。
回答は日本語で行ってください。専門用語は分かりやすく言い換えてください。
断定的な投資アドバイスは避けてください。

重要: ツールや外部APIは一切使用しないでください。以下に提供されたテキストデータだけを分析してください。\
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
        return "N/A（TradingView に接続できませんでした）"

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
        lines.append("（ライブデータなし — チャートに接続されていない可能性があります）")

    return "\n".join(lines)


def _format_message_list(messages: list[dict]) -> str:
    if not messages:
        return "なし"

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
    analysis_results. Targets the 📊 状況 and 💡 判断 sections.

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
