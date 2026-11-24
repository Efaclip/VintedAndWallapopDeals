"""Regression: listings whose brand/model identity was AI-derived must
never enter a strict comparable pool, even when every deterministic
field matches. They may still be estimated as targets."""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import price_estimator

base = dict(
    category="laptop", brand="Apple", model="MacBook Air 13",
    processor="M2", gpu=None, ram_gb=8, storage_gb=256,
    storage_type=None, screen_inches=None, edition=None,
    condition="used", price=700.0, title="MacBook Air 13 M2 8GB 256GB",
)

target = dict(base, id=1, spec_sources="{}")

ai_brand = dict(base, id=2,
                spec_sources=json.dumps({"brand": "ai", "model": "title"}))
ai_model = dict(base, id=3,
                spec_sources=json.dumps({"brand": "title", "model": "ai"}))
deterministic = dict(base, id=4,
                     spec_sources=json.dumps({"brand": "title", "model": "title"}))
legacy_no_sources = dict(base, id=5)  # no spec_sources key at all
garbage_sources = dict(base, id=6, spec_sources="not json {")

prices, matches = price_estimator.find_comparables(
    target, [ai_brand, ai_model, deterministic, legacy_no_sources, garbage_sources])

ids = {m["id"] for m in matches}
checks = [
    ("AI-derived brand excluded", 2 not in ids),
    ("AI-derived model excluded", 3 not in ids),
    ("deterministic candidate kept", 4 in ids),
    ("missing spec_sources treated as deterministic", 5 in ids),
    ("unparseable spec_sources treated as deterministic", 6 in ids),
    ("pool prices all from deterministic candidates", prices == [700.0, 700.0, 700.0] and len(matches) == 3),
    ("ai_derived_identity flags ai brand", price_estimator.ai_derived_identity(ai_brand)),
    ("ai_derived_identity flags ai model", price_estimator.ai_derived_identity(ai_model)),
    ("ai_derived_identity clean on deterministic", not price_estimator.ai_derived_identity(deterministic)),
]
failed = []
for name, ok in checks:
    print(("PASS" if ok else "FAIL"), name)
    if not ok:
        failed.append(name)

# An AI-identified listing can still BE estimated from a deterministic pool.
ai_target = dict(base, id=7, spec_sources=json.dumps({"brand": "ai"}))
prices2, _ = price_estimator.find_comparables(ai_target, [deterministic])
ok = prices2 == [700.0]
print(("PASS" if ok else "FAIL"), "AI-identified listing remains estimable as target")
if not ok:
    failed.append("target estimable")

print(f"\n{len(checks) + 1 - len(failed)}/{len(checks) + 1} AI-pool-guard checks passed")
if failed:
    sys.exit(1)
print("ALL AI-POOL-GUARD CHECKS PASSED")

# --- Target-side gates -------------------------------------------------------
# An AI-identified TARGET on a supported category must not be estimated,
# even when a full deterministic pool exists.
pool = [dict(base, id=i, price=600.0 + i, spec_sources="{}") for i in (10, 11, 12, 13)]

ai_identity_target = dict(base, id=20, spec_sources=json.dumps({"brand": "ai"}))
res = price_estimator.estimate_listing(ai_identity_target, pool)
ok = res["reason_code"] == "ai_derived_fields" and res["status"] == "insufficient_data"
print(("PASS" if ok else "FAIL"), "AI-identity target refused estimate:", res["reason_code"])
failed_t = [] if ok else ["ai target"]

ai_config_target = dict(base, id=21,
                        spec_sources=json.dumps({"brand": "title", "model": "title", "ram_gb": "ai"}))
res = price_estimator.estimate_listing(ai_config_target, pool)
ok = res["reason_code"] == "ai_derived_fields" and "ram_gb" in res["reason"]
print(("PASS" if ok else "FAIL"), "AI-config target refused estimate:", res["reason_code"])
if not ok:
    failed_t.append("ai config target")

det_target = dict(base, id=22, spec_sources="{}")
res = price_estimator.estimate_listing(det_target, pool)
ok = res["status"] == "estimated" and res["comparable_count"] == 4
print(("PASS" if ok else "FAIL"), "deterministic target still estimated:", res["reason_code"])
if not ok:
    failed_t.append("det target")

# Selector predicate: AI identity deal is suppressed even with an estimated row.
import alert_selector
ok = alert_selector._ai_derived_identity(
    {"spec_sources": json.dumps({"model": "ai"})}) is True
ok2 = alert_selector._ai_derived_identity({"spec_sources": "{}"}) is False
ok3 = alert_selector._ai_derived_identity({"spec_sources": None}) is False
print(("PASS" if ok and ok2 and ok3 else "FAIL"), "selector AI-identity predicate")
if not (ok and ok2 and ok3):
    failed_t.append("selector predicate")

if failed_t:
    print("FAILED:", failed_t)
    sys.exit(1)
print("ALL TARGET-SIDE AI GUARD CHECKS PASSED")
