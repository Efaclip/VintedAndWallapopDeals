"""
Regression tests for code-review fixes.

Run:  HARDWARE_DEALS_DB=<temp> python3 tests/test_review.py

Covers: overpriced-deal gating, alert eligibility guards (discount,
quality floor, staleness, live estimate), Telegram input validation,
AI fail-closed + canonicalization, stale-score invalidation, and the
strengthened old-schema migration with a real populated schema.
"""

import json
import os
import sqlite3
import sys
import tempfile
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
import ai_assistant

RESULTS = []


def check(name, condition):
    RESULTS.append((name, bool(condition)))
    print(("PASS" if condition else "FAIL"), name)


def fresh_db():
    tmp = tempfile.TemporaryDirectory()
    os.environ["HARDWARE_DEALS_DB"] = str(Path(tmp.name) / "t.db")
    import importlib
    importlib.reload(config)
    for module in (database, normalize_database, classify_conditions,
                   price_estimator, deal_scorer, alert_selector, telegram_bot):
        importlib.reload(module)
    database.initialize_database()
    connection = sqlite3.connect(config.DB_PATH)
    connection.row_factory = sqlite3.Row
    return tmp, connection


def seed_gpu_pool(prices):
    return [{
        "url": f"https://x/g{i}",
        "title": "KFA2 GeForce RTX 4070 SUPER 12GB",
        "price": f"{p}€",
        "description": "RTX 4070 SUPER 12GB usada, funciona correctamente.",
    } for i, p in enumerate(prices)]


# === 1. Overpriced deals cannot alert =======================================
tmp, connection = fresh_db()
items = seed_gpu_pool([430, 450, 470, 500, 520])
items.append({
    "url": "https://x/over", "title": "KFA2 GeForce RTX 4070 SUPER 12GB",
    "price": "900€",
    "description": "RTX 4070 SUPER 12GB usada, funciona correctamente.",
})
database.save_listings(items, source="wallapop")
normalize_database.main()
classify_conditions.classify_database()
price_estimator.run_estimator()
deal_scorer.score_database()

over_id = connection.execute(
    "SELECT id FROM listings WHERE url='https://x/over'"
).fetchone()["id"]
score = connection.execute(
    "SELECT quality_score, discount_pct FROM deal_scores WHERE listing_id=?",
    (over_id,)).fetchone()
check("overpriced ask scores 0", score["quality_score"] == 0.0)
check("overpriced discount is negative", score["discount_pct"] < 0)

selected, _ = alert_selector.select_alerts(connection, {})
check("overpriced not selected (default prefs)",
      over_id not in [d["id"] for d in selected])
selected, _ = alert_selector.select_alerts(connection, {"min_score": 50})
check("overpriced not selected (/min_score 50)",
      over_id not in [d["id"] for d in selected])

# === 2. Eligibility guards ====================================================
# quality floor: force a low-quality positive-discount deal
low_id = connection.execute(
    "SELECT id FROM listings WHERE url='https://x/g0'"
).fetchone()["id"]
connection.execute(
    "UPDATE deal_scores SET quality_score=10 WHERE listing_id=?", (low_id,))
connection.commit()
selected, _ = alert_selector.select_alerts(connection, {})
check("below-floor quality not selected",
      low_id not in [d["id"] for d in selected])
connection.execute(
    "UPDATE deal_scores SET quality_score=60 WHERE listing_id=?", (low_id,))

# stale listing: last_seen long ago
stale_ts = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat(timespec="seconds")
connection.execute(
    "UPDATE listings SET last_seen=? WHERE id=?", (stale_ts, low_id))
connection.commit()
selected, _ = alert_selector.select_alerts(connection, {})
check("stale listing not selected",
      low_id not in [d["id"] for d in selected])
fresh_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
connection.execute(
    "UPDATE listings SET last_seen=? WHERE id=?", (fresh_ts, low_id))
connection.commit()

# === 3. Stale score invalidation ==============================================
# the pool listings are estimated + scored; now listing g0 loses its
# condition (becomes unknown) and must lose its score after re-scoring.
connection.execute(
    "UPDATE listing_conditions SET condition='unknown' WHERE listing_id=?",
    (low_id,))
connection.commit()
price_estimator.run_estimator()
status = connection.execute(
    "SELECT status FROM price_estimates WHERE listing_id=?", (low_id,)
).fetchone()["status"]
check("estimate became non-estimated", status != "estimated")
deal_scorer.score_database()
row = connection.execute(
    "SELECT COUNT(*) c FROM deal_scores WHERE listing_id=?", (low_id,)
).fetchone()
check("stale score purged after estimate lost", row["c"] == 0)
selected, _ = alert_selector.select_alerts(connection, {})
check("invalidated deal not selected",
      low_id not in [d["id"] for d in selected])
