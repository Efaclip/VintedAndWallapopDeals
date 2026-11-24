"""
Central configuration for PERSONAL_DEALS.

Everything tunable lives here so the rest of the project never
hardcodes paths, thresholds, credentials or marketplace switches.
Secrets come from environment variables, never from this file.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# --- Database -------------------------------------------------------------
# Override with HARDWARE_DEALS_DB for tests or a non-default location.
DB_PATH = Path(os.environ.get("HARDWARE_DEALS_DB", BASE_DIR / "hardware_deals.db"))

# --- Price estimation (Stage A) -------------------------------------------
MIN_COMPARABLES = 3
MIN_VALID_PRICE = 20
MAX_VALID_PRICE = 15000

ELIGIBLE_CATEGORIES = {
    "laptop", "phone", "tablet", "console", "desktop",
    "gpu", "cpu", "ram", "storage", "motherboard", "monitor",
}

# Current estimates are medians of ASKING prices from the local pool,
# not confirmed transaction prices. Kept explicit so asking value and
# actual sale value are never mixed (roadmap Stage A future improvement).
VALUE_BASIS_ASKING = "asking"
VALUE_BASIS_SALE = "sale"  # reserved: no sale-price source exists yet

# Estimate confidence heuristic (tunable): first matching count wins.
CONFIDENCE_BY_COMPARABLES = [(10, 0.90), (6, 0.80), (4, 0.70), (3, 0.60)]
# Wide comparable spreads lower confidence.
SPREAD_PENALTY_THRESHOLD = 0.50   # (max - min) / median above this...
SPREAD_PENALTY_FACTOR = 0.80      # ...multiplies confidence by this

# Minimum quality for a deal to be eligible for ALERTING at all. User
# preferences (/min_score) can only raise this bar, never lower it. This
# is a selection guard, not part of the quality score itself.
MIN_ELIGIBLE_QUALITY = 25.0

# --- Deal scoring (Stage C) ------------------------------------------------
# Weights for the quality score components (sum is normalized internally).
SCORE_WEIGHTS = {
    "discount": 0.40,
    "price_reliability": 0.20,
    "spec_match": 0.15,
    "condition": 0.15,
    "comparables": 0.10,
}

# --- Telegram (Stage E) -----------------------------------------------------
# Never store the token or user ID in code. Both come from the environment.
TELEGRAM_BOT_TOKEN = os.environ.get("PERSONAL_DEALS_TELEGRAM_TOKEN")
TELEGRAM_TIMEOUT_SECONDS = 15
_owner = os.environ.get("PERSONAL_DEALS_TELEGRAM_OWNER_ID")
TELEGRAM_OWNER_ID = int(_owner) if _owner and _owner.isdigit() else None

# --- Local AI (Stage D) ------------------------------------------------------
# Small local model used ONLY for ambiguous listings. Deterministic rules
# always run first; the AI fallback is disabled unless an endpoint is set.
AI_ENDPOINT = os.environ.get("PERSONAL_DEALS_AI_ENDPOINT")
AI_ENABLED = bool(AI_ENDPOINT)
AI_TIMEOUT_SECONDS = 30

# --- Scheduler (Stage F) -----------------------------------------------------
ACTIVE_LISTING_DAYS = 30        # listings not seen for this long are "inactive"
INACTIVE_CHECK_MULTIPLIER = 4   # inactive listings are checked this much less often

# --- Vinted (Stage G) ---------------------------------------------------------
# Collection only when the user explicitly confirms it is permitted and
# technically reliable. Defaults OFF on purpose.
VINTED_COLLECTION_PERMITTED = os.environ.get(
    "PERSONAL_DEALS_VINTED_PERMITTED", ""
).lower() in ("1", "true", "yes")
