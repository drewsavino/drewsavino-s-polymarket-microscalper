"""
executor.py — Order execution via Polymarket's CLOB API.

Handles:
  1. Connecting to Polymarket and authenticating
  2. Validating trade decisions against risk limits
  3. Executing buy orders (market FOK, then limit GTC as fallback)
  4. Selling positions on exit signals
  5. Running in simulation mode when no keys are configured

The CLOB (Central Limit Order Book) API is Polymarket's order matching
engine. It supports:
  - Market orders (FOK = Fill-or-Kill, instant execution)
  - Limit orders (GTC = Good-til-Cancelled, sits in the book)

We prefer FOK for speed, falling back to GTC if the order book
can't fill the entire amount at once.
"""

from datetime import datetime

import config
from logger import log

# ── Polymarket SDK (optional — bot runs in sim mode without it) ──
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import MarketOrderArgs, OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL
    POLY_SDK_AVAILABLE = True
except ImportError:
    POLY_SDK_AVAILABLE = False

# Module-level client reference
_poly_client = None


# ══════════════════════════════════════════════════════════════
# CONNECTION
# ══════════════════════════════════════════════════════════════

def init_polymarket() -> bool:
    """
    Initialize the Polymarket CLOB client.

    Authenticates using the private key from config.
    Returns True if connected successfully, False otherwise.

    If POLY_PRIVATE_KEY is empty or the SDK isn't installed,
    the bot will run in simulation mode (no real trades).
    """
    global _poly_client

    if not POLY_SDK_AVAILABLE:
        log("WARN", "py-clob-client not installed. Running in simulation mode.")
        return False

    if not config.POLY_PRIVATE_KEY or not config.POLY_FUNDER:
        log("INFO", "No Polymarket keys configured. Running in simulation mode.")
        return False

    try:
        log("INFO", "Connecting to Polymarket CLOB API...")

        _poly_client = ClobClient(
            config.POLY_HOST,
            key=config.POLY_PRIVATE_KEY,
            chain_id=config.POLY_CHAIN_ID,
            signature_type=config.POLY_SIGNATURE_TYPE,
            funder=config.POLY_FUNDER,
        )

        # Derive API credentials from the private key
        _poly_client.set_api_creds(
            _poly_client.create_or_derive_api_creds()
        )

        # Try to verify the connection by checking balance
        try:
            balance_wei = _poly_client.get_balance()
            balance_usdc = int(balance_wei) / 1e6
            log("EXEC", f"✅ Connected to Polymarket | Balance: ${balance_usdc:.2f} USDC")
        except Exception as e:
            log("WARN", f"Connected but couldn't verify balance: {e}")

        return True

    except Exception as e:
        log("WARN", f"Failed to connect to Polymarket: {e}")
        return False


def get_real_balance() -> float | None:
    """
    Fetch the actual USDC balance from Polymarket.

    Returns:
        Balance in USDC, or None if not connected.
    """
    if not _poly_client:
        return None
    try:
        balance_wei = _poly_client.get_balance()
        return int(balance_wei) / 1e6
    except Exception:
        return None


def is_connected() -> bool:
    """Check if we have an active Polymarket connection."""
    return _poly_client is not None


# ══════════════════════════════════════════════════════════════
# TRADE EXECUTION
# ══════════════════════════════════════════════════════════════

