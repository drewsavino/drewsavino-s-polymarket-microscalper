# drewsavino's polymarket microscalper

---

## Table of Contents

- How It Works — High Level
- Architecture Overview
- Module Breakdown
  - scalper.py — Main Loop
  - brain.py — AI Decision Engine
  - markets.py — Data & Signal Engine
  - executioner.py — Order Execution
  - exits.py — Position Exit System
  - state.py — Global State
  - config.py — Configuration
  - logger.py — Logging
- Trading Strategy
- Threshold & Risk System
- Exit Conditions
- Circuit Breakers
- Setup & Usage
- Simulation Mode

---

## How It Works — High Level

```
Every 15 seconds (SCAN cycle):
  ├── Fetch live market prices from Polymarket's Gamma API
  ├── Compute technical signals (momentum, volume, trend)
  └── Check all open positions for exit conditions (TP / SL / trailing / time)

Every 30 seconds (BRAIN cycle):
  ├── Send portfolio state + market data to Claude AI
  ├── Claude returns a structured JSON decision: buy or hold
  └── If buy: validate thresholds → size position → execute order
```

The scan cycle runs **twice as often** as the brain cycle so exits are caught quickly without doubling the cost of AI API calls.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                      scalper.py                         │
│                   (Main event loop)                     │
└────────┬──────────────┬───────────────┬─────────────────┘
         │              │               │
         ▼              ▼               ▼
   markets.py       brain.py        exits.py
  (fetch prices   (Claude AI      (TP / SL /
   + signals)      decision)       trailing)
         │              │               │
         └──────────────▼───────────────┘
                    state.py
               (single shared dict)
                        │
            ┌───────────┴────────────┐
            ▼                        ▼
      executioner.py            config.py
    (place orders on           (all thresholds
      Polymarket CLOB)          and parameters)
```

All modules read from and write to the single `state` dictionary created in `state.py`. Nothing is stored in global variables; the entire bot's memory lives in that one object.

---

## Module Breakdown

### `scalper.py` — Main Loop

The entry point and orchestrator. Runs two interleaved loops:

| Cycle | Interval | What it does |
|---|---|---|
| **Scan** | Every 15s | Fetch prices → check exits → circuit breaker check |
| **Brain** | Every 30s | Fetch prices → Claude analysis → execute trade |

Also handles:
- **Startup banner** with configured parameters
- **Graceful shutdown** via `Ctrl+C` with a full session summary
- **Session duration** limit (set `DURATION_HOURS=0` in `.env` to run indefinitely)
- **Banner printing** every 10 brain cycles as a status heartbeat

---

### `brain.py` — AI Decision Engine

The core intelligence of the bot. Every brain cycle it:

1. **Builds a system prompt** that defines Claude's persona as a quantitative scalper, including exact rules for minimum edge, max position size, position count, and loss streak behavior.
2. **Builds a user prompt** containing:
   - Current portfolio balance, P&L, win rate, and streak
   - All open positions with real-time P&L
   - The last 15 closed trades so Claude can learn from patterns
   - Up to 20 filtered short-term markets with their computed signals
3. **Calls the Anthropic API** (primary model first, fallback model on failure)
4. **Parses the JSON response** — Claude must return a structured decision object

Claude's response schema:

```json
{
  "market_read": "Brief market assessment",
  "top_opportunities": [{ "condition_id": "...", "edge": 0.07, ... }],
  "decision": {
    "action": "buy | hold",
    "condition_id": "...",
    "outcome": "YES | NO",
    "amount_usdc": 5.00,
    "price": 0.42,
    "confidence": 0.72,
    "edge": 0.08,
    "reasoning": "..."
  }
}
```

If the API fails entirely, the bot returns a safe `hold` decision with zero confidence and continues operating.

---

### `markets.py` — Data & Signal Engine

Fetches live market data from Polymarket's public **Gamma REST API** and computes technical signals on each market.

**Filtering pipeline:**
1. Pull top `TOP_MARKETS` (200) markets sorted by volume
2. Remove markets below `MIN_VOLUME` ($100) or `MIN_LIQUIDITY` ($100)
3. Remove markets resolving in `> MAX_HOURS_TO_END` hours (default 720h)
4. Remove already-closed markets

**Signals computed per market:**

| Signal | Description |
|---|---|
| `momentum_1m` | Price delta over the last 1 minute |
| `momentum_5m` | Price delta over the last 5 minutes |
| `momentum_15m` | Price delta over the last 15 minutes |
| `volatility` | Standard deviation of prices over the last 10 minutes |
| `vol_spike` | `True` if recent volume is ≥ 2.5× the 10–30 min average |
| `trend` | Categorical: `up_strong`, `up`, `flat`, `down`, `down_strong` |

Markets are cached for `MARKET_CACHE_SECONDS` (12s) so rapid scan cycles don't hammer the API. Sorting prioritizes markets resolving soonest, then by volume.

---

### `executioner.py` — Order Execution

Handles all interaction with Polymarket's **CLOB (Central Limit Order Book) API**.

**Execution flow when Claude says `buy`:**

```
Claude decision
    │
    ▼
