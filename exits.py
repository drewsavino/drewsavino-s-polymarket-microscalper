"""
exits.py — Position exit system.

Checks all open positions against multiple exit conditions every scan cycle.
This runs MORE FREQUENTLY than the brain (every 15s vs every 30s) to ensure
we don't miss fast price moves.

Exit types (checked in this order):
  1. Take Profit   — Close at target profit %
  2. Stop Loss     — Close at max acceptable loss %
  3. Trailing Stop — If price drops X% from its peak, lock in gains
  4. Time Stop     — If position is flat after N minutes, free up capital
  5. Breakeven     — After +8%, stop loss moves to 0% (can't lose anymore)
"""

from datetime import datetime

import config
from logger import log

# BUG FIX: file is named executioner.py, not executor.py
from executioner import sell_position


def check_exits(state: dict, markets: list[dict]) -> None:
    """
    Check all open positions for exit conditions.

    Runs every SCAN_SECONDS (default 15s) and evaluates each position
    against TP, SL, trailing stop, time stop, and breakeven rules.
    """
    if not state["positions"]:
        return

    price_map = {m["condition_id"]: m for m in markets}
    positions_to_close = []

    for cid, position in state["positions"].items():
        market = price_map.get(cid)
        if not market:
            continue

        current_price = (market["yes_price"] if position["outcome"] == "YES"
                         else market["no_price"])
        entry_price = position["entry_price"]
        if entry_price <= 0:
            continue

        pnl_pct  = (current_price - entry_price) / entry_price * 100
        pnl_usdc = position["amount"] * (current_price / entry_price - 1)
        elapsed_min = (
            datetime.now() - datetime.fromisoformat(position["timestamp"])
        ).total_seconds() / 60

        # Track peak price for trailing stop
        if current_price > position.get("max_price", entry_price):
            position["max_price"] = current_price
        max_price = position.get("max_price", entry_price)

        # Determine TP/SL thresholds
        tp_threshold = config.TP_NORMAL
        if position.get("edge", 0) >= config.STRONG_EDGE:
            tp_threshold = config.TP_STRONG

        sl_threshold = config.SL_NORMAL
        time_stop    = position.get("time_stop_min", config.TIME_STOP_MIN)

        # Breakeven: after +8%, guarantee no loss
        if pnl_pct >= config.BREAKEVEN_AFTER:
            sl_threshold = max(sl_threshold, 0)

        exit_reason = None
        is_win = False

        # 1. Take Profit
        if pnl_pct >= tp_threshold:
            exit_reason = "take_profit"
            is_win = True

        # 2. Stop Loss
        elif pnl_pct <= sl_threshold:
            exit_reason = "stop_loss"
            is_win = pnl_usdc >= 0

        # 3. Trailing Stop
        elif max_price > entry_price and pnl_pct > 0:
            drop_from_peak = (max_price - current_price) / max_price * 100
            if drop_from_peak >= config.TRAILING_PCT:
                exit_reason = "trailing_stop"
                is_win = True

        # 4. Time Stop
        elif elapsed_min >= time_stop and abs(pnl_pct) < 3:
            exit_reason = "time_stop"
            is_win = pnl_usdc >= 0

        if exit_reason:
            _close_position(state, cid, position, pnl_usdc, pnl_pct,
                            elapsed_min, exit_reason, is_win)
            positions_to_close.append(cid)
        else:
            if state["cycle"] % 5 == 0:
                log("INFO", f"  {position['outcome']} "
                            f"{entry_price:.3f}→{current_price:.3f} "
                            f"({pnl_pct:+.1f}%) peak:{max_price:.3f} | "
                            f"{elapsed_min:.0f}min")

    for cid in positions_to_close:
        if cid in state["positions"]:
            del state["positions"][cid]

    _update_drawdown(state)

    if state["cycle"] % 50 == 0 and state["blacklist"]:
        state["blacklist"].clear()
        log("INFO", "Blacklist cleared")


def _close_position(state, cid, position, pnl_usdc, pnl_pct,
                    elapsed_min, exit_reason, is_win):
    state["balance"]   += position["amount"] + pnl_usdc
    state["total_pnl"] += pnl_usdc

    if is_win:
        state["wins"]   += 1
        state["streak"]  = max(1, state["streak"] + 1)
    else:
        state["losses"] += 1
        state["streak"]  = min(-1, state["streak"] - 1)
        state["blacklist"].add(cid)

    icons = {
        "take_profit":   "TP  WIN",
        "stop_loss":     "SL  LOSS",
        "trailing_stop": "TRAIL WIN",
        "time_stop":     "TIME EXIT",
    }
    icon = icons.get(exit_reason, "EXIT")
    log("TRADE", f"{icon} {pnl_pct:+.1f}% (${pnl_usdc:+.2f}) | "
                 f"{elapsed_min:.0f}min | {position['question'][:45]}")

    state["closed_trades"].append({
        **position,
        "pnl":          pnl_usdc,
        "pnl_pct":      pnl_pct,
        "result":       "win" if is_win else "loss",
        "exit_reason":  exit_reason,
        "duration_min": elapsed_min,
    })

    sell_position(position)


def _update_drawdown(state):
    current_equity = state["balance"] + sum(
        p["amount"] for p in state["positions"].values()
    )
    if current_equity > state["peak_balance"]:
        state["peak_balance"] = current_equity
    if state["peak_balance"] > 0:
        drawdown = (state["peak_balance"] - current_equity) / state["peak_balance"] * 100
        if drawdown > state["max_drawdown"]:
            state["max_drawdown"] = drawdown
