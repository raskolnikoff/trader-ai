#!/bin/bash
# trader.sh — run the trader CLI without a global install.
#
# Usage (from the project root):
#   ./scripts/trader.sh analyze "What is BTC doing?"
#   ./scripts/trader.sh latency scan
#   ./scripts/trader.sh latency analyze
#   ./scripts/trader.sh latency candidates --threshold 0.1
#   ./scripts/trader.sh history
#   ./scripts/trader.sh search "BTC"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

exec python3 "${PROJECT_ROOT}/trader-cli/main.py" "$@"

