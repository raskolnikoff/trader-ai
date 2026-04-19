# trader-ai

Local-first AI trading assistant. Combines TradingView chart data, Binance price feed, and Polymarket prediction market odds into a unified analysis signal — reasoned by Claude CLI.

No cloud APIs. No permission dialogs. Runs entirely on your machine.

---

## What it does

```
TradingView (CDP:9222)   Binance REST      Polymarket Gamma API
       |                      |                     |
  tv_client.py          price + momentum      market odds
  (OHLCV, indicators,        |                     |
   pine lines)               +----------+----------+
                                        |
                               signal_integrator.py
                               (async, parallel fetch)
                                        |
                               unified_prompt.py
                               (structured prompt)
                                        |
                               claude -p (subprocess)
                                        |
                               📊 Status
                               🎲 Polymarket Consensus
                               💡 Assessment
                               🧠 Rationale
                               ⚠️  Caution
                               🎯 Key levels
```

Past analyses are stored in SQLite and retrieved on the next query (RAG). The longer you run it, the deeper the context gets.

---

## Quick Start

```bash
# Install dependencies
npm install
pip install -r trader-cli/requirements.txt

# Start TradingView with CDP enabled
/Applications/TradingView.app/Contents/MacOS/TradingView \
  --remote-debugging-port=9222

# Run unified analysis (TV + Binance + Polymarket → Claude)
python trader-cli/contrib/tv_polymarket/analyze_unified.py "What is BTC doing?"

# With verbose signal preview
python trader-cli/contrib/tv_polymarket/analyze_unified.py \
  "What is BTC doing?" --verbose

# Different symbol
python trader-cli/contrib/tv_polymarket/analyze_unified.py \
  "Is SPX overextended?" --symbol SPX
```

TradingView is optional — degrades gracefully to Binance + Polymarket if not running.

---

## Copy Trading Monitor

Watches a list of Polymarket wallets and alerts on new trades in real time.

```bash
# Monitor with bot filter (default)
python trader-cli/contrib/copy_trading/monitor.py --interval 30

# Only alert on $10+ trades
python trader-cli/contrib/copy_trading/monitor.py --min-size 10

# Disable bot filter (debug)
python trader-cli/contrib/copy_trading/monitor.py --no-bot-filter

# Add a wallet to watch
python trader-cli/contrib/copy_trading/monitor.py --add 0xABC... --label ultralisk
```

Bot filter removes three noise patterns automatically:
- **Burst**: 5+ trades at the same timestamp (liquidation bots)
- **Dust**: trades under $2 (test/spam trades)
- **Near-certain**: price ≥ 0.97 (already-resolved market sweeps)

---

## Polymarket Market Finder

Find Polymarket markets relevant to a TradingView symbol.

```bash
python trader-cli/contrib/tv_polymarket/polymarket_markets.py --symbol BTCUSDT
python trader-cli/contrib/tv_polymarket/polymarket_markets.py --symbol SPX
```

---

## Requirements

- macOS (TradingView desktop app)
- Node.js ≥ 18
- Python ≥ 3.11 (tested on 3.14)
- Claude CLI installed and authenticated (`claude --version`)

---

## Setup

```bash
# 1. Install all dependencies
npm install
pip install -r trader-cli/requirements.txt

# 2. Verify TV CLI
npx tv status

# 3. (Optional) Register MCP server with Claude Desktop
claude mcp add --transport stdio tradingview -- \
  node /path/to/trader-ai/tradingview-mcp/src/server.js
```

---

## Architecture

| Component | Role |
|-----------|------|
| `tv_client.py` | TradingView data via CDP (symbol, OHLCV, indicators, pine lines) |
| `signal_integrator.py` | Async collection from TV + Binance + Polymarket |
| `unified_prompt.py` | Structured prompt builder for Claude |
| `analyze_unified.py` | CLI entry point + RAG retrieval + DB storage |
| `rag.py` | Symbol-aware RAG with multi-factor scoring |
| `db.py` | SQLite storage (messages, analysis_results, session) |
| `monitor.py` | Copy trading wallet monitor with bot filter |
| `polymarket_markets.py` | TV symbol → Polymarket market lookup |

---

## RAG Memory

Every analysis is stored locally in `data/trader.db` and retrieved on future queries.

Symbol-aware scoring ensures BTC analyses appear when asking about BTC, not when asking about SPX. The system learns from its own history — precision improves with use.

```
data/
├── trader.db          # SQLite: messages, analysis_results, session
├── watched_wallets.json
└── seen_trades.json
```

---

## Supported Symbols

| Symbol | Polymarket keywords |
|--------|-------------------|
| BTCUSDT | bitcoin, btc |
| ETHUSDT | ethereum, eth |
| SOLUSDT | solana, sol |
| SPX / SPXUSD | s&p 500, spx |
| NAS100 | nasdaq, qqq |
| XAUUSD | gold |
| EURUSD | euro, eur/usd |
| USDJPY | yen, usd/jpy |
| AAPL / TSLA / NVDA / MSFT | company name |

---

## Project Structure

```
trader-ai/
├── trader-cli/
│   ├── main.py                    # Legacy CLI entry (analyze, history, search)
│   ├── tv_client.py               # TradingView CDP client
│   ├── claude_client.py           # Claude CLI subprocess wrapper
│   ├── prompts.py                 # Prompt assembly + intent routing
│   ├── rag.py                     # Symbol-aware RAG retrieval
│   ├── db.py                      # SQLite storage layer
│   └── contrib/
│       ├── tv_polymarket/         # Unified signal integration
│       │   ├── analyze_unified.py # Main entry point
│       │   ├── signal_integrator.py
│       │   ├── unified_prompt.py
│       │   └── polymarket_markets.py
│       ├── copy_trading/          # Wallet monitor + bot filter
│       │   ├── monitor.py
│       │   ├── leaderboard.py
│       │   └── wallet_scorer.py
│       └── polymarket_latency/    # WS latency tools
│           ├── ws_detector.py
│           └── backtest.py
├── tradingview-mcp/               # TradingView MCP server (do not modify)
├── scripts/
│   ├── trader.sh
│   └── dev.sh
└── data/
    ├── trader.db
    ├── watched_wallets.json
    └── seen_trades.json
```
