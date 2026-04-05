#!/usr/bin/env python3
"""
trader — Local-first trading assistant CLI.

Commands:
  analyze  Analyze the current chart with Claude (RAG + intent routing)
  history  Show recent conversation history
  search   Full-text search over conversation history
"""

import os
import sys
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel

# Ensure sibling modules resolve correctly regardless of CWD
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from claude_client import ask_claude
from db import (
    get_recent_messages,
    get_session,
    initialize_db,
    save_analysis_result,
    save_message,
    search_messages,
    update_session,
)
from prompts import build_prompt, detect_intent, extract_summary
from rag import retrieve_relevant_messages
from tv_client import collect_tv_context, tv_binary_available, check_tv_reachable

app = typer.Typer(
    name="trader",
    help="Local-first trading assistant CLI with RAG-powered memory.",
    no_args_is_help=True,
)
console = Console()


def _bootstrap() -> None:
    """Idempotent DB initialisation called at the start of every command."""
    try:
        initialize_db()
    except Exception as exc:
        console.print(f"[red]⚠️  DB の初期化に失敗しました: {exc}[/red]")
        raise typer.Exit(code=1)


def _validate_startup() -> bool:
    """
    Check whether the tv CLI binary is on PATH and whether TradingView is
    reachable via CDP. Prints a warning for each issue but never exits —
    analysis continues with whatever data is available.

    Returns True if both tv binary and TradingView are available.
    """
    if not tv_binary_available():
        console.print(
            "[yellow]⚠️  'tv' CLI が見つかりません。"
            "tradingview-mcp/ 内で npm link を実行してください。"
            "（ライブデータなしで分析を続行します）[/yellow]"
        )
        return False

    if not check_tv_reachable():
        console.print(
            "[yellow]⚠️  TradingView に接続できません（CDP port 9222）。"
            "TradingView が起動しているか確認してください。"
            "（過去の記憶のみで分析を続行します）[/yellow]"
        )
        return False

    return True


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------

@app.command()
def analyze(
    query: str = typer.Argument(..., help="Your question or analysis request, e.g. 'BTCどう？'"),
    symbol: Optional[str] = typer.Option(
        None, "--symbol", "-s", help="Override symbol (e.g. BTCUSDT). Saved for next run."
    ),
    timeframe: Optional[str] = typer.Option(
        None, "--timeframe", "-t", help="Timeframe label (e.g. 1h). Saved for next run."
    ),
    recent: int = typer.Option(
        5, "--recent", "-r", help="Number of recent messages to include in context"
    ),
    relevant: int = typer.Option(
        3, "--relevant", help="Number of RAG-retrieved messages to include"
    ),
) -> None:
    """Analyze the current chart using the local Claude CLI with RAG memory."""
    _bootstrap()
    _validate_startup()

    # ── Session state: fill missing symbol/timeframe from last run ────────────
    session = get_session()
    effective_symbol = symbol or session.get("symbol")
    effective_timeframe = timeframe or session.get("timeframe")

    # ── TradingView data (failure → empty context, never crashes) ─────────────
    with console.status("[bold blue]📡 TradingView からデータ取得中...", spinner="dots"):
        try:
            tv_data = collect_tv_context(symbol=effective_symbol)
        except Exception as exc:
            console.print(f"[yellow]⚠️  TV データ取得失敗（スキップ）: {exc}[/yellow]")
            tv_data = {"symbol": effective_symbol, "timeframe": effective_timeframe}

    # TV state takes precedence over session when available
    active_symbol = tv_data.get("symbol") or effective_symbol
    active_timeframe = tv_data.get("timeframe") or effective_timeframe

    # ── Intent detection ──────────────────────────────────────────────────────
    intent = detect_intent(query)

    # ── RAG retrieval (failure → empty lists, never crashes) ──────────────────
    with console.status("[bold blue]🧠 過去の会話を検索中...", spinner="dots"):
        try:
            recent_messages = get_recent_messages(limit=recent)
        except Exception as exc:
            console.print(f"[yellow]⚠️  履歴取得失敗: {exc}[/yellow]")
            recent_messages = []
        try:
            relevant_messages = retrieve_relevant_messages(query, limit=relevant)
        except Exception as exc:
            console.print(f"[yellow]⚠️  RAG 検索失敗: {exc}[/yellow]")
            relevant_messages = []

    # ── Build prompt and call Claude ──────────────────────────────────────────
    with console.status(f"[bold blue]🤖 Claude に送信中 (intent: {intent})...", spinner="dots"):
        prompt = build_prompt(
            intent=intent,
            query=query,
            tv_data=tv_data,
            relevant_messages=relevant_messages,
            recent_messages=recent_messages,
        )
        try:
            response = ask_claude(prompt)
        except RuntimeError as exc:
            console.print(f"\n[red]❌ Claude エラー: {exc}[/red]")
            raise typer.Exit(code=1)

    # ── Persist conversation + session + analysis summary ─────────────────────
    try:
        save_message("user", query, symbol=active_symbol, timeframe=active_timeframe)
        save_message("assistant", response, symbol=active_symbol, timeframe=active_timeframe)
        summary = extract_summary(response)
        save_analysis_result(summary, symbol=active_symbol, timeframe=active_timeframe)
        update_session(symbol=active_symbol, timeframe=active_timeframe)
    except Exception as exc:
        # Persistence failure must not hide the response from the user
        console.print(f"[yellow]⚠️  保存に失敗しました（表示は継続）: {exc}[/yellow]")

    # ── Render output ─────────────────────────────────────────────────────────
    symbol_label = active_symbol or "Chart"
    tf_label = active_timeframe or "?"
    intent_badge = {"analyze": "📊", "recall": "🗂", "explain": "🧠"}.get(intent, "📊")

    console.print()
    console.print(
        Panel(
            response,
            title=(
                f"[bold cyan]{intent_badge} Analysis — "
                f"{symbol_label} ({tf_label})  [{intent}][/bold cyan]"
            ),
            border_style="cyan",
            expand=False,
        )
    )


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------

