"""
brain.py — Claude AI integration for trading decisions.

This module handles all communication with the Anthropic API:
  1. Builds the system prompt (trader persona + rules)
  2. Builds the user prompt (portfolio + markets + history)
  3. Sends the request and parses the JSON response
  4. Falls back to the backup model on failure
"""

import json
import time
from datetime import datetime

import anthropic

import config
from logger import log


# Initialize the Anthropic client once at module level
_client = anthropic.Anthropic(api_key=config.ANTHROPIC_KEY)


def analyze_markets(state: dict, markets: list[dict]) -> dict:
    """
    Send market data to Claude and get a trading decision.

    This is the "brain" of the bot. It constructs a detailed prompt
    with the current portfolio state, open positions, trade history,
    and filtered market data. Claude returns a structured JSON response
    with its analysis and a buy/hold decision.

    Args:
        state:    Global state dict (portfolio, positions, history)
        markets:  List of market dicts from markets.py

    Returns:
        Dict with structure:
        {
            "market_read": "brief market assessment",
            "top_opportunities": [...],
            "decision": {
                "action": "buy" | "hold",
                "condition_id": str | None,
                "yes_token_id": str | None,
                "no_token_id": str | None,
                "question": str | None,
                "outcome": "YES" | "NO" | None,
                "amount_usdc": float | None,
                "price": float | None,
                "confidence": float,
                "edge": float,
                "reasoning": str,
            }
        }
    """
    system_prompt = _build_system_prompt(state)
    user_prompt = _build_user_prompt(state, markets)

    # ── Try primary model first ──
    result = _call_claude(config.MODEL, system_prompt, user_prompt)

    # ── Fallback to backup model on failure ──
    if result is None and config.FALLBACK_MODEL:
        log("WARN", f"Primary model failed. Trying {config.FALLBACK_MODEL}...")
        result = _call_claude(config.FALLBACK_MODEL, system_prompt, user_prompt)

    # ── Return safe default if everything fails ──
    if result is None:
        return {
            "decision": {
                "action": "hold",
                "reasoning": "All models failed",
                "confidence": 0,
                "edge": 0,
            }
        }

    # Log Claude's market assessment if present
    market_read = result.get("market_read", "")
    if market_read:
        log("THINK", f"📖 {market_read[:120]}")

    return result


# ══════════════════════════════════════════════════════════════
# PROMPT CONSTRUCTION
# ══════════════════════════════════════════════════════════════

def _build_system_prompt(state: dict) -> str:
    """

    The system prompt sets:
      - Role and expertise level
      - Trading strategy (short-term scalping)
      - Available signals and how to interpret them
      - Risk rules and position sizing guidelines
      - Output format requirements
    """
    risk_used = sum(p["amount"] for p in state["positions"].values())
    risk_available = max(0, state["balance"] * config.MAX_RISK_TOTAL - risk_used)

    return f"""You are an elite quantitative trader specialized in prediction market scalping.

═══ STRATEGY: SHORT-TERM SCALPING ═══

FOCUS: Markets resolving in MINUTES to HOURS.
GOAL: Find mispricings that will correct quickly.

SIGNALS YOU RECEIVE:
1. MOMENTUM — Price changes over 1m/5m/15m windows.
   - Positive momentum + volume spike = potential informed trading.
   - Divergence between timeframes = possible reversal.
2. VOLUME SPIKE — True if recent volume is {config.VOLUME_SPIKE_MULT}x the average.
   - Often signals breaking news or large informed orders.
3. SPREAD — Low spread = easy entry/exit. High spread = danger.
4. HOURS TO END — Time until market resolves.
   - <6h: highest urgency, strong signals only.
   - 6-24h: sweet spot for scalping.
   - 24-72h: acceptable for strong edges.
5. TREND — Categorical: up_strong, up, flat, down, down_strong.

ABSOLUTE RULES:
• Minimum edge: {config.MIN_EDGE * 100:.0f}% between your estimated probability and market price.
• Max per trade: ${min(state['balance'] * config.MAX_RISK_PCT, risk_available):.2f}
• Max open positions: {config.MAX_POSITIONS} (currently {len(state['positions'])})
• If on a losing streak ({state['streak']}), be MORE selective.
• If no clear edge exists, say HOLD. Not trading is a valid decision.
• NEVER chase losses.

POSITION SIZING:
• Edge 5-8%: small position (${state['balance'] * 0.03:.2f})
• Edge 8-12%: normal position (${state['balance'] * config.MAX_RISK_PCT:.2f})
• Edge >12%: large position (${min(state['balance'] * 0.08, config.MAX_ORDER_USDC):.2f})

Respond ONLY with valid JSON. No markdown. No extra text."""


