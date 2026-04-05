#!/bin/bash
# dev.sh — Start the trader-ai environment and run the Python CLI.
#
# Runtime flow:
#   1. Ensure node_modules/.bin/tv exists (run npm install if not).
#   2. Ensure TradingView is running with CDP on port 9222.
#   3. Run the Python CLI analysis command.
#
# No global install. No npm link. No PATH dependency.
# The tv CLI is invoked from ./node_modules/.bin/tv by the Python layer.

set -euo pipefail

QUERY="${1:-BTCどう？}"
TV_BINARY="/Applications/TradingView.app/Contents/MacOS/TradingView"
TV_DEBUG_PORT=9222
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "🚀 trader-ai starting..."

# ── 1. Ensure tv CLI is available (no global install needed) ──────────────────
TV_BIN="${REPO_ROOT}/node_modules/.bin/tv"
if [[ ! -f "${TV_BIN}" ]]; then
    echo "📦 node_modules/.bin/tv not found — running npm install..."
    cd "${REPO_ROOT}"
    npm install --silent
    echo "✅ npm install complete."
else
    echo "✅ tv CLI ready: ${TV_BIN}"
fi

# ── 2. TradingView ─────────────────────────────────────────────────────────────
if pgrep -f "TradingView.*remote-debugging-port=${TV_DEBUG_PORT}" > /dev/null 2>&1; then
    echo "✅ TradingView is already running on CDP port ${TV_DEBUG_PORT}."
else
    if [[ ! -x "${TV_BINARY}" ]]; then
        echo "⚠️  TradingView binary not found at ${TV_BINARY}."
        echo "    Start TradingView manually with --remote-debugging-port=${TV_DEBUG_PORT}."
        echo "    Analysis will proceed using memory only (no live chart data)."
    else
        echo "▶  Launching TradingView with CDP on port ${TV_DEBUG_PORT}..."
        "${TV_BINARY}" --remote-debugging-port="${TV_DEBUG_PORT}" &
        echo "   Waiting for TradingView to initialise (5 s)..."
        sleep 5
        echo "✅ TradingView launched."
    fi
fi

# ── 3. Python CLI ──────────────────────────────────────────────────────────────
echo ""
echo "🐍 Running: trader analyze \"${QUERY}\""
echo "─────────────────────────────────────────"

python3 "${REPO_ROOT}/trader-cli/main.py" analyze "${QUERY}"