# === 3b. Freshness edge cases ==============================================
def selectable_with(last_seen_value, connection):
    connection.execute("UPDATE listings SET last_seen=?", (last_seen_value,))
    connection.commit()
    sel, _ = alert_selector.select_alerts(connection, {})
    return [d["id"] for d in sel]

connection = sqlite3.connect(config.DB_PATH)
connection.row_factory = sqlite3.Row
aware = datetime.now(timezone.utc).isoformat(timespec="seconds")
naive = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
check("aware last_seen selectable", len(selectable_with(aware, connection)) > 0)
check("naive last_seen treated as UTC, no crash",
      len(selectable_with(naive, connection)) > 0)
# schema enforces last_seen NOT NULL, so NULL is guarded at parser level
check("NULL last_seen parses to unknown", alert_selector._parse_ts(None) is None)
check("malformed last_seen suppressed", selectable_with("not-a-date", connection) == [])
check("empty last_seen suppressed", selectable_with("", connection) == [])
# restore
selectable_with(aware, connection)

# naive paused_until must not crash the pause check
sel, _ = alert_selector.select_alerts(
    connection, {"paused_until": "2999-01-01T00:00:00"})
check("naive paused_until suppresses without crash", sel == [])
connection.close()
tmp.cleanup()

# === 4. Telegram validation ====================================================
tmp, connection = fresh_db()
OWNER = 424242
replies = {
    "pause_overflow": telegram_bot.handle_message(OWNER, "/pause 1e9d", connection, owner_id=OWNER),
    "pause_negative": telegram_bot.handle_message(OWNER, "/pause -5h", connection, owner_id=OWNER),
    "nan": telegram_bot.handle_message(OWNER, "/min_discount nan", connection, owner_id=OWNER),
    "neg_price": telegram_bot.handle_message(OWNER, "/max_price -1", connection, owner_id=OWNER),
    "conf_range": telegram_bot.handle_message(OWNER, "/min_confidence 2", connection, owner_id=OWNER),
}
check("/pause 1e9d rejected, no crash",
      isinstance(replies["pause_overflow"], str) and "Usage" in replies["pause_overflow"])
check("/pause -5h rejected", "Usage" in replies["pause_negative"])
check("NaN min_discount rejected", "between" in replies["nan"])
check("negative max_price rejected", "between" in replies["neg_price"])
check("confidence >1 rejected", "between" in replies["conf_range"])
prefs = telegram_bot.PrefsStore(connection).get_all()
check("invalid values never stored",
      prefs["min_discount"] is None and prefs["max_price"] is None
      and prefs["min_confidence"] is None and prefs["paused_until"] is None)

telegram_bot.handle_message(OWNER, "/category GPU,Laptop", connection, owner_id=OWNER)
telegram_bot.handle_message(OWNER, "/source Wallapop", connection, owner_id=OWNER)
prefs = telegram_bot.PrefsStore(connection).get_all()
check("categories stored lowercase", prefs["categories"] == ["gpu", "laptop"])
check("sources stored lowercase", prefs["sources"] == ["wallapop"])
check("valid pause still works",
      "paused" in telegram_bot.handle_message(OWNER, "/pause 2h", connection, owner_id=OWNER))
connection.close()
tmp.cleanup()

# === 5. AI fail-closed + canonicalization ======================================
class CrashClient(ai_assistant.LocalModelClient):
    def __init__(self, payload=None, error=None):
        super().__init__(endpoint="http://x")
        self.payload = payload
        self.error = error
    @property
    def enabled(self):
        return True
    def complete(self, prompt):
        if self.error:
            raise self.error
        return {"content": self.payload}

out = ai_assistant.resolve_listing(
    "Ordenador raro", "Misterio", client=CrashClient(error=TimeoutError()))
check("AI timeout falls back closed",
      out["source"] == "deterministic(ai_error)" and out["brand"] is None)

out = ai_assistant.resolve_listing(
    "Ordenador raro", "Misterio", client=CrashClient(payload="not json"))
check("non-JSON AI reply falls back closed", out["source"] == "deterministic(ai_error)")

out = ai_assistant.resolve_listing(
    "Ordenador raro", "Misterio", client=CrashClient(payload="[1]"))
check("non-object AI reply falls back closed", out["source"] == "deterministic(ai_error)")

