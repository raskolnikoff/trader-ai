# tv_polymarket

Unified signal integration: TradingView + Binance + Polymarket -> Claude analysis.

---

## Architecture

```
TradingView (CDP:9222)     Binance REST API      Polymarket Gamma API
      |                          |                       |
  tv_client.py            collect_binance()    find_markets_for_symbol()
  (symbol, OHLCV,          (price, 5m/30m        (relevant markets
   indicators,              momentum)              + odds)
   pine lines)
      |                          |                       |
      +----------+---------------+-----------------------+
                 |
          signal_integrator.py
          (SignalContext dataclass, async collection)
                 |
          unified_prompt.py
          (builds structured prompt with all 3 sources)
                 |
          claude_client.py
          (Claude CLI subprocess)
                 |
          analyze_unified.py
          (CLI entry point + RAG + DB storage)
```

---

## Usage

```bash
# Analyze BTC (TradingView optional, degrades gracefully)
python trader-cli/contrib/tv_polymarket/analyze_unified.py "What is BTC doing?"

# Different symbol
python trader-cli/contrib/tv_polymarket/analyze_unified.py \
  "Is SPX overextended?" --symbol SPX

# Verbose: show collected data before Claude response
python trader-cli/contrib/tv_polymarket/analyze_unified.py \
  "Analyze" --verbose

# Preview signal data only (no Claude)
python trader-cli/contrib/tv_polymarket/signal_integrator.py --symbol BTCUSDT
python trader-cli/contrib/tv_polymarket/signal_integrator.py --symbol SPX --json

# Find Polymarket markets for a symbol
python trader-cli/contrib/tv_polymarket/polymarket_markets.py --symbol BTCUSDT
python trader-cli/contrib/tv_polymarket/polymarket_markets.py --symbol SPX
```

---

## Claude output format

```
📊 Status
(TV chart + Binance momentum combined)

🎲 Polymarket Consensus
(Prediction market odds vs technical reality -- alignment or divergence)

💡 Assessment
(Buy / Sell / Neutral)

🧠 Rationale
(Up to 3 points referencing all three sources)

⚠️ Caution
(Especially flags when the three sources diverge)

🎯 Key levels & opportunities
(Technical levels + Polymarket markets worth watching)
```

---

## Supported symbols

| Symbol | Polymarket search |
|--------|-------------------|
| BTCUSDT | bitcoin, BTC price |
| ETHUSDT | ethereum, ETH price |
| SOLUSDT | solana, SOL price |
| SPX / SPXUSD | S&P 500, SPX |
| NAS100 | nasdaq, QQQ |
| XAUUSD | gold price |
| EURUSD | euro, EUR/USD |
| USDJPY | yen, USD/JPY |
| AAPL / TSLA / NVDA / MSFT | company name |

Add more in `polymarket_markets.py -> SYMBOL_KEYWORDS`.

---

## Graceful degradation

TradingView not running? No problem:
- TV unavailable -> Binance + Polymarket analysis only
- Binance unavailable -> TV + Polymarket analysis only
- Both unavailable -> Polymarket + RAG memory only
