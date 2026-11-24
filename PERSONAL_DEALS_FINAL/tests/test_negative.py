"""
Negative, regression and idempotence tests.

Run:  HARDWARE_DEALS_DB=/tmp/pd_neg.db python3 tests/test_negative.py

Covers: bad/missing prices, malformed specs, self-comparables, spec
conflicts, laptop year / generic-CPU rules, condition exclusions,
old-schema migration, non-destructive re-runs, idempotence, alert
filters/pause/dedup, and the Telegram owner gate on every command.
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
import alert_selector
import telegram_bot

DB = Path(os.environ["HARDWARE_DEALS_DB"])
if DB.exists():
    DB.unlink()

RESULTS = []


def check(name, condition):
    RESULTS.append((name, bool(condition)))
    print(("PASS" if condition else "FAIL"), name)


def reason_for(connection, url_like):
    row = connection.execute("""
        SELECT e.reason_code FROM price_estimates e
        JOIN listings l ON l.id = e.listing_id
        WHERE l.url LIKE ?
    """, (url_like,)).fetchone()
    return row["reason_code"] if row else None


# --- bad prices -------------------------------------------------------------
check("parse_price rejects garbage", price_estimator.parse_price("abc") is None)
check("parse_price rejects empty", price_estimator.parse_price("") is None)
check("parse_price rejects zero", price_estimator.parse_price("0€") is None)
check("parse_price rejects negative", price_estimator.parse_price("-50") is None)
check("parse_price european", price_estimator.parse_price("1.234,56 €") == 1234.56)
check("db parse_price thousands", database.parse_price("1.200") == 1200.0)

# --- build a tricky sample DB ----------------------------------------------
database.initialize_database()
items = [
    {  # lone listing: only comparable is itself
        "url": "https://x/lone", "title": "RTX 4070 SUPER 12GB usada",
        "price": "400€", "description": "Usada, funciona correctamente. RTX 4070 SUPER 12GB.",
    },
    {  # garbage title: no brand/model
        "url": "https://x/garbage", "title": "cosa rara barata",
        "price": "100€", "description": "Usada.",
    },
    {  # no valid price
        "url": "https://x/noprice", "title": "RTX 4060 Ti 8GB usada",
        "price": "", "description": "Usada. RTX 4060 Ti 8GB.",
    },
    {  # laptop pair, different years: must NOT match each other
        "url": "https://x/mbp2019", "title": "MacBook Pro 2019 16GB 512GB",
        "price": "800€", "description": "Usada. 16GB RAM, 512GB SSD.",
    },
    {
        "url": "https://x/mbp2021", "title": "MacBook Pro 2021 16GB 512GB",
        "price": "900€", "description": "Usada. 16GB RAM, 512GB SSD.",
    },
    {  # laptop pair, missing years + generic CPU: must NOT match
        "url": "https://x/lap1", "title": "Lenovo ThinkPad T14 i5 8GB 256GB",
        "price": "300€", "description": "Usada. Intel Core i5, 8GB RAM, 256GB SSD.",
    },
    {
        "url": "https://x/lap2", "title": "Lenovo ThinkPad T14 i5 8GB 256GB",
        "price": "320€", "description": "Usada. Intel Core i5, 8GB RAM, 256GB SSD.",
    },
    {  # GPU pair with a spec conflict (8GB vs 12GB): must NOT match
        "url": "https://x/4060-8", "title": "RTX 4060 Ti 8GB usada",
        "price": "260€", "description": "Usada. RTX 4060 Ti 8GB.",
    },
    {
        "url": "https://x/4060-16", "title": "RTX 4060 Ti 16GB usada",
        "price": "350€", "description": "Usada. RTX 4060 Ti 16GB.",
    },
]
database.save_listings(items, source="wallapop")
normalize_database.main()
classify_conditions.classify_conditions = classify_conditions  # no-op
classify_conditions.classify_database()
price_estimator.run_estimator()

connection = sqlite3.connect(DB)
connection.row_factory = sqlite3.Row

check("lone listing: self is not a comparable",
      reason_for(connection, "%lone") == price_estimator.REASON_INSUFFICIENT_COMPARABLES)
check("garbage specs: brand/model missing",
      reason_for(connection, "%garbage") in (
          price_estimator.REASON_MISSING_BRAND_MODEL,
          price_estimator.REASON_UNSUPPORTED_CATEGORY,
          price_estimator.REASON_NO_ANCHOR,
      ))
check("empty price rejected",
      reason_for(connection, "%noprice") == price_estimator.REASON_NO_PRICE)
check("different laptop years never match",
      reason_for(connection, "%mbp2019") == price_estimator.REASON_INSUFFICIENT_COMPARABLES)
check("generic CPU without year never matches",
      reason_for(connection, "%lap1") == price_estimator.REASON_INSUFFICIENT_COMPARABLES)
check("VRAM conflict (8 vs 16GB) never matches",
      reason_for(connection, "%4060-8") == price_estimator.REASON_INSUFFICIENT_COMPARABLES)

# --- idempotence: second full run changes nothing ---------------------------
first = connection.execute(
    "SELECT listing_id, status, fair_value, reason_code FROM price_estimates ORDER BY listing_id"
).fetchall()
price_estimator.run_estimator()
second = connection.execute(
    "SELECT listing_id, status, fair_value, reason_code FROM price_estimates ORDER BY listing_id"
).fetchall()
check("estimator idempotent", [dict(r) for r in first] == [dict(r) for r in second])

s1 = deal_scorer.score_database()
s2 = deal_scorer.score_database()
check("scorer idempotent", s1 == s2)

# --- non-destructiveness -----------------------------------------------------
history_before = connection.execute("SELECT COUNT(*) c FROM price_history").fetchone()["c"]
database.save_listings([dict(items[0], description="")], source="wallapop")
row = connection.execute(
    "SELECT description FROM listings WHERE url='https://x/lone'"
).fetchone()
check("empty re-scrape keeps old description", row["description"].startswith("Usada"))
history_after = connection.execute("SELECT COUNT(*) c FROM price_history").fetchone()["c"]
check("unchanged price adds no history", history_before == history_after)

# --- old-schema migration ----------------------------------------------------
mig = Path("/tmp/pd_mig.db")
if mig.exists():
    mig.unlink()
os.environ["HARDWARE_DEALS_DB"] = str(mig)
import importlib
importlib.reload(config)
importlib.reload(database)
importlib.reload(price_estimator)
database.initialize_database()
database.save_listings(items, source="wallapop")
mc = sqlite3.connect(mig)
mc.row_factory = sqlite3.Row
# simulate his original schema: estimates table WITHOUT the new columns
mc.execute("""
    CREATE TABLE price_estimates (
        listing_id INTEGER PRIMARY KEY, status TEXT NOT NULL,
        condition TEXT, fair_value REAL,
        comparable_count INTEGER NOT NULL DEFAULT 0,
        lowest_comparable REAL, highest_comparable REAL,
        reason TEXT, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
""")
mc.execute("INSERT INTO price_estimates (listing_id, status, reason) VALUES (1, 'estimated', 'legacy row')")
mc.commit()
# minimal normalized/conditions tables so the estimator can run
mc.execute("""
    CREATE TABLE normalized_listings (
        listing_id INTEGER PRIMARY KEY, category TEXT, brand TEXT,
        model TEXT, processor TEXT, gpu TEXT, ram_gb INTEGER,
        storage_gb INTEGER, storage_type TEXT, screen_inches REAL,
        edition TEXT, normalized_at TEXT, spec_sources TEXT,
        spec_confidence TEXT, conflicts TEXT
    )
""")
mc.execute("""
    CREATE TABLE listing_conditions (listing_id INTEGER PRIMARY KEY, condition TEXT)
""")
mc.commit()
price_estimator.run_estimator(mc)
cols = price_estimator.get_columns(mc, "price_estimates")
check("migration adds new columns",
      {"value_basis", "estimate_confidence", "reason_code"} <= cols)
legacy = mc.execute("SELECT reason FROM price_estimates WHERE listing_id=1").fetchone()
check("legacy row survives migration", legacy is not None)
mc.close()
os.environ["HARDWARE_DEALS_DB"] = str(DB)
importlib.reload(config)
importlib.reload(database)
importlib.reload(price_estimator)

# --- alert filters, pause, dedup ---------------------------------------------
connection.close()
connection = sqlite3.connect(DB)
connection.row_factory = sqlite3.Row
# give the lone GPU a synthetic score so selection has something to filter
connection.execute("""
    CREATE TABLE IF NOT EXISTS deal_scores (
        listing_id INTEGER PRIMARY KEY, quality_score REAL NOT NULL,
        discount_pct REAL, fair_value REAL, value_basis TEXT,
        estimate_confidence REAL, components TEXT NOT NULL DEFAULT '{}',
        scored_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
""")
gpu_id = connection.execute(
    "SELECT id FROM listings WHERE url='https://x/lone'"
).fetchone()["id"]
connection.execute("""
    INSERT OR REPLACE INTO deal_scores
    (listing_id, quality_score, discount_pct, fair_value, value_basis, estimate_confidence)
    VALUES (?, 85, 20.0, 500, 'asking', 0.8)
""", (gpu_id,))
# the selector requires a LIVE estimate behind every score
connection.execute("""
    INSERT OR REPLACE INTO price_estimates
    (listing_id, status, condition, fair_value, comparable_count, reason)
    VALUES (?, 'estimated', 'used', 500, 3, 'synthetic for alert tests')
""", (gpu_id,))
connection.commit()

selected, _ = alert_selector.select_alerts(connection, {})
check("no filters: deal selected", [d["id"] for d in selected] == [gpu_id])

for prefs, name in [
    ({"min_score": 90}, "min_score filters"),
    ({"min_discount": 30}, "min_discount filters"),
    ({"min_confidence": 0.9}, "min_confidence filters"),
    ({"max_price": 399}, "max_price filters"),
    ({"categories": ["laptop"]}, "category filters"),
    ({"sources": ["vinted"]}, "source filters"),
]:
    sel, _ = alert_selector.select_alerts(connection, prefs)
    check(name, sel == [])

future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(timespec="seconds")
# the synthetic deal passes guards, so pause must be what suppresses it
sel_before, _ = alert_selector.select_alerts(connection, {})
sel, suppressed = alert_selector.select_alerts(connection, {"paused_until": future})
check("pause suppresses everything",
      len(sel_before) == 1 and sel == [] and len(suppressed) == 1)

recorded = alert_selector.record_alerts(connection, selected)
check("alerts recorded", recorded == 1)
sel2, _ = alert_selector.select_alerts(connection, {})
check("alerts deduped on repeat", sel2 == [])
recorded2 = alert_selector.record_alerts(connection, selected)
check("record is idempotent", recorded2 == 0)
# price change re-alerts
connection.execute("UPDATE listings SET price_amount=390 WHERE id=?", (gpu_id,))
connection.execute("UPDATE deal_scores SET discount_pct=22.5 WHERE listing_id=?", (gpu_id,))
connection.commit()
sel3, _ = alert_selector.select_alerts(connection, {})
check("price change re-alerts", [d["id"] for d in sel3] == [gpu_id])

# --- owner gate on every command path ----------------------------------------
OWNER = 424242
commands = [
    "/help", "/status", "/why 1", "/reset", "/pause 2h", "/digest daily",
    "/watch RTX 4060 Ti", "/prioritize gpu", "/min_discount 30",
    "/min_score 80", "/min_confidence 0.85", "/max_price 300",
    "/category gpu,laptop", "/source wallapop,vinted", "not a command",
]
stranger_replies = [
    telegram_bot.handle_message(999999, cmd, connection, owner_id=OWNER)
    for cmd in commands
]
check("stranger gets silence on ALL commands",
      all(reply is None for reply in stranger_replies))
owner_replies = [
    telegram_bot.handle_message(OWNER, cmd, connection, owner_id=OWNER)
    for cmd in commands
]
check("owner gets a reply on ALL commands",
      all(reply is not None for reply in owner_replies))
check("no owner configured: bot answers nobody",
      telegram_bot.handle_message(OWNER, "/status", connection, owner_id=None) is None)

failed = [name for name, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} negative/regression checks passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
print("ALL NEGATIVE/REGRESSION CHECKS PASSED")
