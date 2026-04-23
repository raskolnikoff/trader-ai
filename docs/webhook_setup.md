# TradingView Webhook Setup

This guide covers the full setup for exposing the TV overlay webhook server via ngrok and wiring it up to TradingView alerts.

## Prerequisites

- **TradingView plan**: Pro, Pro+, or Premium (webhooks are gated behind paid plans as of 2026). Free plan users should skip this doc and use the dashboard-only flow described in the main README.
- **ngrok account** (free is fine) and `ngrok config add-authtoken YOUR_TOKEN` done once.
- `.env` file in the repo root (see `.env.example`).

## 1. Generate a webhook secret

TradingView webhooks cannot send custom HTTP headers, so the shared secret travels inside the JSON body. Pick something random (32+ chars):

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Add to `.env`:

```bash
TV_WEBHOOK_SECRET=<the-generated-string>
WEBHOOK_RATE_LIMIT_PER_MIN=60   # optional, default 60
```

Rotate this whenever you suspect it has leaked. Never commit `.env`.

## 2. Start the webhook server

```bash
conda activate trader-ai
python trader-cli/contrib/maker_bot/tv_overlay_webhook.py
```

Expected startup output:

```
[INFO] TV Overlay Webhook Server running on http://0.0.0.0:8765
[INFO]   auth:       REQUIRED (TV_WEBHOOK_SECRET)
[INFO]   rate limit: 60 req/min per IP
[INFO]   tv CLI:     node /path/to/tradingview-mcp/src/cli/index.js
```

If you see `auth: DISABLED`, the secret is not being loaded. Check that `.env` is in the repo root and the server was started from the repo root so the dotenv path resolves correctly.

Verify with curl:

```bash
curl http://localhost:8765/health
# {"ok":true,"auth":"required","rate_limit_per_min":60}

curl http://localhost:8765/feed | jq
# combined alerts + positions + tv_chart
```

## 3. Expose via ngrok

```bash
./scripts/start_ngrok.sh
```

Output includes the HTTPS URL. Copy the `Webhook (POST)` line for step 4.

Free-plan ngrok caveats:

- URL rotates every session — update the TradingView alert URL each time.
- Sessions disconnect after 2 hours — rerun the script.
- Only 1 simultaneous tunnel per account.

If you upgrade to ngrok paid ($8/month) you get a static domain:

```bash
./scripts/start_ngrok.sh --domain your-handle.ngrok.app
```

Then TradingView alerts never need to be reconfigured again.

## 4. Configure the TradingView alert

In TradingView (Pro+ plan):

1. Open a chart (any symbol/timeframe).
2. Right-click the chart → **Add alert** (or press **Alt+A**).
3. Set your condition (price crossing, indicator signal, Pine Script strategy, etc.).
4. In the **Notifications** tab:
   - Check **Webhook URL**
   - Paste the `Webhook (POST)` URL from `start_ngrok.sh` output
5. In the **Message** box (JSON, required verbatim):

   ```json
   {
     "secret": "PASTE_YOUR_TV_WEBHOOK_SECRET_HERE",
     "symbol": "{{ticker}}",
     "price":  {{close}},
     "action": "{{strategy.order.action}}",
     "time":   "{{timenow}}"
   }
   ```

   Replace `PASTE_YOUR_TV_WEBHOOK_SECRET_HERE` with the exact value from your `.env`. TradingView stores alerts in its own account, not in the repo, so the secret does not end up in version control.

6. Click **Create**.

The alert now fires your webhook whenever the condition triggers. In the server logs you should see:

```
[INFO] TV alert received: symbol=BTCUSDT price=67234.5 action=buy ip=1.2.3.4
```

## 5. Verify end-to-end

From any other terminal:

```bash
# Should succeed (correct secret)
curl -X POST https://YOUR-NGROK-URL/webhook \
  -H "Content-Type: application/json" \
  -d '{"secret":"YOUR_TV_WEBHOOK_SECRET","symbol":"BTCUSDT","price":67234,"action":"test"}'
# {"ok":true,"ts":...}

# Should fail (wrong secret)
curl -X POST https://YOUR-NGROK-URL/webhook \
  -H "Content-Type: application/json" \
  -d '{"secret":"wrong","symbol":"BTCUSDT","price":67234,"action":"test"}'
# {"error":"unauthorized"}

# Should fail (no secret)
curl -X POST https://YOUR-NGROK-URL/webhook \
  -H "Content-Type: application/json" \
  -d '{"symbol":"BTCUSDT","price":67234,"action":"test"}'
# {"error":"unauthorized"}

# Inspect what was stored
curl https://YOUR-NGROK-URL/feed | jq '.alerts[0]'
```

The stored alert has the `secret` field stripped. Only `symbol`, `price`, `action`, and the raw sanitized body are persisted.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `auth: DISABLED` at server startup | `.env` not loaded | Start from repo root; check `.env` exists and has `TV_WEBHOOK_SECRET=` |
| TV alert fires but server returns 401 | Secret mismatch | Copy-paste the exact value from `.env` into the TV alert message |
| TV alert fires but nothing happens at all | Wrong URL | The ngrok URL changed; rerun `start_ngrok.sh` and update the alert |
| `ERROR: Timed out waiting for ngrok tunnel URL` | ngrok authtoken not set | `ngrok config add-authtoken YOUR_TOKEN` |
| 429 rate limit errors | Alert firing every tick | Set TV alert to "Once Per Bar Close" instead of "Once Per Bar" |
| `/tv/chart` returns `connected: false` | TV Desktop not running in debug mode | Launch TV with `--remote-debugging-port=9222` (see tradingview-mcp/README.md) |

## Security model

- The webhook server binds to `0.0.0.0:8765` and is reachable from any process on the local machine. Do not run it on a shared machine.
- When exposed via ngrok, the server is reachable from the public internet. The shared secret is the only gate. Rate limiting caps abuse at 60 requests/minute per source IP.
- The secret travels in plaintext inside HTTPS TLS. HTTPS is enforced because ngrok provides it; do not ever substitute an `http://` tunnel.
- The `/tv/chart` endpoint is read-only and is served from a 10-second in-memory cache. It never triggers order placement or any write operation against Polymarket or TradingView.
- Webhook data is stored locally in `trader-cli/tv_overlay.db`. The `secret` field is stripped before storage.

## What this does NOT do

- Execute trades on behalf of TradingView alerts. The webhook server only records alerts; the Polymarket maker bot is a completely separate process and makes its own decisions based on Binance + Polymarket data.
- Send any data back to TradingView. All TV integration is read-only (`tv` CLI queries the local Desktop app via Chrome DevTools Protocol; nothing goes to TradingView's servers from our side).
