"""
config.py — All configurable parameters for the Polymarket AI Scalper.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ══════════════════════════════════════════════════════════════
# CREDENTIALS — Set these in your .env file ONLY. Never hardcode.
# ══════════════════════════════════════════════════════════════

# BUG FIX: os.getenv() takes the VARIABLE NAME, not the value itself.
# The original code passed the raw API key string as the argument,
# which means it was looking for an env var literally named
# "sk-ant-api03-..." and always returning None.
ANTHROPIC_KEY    = os.getenv("ANTHROPIC_KEY")
POLY_PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY", "")
POLY_FUNDER      = os.getenv("POLY_FUNDER", "")

# ══════════════════════════════════════════════════════════════
# BALANCE & SESSION
# ══════════════════════════════════════════════════════════════

INITIAL_BALANCE  = float(os.getenv("INITIAL_BALANCE", "100"))
DURATION_HOURS   = float(os.getenv("DURATION_HOURS", "0"))   # 0 = run forever

# ══════════════════════════════════════════════════════════════
# TIMING
# ══════════════════════════════════════════════════════════════

SCAN_SECONDS  = 15   # How often to fetch prices + check exits
BRAIN_SECONDS = 30   # How often to call Claude for new decisions

# ══════════════════════════════════════════════════════════════
# CLAUDE MODEL SELECTION
# ══════════════════════════════════════════════════════════════

MODEL          = os.getenv("MODEL", "claude-sonnet-4-6")
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL", "claude-haiku-4-5-20251001")

# ══════════════════════════════════════════════════════════════
# MARKET FILTERS
# ══════════════════════════════════════════════════════════════

TOP_MARKETS       = 200
MIN_VOLUME        = 100
MIN_LIQUIDITY     = 100
MAX_HOURS_TO_END  = 720
PREFER_HOURS      = 168

# ══════════════════════════════════════════════════════════════
# SIGNAL THRESHOLDS
# ══════════════════════════════════════════════════════════════

MIN_CONFIDENCE    = 0.55
MIN_EDGE          = 0.02
STRONG_EDGE       = 0.06
VOLUME_SPIKE_MULT = 2.5

# ══════════════════════════════════════════════════════════════
# POSITION SIZING & RISK
# ══════════════════════════════════════════════════════════════

MAX_RISK_PCT   = float(os.getenv("MAX_RISK_PCT", "0.05"))
MAX_RISK_TOTAL = 0.30
MAX_POSITIONS  = 10
MIN_ORDER_USDC = 1.0
MAX_ORDER_USDC = 10.0

# ══════════════════════════════════════════════════════════════
# EXIT STRATEGY
# ══════════════════════════════════════════════════════════════

TP_NORMAL       = 15       # Take profit at +15%
TP_STRONG       = 25       # Take profit at +25% for high-edge trades
SL_NORMAL       = -8       # Stop loss at -8%
TRAILING_PCT    = 3.5      # Trailing stop: close if drops 3.5% from peak
BREAKEVEN_AFTER = 8        # Move SL to 0% after +8% gain
TIME_STOP_MIN   = 20       # Close flat positions after 20 minutes

# ══════════════════════════════════════════════════════════════
# CIRCUIT BREAKER
# ══════════════════════════════════════════════════════════════

CB_MAX_LOSS_PCT  = -15     # Pause if total P&L drops below -15%
CB_MAX_STREAK    = -5      # Pause after 5 consecutive losses
CB_MAX_DRAWDOWN  = 20      # Pause if drawdown exceeds 20%

# ══════════════════════════════════════════════════════════════
# POLYMARKET CONNECTION
# ══════════════════════════════════════════════════════════════

POLY_HOST           = "https://clob.polymarket.com"
POLY_CHAIN_ID       = 137
POLY_SIGNATURE_TYPE = 1
MARKET_CACHE_SECONDS = 12