good = json.dumps({"brand": "Apple", "model": "MacBook Pro M4", "category": "laptop"})
out = ai_assistant.resolve_listing(
    "Portatil misterioso", "Muy bueno",
    client=CrashClient(payload=good))
check("canonical AI values accepted with provenance",
      out["source"] == "deterministic+ai"
      and out["brand"] == "Apple"
      and out["spec_sources"].get("brand") == "ai"
      and out["spec_confidence"].get("brand") == ai_assistant.AI_FIELD_CONFIDENCE)
check("AI category fills only 'other'",
      out["category"] == "laptop")

invented = json.dumps({"brand": "RTX4060SuperTi", "model": "Zzz 9000"})
out = ai_assistant.resolve_listing(
    "Portatil misterioso", "Muy bueno",
    client=CrashClient(payload=invented))
check("AI-invented names rejected (not in normalizer vocabulary)",
      out["brand"] is None and out["model"] is None)

existing = ai_assistant.resolve_listing(
    "MacBook Pro M4 16GB", "", client=CrashClient(payload=json.dumps({"brand": "Samsung"})))
check("AI never overwrites deterministic fields",
      existing["brand"] == "Apple" and existing["source"] == "deterministic")

# === 6. Strengthened migration test ============================================
with tempfile.TemporaryDirectory() as tmpdir:
    os.environ["HARDWARE_DEALS_DB"] = str(Path(tmpdir) / "mig.db")
    import importlib
    importlib.reload(config)
    for module in (database, normalize_database, classify_conditions,
                   price_estimator):
        importlib.reload(module)

    database.initialize_database()
    database.save_listings(seed_gpu_pool([430, 450, 470, 500, 520]), source="wallapop")
    # one listing with NO normalized row: its legacy estimate must survive
    database.save_listings([{
        "url": "https://x/legacy", "title": "Old thing",
        "price": "50€", "description": "Viejo.",
    }], source="wallapop")
    normalize_database.main()
    classify_conditions.classify_database()
    # Keep the legacy listing OUT of the pipeline: with no normalized row
    # the estimator JOIN skips it, so its stored estimate row must
    # survive the migration and run untouched.
    _c = sqlite3.connect(config.DB_PATH)
    _c.execute(
        "DELETE FROM normalized_listings WHERE listing_id = "
        "(SELECT id FROM listings WHERE url='https://x/legacy')"
    )
    _c.commit()
    _c.close()

    mc = sqlite3.connect(config.DB_PATH)
    mc.row_factory = sqlite3.Row
    # his ORIGINAL estimates schema, pre-migration
    mc.execute("DROP TABLE IF EXISTS price_estimates")
    mc.execute("""
        CREATE TABLE price_estimates (
            listing_id INTEGER PRIMARY KEY, status TEXT NOT NULL,
            condition TEXT, fair_value REAL,
            comparable_count INTEGER NOT NULL DEFAULT 0,
            lowest_comparable REAL, highest_comparable REAL,
            reason TEXT, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    legacy_id = mc.execute(
        "SELECT id FROM listings WHERE url='https://x/legacy'"
    ).fetchone()["id"]
    processed_id = mc.execute(
        "SELECT id FROM listings WHERE url='https://x/g0'"
    ).fetchone()["id"]
    mc.execute(
        "INSERT INTO price_estimates (listing_id, status, reason) VALUES (?, 'estimated', 'legacy untouched')",
        (legacy_id,))
    mc.execute(
        "INSERT INTO price_estimates (listing_id, status, reason) VALUES (?, 'insufficient_data', 'legacy stale')",
        (processed_id,))
    mc.commit()

    price_estimator.run_estimator(mc)

    cols = price_estimator.get_columns(mc, "price_estimates")
    check("migration adds Stage A columns",
          {"value_basis", "estimate_confidence", "reason_code"} <= cols)
    legacy = mc.execute(
        "SELECT status, reason FROM price_estimates WHERE listing_id=?",
        (legacy_id,)).fetchone()
    check("unprocessed legacy row preserved exactly",
          legacy["status"] == "estimated" and legacy["reason"] == "legacy untouched")
    processed = mc.execute(
        "SELECT status, value_basis, reason_code FROM price_estimates WHERE listing_id=?",
        (processed_id,)).fetchone()
    check("processed legacy row upserted to real estimate",
          processed["status"] == "estimated"
          and processed["value_basis"] == config.VALUE_BASIS_ASKING
          and processed["reason_code"] == "estimated")
    mc.close()

failed = [name for name, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} review-fix checks passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
print("ALL REVIEW-FIX CHECKS PASSED")
