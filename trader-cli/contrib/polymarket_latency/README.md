# Polymarket Latency Detector

> **Experimental / observation tool. Not a trading strategy.**

## What this does

This module measures the **latency** (reaction delay) between:

- A significant price move on **Binance BTC/USDT**
- A corresponding price change in **Polymarket** prediction markets related to Bitcoin

It answers the question: *"After BTC moves ±0.2%, how many seconds does it take for each Polymarket market to reflect that move?"*

## What this does NOT do

- ❌ It does NOT execute trades
- ❌ It does NOT give financial advice
- ❌ It does NOT guarantee profitable opportunities
- ❌ Results are unstable and exploratory — market structure, liquidity, and API latency all affect readings

## Purpose

Observe price discovery inefficiencies between spot markets and prediction markets.  
Results are informational only. This is a measurement instrument, not a signal generator.

---

## Requirements

- Python 3.10+
- No additional packages (uses stdlib only: `urllib`, `json`, `time`)
- Internet access (Binance public API + Polymarket CLOB public API)
- No API keys required

---

## Usage

```bash
# Live monitor — watches Binance and measures Polymarket reaction time
python trader-cli/contrib/polymarket_latency/detector.py

# Or via the trader CLI
trader latency scan

# Custom threshold (trigger on 0.1% move instead of default 0.20%)
trader latency scan --threshold 0.1

# Env-var alternative
TRADER_LATENCY_THRESHOLD=0.1 trader latency scan
```

---

## Analyze latency logs

After running `scan` at least once, analyze the collected data offline:

```bash
# Via the trader CLI (recommended)
trader latency analyze

# Or run the script directly
python trader-cli/contrib/polymarket_latency/analyze.py
```

### What it outputs

```
📊 Latency Summary
Events: 128
Mean:   2.14s
p50:    1.82s
p95:    4.76s
Max:    7.92s

📈 Direction
UP:    2.30s
DOWN:  1.98s

⚡ Top Lagging Markets
  [0xabc12345]  avg 3.92s  (12 events)
  [0xdef67890]  avg 3.51s  (9 events)

⏱ Frequency
Events/hour: 24.6
```

### What the metrics mean

| Metric | Description |
|---|---|
| Events | Total latency records in the log |
| Mean / p50 / p95 / Max | Latency distribution across all observed reactions |
| UP / DOWN | Mean latency split by the Binance move direction |
| Top Lagging Markets | Markets that consistently react slowest (highest avg latency) |
| Events/hour | How frequently significant BTC moves occur in your session |

> **Interpretation note:** High latency in a market does not imply opportunity.
> It may reflect illiquidity, low trader attention, or simply Polymarket's infrastructure.
> These numbers are for observation only.

---

## Candidate selection

After accumulating latency data with `scan`, use this command to filter markets that
show **consistent, repeatable** lag — meaning they reliably react slowly across
multiple events, not just once.

```bash
trader latency candidates
```

### How it works

Markets are scored using:

```
score = avg_latency × log(event_count + 1)
```

This rewards **both** high average latency **and** a larger number of observations.
A market seen once with a 10 s lag scores lower than one seen 20 times with 4 s avg.

### Filter criteria (all three must be met)

| Filter | Default | Meaning |
|---|---|---|
| `avg_latency ≥ 2.0s` | minimum | Ignores markets that react quickly on average |
| `event_count ≥ 5` | minimum | Needs enough data to trust the average |
| `p95 ≥ 3.0s` | minimum | Tail latency must also be elevated (no isolated spikes) |

### Example output

```
🎯 Candidate Markets

[0xabc12345]
  BTC above 85k by end of April?
  score: 8.76  |  avg: 3.92s  |  events: 12  |  p95: 5.10s

[0xdef67890]
  Bitcoin price above $80,000 on April 30?
  score: 5.83  |  avg: 3.51s  |  events: 9   |  p95: 4.20s
```

> **Important:** This is a filtering and observation tool. Consistent latency does not
> guarantee that a market can be profitably exploited. Treat results as hypotheses
> to inspect manually, not as trade signals.

---

## Configuration (top of detector.py)

| Constant | Default | Description |
|---|---|---|
| `BINANCE_MOVE_THRESHOLD_PCT` | `0.20` | % move that triggers tracking |
| `POLL_INTERVAL_SECONDS` | `2.0` | How often Binance is polled |
| `TRACKING_WINDOW_SECONDS` | `60.0` | How long Polymarket is watched after an event |
| `TOP_N_MARKETS` | `5` | How many lagging markets to display |

---

## Example output

```
🔍 Monitoring Binance BTC price...
   Trigger threshold: 0.20%  /  Tracking window: 60s

  Initial BTC price: $83,412.00

📡 EVENT: +0.23%

  Fetching Polymarket baseline...
  4 markets being tracked (up to 60s)...

⚡ Lagging markets:
  📈  BTC above 85k by end of April? → 8.34s
  📈  Bitcoin price above $80,000 on April 30? → 5.12s
  ➡️   Will BTC reach $100k in 2025? → 3.41s
```

---

## Limitations

- Polymarket CLOB API structure may change without notice — the detector degrades gracefully
- Latency readings include network overhead and are not true market microstructure measurements
- Bitcoin markets on Polymarket may be illiquid — mid prices are noisy
- A "reaction" is detected when mid price changes by >0.1%, which is a loose heuristic

---

## Disclaimer

This tool is provided for **educational and research purposes only**.  
It does not constitute financial advice. Past latency patterns do not predict future behaviour.

