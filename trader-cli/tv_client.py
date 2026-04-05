"""
Thin subprocess wrapper around the `tv` CLI (tradingview-mcp).

No global install or npm link required.

Binary resolution order (first match wins):
  1. <repo_root>/node_modules/.bin/tv  — created by `npm install` at the project root.
  2. node <repo_root>/tradingview-mcp/src/cli/index.js  — direct Node.js invocation,
     works as long as `cd tradingview-mcp && npm install` has been run.

Correct runtime flow:
  Python → tv CLI (local node_modules) → TradingView (CDP port 9222)

Claude is NOT involved in data collection. It only receives the text output
that this module assembles, then reasons about it as plain text.
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

TV_TIMEOUT_SECONDS = 15

# ── Path constants (all relative to the repo root) ────────────────────────────

_REPO_ROOT = Path(__file__).parent.parent
_TV_MCP_DIR = _REPO_ROOT / "tradingview-mcp"
_TV_CLI_ENTRY = _TV_MCP_DIR / "src" / "cli" / "index.js"
# Populated by `npm install` at the project root (via the file: dependency).
_TV_LOCAL_BIN = _REPO_ROOT / "node_modules" / ".bin" / "tv"


# ── Binary resolution ─────────────────────────────────────────────────────────

def _build_tv_command(*args: str) -> Optional[list[str]]:
    """
    Return the full command list to invoke the tv CLI, or None if unavailable.

    Resolution order:
      1. node_modules/.bin/tv at the project root (preferred, created by npm install).
      2. Direct `node <cli_entry>` invocation (fallback; requires tradingview-mcp
         deps to be installed via `cd tradingview-mcp && npm install`).

    No global install, no npm link, no PATH dependency.
    """
    if _TV_LOCAL_BIN.exists():
        return [str(_TV_LOCAL_BIN)] + list(args)

    if _TV_CLI_ENTRY.exists() and shutil.which("node"):
        return ["node", str(_TV_CLI_ENTRY)] + list(args)

    return None


def tv_binary_available() -> bool:
    """Return True if the tv CLI can be invoked (no global install required)."""
    return _build_tv_command() is not None


# ── Core runner ───────────────────────────────────────────────────────────────

def _run_tv(*args: str) -> Optional[dict[str, Any]]:
    """
    Execute a tv CLI command and return the parsed JSON response.

    The tv CLI always emits JSON on stdout (exit 0) and JSON on stderr (exit 1/2).
    Returns None silently on any failure so callers can degrade gracefully.
    """
    command = _build_tv_command(*args)
    if command is None:
        return None

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=TV_TIMEOUT_SECONDS,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
        return None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


# ── Public helpers ────────────────────────────────────────────────────────────

def check_tv_reachable() -> bool:
    """
    Return True if TradingView is running and the CDP connection is live.
    Uses `tv status` which exits 0 on success and 2 when CDP is unreachable.
    """
    result = _run_tv("status")
    return isinstance(result, dict) and result.get("success") is True


def get_chart_state() -> Optional[dict[str, Any]]:
    """Return the current chart symbol, timeframe, and active studies."""
    return _run_tv("state")


def get_quote(symbol: Optional[str] = None) -> Optional[dict[str, Any]]:
    """
    Return the real-time price quote for the given symbol.
    If symbol is None, the tv CLI uses the currently active chart symbol.
    """
    if symbol:
        return _run_tv("quote", symbol)
    return _run_tv("quote")


def get_ohlcv_summary() -> Optional[dict[str, Any]]:
    """Return OHLCV summary statistics for the bars currently visible."""
    return _run_tv("ohlcv", "--summary")


def get_indicator_values() -> Optional[dict[str, Any]]:
    """Return current indicator values from the TradingView data window."""
    return _run_tv("values")


def get_pine_lines() -> Optional[dict[str, Any]]:
    """Return Pine Script line.new() price levels drawn on the chart."""
    return _run_tv("data", "lines")


def collect_tv_context(symbol: Optional[str] = None) -> dict[str, Any]:
    """
    Gather all available TradingView data into one structured dict.

    Commands used (each degrades independently to None on failure):
      tv state         → symbol, timeframe, active studies
      tv quote         → real-time bid/ask/last price
      tv ohlcv -s      → OHLCV bar summary statistics
      tv values        → current indicator values
      tv data lines    → Pine Script drawn price levels

    The caller is expected to handle None fields gracefully.
    """
    state = get_chart_state()

    active_symbol = symbol or (state.get("symbol") if state else None)
    active_timeframe = state.get("resolution") if state else None

    quote = get_quote(symbol=active_symbol)
    ohlcv = get_ohlcv_summary()
    indicators = get_indicator_values()
    pine_lines = get_pine_lines()

    return {
        "symbol": active_symbol,
        "timeframe": active_timeframe,
        "state": state,
        "quote": quote,
        "ohlcv_summary": ohlcv,
        "indicators": indicators,
        "pine_lines": pine_lines,
    }
