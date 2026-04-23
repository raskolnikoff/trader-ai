#!/usr/bin/env bash
# cron_setup.sh -- Install/remove cron job for maker_bot (live mode)
#
# Usage:
#   ./cron_setup.sh install   # add cron entry (runs every 2 minutes)
#   ./cron_setup.sh remove    # remove cron entry + logrotate
#   ./cron_setup.sh status    # show current cron entry
#
# Features:
#   - Auto-detects anaconda3 / miniconda3 / homebrew miniconda
#   - Auto-detects flock (Linux) or falls back to mkdir-based locking (macOS)
#   - Loads .env automatically before each cycle
#   - Prevents overlapping cron runs
#   - Daily logrotate at 4am, keeps 14 days of gzipped logs
#
# The primary cron job:
#   - Runs maker_bot.py --live --once every 2 minutes
#   - Appends stdout/stderr to ~/logs/maker_bot.log
#   - Uses the conda environment 'trader-ai'
#
# macOS NOTE: grant Full Disk Access to /usr/sbin/cron:
#   System Settings -> Privacy & Security -> Full Disk Access
#   -> click '+' -> Shift+Cmd+G -> /usr/sbin/cron -> enable

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Repo root is three levels up from this script:
#   trader-ai/trader-cli/contrib/maker_bot/cron_setup.sh
#   -> trader-ai/trader-cli/contrib/maker_bot
#   -> trader-ai/trader-cli/contrib
#   -> trader-ai/trader-cli
#   -> trader-ai                          (this is the repo root)
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
BOT_SCRIPT="${SCRIPT_DIR}/maker_bot.py"
ENV_FILE="${REPO_ROOT}/.env"
LOG_DIR="${HOME}/logs"
LOG_FILE="${LOG_DIR}/maker_bot.log"
LOCK_FILE="/tmp/maker_bot_trader_ai.lock"
LOCK_DIR="/tmp/maker_bot_trader_ai.lockdir"
CONDA_ENV="${CONDA_ENV_OVERRIDE:-trader-ai}"
CRON_TAG="# maker_bot_trader_ai"
LOGROTATE_TAG="# maker_bot_logrotate"

# -- Detect conda installation -------------------------------------------------

if [ -n "${CONDA_SH_OVERRIDE:-}" ] && [ -f "${CONDA_SH_OVERRIDE}" ]; then
    CONDA_SH="${CONDA_SH_OVERRIDE}"
elif [ -f "${HOME}/anaconda3/etc/profile.d/conda.sh" ]; then
    CONDA_SH="${HOME}/anaconda3/etc/profile.d/conda.sh"
elif [ -f "${HOME}/miniconda3/etc/profile.d/conda.sh" ]; then
    CONDA_SH="${HOME}/miniconda3/etc/profile.d/conda.sh"
elif [ -f "/opt/homebrew/Caskroom/miniconda/base/etc/profile.d/conda.sh" ]; then
    CONDA_SH="/opt/homebrew/Caskroom/miniconda/base/etc/profile.d/conda.sh"
elif [ -f "/opt/miniconda3/etc/profile.d/conda.sh" ]; then
    CONDA_SH="/opt/miniconda3/etc/profile.d/conda.sh"
else
    echo "ERROR: Could not locate conda.sh." >&2
    echo "Tried:" >&2
    echo "  \$HOME/anaconda3, \$HOME/miniconda3, /opt/homebrew/Caskroom/miniconda/base, /opt/miniconda3" >&2
    echo "Set CONDA_SH_OVERRIDE=/path/to/conda.sh and rerun." >&2
    exit 1
fi

# -- Detect flock (Linux) or choose fallback (macOS) --------------------------
#
# macOS does not ship with flock. Homebrew's `flock` package provides it at
# /opt/homebrew/bin/flock (Apple Silicon) or /usr/local/bin/flock (Intel).
# If no flock is found, we fall back to mkdir-based locking, which is atomic
# on all POSIX filesystems and needs no external dependency.

FLOCK_BIN=""
for candidate in \
    "${FLOCK_OVERRIDE:-}" \
    "/usr/bin/flock" \
    "/usr/local/bin/flock" \
    "/opt/homebrew/bin/flock" \
    "$(command -v flock 2>/dev/null || true)"
do
    if [ -n "${candidate}" ] && [ -x "${candidate}" ]; then
        FLOCK_BIN="${candidate}"
        break
    fi
done

# -- Build cron commands -------------------------------------------------------

# The inner command: cd to repo, source .env, activate conda, run the bot.
# Shared by both lock strategies.
INNER_CMD="cd ${REPO_ROOT} && set -a && [ -f ${ENV_FILE} ] && source ${ENV_FILE} || true; set +a && source ${CONDA_SH} && conda activate ${CONDA_ENV} && python ${BOT_SCRIPT} --live --once >> ${LOG_FILE} 2>&1"

if [ -n "${FLOCK_BIN}" ]; then
    # flock-based locking (Linux, or macOS with Homebrew flock installed)
    CRON_CMD="*/2 * * * * ${FLOCK_BIN} -n ${LOCK_FILE} -c '${INNER_CMD}' ${CRON_TAG}"
    LOCK_STRATEGY="flock (${FLOCK_BIN})"
