#!/usr/bin/env bash
# cron_setup.sh -- Install/remove cron job for maker_bot (live mode)
#
# Usage:
#   ./cron_setup.sh install   # add cron entry (runs every 2 minutes)
#   ./cron_setup.sh remove    # remove cron entry
#   ./cron_setup.sh status    # show current cron entry
#
# The cron job:
#   - Runs maker_bot.py --live --once every 2 minutes
#   - Appends stdout/stderr to ~/logs/maker_bot.log
#   - Uses the conda environment 'trader-ai'

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../../.." && pwd)"
BOT_SCRIPT="${SCRIPT_DIR}/maker_bot.py"
LOG_DIR="${HOME}/logs"
LOG_FILE="${LOG_DIR}/maker_bot.log"
CONDA_ENV="trader-ai"
CRON_TAG="# maker_bot_trader_ai"

# Build the cron command
CRON_CMD="*/2 * * * * source \"${HOME}/anaconda3/etc/profile.d/conda.sh\" && conda activate ${CONDA_ENV} && python \"${BOT_SCRIPT}\" --live --once >> \"${LOG_FILE}\" 2>&1 ${CRON_TAG}"

install_cron() {
    mkdir -p "${LOG_DIR}"
    # Remove existing entry first to avoid duplicates
    (crontab -l 2>/dev/null | grep -v "${CRON_TAG}") | crontab -
    # Add new entry
    (crontab -l 2>/dev/null; echo "${CRON_CMD}") | crontab -
    echo "Cron job installed:"
    echo "  ${CRON_CMD}"
    echo "Log file: ${LOG_FILE}"
}

remove_cron() {
    (crontab -l 2>/dev/null | grep -v "${CRON_TAG}") | crontab -
    echo "Cron job removed."
}

status_cron() {
    echo "Current maker_bot cron entries:"
    crontab -l 2>/dev/null | grep "${CRON_TAG}" || echo "  (none)"
}

case "${1:-}" in
    install) install_cron ;;
    remove)  remove_cron  ;;
    status)  status_cron  ;;
    *)
        echo "Usage: $0 {install|remove|status}"
        exit 1
        ;;
esac