Validate confidence ≥ MIN_CONFIDENCE (0.55)
    │
    ▼
Validate edge ≥ MIN_EDGE (2%)
    │
    ▼
Check position limits (max 10 open, not blacklisted)
    │
    ▼
Compute position size (edge-based, streak-adjusted)
    │
    ▼
Place FOK market order → on rejection → GTC limit order
    │
    ▼
Record position in state["positions"]
```

**Dynamic position sizing:**

| Edge Strength | Base Size |
|---|---|
| Weak (5–8%) | 3% of balance |
| Normal (8–12%) | `MAX_RISK_PCT` (default 5%) of balance |
| Strong (> 12%) | 8% of balance, capped at $10 |

Additionally:
- A **losing streak ≤ −3** halves the position size automatically
- A **winning streak ≥ 3** increases size by 20%
- All sizes are hard-capped at `MAX_ORDER_USDC` ($10), 15% of balance, and the remaining risk budget

If `POLY_PRIVATE_KEY` is not set, the module runs in **simulation mode** — all orders are logged but never sent to the exchange.

---

### `exits.py` — Position Exit System

Runs every scan cycle (15s) and checks each open position against five exit conditions in priority order:

| # | Exit Type | Condition |
|---|---|---|
| 1 | **Take Profit** | P&L ≥ +15% (or +25% for high-edge trades) |
| 2 | **Stop Loss** | P&L ≤ −8% |
| 3 | **Trailing Stop** | Price drops ≥ 3.5% from its peak while in profit |
| 4 | **Time Stop** | Position is flat (< ±3%) after 20+ minutes |
| 5 | **Breakeven** | After +8% gain, stop loss floor rises to 0% |

When a position closes:
- Balance is updated with realized P&L
- Win/loss counters and streak are updated
- Losing trades add the market to the **blacklist** (skipped for ~50 cycles)
- The trade is appended to `closed_trades` history for Claude to learn from
- `sell_position()` is called in `executioner.py` to place the sell order

The module also maintains a **max drawdown** tracker by comparing current equity to the session peak.

---

### `state.py` — Global State

Creates and owns the single dictionary that all modules share. No module stores its own persistent data outside this object.

Key fields:

```python
{
  "balance":       float,        # Available USDC
  "positions":     dict,         # Open trades keyed by condition_id
  "closed_trades": list,         # Full history of resolved trades
  "price_history": dict,         # Per-market deque of (timestamp, price, volume)
  "signals":       dict,         # Latest computed signals per market
  "cycle":         int,          # Brain cycles run
  "wins":          int,
  "losses":        int,
  "streak":        int,          # Positive = win streak, negative = loss streak
  "max_drawdown":  float,
  "peak_balance":  float,
  "blacklist":     set,          # Condition IDs to temporarily avoid
}
```

---

### `config.py` — Configuration

All tunable parameters in one place. Sensitive credentials are loaded from a `.env` file via `python-dotenv` — **never hardcoded**.

Key parameter groups:

| Group | Parameters |
|---|---|
| **Credentials** | `ANTHROPIC_KEY`, `POLY_PRIVATE_KEY`, `POLY_FUNDER` |
| **Timing** | `SCAN_SECONDS=15`, `BRAIN_SECONDS=30` |
| **AI Models** | `MODEL`, `FALLBACK_MODEL` |
| **Market Filters** | `MIN_VOLUME`, `MAX_HOURS_TO_END`, `TOP_MARKETS` |
| **Risk Limits** | `MAX_RISK_PCT`, `MAX_POSITIONS`, `MAX_ORDER_USDC` |
| **Exit Thresholds** | `TP_NORMAL`, `SL_NORMAL`, `TRAILING_PCT`, `TIME_STOP_MIN` |
| **Circuit Breakers** | `CB_MAX_LOSS_PCT`, `CB_MAX_STREAK`, `CB_MAX_DRAWDOWN` |

---

### `logger.py` — Logging

Color-coded terminal output with timestamps. Each log tag maps to a distinct ANSI color for fast visual scanning:

| Tag | Color | Used for |
|---|---|---|
| `SYS` | Magenta | System lifecycle events |
| `INFO` | Blue | General info |
| `TRADE` | Green | Trade open/close events |
| `THINK` | Yellow | Claude's reasoning |
| `WARN` | Red | Warnings and errors |
| `SCALP` | Cyan | Signal analysis |
| `EXEC` | Bold green | Order confirmations |

Also provides `banner()` for periodic status summaries showing balance, P&L, win rate, streak, and drawdown.

---

## Trading Strategy

The bot implements **short-term prediction market scalping** — not directional speculation. The core thesis is:

> Prediction markets occasionally misprice events due to low liquidity, delayed information, or emotional trading. These mispricings revert quickly. The bot targets markets resolving within **hours** where price corrections happen fast enough to profit before the position decays.

Claude is given:
- The current market price (implied probability)
- Technical signals indicating momentum, volume pressure, and trend
- Portfolio context (P&L, streak, available risk budget)
- Recent trade history to self-correct

Claude returns its **estimated true probability** for the event and the **edge** (difference from market price). If the edge exceeds the minimum threshold and confidence is sufficient, the bot enters.

---

## Threshold & Risk System

A trade is only executed when **all** of the following gates pass:

```
Gate 1:  Claude action == "buy"
Gate 2:  confidence >= 0.55
Gate 3:  |edge| >= 2%
Gate 4:  condition_id not already in positions
Gate 5:  condition_id not in blacklist
Gate 6:  open position count < 10
Gate 7:  computed amount >= $1.00 (MIN_ORDER_USDC)
Gate 8:  computed amount <= available balance
Gate 9:  total risk in use < 30% of balance
```

Position size scales with edge strength and is compressed automatically during losing streaks to protect capital.

---

## Exit Conditions

```
Position opened at entry_price
         │
         ├── P&L ≥ +15%? ──────────────────► TAKE PROFIT ✅
         │
         ├── P&L ≤ −8%? ──────────────────► STOP LOSS ❌
         │
         ├── Peak reached, now −3.5% from peak? ► TRAILING STOP ✅
         │
         ├── Flat (< ±3%) for 20+ minutes? ───► TIME STOP (recycle capital)
         │
         └── Gained +8% at any point?
               └── SL floor raised to 0% ───── BREAKEVEN LOCK
