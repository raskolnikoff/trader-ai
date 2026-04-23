#!/usr/bin/env bash
# kill_bot.sh -- Emergency stop for maker_bot cron + running processes.
#
# Usage: ./scripts/kill_bot.sh
#
# This script:
#   1. Removes both cron entries (bot cycle + logrotate)
#   2. Kills any running maker_bot.py processes (SIGTERM then SIGKILL)
#   3. Clears the flock lock file
#   4. Prints final state for verification
#
# Safe to run multiple times (idempotent).

set -euo pipefail

CRON_TAG="maker_bot_trader_ai"
LOGROTATE_TAG="maker_bot_logrotate"
LOCK_FILE="/tmp/maker_bot_trader_ai.lock"

echo "Stopping maker_bot..."
echo ""

# 1. Remove cron entries
(crontab -l 2>/dev/null | grep -v "${CRON_TAG}" | grep -v "${LOGROTATE_TAG}") | crontab - || true

# 2. Kill any running instances (SIGTERM, then SIGKILL if still alive)
if pgrep -f "maker_bot.py" > /dev/null 2>&1; then
    echo "Sending SIGTERM to running maker_bot processes..."
    pkill -TERM -f "maker_bot.py" || true
    sleep 2
    if pgrep -f "maker_bot.py" > /dev/null 2>&1; then
        echo "Process still alive after SIGTERM; sending SIGKILL..."
        pkill -KILL -f "maker_bot.py" || true
    fi
fi

# 3. Clear lock
rm -f "${LOCK_FILE}"

# 4. Final state
echo ""
echo "Stopped. Current state:"
echo ""
echo "Cron entries:"
crontab -l 2>/dev/null | grep -E "${CRON_TAG}|${LOGROTATE_TAG}" | sed 's/^/  /' || echo "  (none)"
echo ""
echo "Running processes:"
pgrep -fl maker_bot.py 2>/dev/null | sed 's/^/  /' || echo "  (none)"
echo ""
echo "Lock file:"
if [ -f "${LOCK_FILE}" ]; then
    echo "  ${LOCK_FILE} (still present - investigate)"
else
    echo "  ${LOCK_FILE} (cleared)"
fi
echo ""
echo "Done."