@app.command()
def history(
    limit: int = typer.Option(10, "--limit", "-n", help="Number of recent messages to show"),
) -> None:
    """Show recent conversation history."""
    _bootstrap()

    messages = get_recent_messages(limit=limit)
    if not messages:
        console.print("[yellow]会話履歴がありません。[/yellow]")
        return

    console.print(f"\n[bold]🗂  直近 {len(messages)} 件の会話[/bold]\n")
    for msg in messages:
        role_color = "cyan" if msg["role"] == "user" else "green"
        symbol_tag = f" [{msg['symbol']}]" if msg.get("symbol") else ""
        timestamp = msg.get("created_at", "")[:19]
        content_preview = msg["content"][:200]
        console.print(
            f"[dim]{timestamp}[/dim]{symbol_tag} "
            f"[{role_color}]{msg['role'].upper()}[/{role_color}]: {content_preview}"
        )


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

@app.command()
def search(
    query: str = typer.Argument(..., help="Keyword(s) to search in conversation history"),
    limit: int = typer.Option(5, "--limit", "-n", help="Maximum number of results to show"),
) -> None:
    """Full-text search over conversation history."""
    _bootstrap()

    results = search_messages(query, limit=limit)
    if not results:
        console.print(f"[yellow]「{query}」に一致する会話が見つかりませんでした。[/yellow]")
        return

    console.print(f"\n[bold]🔍 「{query}」の検索結果（{len(results)} 件）[/bold]\n")
    for msg in results:
        role_color = "cyan" if msg["role"] == "user" else "green"
        symbol_tag = f" [{msg['symbol']}]" if msg.get("symbol") else ""
        timestamp = msg.get("created_at", "")[:19]
        content_preview = msg["content"][:300]
        console.print(
            f"[dim]{timestamp}[/dim]{symbol_tag} "
            f"[{role_color}]{msg['role'].upper()}[/{role_color}]: {content_preview}"
        )
        console.print()


if __name__ == "__main__":
    app()
