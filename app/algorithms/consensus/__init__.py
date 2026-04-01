"""
Consensus Engine — Multi-Timeframe Weighted Voting System.

Architecture:
    collector (Docker) → CSV data → data.py loader
    data.py → DataFrames per TF → voters.py → scored votes
    engine.py → aggregate score → BUY / SELL / HOLD
    consensus_strategy.py → BaseStrategy wrapper for bot runner
"""