else
    # mkdir-based locking (portable fallback, works on vanilla macOS).
    # mkdir on an existing directory fails atomically, so only one process at
    # a time passes through. We rmdir at the end, and trap EXIT to clean up
    # if the command dies. The `mkdir … || exit 0` path makes overlapping
    # invocations exit quietly without spawning a second bot.
    CRON_CMD="*/2 * * * * /bin/bash -c 'mkdir ${LOCK_DIR} 2>/dev/null || exit 0; trap \"rmdir ${LOCK_DIR}\" EXIT; ${INNER_CMD}' ${CRON_TAG}"
    LOCK_STRATEGY="mkdir fallback (no flock found; install with: brew install flock)"
fi

# Secondary: daily 4am logrotate, keep 14 days of gzipped logs
LOGROTATE_CMD="0 4 * * * [ -f \"${LOG_FILE}\" ] && mv \"${LOG_FILE}\" \"${LOG_FILE}.\$(date +\%Y\%m\%d)\" && gzip \"${LOG_FILE}.\$(date +\%Y\%m\%d)\" && find \"\$(dirname ${LOG_FILE})\" -name \"maker_bot.log.*.gz\" -mtime +14 -delete ${LOGROTATE_TAG}"

# -- Actions -------------------------------------------------------------------

install_cron() {
    mkdir -p "${LOG_DIR}"

    # Verify .env exists and warn if not
    if [ ! -f "${ENV_FILE}" ]; then
        echo "WARNING: ${ENV_FILE} not found. Bot will fail at runtime without credentials." >&2
        echo "Copy .env.example to .env and fill in POLY_API_KEY / POLY_SECRET / etc." >&2
        echo "" >&2
    fi

    # Remove existing entries first to avoid duplicates
    (crontab -l 2>/dev/null | grep -v "${CRON_TAG}" | grep -v "${LOGROTATE_TAG}") | crontab - || true

    # Install both entries
    (crontab -l 2>/dev/null; echo "${CRON_CMD}"; echo "${LOGROTATE_CMD}") | crontab -

    echo "Installed cron entries:"
    echo "  Bot cycle (every 2min):"
    echo "    ${CRON_CMD}"
    echo "  Logrotate (daily 4am, keep 14 days):"
    echo "    ${LOGROTATE_CMD}"
    echo ""
    echo "Log file:       ${LOG_FILE}"
    echo "Lock strategy:  ${LOCK_STRATEGY}"
    echo "Conda env:      ${CONDA_ENV}  (conda.sh: ${CONDA_SH})"
    echo ""
    echo "macOS users: grant Full Disk Access to /usr/sbin/cron in System Settings."
    echo ""
    echo "Monitor with: tail -f ${LOG_FILE}"
    echo "Emergency stop: ${REPO_ROOT}/scripts/kill_bot.sh"
}

remove_cron() {
    (crontab -l 2>/dev/null | grep -v "${CRON_TAG}" | grep -v "${LOGROTATE_TAG}") | crontab - || true
    rm -f "${LOCK_FILE}"
    rmdir "${LOCK_DIR}" 2>/dev/null || true
    echo "Removed bot + logrotate cron entries."
    echo "Lock file cleared:      ${LOCK_FILE}"
    echo "Lock directory cleared: ${LOCK_DIR}"
}

status_cron() {
    echo "Current maker_bot cron entries:"
    local found=0
    while IFS= read -r line; do
        case "${line}" in
            *"${CRON_TAG}"*|*"${LOGROTATE_TAG}"*)
                echo "  ${line}"
                found=1
                ;;
        esac
    done < <(crontab -l 2>/dev/null || true)
    if [ "${found}" -eq 0 ]; then
        echo "  (none)"
    fi
    echo ""
    echo "Lock strategy: ${LOCK_STRATEGY}"
    echo "Lock state:"
    if [ -f "${LOCK_FILE}" ]; then
        echo "  ${LOCK_FILE} (flock file exists)"
    fi
    if [ -d "${LOCK_DIR}" ]; then
        echo "  ${LOCK_DIR} (mkdir lock held - a cycle is running)"
    fi
    if [ ! -f "${LOCK_FILE}" ] && [ ! -d "${LOCK_DIR}" ]; then
        echo "  no lock held (OK - ready for next cycle)"
    fi
    echo ""
    echo "Running processes:"
    pgrep -fl maker_bot.py 2>/dev/null || echo "  (none)"
    echo ""
    if [ -f "${LOG_FILE}" ]; then
        echo "Last 3 log lines (${LOG_FILE}):"
        tail -n 3 "${LOG_FILE}" | sed 's/^/  /'
    else
        echo "Log file does not exist yet: ${LOG_FILE}"
    fi
}

case "${1:-}" in
    install) install_cron ;;
    remove)  remove_cron  ;;
    status)  status_cron  ;;
    *)
        echo "Usage: $0 {install|remove|status}"
        echo ""
        echo "Environment overrides:"
        echo "  CONDA_ENV_OVERRIDE  - use a different conda env name (default: trader-ai)"
        echo "  CONDA_SH_OVERRIDE   - use a different conda.sh path"
        echo "  FLOCK_OVERRIDE      - use a specific flock binary (default: auto-detect)"
        exit 1
        ;;
esac
