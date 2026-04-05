"""
Smoke test — runs without a live TradingView session or Claude CLI.
Covers: db, session, analysis_results, RAG scoring, intent detection,
        prompt building, tv_client graceful degradation.
"""

import os
import sys
import shutil
import tempfile

# ── Isolated DB — must be set before importing db module ─────────────────────
_tmp_dir = tempfile.mkdtemp(prefix="trader_smoke_")
os.environ["TRADER_DB_DIR"] = _tmp_dir

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── DB: tables + messages ─────────────────────────────────────────────────────
from db import (
    initialize_db,
    save_message,
    get_recent_messages,
    search_messages,
    get_session,
    update_session,
    save_analysis_result,
    get_recent_analysis_results,
)

initialize_db()
print("✅ db.initialize_db OK")

msg_id = save_message("user", "BTCどう？", symbol="BTCUSDT", timeframe="1h")
assert isinstance(msg_id, int) and msg_id > 0
save_message("assistant", "📊 状況\nBTCは上昇トレンド中です。", symbol="BTCUSDT", timeframe="1h")

recent = get_recent_messages(limit=5)
assert len(recent) == 2
assert recent[0]["role"] == "user"
assert recent[1]["role"] == "assistant"
print(f"✅ db.get_recent_messages OK ({len(recent)} rows)")

fts_results = search_messages("BTC", limit=5)
assert len(fts_results) >= 1, "FTS5 trigram search returned no results"
print(f"✅ db.search_messages (FTS5 trigram) OK ({len(fts_results)} hits)")

# ── DB: session ───────────────────────────────────────────────────────────────
session = get_session()
assert session["symbol"] is None and session["timeframe"] is None
print("✅ db.get_session OK (empty initial state)")

update_session(symbol="BTCUSDT", timeframe="1h")
session = get_session()
assert session["symbol"] == "BTCUSDT"
assert session["timeframe"] == "1h"
print("✅ db.update_session OK")

# Partial update — only symbol changes, timeframe preserved
update_session(symbol="ETHUSDT")
session = get_session()
assert session["symbol"] == "ETHUSDT"
assert session["timeframe"] == "1h"
print("✅ db.update_session partial update OK")

# ── DB: analysis_results ──────────────────────────────────────────────────────
ar_id = save_analysis_result(
    "📊 状況 BTCは上昇トレンド 💡 判断 中立", symbol="BTCUSDT", timeframe="1h"
)
assert isinstance(ar_id, int) and ar_id > 0
results = get_recent_analysis_results(limit=5)
assert len(results) == 1
assert results[0]["symbol"] == "BTCUSDT"
print("✅ db.save_analysis_result / get_recent_analysis_results OK")

# ── RAG: scoring ──────────────────────────────────────────────────────────────
from rag import retrieve_relevant_messages

retrieved = retrieve_relevant_messages("BTC上昇", limit=3)
assert len(retrieved) >= 1
# The analysis_result entry (ar_1) must appear — it has the highest source priority
ar_ids = [m["id"] for m in retrieved if str(m.get("id", "")).startswith("ar_")]
assert len(ar_ids) >= 1, "analysis_results entry should be top-ranked"
print(f"✅ rag.retrieve_relevant_messages OK ({len(retrieved)} results, analysis_results prioritised)")

# ── tv_client: graceful degradation ──────────────────────────────────────────
from tv_client import collect_tv_context

ctx = collect_tv_context()
assert isinstance(ctx, dict)
assert "symbol" in ctx and "timeframe" in ctx
print("✅ tv_client.collect_tv_context OK (graceful degradation verified)")

# ── prompts: intent detection ─────────────────────────────────────────────────
from prompts import detect_intent, build_prompt, extract_summary

assert detect_intent("BTCどう？") == "analyze"
assert detect_intent("なぜ下落したの？") == "explain"
assert detect_intent("前回の分析を教えて") == "recall"
assert detect_intent("why did BTC drop") == "explain"
assert detect_intent("last time you said...") == "recall"
print("✅ prompts.detect_intent OK (all 5 cases)")

# ── prompts: build_prompt (all three intents) ─────────────────────────────────
for intent in ("analyze", "recall", "explain"):
    prompt = build_prompt(
        intent=intent,
        query="BTCどう？",
        tv_data=ctx,
        relevant_messages=retrieved,
        recent_messages=recent,
    )
    assert "[Current Data]" in prompt
    assert "[Relevant Memory]" in prompt
    assert "[Recent Conversation]" in prompt
    assert "[User Query]" in prompt
    assert "BTCどう？" in prompt
print("✅ prompts.build_prompt OK (analyze / recall / explain)")

# ── prompts: extract_summary — robustness edge cases ─────────────────────────
fake_response = (
    "📊 状況\nBTCは横ばいです。\n"
    "💡 判断\n中立\n"
    "🧠 根拠（最大3つ）\n- RSIが50付近\n"
    "⚠️ 注意\n急変に注意\n"
    "🎯 次に見る価格帯\n85000–90000"
)
summary = extract_summary(fake_response)
assert "📊" in summary or "💡" in summary
assert len(summary) <= 500
print("✅ prompts.extract_summary OK (normal response)")

# Empty string must not raise
assert extract_summary("") == "(empty response)"
print("✅ prompts.extract_summary OK (empty string fallback)")

# No emoji markers — should fall back to raw text
plain = extract_summary("BTCは上昇中です。" * 20)
assert len(plain) <= 500
print("✅ prompts.extract_summary OK (no-marker fallback)")

# ── claude_client: import only (no live claude binary needed) ─────────────────
from claude_client import ask_claude, analyze  # noqa: F401
print("✅ claude_client import OK")

# ── Cleanup ───────────────────────────────────────────────────────────────────
shutil.rmtree(_tmp_dir, ignore_errors=True)
print("\n✅ All smoke tests passed.")
