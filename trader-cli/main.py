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
        console.print(f"[red]⚠️  DB initialization failed: {exc}[/red]")
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
            "[yellow]⚠️  'tv' CLI not found. "
            "Run npm install in the project root. "
            "(Continuing without live data)[/yellow]"
        )
        return False

    if not check_tv_reachable():
        console.print(
            "[yellow]⚠️  Cannot connect to TradingView (CDP port 9222). "
            "Ensure TradingView is running. "
            "(Continuing with memory only)[/yellow]"
        )
        return False

    return True


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------

@app.command()
def analyze(
    query: str = typer.Argument(..., help="Your question or analysis request, e.g. 'What is BTC doing?'"),
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
    with console.status("[bold blue]📡 Fetching data from TradingView...", spinner="dots"):
        try:
            tv_data = collect_tv_context(symbol=effective_symbol)
        except Exception as exc:
            console.print(f"[yellow]⚠️  TV data fetch failed (skipping): {exc}[/yellow]")
            tv_data = {"symbol": effective_symbol, "timeframe": effective_timeframe}

    # TV state takes precedence over session when available
    active_symbol = tv_data.get("symbol") or effective_symbol
    active_timeframe = tv_data.get("timeframe") or effective_timeframe

    # ── Intent detection ──────────────────────────────────────────────────────
    intent = detect_intent(query)

    # ── RAG retrieval (failure → empty lists, never crashes) ──────────────────
    with console.status("[bold blue]🧠 Searching conversation history...", spinner="dots"):
        try:
            recent_messages = get_recent_messages(limit=recent)
        except Exception as exc:
            console.print(f"[yellow]⚠️  Failed to retrieve history: {exc}[/yellow]")
            recent_messages = []
        try:
            relevant_messages = retrieve_relevant_messages(query, limit=relevant)
        except Exception as exc:
            console.print(f"[yellow]⚠️  RAG search failed: {exc}[/yellow]")
            relevant_messages = []

    # ── Build prompt and call Claude ──────────────────────────────────────────
    with console.status(f"[bold blue]🤖 Sending to Claude (intent: {intent})...", spinner="dots"):
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
            console.print(f"\n[red]❌ Claude error: {exc}[/red]")
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
        console.print(f"[yellow]⚠️  Save failed (output still displayed): {exc}[/yellow]")

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
        console.print("[yellow]No conversation history found.[/yellow]")
        return

    console.print(f"\n[bold]🗂  Recent {len(messages)} messages[/bold]\n")
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
        console.print(f"[yellow]No results found for '{query}'.[/yellow]")
        return

    console.print(f"\n[bold]🔍 Search results for '{query}' ({len(results)} found)[/bold]\n")
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


# ---------------------------------------------------------------------------
# latency
# ---------------------------------------------------------------------------

@app.command()
def latency(
    action: str = typer.Argument(
        "scan",
        help="Sub-command: 'scan' (live monitor), 'analyze' (log stats), or 'candidates'.",
    ),
    threshold: Optional[float] = typer.Option(
        None,
        "--threshold",
        help=(
            "BTC move %% that triggers Polymarket tracking "
            "(default: 0.20, env: TRADER_LATENCY_THRESHOLD). "
            "Only applies to the 'scan' sub-command."
        ),
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        is_flag=True,
        help="Output machine-readable JSON. Only applies to the 'analyze' sub-command.",
    ),
) -> None:
    """
    [Experimental] Polymarket vs Binance latency tools.

    Sub-commands:
      scan        – Live monitor. Detects BTC moves and measures Polymarket reaction time.
      analyze     – Offline report. Reads data/latency.jsonl and prints summary statistics.
      candidates  – Filter markets with consistent, repeatable lag.

    This is an observation tool, NOT a trading strategy.
    """
    if action == "scan":
        _run_latency_scan(threshold)
    elif action == "analyze":
        _run_latency_analyze(as_json=as_json)
    elif action == "candidates":
        _run_latency_candidates()
    else:
        console.print(
            f"[red]Unknown latency sub-command: '{action}'. "
            "Use 'scan', 'analyze', or 'candidates'.[/red]"
        )
        raise typer.Exit(code=1)


def _run_latency_scan(threshold: Optional[float]) -> None:
    """Start the live Binance → Polymarket latency monitor."""
    try:
        from contrib.polymarket_latency.detector import run_scan
    except ImportError as exc:
        console.print(f"[red]❌ Failed to load latency module: {exc}[/red]")
        raise typer.Exit(code=1)

    console.print(
        Panel(
            "[bold yellow]⚠️  This is an experimental observation tool.[/bold yellow]\n"
            "Measures the reaction latency of Polymarket markets to Binance BTC price moves.\n"
            "This is NOT a trading signal.",
            title="[bold]Polymarket Latency Detector[/bold]",
            border_style="yellow",
            expand=False,
        )
    )
    console.print()

    try:
        run_scan(threshold=threshold)
    except Exception as exc:
        console.print(f"[red]❌ An error occurred during scan: {exc}[/red]")
        raise typer.Exit(code=1)


def _run_latency_analyze(as_json: bool = False) -> None:
    """Read data/latency.jsonl and print summary statistics."""
    try:
        from contrib.polymarket_latency.analyze import run_analysis
    except ImportError as exc:
        console.print(f"[red]❌ Failed to load analyze module: {exc}[/red]")
        raise typer.Exit(code=1)

    report = run_analysis(as_json=as_json)

    if as_json:
        # Raw JSON must reach stdout undecorated — no Rich panel, no colour codes
        print(report)
    else:
        console.print(
            Panel(
                report,
                title="[bold cyan]Polymarket Latency Analysis[/bold cyan]",
                border_style="cyan",
                expand=False,
            )
        )


def _run_latency_candidates() -> None:
    """Filter and score markets from data/latency.jsonl by latency consistency."""
    try:
        from contrib.polymarket_latency.candidates import run_candidates
    except ImportError as exc:
        console.print(f"[red]❌ Failed to load candidates module: {exc}[/red]")
        raise typer.Exit(code=1)

    report = run_candidates()
    console.print(
        Panel(
            report,
            title="[bold green]Polymarket Latency Candidates[/bold green]",
            border_style="green",
            expand=False,
        )
    )


if __name__ == "__main__":
    app()
