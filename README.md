# trader-ai

Local-first trading assistant CLI. Fetches live chart data from TradingView,
retrieves relevant past analysis from SQLite (RAG), and reasons with the local
Claude CLI — no cloud APIs, no permission dialogs at runtime.

---

## ⚡ Quick Start

```bash
npm install                              # installs deps + links trader CLI
pip install -r trader-cli/requirements.txt

./scripts/trader.sh latency scan         # live Binance → Polymarket monitor
./scripts/trader.sh latency analyze      # summary stats from saved log
./scripts/trader.sh latency candidates   # markets with consistent lag
./scripts/trader.sh analyze "What is BTC doing?"  # AI analysis via Claude
```

---

## Architecture

```
Python CLI (main.py)
  │
  ├── tv CLI (subprocess) ──► TradingView (CDP port 9222)
  │     tv state / tv quote / tv ohlcv --summary / tv values / tv data lines
  │
  ├── SQLite (data/trader.db)
  │     messages · analysis_results · session
  │
  └── claude -p (subprocess) ──► local Claude CLI (text-only reasoning)
        Receives assembled prompt as stdin text.
        Does NOT call any MCP tools at runtime.
```

**Claude is used only as a text reasoning engine.**
TradingView data is collected by Python directly via the `tv` CLI.
MCP tool registration is a one-time developer setup — never a runtime step.

---

## Requirements

- macOS (TradingView desktop app)
- Node.js ≥ 18
- Python ≥ 3.11
- Claude CLI (`claude`) installed and authenticated

---

## Setup

### 1. Install all dependencies (one command)

From the **project root**:

```bash
npm install
```

This installs `tradingview-mcp` as a local file dependency and creates
`./node_modules/.bin/tv`. No global install. No `npm link`. No PATH changes.

Verify:

```bash
npx tv --help
```

### 2. Install Python dependencies

```bash
pip install -r trader-cli/requirements.txt
```

### 3. Start TradingView with CDP enabled

TradingView must be running with the remote debugging port open so the `tv`
CLI can connect via CDP:

```bash
/Applications/TradingView.app/Contents/MacOS/TradingView \
  --remote-debugging-port=9222
```

`dev.sh` handles this automatically (skipped if already running, and also
auto-runs `npm install` if `node_modules/.bin/tv` is missing).

### 4. (One-time) Register the MCP server with Claude

This is for Claude Desktop / Claude CLI MCP integration only.
It is **not** required at runtime and must **not** be added to any startup script.

```bash
claude mcp add --transport stdio tradingview -- \
  node /Users/<you>/WebstormProjects/trader-ai/tradingview-mcp/src/server.js
```

---

## Usage

### Run CLI

```bash
# Recommended — works from the project root, no PATH setup needed
./scripts/trader.sh analyze "What is BTC doing?"
./scripts/trader.sh latency scan
./scripts/trader.sh latency analyze
./scripts/trader.sh latency candidates
./scripts/trader.sh history
./scripts/trader.sh search "BTC"
```

> **Alternative / debug options** (both are equivalent to the above):
>
> ```bash
> # Direct Python — useful when debugging imports or the venv
> python3 trader-cli/main.py analyze "What is BTC doing?"
>
> # Via node_modules/.bin — created automatically by `npm install`
> ./node_modules/.bin/trader latency scan
> ```

### Quick start (via dev.sh)

```bash
bash scripts/dev.sh "What is BTC doing?"
```

### Direct CLI commands

```bash
# Analyze with a question
python3 trader-cli/main.py analyze "What is BTC doing?"

# Override symbol and timeframe
python3 trader-cli/main.py analyze "What do you think?" --symbol BTCUSDT --timeframe 1h

# Show recent conversation history
python3 trader-cli/main.py history

# Search past conversations
python3 trader-cli/main.py search "BTC"
```

### Output format

Every analysis response follows this structure:

```
📊 Status      — objective market description
💡 Assessment  — buy / sell / neutral in one sentence
🧠 Rationale   — up to 3 technical or fundamental reasons
⚠️ Caution     — risks and caveats
🎯 Key levels  — support / resistance / target zones
```

---

## Data storage

All conversation history and analysis summaries are stored locally in:

```
data/trader.db
```

Tables:

| Table              | Purpose                                      |
|--------------------|----------------------------------------------|
| `messages`         | Full user / assistant conversation history   |
| `messages_fts`     | FTS5 trigram index for keyword search        |
| `analysis_results` | Compact summaries used for RAG retrieval     |
| `session`          | Last-used symbol and timeframe               |

---

## Troubleshooting

### `tv_binary_available()` returns False / no TradingView data

The tv CLI could not be found. Run from the project root:

```bash
npm install
```

Then verify:

```bash
npx tv --help
npx tv status
npx tv quote BTCUSDT
npx tv ohlcv --summary
npx tv data lines
```

### `Cannot connect to TradingView (CDP port 9222)`

TradingView is not running with the debug port. Either:
- Use `dev.sh` which starts TradingView automatically, or
- Launch manually: `/Applications/TradingView.app/Contents/MacOS/TradingView --remote-debugging-port=9222`

### `Claude CLI not found`

Install and authenticate the Claude CLI:
```bash
# Follow Anthropic's installation guide, then:
claude --version
```

### Claude attempts to use MCP tools / permission dialog appears

This means the prompt is asking Claude to call tools instead of analyse text.
Ensure you are running `python3 trader-cli/main.py analyze "..."` directly,
**not** running `claude mcp add` anywhere in your startup flow.

---

## Project structure

```
trader-ai/
├── package.json        # Root package — tradingview-mcp + postinstall that links trader bin
├── scripts/
│   ├── dev.sh          # Start TV + run CLI (daily driver; auto-runs npm install)
│   ├── trader.sh       # Thin wrapper: python3 trader-cli/main.py "$@"  ← recommended entry
│   └── setup.js        # Node script that creates node_modules/.bin/trader symlink
├── trader-cli/
│   ├── main.py         # CLI entry point — executable (chmod +x)
│   ├── package.json    # bin: { trader: ./main.py } declaration
│   ├── tv_client.py    # TradingView data via ./node_modules/.bin/tv (no global install)
│   ├── claude_client.py# Claude CLI subprocess wrapper (stdin, no API key)
│   ├── prompts.py      # Prompt assembly + intent routing
│   ├── rag.py          # RAG retrieval with weighted scoring
│   ├── db.py           # SQLite storage layer
│   ├── requirements.txt
│   └── contrib/
│       └── polymarket_latency/   # Experimental latency measurement tools
│           ├── detector.py       # Live Binance → Polymarket latency scanner
│           ├── analyze.py        # Offline summary stats from latency.jsonl
│           └── candidates.py     # Filters markets with consistent lag
├── tradingview-mcp/    # External — do NOT modify
├── node_modules/       # Created by npm install; contains .bin/tv and .bin/trader
└── data/
    ├── trader.db       # Local SQLite database
    └── latency.jsonl   # Append-only latency event log (created by detector.py)
```

---

## Roadmap

Priorities for the next development cycle, in order:

| Priority | Command | Description |
|---|---|---|
| 1 | `trader latency analyze --json` | Output analysis as JSON for piping / scripting |
| 2 | `trader latency candidates --top N --min-events N` | Configurable filter thresholds at runtime |
| 3 | README Quick Start | Single-command getting-started block at the top of the README |

