"""
copy_trading package

Tools for identifying high-performing Polymarket wallets and monitoring
their trades for copy trading opportunities.

Modules:
    leaderboard    -- fetch and filter top wallets from the Data API
    wallet_scorer  -- score wallets by win rate, PnL, recency, and consistency
    monitor        -- poll watched wallets and alert on new trades
"""
