# copy_trading

Manual copy trading toolkit for Polymarket.
Identifies high-performing bot wallets and alerts you in real time when they open new positions.

No wallet required to run. Read-only Data API only (no auth).

---

## Architecture

```
leaderboard.py      -- fetch top wallets by PnL (Data API, no auth)
       |
wallet_scorer.py    -- score each wallet: win_rate, recency, composite
       |
monitor.py          -- poll watched wallets, alert on new trades -> copy manually
```

---

## Quick Start

```bash
# 1. Install deps (no new deps needed beyond existing requirements.txt)
pip install -r trader-cli/requirements.txt

# 2. See who is winning right now
python trader-cli/contrib/copy_trading/leaderboard.py --period week --limit 30

# 3. Score a specific wallet
python trader-cli/contrib/copy_trading/wallet_scorer.py --address 0xYOUR_TARGET

# 4. Auto-populate watch list from top leaderboard wallets (scored + filtered)
python trader-cli/contrib/copy_trading/monitor.py --scan-leaderboard

# 5. Or add a wallet manually
python trader-cli/contrib/copy_trading/monitor.py --add 0xADDRESS --label "bot_A"

# 6. Start monitoring (alerts printed to stdout)
python trader-cli/contrib/copy_trading/monitor.py --interval 30
```

---

## Alert Format

```
[ALERT] bot_A (0x1234abcd...)  @ 14:32:07 UTC
  Market : Will BTC close above $80k on April 30?
  Side   : YES
  Size   : $250.00  @  0.420
  Link   : https://polymarket.com/event/will-btc-close-above-80k-april-30
```

When you see an alert: open the Link, place the same trade manually on Polymarket.

---

## Wallet Scoring Criteria

| Filter | Default | Description |
|--------|---------|-------------|
| min_trades | 20 | Resolved trades required |
| min_win_rate | 55% | Must win >55% of resolved bets |
| max_avg_trade | $5,000 | Skip whales (too large to copy) |
| min_avg_trade | $5 | Skip dust traders |

Composite score = `win_rate * log(resolved_count + 1) * (recency + 0.1)`

---

## Data Files

| File | Contents |
|------|----------|
| `data/watched_wallets.json` | List of wallets to monitor |
| `data/seen_trades.json` | Dedup set of already-alerted tx hashes |

---

## Roadmap

| Phase | Description |
|-------|-------------|
| 1 (current) | Manual copy: alert + place trade yourself |
| 2 | Semi-auto: Ares copytrading integration |
| 3 | Full auto: py-clob-client order execution |