def execute_trade(state: dict, analysis: dict) -> bool:
    """
    Validate and execute a trade based on Claude's analysis.

    This function:
      1. Extracts the decision from Claude's response
      2. Validates against all risk limits
      3. Computes position size (dynamic based on edge + streak)
      4. Executes the order (real or simulated)
      5. Updates state with the new position

    Args:
        state:     Global state dict
        analysis:  Claude's response dict (from brain.py)

    Returns:
        True if a trade was executed, False otherwise.
    """
    decision = analysis.get("decision", {})
    action = decision.get("action", "hold")
    confidence = float(decision.get("confidence", 0))
    edge = float(decision.get("edge", 0))
    reasoning = decision.get("reasoning", "")

    # ── Log top opportunities ──
    _log_opportunities(analysis)

    # ── HOLD check ──
    if action == "hold":
        log("SCALP", f"⏸  HOLD (conf:{confidence:.2f}) — {reasoning[:90]}")
        return False

    # ── Confidence check ──
    if confidence < config.MIN_CONFIDENCE:
        log("SCALP", f"⏸  Confidence {confidence:.2f} < {config.MIN_CONFIDENCE}")
        return False

    # ── Edge check ──
    if abs(edge) < config.MIN_EDGE:
        log("SCALP", f"⏸  Edge {edge:.3f} < {config.MIN_EDGE}")
        return False

    # ── Position limit check ──
    condition_id = decision.get("condition_id", "")
    if condition_id in state["positions"]:
        log("INFO", "Already have a position in this market")
        return False
    if condition_id in state["blacklist"]:
        log("INFO", "Market is blacklisted (recent loss)")
        return False
    if len(state["positions"]) >= config.MAX_POSITIONS:
        log("INFO", f"Max positions ({config.MAX_POSITIONS}) reached")
        return False

    # ── Compute position size ──
    amount = _compute_position_size(state, decision)
    if amount is None:
        return False

    # ── Extract order details ──
    outcome = decision.get("outcome")
    price = float(decision.get("price") or 0.5)
    token_id = (decision.get("yes_token_id") if outcome == "YES"
                else decision.get("no_token_id"))

    if not token_id:
        log("WARN", "No token_id in decision — cannot execute")
        return False

    log("EXEC", f"🎯 {outcome} | ${amount:.2f} @ {price:.3f} | "
                f"conf:{confidence:.2f} | edge:{edge:+.3f}")
    log("THINK", f"   {reasoning[:110]}")

    # ── Execute (real or simulated) ──
    success, order_id = _place_order(outcome, token_id, amount, price)

    if success:
        # Record the new position in state
        state["balance"] -= amount
        state["positions"][condition_id] = {
            "question":      decision.get("question", ""),
            "outcome":       outcome,
            "token_id":      token_id,
            "amount":        amount,
            "entry_price":   price,
            "timestamp":     datetime.now().isoformat(),
            "confidence":    confidence,
            "edge":          edge,
            "order_id":      order_id,
            "max_price":     price,     # Tracks highest price (for trailing stop)
            "time_stop_min": decision.get("time_horizon_minutes", config.TIME_STOP_MIN),
            "simulated":     not is_connected(),
        }
        return True

    return False


def sell_position(position: dict) -> bool:
    """
    Sell/close a position on Polymarket.

    Args:
        position: Position dict from state["positions"]

    Returns:
        True if the sell order was placed successfully.
    """
    if not _poly_client:
        return True  # In simulation, sells always "succeed"

    if not position.get("token_id") or position.get("simulated"):
        return True

    try:
        sell_args = MarketOrderArgs(
            token_id=position["token_id"],
            amount=position["amount"],
            side=SELL,
            order_type=OrderType.FOK,
        )
        signed = _poly_client.create_market_order(sell_args)
        _poly_client.post_order(signed, OrderType.FOK)
        return True
    except Exception as e:
        log("WARN", f"Error selling position: {e}")
        return False


# ══════════════════════════════════════════════════════════════
# HELPERS (private)
# ══════════════════════════════════════════════════════════════

