"""
markets.py — Market data fetching and technical signal computation.

Responsibilities:
  1. Fetch active markets from Polymarket's Gamma API
  2. Filter to short-term markets only (based on MAX_HOURS_TO_END)
  3. Compute technical signals: momentum, volatility, volume spikes, trend
  4. Maintain price history for each market
  5. Cache results to avoid hammering the API

The Gamma API is Polymarket's public REST API for market metadata.
It's separate from the CLOB API (which handles order execution).
"""

import time
import requests
from datetime import datetime, timezone
from collections import deque

import config
from logger import log


def fetch_markets(state: dict) -> list[dict]:
    """
    Fetch active markets from Polymarket and filter to short-term only.

    This is the main entry point. It handles caching, fetching, filtering,
    signal computation, and sorting.

    Args:
        state: Global state dict (reads/writes price_history, signals, cache)

    Returns:
        List of market dicts sorted by time priority and volume,
        each containing: condition_id, token IDs, prices, signals, etc.
    """
    now = time.time()

    # ── Return cached data if fresh enough ──
    if (now - state["cache_time"] < config.MARKET_CACHE_SECONDS
            and state["market_cache"]):
        return state["market_cache"]

    try:
        # Fetch top markets by volume from Gamma API
        url = (
            f"https://gamma-api.polymarket.com/markets"
            f"?limit={config.TOP_MARKETS}"
            f"&active=true&closed=false"
            f"&order=volume&ascending=false"
        )
        response = requests.get(url, timeout=8)
        response.raise_for_status()
        raw_markets = response.json()

        now_utc = datetime.now(timezone.utc)
        markets = []

        for market in raw_markets:
            # ── Basic filters ──
            volume = float(market.get("volume", 0) or 0)
            liquidity = float(market.get("liquidity", 0) or 0)
            if volume < config.MIN_VOLUME:
                continue

            # ── Time filter: only short-term markets ──
            hours_to_end = _parse_hours_to_end(market, now_utc)
            if hours_to_end is not None:
                if hours_to_end <= 0:
                    continue  # Already closed
                if hours_to_end > config.MAX_HOURS_TO_END:
                    continue  # Too far in the future

            # ── Extract token IDs (needed for order execution) ──
            yes_token_id, no_token_id = _extract_token_ids(market)

            # ── Parse prices ──
            yes_price, no_price, best_bid, best_ask, spread = _parse_prices(market)

            # ── Compute condition_id and update price history ──
            condition_id = market.get("conditionId", "")
            _update_price_history(state, condition_id, now, yes_price, volume)

            # ── Compute technical signals ──
            signals = compute_signals(state, condition_id, yes_price, volume)

            # ── Time priority score (closer = higher priority) ──
            time_score = _compute_time_score(hours_to_end)

            markets.append({
                "condition_id":   condition_id,
                "yes_token_id":   yes_token_id,
                "no_token_id":    no_token_id,
                "question":       market.get("question", ""),
                "yes_price":      round(yes_price, 4),
                "no_price":       no_price,
                "volume_usd":     round(volume, 0),
                "liquidity":      round(liquidity, 0),
                "spread":         spread,
                "best_bid":       best_bid,
                "best_ask":       best_ask,
                "hours_to_end":   hours_to_end,
                "time_score":     time_score,
                "end_date":       (market.get("endDate") or "")[:16] or "?",
                "category":       market.get("category", ""),
                # Signals (computed from price history)
                "momentum_1m":    signals.get("momentum_1m", 0),
                "momentum_5m":    signals.get("momentum_5m", 0),
                "momentum_15m":   signals.get("momentum_15m", 0),
                "vol_spike":      signals.get("vol_spike", False),
                "volatility":     signals.get("volatility", 0),
                "trend":          signals.get("trend", "flat"),
            })

        # Sort: short-term first, then by volume
        markets.sort(key=lambda m: (-m["time_score"], -m["volume_usd"]))

        # Update cache
        state["market_cache"] = markets
        state["cache_time"] = now

        # Log summary
        short_count = sum(
            1 for m in markets
            if m.get("hours_to_end") and m["hours_to_end"] <= config.PREFER_HOURS
        )
        log("INFO", f"📊 {len(markets)} markets | "
                     f"{short_count} short-term (≤{config.PREFER_HOURS}h)")

        return markets

    except requests.RequestException as e:
        log("WARN", f"Error fetching markets: {e}")
        return state["market_cache"] or []


# ══════════════════════════════════════════════════════════════
# SIGNAL COMPUTATION
# ══════════════════════════════════════════════════════════════