def _build_user_prompt(state: dict, markets: list[dict]) -> str:
    """
    Build the user prompt with current portfolio state and market data.

    Includes:
      - Portfolio summary (balance, P&L, win rate)
      - Open positions with current P&L
      - Recent trade history (for Claude to learn from)
      - Filtered short-term market data with signals
    """
    pnl = state["balance"] - config.INITIAL_BALANCE
    total = state["wins"] + state["losses"]
    win_rate = (state["wins"] / total * 100) if total > 0 else 0
    risk_used = sum(p["amount"] for p in state["positions"].values())
    risk_available = max(0, state["balance"] * config.MAX_RISK_TOTAL - risk_used)

    # ── Open positions context ──
    positions_text = _format_positions(state, markets)

    # ── Recent trade history (so Claude can learn from past mistakes) ──
    history_text = _format_trade_history(state)

    # ── Filter markets: remove blacklisted, keep short-term ──
    filtered = [
        m for m in markets
        if m["condition_id"] not in state["blacklist"]
        and (m.get("hours_to_end") is None
             or m["hours_to_end"] <= config.MAX_HOURS_TO_END)
    ][:20]  # Limit to 20 to control prompt size / token cost

    return f"""═══ CYCLE #{state['cycle']} — {datetime.now().strftime("%H:%M:%S")} ═══

PORTFOLIO:
  Balance: ${state['balance']:.2f} USDC
  P&L: {'+'if pnl>=0 else ''}{pnl:.2f} ({(pnl/config.INITIAL_BALANCE*100):+.1f}%)
  Win rate: {win_rate:.0f}% ({state['wins']}W/{state['losses']}L)
  Streak: {state['streak']}
  Risk in use: ${risk_used:.2f} | Available: ${risk_available:.2f}
  Max drawdown: {state['max_drawdown']:.1f}%
{positions_text}{history_text}

═══ SHORT-TERM MARKETS ({len(filtered)}) ═══
{json.dumps(filtered, indent=1, ensure_ascii=False)}

Respond with JSON:
{{
  "market_read": "1-2 sentences on overall market state",
  "top_opportunities": [
    {{
      "condition_id": "...",
      "question": "...",
      "my_probability": 0.XX,
      "market_price": 0.XX,
      "edge": 0.XX,
      "signals": "supporting signals",
      "risk": "main risk",
      "time_horizon": "estimated minutes/hours"
    }}
  ],
  "decision": {{
    "action": "buy" | "hold",
    "condition_id": "..." | null,
    "yes_token_id": "..." | null,
    "no_token_id": "..." | null,
    "question": "..." | null,
    "outcome": "YES" | "NO" | null,
    "amount_usdc": number | null,
    "price": number | null,
    "confidence": 0.0-1.0,
    "edge": number,
    "reasoning": "2-3 sentences"
  }}
}}"""


def _format_positions(state: dict, markets: list[dict]) -> str:
    """Format open positions with current P&L for the prompt."""
    if not state["positions"]:
        return ""

    lines = ["\n\n── OPEN POSITIONS ──"]
    for cid, pos in state["positions"].items():
        elapsed = (datetime.now() -
                   datetime.fromisoformat(pos["timestamp"])).total_seconds() / 60
        # Find current market price
        market = next((m for m in markets if m["condition_id"] == cid), None)
        current = "?"
        pnl_pct = 0
        if market:
            current = (market["yes_price"] if pos["outcome"] == "YES"
                       else market["no_price"])
            if isinstance(current, (int, float)) and pos["entry_price"] > 0:
                pnl_pct = (current - pos["entry_price"]) / pos["entry_price"] * 100

        lines.append(
            f"  {pos['outcome']} @ {pos['entry_price']:.3f} → {current} "
            f"({pnl_pct:+.1f}%) | ${pos['amount']:.2f} | {elapsed:.0f}min | "
            f"{pos['question'][:50]}"
        )
    return "\n".join(lines)


def _format_trade_history(state: dict) -> str:
    """Format recent closed trades for Claude to learn from."""
    recent = state["closed_trades"][-15:]
    if not recent:
        return ""

    lines = ["\n\n── RECENT TRADES (learn from these) ──"]
    for trade in recent:
        icon = "W" if trade.get("result") == "win" else "L"
        lines.append(
            f"  [{icon}] {trade.get('outcome')} | "
            f"{trade.get('pnl', 0):+.2f} | "
            f"{trade.get('exit_reason', '?')} | "
            f"{trade.get('duration_min', 0):.0f}min | "
            f"{trade.get('question', '')[:45]}"
        )

    # Aggregate stats
    wins = [t for t in recent if t.get("result") == "win"]
    losses = [t for t in recent if t.get("result") == "loss"]
    if wins:
        avg_win = sum(t["pnl"] for t in wins) / len(wins)
        avg_dur = sum(t.get("duration_min", 0) for t in wins) / len(wins)
        lines.append(f"  Avg win: +${avg_win:.2f} in {avg_dur:.0f}min")
    if losses:
        avg_loss = sum(t["pnl"] for t in losses) / len(losses)
        lines.append(f"  Avg loss: ${avg_loss:.2f}")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# API CALL
# ══════════════════════════════════════════════════════════════

def _call_claude(model: str, system: str, user: str) -> dict | None:
    """
    Make a single API call to Claude and parse the JSON response.

    Args:
        model:   Model identifier (e.g., "claude-sonnet-4-6")
        system:  System prompt text
        user:    User prompt text

    Returns:
        Parsed JSON dict, or None on failure.
    """
    try:
        t0 = time.time()
        response = _client.messages.create(
            model=model,
            max_tokens=2500,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        elapsed = time.time() - t0
        log("THINK", f"🧠 {model} responded in {elapsed:.1f}s")

        # Extract text from response
        text = response.content[0].text.strip()

        # Strip markdown code fences if present
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        return json.loads(text)

    except json.JSONDecodeError as e:
        log("WARN", f"Invalid JSON from {model}: {e}")
        return None
    except anthropic.RateLimitError:
        log("WARN", f"Rate limited on {model}. Will retry next cycle.")
        return None
    except anthropic.APIError as e:
        log("WARN", f"API error ({model}): {e}")
        return None
    except Exception as e:
        log("WARN", f"Unexpected error ({model}): {e}")
        return None
