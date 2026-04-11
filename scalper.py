"""
scalper.py — Main entry point for the Polymarket AI Scalper.

Usage:
    python scalper.py
"""

import time
import signal
import sys
from datetime import datetime

import config
from state import create_initial_state
from logger import log, banner, COLORS
from markets import fetch_markets
from brain import analyze_markets

# BUG FIX: file is named executioner.py, not executor.py
from executioner import init_polymarket, execute_trade, get_real_balance, is_connected
from exits import check_exits


def print_startup_banner() -> None:
    c = COLORS
    print(f"""{c['B']}{c['SYS']}
╔══════════════════════════════════════════════════════════════╗
║              POLYMARKET AI SCALPER                           ║
╠══════════════════════════════════════════════════════════════╣
║  Model:    {config.MODEL:<51}║
║  Scan:     every {config.SCAN_SECONDS}s  |  Brain: every {config.BRAIN_SECONDS}s{' '*(30-len(str(config.SCAN_SECONDS))-len(str(config.BRAIN_SECONDS)))}║
║  Focus:    markets <= {config.MAX_HOURS_TO_END}h to resolution{' '*26}║
║  TP/SL:    +{config.TP_NORMAL}% / {config.SL_NORMAL}%  (trailing {config.TRAILING_PCT}%){' '*22}║
║  Max pos:  {config.MAX_POSITIONS} positions x ${config.MAX_ORDER_USDC:.0f} max each{' '*22}║
║  Breaker:  {config.CB_MAX_LOSS_PCT}% P&L / {abs(config.CB_MAX_STREAK)} losses / {config.CB_MAX_DRAWDOWN}% DD{' '*17}║
╚══════════════════════════════════════════════════════════════╝
{c['R']}""")


def circuit_breaker_check(state: dict) -> bool:
    """Pause trading if losses breach emergency thresholds."""
    pnl_pct = ((state["balance"] - config.INITIAL_BALANCE)
               / config.INITIAL_BALANCE * 100)

    if pnl_pct <= config.CB_MAX_LOSS_PCT:
        log("WARN", f"CIRCUIT BREAKER: P&L {pnl_pct:.1f}% — Pausing 10 minutes")
        time.sleep(600)
        return True

    if state["streak"] <= config.CB_MAX_STREAK:
        log("WARN", f"CIRCUIT BREAKER: {abs(state['streak'])} consecutive losses — Pausing 5 minutes")
        time.sleep(300)
        state["streak"] = -2
        return True

    if state["max_drawdown"] > config.CB_MAX_DRAWDOWN:
        log("WARN", f"CIRCUIT BREAKER: Drawdown {state['max_drawdown']:.1f}% — Pausing 10 minutes")
        time.sleep(600)
        return True

    return False