def _compute_position_size(state: dict, decision: dict) -> float | None:
    """
    Compute the position size based on edge strength and streak.

    Sizing rules:
      - Weak edge (5-8%):   3% of balance
      - Normal edge (8-12%): MAX_RISK_PCT of balance
      - Strong edge (>12%):  8% of balance (capped at MAX_ORDER_USDC)
      - Losing streak ≤-3:   halve the size (protect capital)
      - Winning streak ≥3:   slightly increase (ride the wave)

    All sizes are capped by:
      - MAX_ORDER_USDC (absolute max per trade)
      - Available risk budget (MAX_RISK_TOTAL - current exposure)
      - 15% of balance (hard safety cap)
    """
    edge = abs(float(decision.get("edge", 0)))
    risk_used = sum(p["amount"] for p in state["positions"].values())
    risk_available = max(0, state["balance"] * config.MAX_RISK_TOTAL - risk_used)

    # Base size from edge strength
    if edge >= config.STRONG_EDGE:
        base = state["balance"] * 0.08
    elif edge >= 0.08:
        base = state["balance"] * config.MAX_RISK_PCT
    else:
        base = state["balance"] * 0.03

    # Streak adjustment
    if state["streak"] <= -3:
        base *= 0.5
        log("WARN", "⚠️  Losing streak — reducing position size")
    elif state["streak"] >= 3:
        base *= 1.2  # Slight increase on hot streak

    # Apply all caps
    amount = min(
        float(decision.get("amount_usdc") or base),
        base,
        risk_available,
        config.MAX_ORDER_USDC,
        state["balance"] * 0.15,
    )
    amount = max(amount, 0)

    # Final validation
    if amount < config.MIN_ORDER_USDC:
        log("WARN", f"Position ${amount:.2f} < minimum ${config.MIN_ORDER_USDC}")
        return None
    if amount > state["balance"]:
        log("WARN", "Insufficient balance")
        return None

    return amount


def _place_order(outcome: str, token_id: str, amount: float,
                 price: float) -> tuple[bool, str]:
    """
    Place an order on Polymarket (or simulate).

    Tries FOK (instant fill) first. If rejected, falls back to
    GTC (limit order that sits in the book).

    Args:
        outcome:   "YES" or "NO"
        token_id:  The token to buy
        amount:    USDC amount
        price:     Target price

    Returns:
        (success: bool, order_id: str)
    """
    # ── Simulation mode ──
    if not _poly_client:
        log("SCALP", f"📝 [SIM] {outcome} ${amount:.2f} @ {price:.3f}")
        return True, "SIM"

    # ── Real execution: try FOK first ──
    try:
        fok_args = MarketOrderArgs(
            token_id=token_id,
            amount=amount,
            side=BUY,
            order_type=OrderType.FOK,
        )
        signed = _poly_client.create_market_order(fok_args)
        response = _poly_client.post_order(signed, OrderType.FOK)

        if response and response.get("success", False):
            order_id = response.get("orderID", "?")
            log("EXEC", f"✅ FOK FILLED | ID: {order_id}")
            return True, order_id

        # ── FOK rejected → try GTC limit order ──
        log("INFO", "FOK rejected → trying GTC limit order...")
        gtc_args = OrderArgs(
            price=round(price, 2),
            size=round(amount / price, 2),  # Size in tokens
            side=BUY,
            token_id=token_id,
        )
        signed = _poly_client.create_order(gtc_args)
        response = _poly_client.post_order(signed, OrderType.GTC)

        if response:
            order_id = response.get("orderID", "?")
            log("EXEC", f"✅ GTC PLACED | ID: {order_id}")
            return True, order_id

    except Exception as e:
        log("WARN", f"Order execution failed: {e}")

    return False, ""


def _log_opportunities(analysis: dict) -> None:
    """Log the top opportunities identified by Claude."""
    opps = analysis.get("top_opportunities", [])
    if not opps:
        return
    for opp in opps[:3]:
        edge = float(opp.get("edge", 0))
        symbol = "★" if abs(edge) >= config.MIN_EDGE else "·"
        horizon = opp.get("time_horizon", "?")
        question = str(opp.get("question", ""))[:50]
        log("SCALP", f"  {symbol} edge:{edge:+.3f} | {horizon} | {question}")