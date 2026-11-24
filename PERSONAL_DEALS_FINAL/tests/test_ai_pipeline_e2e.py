"""End-to-end: AI opt-in propagates through run_pipeline's normalize step,
AI fills persist across reruns, and stale AI answers never survive a
source-text change. Uses a stub client; no network."""
import sys, os, json, sqlite3, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import normalize_database
import run_pipeline

class StubClient:
    calls = 0
    @property
    def enabled(self):
        return True
    def complete(self, prompt):
        StubClient.calls += 1
        return {"content": json.dumps(
            {"brand": "NVIDIA", "model": "RTX 4070 SUPER",
             "category": "gpu"})}

def fresh_db(path):
    database.initialize_database()
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO listings (source, title, description, price_amount,"
        " url, first_seen, last_seen) VALUES ('wallapop', ?, '', 500.0,"
        " 'http://x/1', datetime('now'), datetime('now'))",
        ("Tarjeta grafica 12GB gaming potente",))
    conn.commit(); conn.close()

results = []
def check(name, ok):
    print(("PASS" if ok else "FAIL"), name)
    results.append(bool(ok))

with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "t.db")
    os.environ["HARDWARE_DEALS_DB"] = db
    os.environ["HARDWARE_DEALS_AI"] = "1"
    import importlib, config
    importlib.reload(config)
    normalize_database.DB_PATH = config.DB_PATH
    database.DB_FILE = str(config.DB_PATH)
    fresh_db(db)

    # patch the stub into both modules that reference the client class
    normalize_database.LocalModelClient = StubClient

    # Run 1 via the pipeline normalize step: opt-in must propagate.
    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
    run_pipeline.normalize_database.main(use_ai=run_pipeline.normalize_database.ai_opt_in_enabled())
    row = conn.execute("SELECT brand, model, category, spec_sources FROM normalized_listings WHERE listing_id=1").fetchone()
    check("AI fill landed via pipeline opt-in",
          row["brand"] == "NVIDIA" and json.loads(row["spec_sources"]).get("brand") == "ai")
    calls_after_first = StubClient.calls
    check("stub was consulted", calls_after_first >= 1)

    # Run 2 unchanged: AI fill must persist, stub NOT consulted again.
    run_pipeline.normalize_database.main(use_ai=True)
    row2 = conn.execute("SELECT brand, spec_sources FROM normalized_listings WHERE listing_id=1").fetchone()
    check("AI fill persists across rerun", row2["brand"] == "NVIDIA"
          and json.loads(row2["spec_sources"]).get("brand") == "ai")
    check("stub not re-consulted for unchanged content", StubClient.calls == calls_after_first)

    # Change the source text: stale AI answer must be recomputed.
    conn.execute("UPDATE listings SET title='Tarjeta grafica 12GB ROTA para piezas' WHERE id=1")
    conn.commit()
    run_pipeline.normalize_database.main(use_ai=True)
    check("changed content re-consults the model", StubClient.calls > calls_after_first)

    # Deterministic path (no AI flag) must not wipe a stored AI fill for
    # unchanged content either.
    run_pipeline.normalize_database.main(use_ai=False)
    row3 = conn.execute("SELECT brand FROM normalized_listings WHERE listing_id=1").fetchone()
    check("deterministic rerun keeps AI fill for unchanged content",
          row3["brand"] == "NVIDIA")
    # AI top-up: a row cached WITHOUT ai (ai_state none) must be
    # revisited when an AI-enabled run arrives, not skipped.
    conn.execute("UPDATE listings SET title='Otra tarjeta grafica 8GB sin marca visible' WHERE id=1")
    conn.commit()
    run_pipeline.normalize_database.main(use_ai=False)
    row4 = conn.execute("SELECT ai_state, brand FROM normalized_listings WHERE listing_id=1").fetchone()
    check("deterministic-only run stores ai_state none", row4["ai_state"] == "none")
    calls_before = StubClient.calls
    run_pipeline.normalize_database.main(use_ai=True)
    row5 = conn.execute("SELECT ai_state, brand FROM normalized_listings WHERE listing_id=1").fetchone()
    check("AI-enabled run tops up a none row", StubClient.calls > calls_before
          and row5["ai_state"] == "filled" and row5["brand"] == "NVIDIA")

    # Version bump: a normalizer upgrade recomputes cached rows.
    calls_before = StubClient.calls
    run_pipeline.normalize_database.NORMALIZER_VERSION = "999-test"
    run_pipeline.normalize_database.main(use_ai=True)
    check("version bump recomputes cached rows", StubClient.calls > calls_before)
    conn.close()

print(f"\n{sum(results)}/{len(results)} AI pipeline e2e checks passed")
if not all(results):
    sys.exit(1)
print("ALL AI PIPELINE E2E CHECKS PASSED")
