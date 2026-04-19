"""
tv_polymarket integration package.

Bridges TradingView chart data, Binance price feed, and Polymarket
prediction market odds into a unified signal for Claude analysis.

Modules:
    polymarket_markets   -- find Polymarket markets relevant to a TV symbol
    signal_integrator    -- collect all three data sources asynchronously
    unified_prompt       -- build the combined prompt for Claude
"""
