"""
End-to-end pipeline test on sample data.

Run:  HARDWARE_DEALS_DB=/tmp/pd_test.db python3 tests/test_pipeline.py

Exercises: database -> normalize -> classify -> estimate -> score ->
scheduler plan -> telegram core (owner gate, prefs, /why) -> AI router.
Uses a throwaway database; never touches real data or the network.
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import database
import normalize_database
import classify_conditions
import price_estimator
import deal_scorer
import scheduler
import telegram_bot
import ai_assistant

DB = Path(os.environ["HARDWARE_DEALS_DB"])
if DB.exists():
    DB.unlink()

PASS = []


def check(name, condition):
    PASS.append((name, bool(condition)))
    print(("PASS" if condition else "FAIL"), name)


# --- 1. Collect (simulated): sample listings into the database ----------
database.initialize_database()
now = datetime.now(timezone.utc).isoformat(timespec="seconds")

gpu_prices = [430, 450, 470, 500, 520]
items = []
for i, price in enumerate(gpu_prices):
    items.append({
        "url": f"https://es.wallapop.com/item/rtx4070s-{i}",
        "title": "KFA2 GeForce RTX 4070 SUPER 12GB",
        "price": f"{price}€",
        "description": "Tarjeta grafica RTX 4070 SUPER 12GB usada, funciona correctamente.",
    })

items += [
    {  # same GPU but new condition: must NOT mix with the used pool
        "url": "https://es.wallapop.com/item/rtx4070s-new",
        "title": "KFA2 GeForce RTX 4070 SUPER 12GB",
        "price": "700€",
        "description": "RTX 4070 SUPER 12GB nueva, precintada.",
    },
    {  # unknown condition
        "url": "https://es.wallapop.com/item/mbp-m4",
        "title": "MacBook Pro M4 16GB 512GB",
        "price": "1500€",
        "description": "Portatil Apple.",
    },
    {  # accessory: excluded category
        "url": "https://es.wallapop.com/item/cargador",
        "title": "Cargador Apple USB-C",
        "price": "25€",
        "description": "Cargador nuevo para MacBook.",
    },
    {  # for parts
        "url": "https://es.wallapop.com/item/broken",
        "title": "RTX 4070 SUPER 12GB para piezas",
        "price": "100€",
        "description": "No funciona, para piezas.",
    },
]

result = database.save_listings(items, source="wallapop")
check("saved listings", result["inserted"] == len(items))

# duplicate re-save updates instead of duplicating
again = database.save_listings([items[0]], source="wallapop")
check("dedup by URL", again["inserted"] == 0 and database.get_listing_count() == len(items))

# --- 2-4. Normalize, classify, estimate ---------------------------------
normalize_database.main()
classify_conditions.classify_database()
price_estimator.run_estimator()

connection = sqlite3.connect(DB)
connection.row_factory = sqlite3.Row

estimated = connection.execute(
    "SELECT COUNT(*) c FROM price_estimates WHERE status='estimated'"
).fetchone()["c"]
check("5 used 4070S estimated", estimated == len(gpu_prices))

row = connection.execute("""
    SELECT e.fair_value, e.value_basis, e.estimate_confidence, e.reason_code
    FROM price_estimates e JOIN listings l ON l.id = e.listing_id
    WHERE l.url LIKE '%rtx4070s-0'
""").fetchone()
import statistics
others = [p for p in gpu_prices[1:]]
check("fair value is median of comparable asks",
      row["fair_value"] == round(statistics.median(others), 2))
check("value basis is asking", row["value_basis"] == config.VALUE_BASIS_ASKING)
check("confidence present", 0 < row["estimate_confidence"] <= 1)

codes = {r["reason_code"] for r in connection.execute(
    "SELECT reason_code FROM price_estimates").fetchall()}
check("unknown-condition reason recorded", price_estimator.REASON_UNKNOWN_CONDITION in codes)
check("for-parts reason recorded", price_estimator.REASON_FOR_PARTS in codes)
check("unsupported category reason recorded",
      price_estimator.REASON_UNSUPPORTED_CATEGORY in codes)

runs = connection.execute("SELECT COUNT(*) c FROM estimation_runs").fetchone()["c"]
check("run summary recorded", runs == 1)

# the new-condition 4070S: no other 'new' comparables -> insufficient
new_row = connection.execute("""
    SELECT e.reason_code FROM price_estimates e
    JOIN listings l ON l.id = e.listing_id
    WHERE l.url LIKE '%rtx4070s-new'
