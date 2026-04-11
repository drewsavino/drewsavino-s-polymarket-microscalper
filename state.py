"""
state.py — Global state management for the scalper.

Centralizes all mutable state into a single dictionary. This makes it
easy to inspect, serialize (for future DB support), and pass around.

No state lives outside this dictionary — if the bot needs to remember
something, it goes here.
"""

from datetime import datetime
from collections import deque

import config


def create_initial_state() -> dict:
    """
    Create a fresh state dictionary with all default values.

    Returns:
        dict: The initial state with all required keys.

    State keys:
        balance         — Current available USDC balance
        positions       — Dict of open positions {condition_id: position_data}
        closed_trades   — List of all completed trades with P&L
        price_history   — Price time series per market {cid: deque([(ts, price, vol)])}
        signals         — Computed signals per market {cid: signal_dict}
        cycle           — Number of brain (Claude) cycles completed
        scan_cycle      — Number of price scan cycles completed
        wins / losses   — Trade outcome counters
        total_pnl       — Cumulative realized P&L in USDC
        start_time      — Session start timestamp
        streak          — Current win/loss streak (+N wins, -N losses)
        max_drawdown    — Maximum drawdown percentage from peak
        peak_balance    — Highest equity value reached
        market_cache    — Cached market data from last fetch
        cache_time      — Timestamp of last market cache
        blacklist       — Set of condition_ids to temporarily avoid
    """
    return {
        # ── Portfolio ──
        "balance":        config.INITIAL_BALANCE,
        "positions":      {},
        "closed_trades":  [],

        # ── Market data ──
        "price_history":  {},
        "signals":        {},
        "market_cache":   [],
        "cache_time":     0,

        # ── Counters ──
        "cycle":          0,
        "scan_cycle":     0,
        "wins":           0,
        "losses":         0,

        # ── Performance tracking ──
        "total_pnl":      0.0,
        "start_time":     datetime.now(),
        "streak":         0,
        "max_drawdown":   0.0,
        "peak_balance":   config.INITIAL_BALANCE,

        # ── Safety ──
        "blacklist":      set(),
    }