```

---

## Circuit Breakers

Three automatic safety pauses protect against catastrophic losses:

| Trigger | Action |
|---|---|
| Session P&L drops below −15% | Pause all trading for **10 minutes** |
| 5 consecutive losses | Pause for **5 minutes**, reset streak to −2 |
| Drawdown exceeds 20% from peak | Pause for **10 minutes** |

---

## Setup & Usage

### Prerequisites

```bash
pip install anthropic requests python-dotenv py-clob-client
```

### Environment Variables

Create a `.env` file in the project root:

```env
ANTHROPIC_KEY=sk-ant-api03-...
POLY_PRIVATE_KEY=0x...          # Leave blank to run in simulation mode
POLY_FUNDER=0x...               # Your Polymarket wallet address
INITIAL_BALANCE=100
DURATION_HOURS=0                # 0 = run forever
MODEL=claude-sonnet-4-6
MAX_RISK_PCT=0.05
```

### Run

```bash
python scalper.py
```

Press `Ctrl+C` at any time to stop and print a full session summary including per-trade history, win rate, average win/loss, profit factor, and exit type breakdown.

---

## Simulation Mode

If `POLY_PRIVATE_KEY` is not set (or `py-clob-client` is not installed), the bot runs in **full simulation mode**:

- All market data is fetched live from Polymarket
- Claude makes real decisions using the real Anthropic API
- Orders are logged with `[SIM]` prefix but never sent to the exchange
- All P&L tracking, exits, and circuit breakers function normally

This lets you validate strategy and prompt quality without risking real funds.

## Disclaimer
This project is intended for educational and experimental purposes only. The Polymarket Microscalper is an autonomous trading bot that interacts with real prediction markets and may involve the use of real funds. By using, copying, or modifying this code, you acknowledge and accept that you do so entirely at your own risk. The creator (David Savino) makes no guarantees regarding profitability, accuracy, or reliability of the bot's decisions. Prediction market trading carries significant financial risk, and past performance — if any — is not indicative of future results. This tool is not financial advice. You are solely responsible for any losses, damages, or consequences that arise from using this software. Always trade responsibly and never risk more than you can afford to lose.