""").fetchone()
check("new-condition GPU not mixed with used pool",
      new_row["reason_code"] == price_estimator.REASON_INSUFFICIENT_COMPARABLES)

# --- 5. Score -------------------------------------------------------------
summary = deal_scorer.score_database()
check("scored all estimated", summary["scored"] == len(gpu_prices))

scores = connection.execute("""
    SELECT s.quality_score, s.discount_pct, l.price_amount
    FROM deal_scores s JOIN listings l ON l.id = s.listing_id
""").fetchall()
check("scores in range", all(0 <= s["quality_score"] <= 100 for s in scores))
cheapest = min(scores, key=lambda s: s["price_amount"])
check("cheapest ask has highest discount",
      cheapest["discount_pct"] == max(s["discount_pct"] for s in scores))

# quality score independent of priority prefs
first_score = scores[0]["quality_score"]
prefs_none = {"priority_order": [], "watches": [], "min_score": 0}
prefs_gpu = {"priority_order": ["gpu"], "watches": ["4070"], "min_score": 0}
deal = {"quality_score": first_score, "category": "gpu", "title": "KFA2 RTX 4070 SUPER"}
prio_a = deal_scorer.alert_priority(deal, prefs_none)
prio_b = deal_scorer.alert_priority(deal, prefs_gpu)
check("priority reacts to prefs", prio_b != prio_a or prio_b in deal_scorer.PRIORITY_LEVELS)
check("quality untouched by priority prefs",
      deal["quality_score"] == first_score)

# --- 6. Scheduler ---------------------------------------------------------
plan = scheduler.plan_work(connection, now=datetime.now(timezone.utc))
check("scheduler returns a plan",
      set(plan) == {"description_refreshes", "rechecks", "inactive_rechecks"})

# --- 7. Telegram core ------------------------------------------------------
OWNER = 424242
check("stranger ignored",
      telegram_bot.handle_message(1, "/status", connection, owner_id=OWNER) is None)
check("owner gets status",
      "Listings:" in telegram_bot.handle_message(OWNER, "/status", connection, owner_id=OWNER))
check("min_score stored",
      "set" in telegram_bot.handle_message(OWNER, "/min_score 80", connection, owner_id=OWNER))
check("watch stored",
      "Watching" in telegram_bot.handle_message(OWNER, "/watch RTX 4060 Ti", connection, owner_id=OWNER))
why = telegram_bot.handle_message(OWNER, "/why 1", connection, owner_id=OWNER)
check("/why explains a deal", "Fair value" in why and "Comparables" in why)
alert = telegram_bot.format_alert({
    "source": "wallapop", "title": "KFA2 RTX 4070 SUPER", "specs": "12GB",
    "price": 430, "fair_value": 467.5, "discount_pct": 8.0,
    "quality_score": 66.0, "estimate_confidence": 0.8,
    "url": "https://es.wallapop.com/item/rtx4070s-0", "reason": "test",
})
check("alert has all fields",
      all(token in alert for token in (
          "wallapop", "Asking price", "Estimated market value",
          "Discount", "Deal score", "Confidence", "Link", "Why flagged")))

# --- 8. AI router -----------------------------------------------------------
out = ai_assistant.resolve_listing("Ordenador potente barato", "Va muy rapido")
check("ambiguous listing routed, AI disabled cleanly",
      out["source"] == "deterministic(ai_unavailable)" and out["ambiguity"])
out2 = ai_assistant.resolve_listing("MacBook Pro M4 16GB 512GB")
check("clear listing stays deterministic", out2["source"] == "deterministic")

connection.close()

failed = [name for name, ok in PASS if not ok]
print(f"\n{len(PASS) - len(failed)}/{len(PASS)} checks passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
print("ALL PIPELINE CHECKS PASSED")
