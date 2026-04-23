#!/usr/bin/env bash
# start_ngrok.sh -- Start an ngrok tunnel for the TV overlay webhook server.
#
# Usage:
#   ./scripts/start_ngrok.sh                 # default port 8765, random URL
#   ./scripts/start_ngrok.sh --port 9000     # custom port
#   ./scripts/start_ngrok.sh --domain YOUR.ngrok.app  # pinned domain (paid plan)
#
# What it does:
#   1. Starts ngrok tunneling localhost:PORT to a public HTTPS URL.
#   2. Polls ngrok's local API (http://127.0.0.1:4040) to retrieve the URL.
#   3. Writes the URL to ~/.ngrok_webhook_url for other scripts to pick up.
#   4. Prints the URL and the webhook endpoint you'd paste into TradingView.
#   5. Streams ngrok logs to ~/logs/ngrok.log.
#
# Prerequisites:
#   - ngrok installed and authenticated (`ngrok config add-authtoken ...`)
#   - Webhook server running on localhost:PORT (python tv_overlay_webhook.py)
#
# Free plan notes:
#   - Tunnel URL rotates every session. Paid plan ($8/mo) gives static domain.
#   - ngrok sessions auto-disconnect after 2 hours on free plan; rerun to refresh.
#   - Only 1 simultaneous tunnel on free plan.
#
# Security reminder:
#   The webhook server enforces TV_WEBHOOK_SECRET if set in .env.
#   NEVER expose /webhook publicly without a secret configured.

set -euo pipefail

PORT=8765
DOMAIN=""
URL_CACHE="${HOME}/.ngrok_webhook_url"
LOG_DIR="${HOME}/logs"
LOG_FILE="${LOG_DIR}/ngrok.log"

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)    PORT="$2"; shift 2 ;;
        --domain)  DOMAIN="$2"; shift 2 ;;
        --help|-h)
            grep '^#' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            echo "Run with --help for usage." >&2
            exit 1
            ;;
    esac
done

# Sanity checks
if ! command -v ngrok > /dev/null; then
    echo "ERROR: ngrok is not installed." >&2
    echo "Install: brew install ngrok  (or https://ngrok.com/download)" >&2
    exit 1
fi

if ! nc -z 127.0.0.1 "${PORT}" 2>/dev/null; then
    echo "WARNING: No server appears to be listening on localhost:${PORT}." >&2
    echo "Start the webhook server first:" >&2
    echo "  python trader-cli/contrib/maker_bot/tv_overlay_webhook.py --port ${PORT}" >&2
    echo "" >&2
    echo "Continuing anyway (ngrok will connect once the server is up)..." >&2
    echo "" >&2
fi

mkdir -p "${LOG_DIR}"

# Build ngrok command
NGROK_ARGS=("http" "${PORT}" "--log=stdout" "--log-format=logfmt")
if [ -n "${DOMAIN}" ]; then
    NGROK_ARGS+=("--domain=${DOMAIN}")
fi

# Start ngrok in background
echo "Starting ngrok tunnel on port ${PORT}..."
if [ -n "${DOMAIN}" ]; then
    echo "  Using pinned domain: ${DOMAIN}"
else
    echo "  (Free plan: URL rotates every session; upgrade + --domain for static URL)"
fi

ngrok "${NGROK_ARGS[@]}" > "${LOG_FILE}" 2>&1 &
NGROK_PID=$!

# Wait up to 10s for the local ngrok API to come up and report the tunnel URL
echo "Waiting for ngrok to establish tunnel..."
TUNNEL_URL=""
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    sleep 1
    if ! kill -0 "${NGROK_PID}" 2>/dev/null; then
        echo "ERROR: ngrok process died. Check ${LOG_FILE}" >&2
        tail -n 20 "${LOG_FILE}" >&2
        exit 1
    fi
    # ngrok local API lives on 127.0.0.1:4040
    RESPONSE=$(curl -fsS http://127.0.0.1:4040/api/tunnels 2>/dev/null || true)
    if [ -n "${RESPONSE}" ]; then
        # Extract first https tunnel URL (handles both JSON whitespace patterns)
        TUNNEL_URL=$(echo "${RESPONSE}" \
            | grep -oE '"public_url":"https://[^"]+"' \
            | head -n 1 \
            | sed 's/"public_url":"//; s/"$//')
        if [ -n "${TUNNEL_URL}" ]; then
            break
        fi
    fi
done

if [ -z "${TUNNEL_URL}" ]; then
    echo "ERROR: Timed out waiting for ngrok tunnel URL." >&2
    echo "  Check ${LOG_FILE} for details." >&2
    echo "  ngrok PID: ${NGROK_PID} (still running; kill manually if needed)" >&2
    exit 1
fi

# Cache URL for other scripts
echo "${TUNNEL_URL}" > "${URL_CACHE}"

# Report
echo ""
echo "============================================================"
echo "ngrok tunnel is up."
echo "============================================================"
echo ""
echo "Public URL:      ${TUNNEL_URL}"
echo "Webhook (POST):  ${TUNNEL_URL}/webhook"
echo "Health (GET):    ${TUNNEL_URL}/health"
echo "Feed (GET):      ${TUNNEL_URL}/feed"
echo ""
echo "URL cached to:   ${URL_CACHE}"
echo "ngrok logs:      ${LOG_FILE}"
echo "ngrok PID:       ${NGROK_PID}"
echo ""
echo "TradingView setup:"
echo "  1. Verify TV_WEBHOOK_SECRET is set in .env"
echo "  2. Create alert in TradingView (Pro+ plan required for webhooks)"
echo "  3. Webhook URL: ${TUNNEL_URL}/webhook"
echo "  4. Alert message body (JSON):"
cat <<JSON_TEMPLATE
       {
         "secret": "YOUR_TV_WEBHOOK_SECRET",
         "symbol": "{{ticker}}",
         "price":  {{close}},
         "action": "{{strategy.order.action}}",
         "time":   "{{timenow}}"
       }
JSON_TEMPLATE
echo ""
echo "To stop:  kill ${NGROK_PID}    (or killall ngrok)"
echo "============================================================"