def compute_signals(state: dict, cid: str, current_price: float,
                    volume: float) -> dict:
    """
    Compute technical signals from price history for a single market.

    Signals computed:
        momentum_Xm  — Price change over last X minutes. Positive = rising.
        volatility    — Standard deviation of recent prices (10 min window).
        vol_spike     — True if recent volume is significantly above average.
        trend         — Categorical: up_strong, up, flat, down, down_strong.

    Args:
        state:          Global state (reads price_history)
        cid:            Market condition_id
        current_price:  Current YES price
        volume:         Current volume

    Returns:
        Dict of computed signals
    """
    history = state["price_history"].get(cid, deque())
    now = time.time()

    signals = {
        "momentum_1m": 0, "momentum_5m": 0, "momentum_15m": 0,
        "vol_spike": False, "volatility": 0, "trend": "flat",
    }

    if len(history) < 2:
        return signals

    # ── Momentum: price change over different timeframes ──
    for label, seconds in [("momentum_1m", 60),
                           ("momentum_5m", 300),
                           ("momentum_15m", 900)]:
        cutoff = now - seconds
        old_prices = [price for ts, price, vol in history if ts <= cutoff]
        if old_prices:
            signals[label] = round(current_price - old_prices[-1], 4)

    # ── Volatility: std dev of prices in last 10 minutes ──
    recent_prices = [price for ts, price, vol in history if now - ts <= 600]
    if len(recent_prices) >= 3:
        mean = sum(recent_prices) / len(recent_prices)
        variance = sum((p - mean) ** 2 for p in recent_prices) / len(recent_prices)
        signals["volatility"] = round(variance ** 0.5, 4)

    # ── Volume spike: recent vs older volume comparison ──
    recent_vols = [vol for ts, price, vol in history if now - ts <= 600]
    older_vols = [vol for ts, price, vol in history if 600 < now - ts <= 1800]
    if recent_vols and older_vols:
        max_recent = max(recent_vols)
        avg_older = sum(older_vols) / len(older_vols) if older_vols else 1
        if avg_older > 0 and max_recent / avg_older >= config.VOLUME_SPIKE_MULT:
            signals["vol_spike"] = True

    # ── Trend detection based on momentum alignment ──
    m1 = signals["momentum_1m"]
    m5 = signals["momentum_5m"]
    if m1 > 0.005 and m5 > 0.01:
        signals["trend"] = "up_strong"
    elif m1 > 0.002:
        signals["trend"] = "up"
    elif m1 < -0.005 and m5 < -0.01:
        signals["trend"] = "down_strong"
    elif m1 < -0.002:
        signals["trend"] = "down"
    else:
        signals["trend"] = "flat"

    # Store in state for other modules to access
    state["signals"][cid] = signals
    return signals


# ══════════════════════════════════════════════════════════════
# HELPER FUNCTIONS (private)
# ══════════════════════════════════════════════════════════════

def _parse_hours_to_end(market: dict, now_utc: datetime) -> float | None:
    """Parse end date and return hours until resolution, or None."""
    end_str = market.get("endDate") or market.get("end_date_iso") or ""
    if not end_str:
        return None
    try:
        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        delta_hours = (end_dt - now_utc).total_seconds() / 3600
        return round(delta_hours, 1)
    except (ValueError, TypeError):
        return None


def _extract_token_ids(market: dict) -> tuple[str | None, str | None]:
    """Extract YES and NO token IDs from market data."""
    yes_tid, no_tid = None, None
    for token in market.get("tokens", []):
        outcome = token.get("outcome", "").upper()
        tid = token.get("token_id") or token.get("tokenId")
        if outcome == "YES":
            yes_tid = tid
        elif outcome == "NO":
            no_tid = tid
    return yes_tid, no_tid


def _parse_prices(market: dict) -> tuple[float, float, float, float, float]:
    """
    Parse prices from market data.

    Returns:
        (yes_price, no_price, best_bid, best_ask, spread)
    """
    # Start with outcome prices
    prices = market.get("outcomePrices", ["0.5", "0.5"])
    try:
        yes_price = float(prices[0]) if prices else 0.5
    except (ValueError, IndexError):
        yes_price = 0.5

    # Use best ask if available (more accurate for buying)
    best_bid = float(market.get("bestBid", 0) or 0)
    best_ask = float(market.get("bestAsk", 0) or 0)
    if best_ask > 0:
        yes_price = best_ask

    # Clamp to valid range
    yes_price = max(0.02, min(0.98, yes_price))
    no_price = round(1.0 - yes_price, 4)

    # Spread
    spread = round(abs(best_ask - best_bid), 4) if (best_bid and best_ask) else 0.02

    return yes_price, no_price, best_bid, best_ask, spread


def _compute_time_score(hours_to_end: float | None) -> int:
    """
    Assign a priority score based on proximity to resolution.
    Higher score = higher priority.

    Score 3: resolves within 6 hours (highest urgency)
    Score 2: resolves within PREFER_HOURS (default 24h)
    Score 1: resolves within MAX_HOURS_TO_END (default 72h)
    Score 0: unknown end date
    """
    if hours_to_end is None:
        return 0
    if hours_to_end <= 6:
        return 3
    if hours_to_end <= config.PREFER_HOURS:
        return 2
    if hours_to_end <= config.MAX_HOURS_TO_END:
        return 1
    return 0


def _update_price_history(state: dict, cid: str, timestamp: float,
                          price: float, volume: float) -> None:
    """Append a price/volume data point to the market's history."""
    if cid not in state["price_history"]:
        # Keep last 120 data points (~30 min at 15s intervals)
        state["price_history"][cid] = deque(maxlen=120)
    state["price_history"][cid].append((timestamp, price, volume))