# LinkedIn post templates

Three drafts for announcing trader-ai on LinkedIn. Each has a different angle. Pick one, tweak the personal voice, ship.

All three intentionally:
- Do NOT promise returns, edge, or a working strategy.
- DO highlight engineering craft (balance-aware failsafes, local-first design, MCP ops).
- Link to the repo, not to any trading outcome.

---

## Draft A — the craft angle (recommended default)

> Built a local-first Polymarket maker bot this week.
>
> No cloud, no VPS, no hosted API. It runs on my laptop through cron, pulls USDC.e balance straight from Polygon before every cycle, and halts the moment anything looks wrong.
>
> What went into it:
> → py-clob-client for maker orders (zero fees, small rebate)
> → Binance REST for BTC price and 5m momentum
> → Polymarket Gamma API for short-term markets (weekly / monthly filter, liquidity threshold)
> → SQLite-backed memory for every cycle and every alert
> → Fail-safe balance check: if the RPC call fails, the cycle halts — no "proceed without cap" paths
> → Emergency kill switch (SIGTERM → SIGKILL, cron removed, lock cleared)
> → Optional TradingView overlay via Pine Script + ngrok webhook, shared-secret auth
>
> Started with $12. The interesting part isn't the $12 — it's that every piece is inspectable, reproducible, and runs on your own machine.
>
> Claude Code drove most of the PRs end-to-end through GitHub's MCP, which still feels like cheating.
>
> Repo → github.com/raskolnikoff/trader-ai
>
> #fintech #polymarket #claude #localfirst

---

## Draft B — the "AI-assisted dev" angle

> Spent this week shipping a local-first trading system end-to-end with Claude Code.
>
> Stack:
> — Polymarket CLOB + Gamma API
> — Binance REST
> — Python 3.14, py-clob-client, web3.py
> — SQLite for RAG memory
> — cron + flock + logrotate for ops
> — ngrok for the webhook side
>
> What changed vs. the way I used to build:
>
> 1. I never touched a file directly. Every change went through PR #15, #16, #17, #18 — Claude drove the branch creation, commits, and PRs via GitHub MCP, and I reviewed and merged.
>
> 2. Copilot's PR-14 review caught a fail-open bug (float('inf') on RPC error would silently disable the per-cycle balance cap). PR #15 fixed it. A year ago I would've shipped that and found out via a drained wallet.
>
> 3. The "dev loop" collapsed. Instead of write → run → fix → write, it was describe → review → merge. The bottleneck moved from typing to judgment.
>
> The bot itself is honest about what it is: an edge-scanner and maker-order poster on Polymarket. It starts with $12. It's explicit about its failure modes (no edge found → idle; insufficient balance → skip cycle; RPC down → halt).
>
> Next: the LinkedIn-ready live dashboard is open source too. Vanilla JS, Geist fonts, polls a /feed endpoint from the webhook server.
>
> Repo: github.com/raskolnikoff/trader-ai
>
> #claude #ai #fintech #polymarket

---

## Draft C — the ops / safety angle

> A short list of things I added to my Polymarket bot before I let it touch live funds:
>
> 1. Balance check from the chain, every cycle, before any order logic runs. If it fails, cycle halts. No "proceed without cap" fallback.
>
> 2. `MIN_EDGE_THRESHOLD = 0.04`. 4% fair-value gap or the bot does nothing. Most cycles do nothing, by design.
>
> 3. Per-cycle order cap tied to spendable balance, not a hard-coded number. `max_orders = int(spendable * 0.9 // max_size)`.
>
> 4. cron with flock so a slow cycle can't overlap the next one. Logrotate keeps 14 days gzipped.
>
> 5. Emergency kill script: removes both cron entries, SIGTERMs then SIGKILLs any running process, clears the lock, prints state. Safe to run repeatedly.
>
> 6. Shared-secret auth on the TradingView webhook. TV can't send custom headers, so the secret travels in the JSON body and gets stripped before persistence.
>
> 7. Rate limiter on the webhook (60/min/IP, sliding window). 65 KB payload cap.
>
> 8. The bot never places taker orders. Maker-only. Zero fees, small rebate.
>
> None of this is novel in isolation. What I'm proud of is that every one of those is in the commit history with a rationale — not bolted on after the first panic.
>
> Repo: github.com/raskolnikoff/trader-ai
>
> #fintech #riskmanagement #polymarket

---

## Posting tips

- Screenshot of the running dashboard is higher-impact than a code snippet. Save one with the price animating, the activity feed populated, a few markets in the table. Crop to 16:9.
- GIF of the price flashing lime on an uptick reads even better. Use `kap` or macOS Screen Recording → `gifski` for small file size.
- Thread this with a second post showing the architecture diagram (`docs/architecture.svg`) rendered on GitHub — light and dark both work.
- Pin the repo to your profile before posting.
- Reply to early comments in the first two hours; LinkedIn's algorithm weighs engagement velocity heavily.

## Screenshot / GIF checklist

Before recording:
- [ ] `.env` tab is closed (don't leak credentials in a browser shot)
- [ ] wallet address is partially redacted, or use the "0x9aB0…1daA" short form
- [ ] no filenames from your home directory leaking path info
- [ ] no notification badges in the menu bar
- [ ] browser is in dark mode (matches dashboard)
