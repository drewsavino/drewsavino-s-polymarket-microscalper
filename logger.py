"""
logger.py — Colored console logging for the scalper.

Provides a simple log() function with colored tags and timestamps,
plus a banner() function for periodic status summaries.
"""

from datetime import datetime
import config

# ── ANSI color codes for terminal output ────────────────
# Each log tag gets a distinct color for easy visual scanning.
COLORS = {
    "SYS":   "\033[95m",   # Magenta  — system messages
    "INFO":  "\033[94m",   # Blue     — general info
    "TRADE": "\033[92m",   # Green    — trade executions
    "THINK": "\033[93m",   # Yellow   — AI reasoning
    "WARN":  "\033[91m",   # Red      — warnings & errors
    "SCALP": "\033[96m",   # Cyan     — scalping signals
    "EXEC":  "\033[92;1m", # Bold green — order confirmations
    "R":     "\033[0m",    # Reset
    "B":     "\033[1m",    # Bold
    "DIM":   "\033[2m",    # Dim
}


def log(tag: str, msg: str) -> None:
    """
    Print a colored, timestamped log line.

    Args:
        tag:  Category label (SYS, INFO, TRADE, THINK, WARN, SCALP, EXEC)
        msg:  Message text to display

    Example output:
        [14:32:05.123][TRADE] ✅ TP +12.3% (+$1.45) | Will BTC hit 100k? [8min]
    """
    color = COLORS.get(tag, COLORS["INFO"])
    reset = COLORS["R"]
    bold = COLORS["B"]
    timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"{color}{bold}[{timestamp}][{tag:5}]{reset} {msg}")


def banner(state: dict) -> None:
    """
    Print a formatted status banner with key portfolio metrics.

    Called periodically (every N cycles) to give a quick overview
    of the bot's performance without scrolling through individual logs.

    Args:
        state: The global state dictionary from state.py
    """
    s = state
    pnl = s["balance"] - config.INITIAL_BALANCE
    pct = (pnl / config.INITIAL_BALANCE) * 100 if config.INITIAL_BALANCE > 0 else 0
    elapsed = (datetime.now() - s["start_time"]).total_seconds() / 3600
    total = s["wins"] + s["losses"]
    wr = (s["wins"] / total * 100) if total > 0 else 0
    pos_val = sum(p["amount"] for p in s["positions"].values())

    # Streak icon: fire for hot, snowflake for cold
    streak_icon = "🔥" if s["streak"] > 2 else ("❄️" if s["streak"] < -2 else "➡️")

    bold = COLORS["B"]
    reset = COLORS["R"]

    print(f"\n{bold}{'═' * 60}")
    print(f"  POLYMARKET AI SCALPER — Status")
    print(f"{'─' * 60}")
    print(f"  💰 Free balance:     ${s['balance']:.2f} USDC")
    print(f"  📦 In positions:     ${pos_val:.2f} ({len(s['positions'])} open)")
    print(f"  📈 Realized P&L:     {'+'if s['total_pnl']>=0 else ''}{s['total_pnl']:.2f} USDC")
    print(f"  📊 Total P&L:        {'+'if pnl>=0 else ''}{pnl:.2f} ({pct:+.1f}%)")
    print(f"  🎯 Win rate:         {wr:.0f}% ({s['wins']}W / {s['losses']}L)")
    print(f"  {streak_icon} Streak:            {'+' if s['streak']>0 else ''}{s['streak']}")
    print(f"  📉 Max drawdown:     {s['max_drawdown']:.1f}%")
    print(f"  ⏱  Runtime:          {elapsed:.1f}h | Cycles: {s['cycle']}")
    print(f"  🧠 Model:            {config.MODEL}")
    print(f"{'═' * 60}{reset}\n")