def print_session_summary(state: dict) -> None:
    log("SYS", "=" * 60)
    log("SYS", "SESSION COMPLETE")
    log("SYS", "=" * 60)
    banner(state)

    trades = state["closed_trades"]
    if not trades:
        log("SYS", "No trades were executed this session.")
        return

    log("SYS", "\nTrade History:")
    for t in trades:
        icon = "WIN" if t["result"] == "win" else "LOSS"
        log("SYS", f"  [{icon}] {t['outcome']} | {t['pnl']:+.2f} | "
                    f"{t['exit_reason']} | {t.get('duration_min', 0):.0f}min | "
                    f"{t['question'][:40]}")

    win_trades  = [t for t in trades if t["result"] == "win"]
    loss_trades = [t for t in trades if t["result"] == "loss"]

    print(f"\n-- Statistics --")
    print(f"  Total trades:  {len(trades)}")
    print(f"  Win rate:      {len(win_trades)}/{len(trades)} "
          f"({len(win_trades)/max(len(trades),1)*100:.0f}%)")

    if win_trades:
        avg_win     = sum(t["pnl"] for t in win_trades) / len(win_trades)
        avg_win_dur = sum(t.get("duration_min", 0) for t in win_trades) / len(win_trades)
        print(f"  Avg win:       +${avg_win:.2f} ({avg_win_dur:.0f}min avg)")

    if loss_trades:
        avg_loss     = sum(t["pnl"] for t in loss_trades) / len(loss_trades)
        avg_loss_dur = sum(t.get("duration_min", 0) for t in loss_trades) / len(loss_trades)
        print(f"  Avg loss:      ${avg_loss:.2f} ({avg_loss_dur:.0f}min avg)")

    if win_trades and loss_trades:
        avg_w = sum(t["pnl"] for t in win_trades) / len(win_trades)
        avg_l = abs(sum(t["pnl"] for t in loss_trades) / len(loss_trades))
        print(f"  Profit factor: {avg_w/max(avg_l,0.01):.2f}")

    print(f"  Max drawdown:  {state['max_drawdown']:.1f}%")
    print(f"  Total P&L:     {'+'if state['total_pnl']>=0 else ''}{state['total_pnl']:.2f} USDC")

    reasons = {}
    for t in trades:
        r = t.get("exit_reason", "unknown")
        reasons.setdefault(r, {"count": 0, "pnl": 0.0})
        reasons[r]["count"] += 1
        reasons[r]["pnl"]   += t.get("pnl", 0)

    print("\n-- By Exit Type --")
    for reason, data in sorted(reasons.items(), key=lambda x: -x[1]["pnl"]):
        print(f"  {reason:<20}: {data['count']} trades | ${data['pnl']:+.2f}")


def main() -> None:
    state = create_initial_state()

    def signal_handler(sig, frame):
        log("SYS", "\nInterrupted! Printing summary...")
        print_session_summary(state)
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    print_startup_banner()

    if not config.ANTHROPIC_KEY:
        log("WARN", "ANTHROPIC_KEY not set in .env — cannot start")
        log("WARN", "Open your .env file and add: ANTHROPIC_KEY=sk-ant-api03-...")
        return

    log("SYS", f"Balance: ${config.INITIAL_BALANCE:.2f} USDC | Duration: {config.DURATION_HOURS}h (0=unlimited)")

    poly_ok = init_polymarket()
    if poly_ok:
        log("SYS", "MODE: LIVE TRADING")
        real_balance = get_real_balance()
        if real_balance and real_balance > 0:
            state["balance"]      = real_balance
            state["peak_balance"] = real_balance
            log("INFO", f"Real balance: ${real_balance:.2f}")
    else:
        log("SYS", "MODE: SIMULATION (no Polymarket keys — safe to test)")

    log("SYS", "Scalper started! Press Ctrl+C to stop and see summary.")

    end_time = (time.time() + config.DURATION_HOURS * 3600
                if config.DURATION_HOURS > 0 else float("inf"))

    last_brain_time = 0

    while time.time() < end_time:
        state["scan_cycle"] += 1
        now = time.time()

        # 1. Fetch market prices
        markets = fetch_markets(state)
        if not markets:
            log("WARN", "No markets returned — retrying in 15s")
            time.sleep(15)
            continue

        # 2. Check exits (runs every scan — fast reaction)
        if state["positions"]:
            check_exits(state, markets)

        # 3. Circuit breaker
        if circuit_breaker_check(state):
            continue

        # 4. Brain analysis (every BRAIN_SECONDS)
        if now - last_brain_time >= config.BRAIN_SECONDS:
            state["cycle"] += 1
            remaining = (end_time - now) / 3600 if end_time != float("inf") else 0

            log("SYS", "-" * 56)
            log("SYS", f"CYCLE #{state['cycle']} | ${state['balance']:.2f} | "
                       f"{len(state['positions'])} positions open"
                       + (f" | {remaining:.1f}h left" if remaining else ""))

            analysis = analyze_markets(state, markets)
            execute_trade(state, analysis)
            last_brain_time = now

            if state["cycle"] % 10 == 0:
                banner(state)

        time.sleep(config.SCAN_SECONDS)

    print_session_summary(state)


if __name__ == "__main__":
    main()
