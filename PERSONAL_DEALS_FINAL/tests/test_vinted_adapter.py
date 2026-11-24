"""Vinted adapter fixture tests - NO network, NO live collection.

Verifies the mapping/parsing layer against realistic Vinted catalog JSON
and that the fetch seam stays gated. Nothing here contacts Vinted."""
import sys, os, json, sqlite3, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import vinted_scraper

RESULTS = []
def check(name, ok):
    RESULTS.append((name, bool(ok)))
    print(("PASS" if ok else "FAIL"), name)

CATALOG = {
    "items": [
        {
            "id": 101,
            "title": "MacBook Air M2 13 8GB 256GB",
            "price": {"amount": "750.00", "currency_code": "EUR"},
            "url": "https://www.vinted.es/items/101-macbook-air-m2?referrer=catalog",
            "description": "Poco uso, con caja y cargador.",
            "brand": {"id": 5, "title": "Apple"},
            "size": {"id": 9, "title": "13 pulgadas"},
            "status": "Muy bueno",
            "user": {"login": "vendedor1"},
        },
        {
            "id": 102,
            "title": "RTX 3060 12GB",
            "price": "220",
            "url": "https://www.vinted.es/items/102-rtx-3060",
            "description": "",
            "status": "Bueno",
        },
        {  # no URL: must be dropped by map_catalog
            "id": 103, "title": "Sin url", "price": "10",
        },
        "not-a-dict",
    ]
}

records = vinted_scraper.map_catalog(CATALOG)
check("catalog maps 2 records, url-less and non-dict dropped", len(records) == 2)

r = records[0]
check("object price -> amount string", r["price"] == "750.00")
check("query string stripped from url",
      r["url"] == "https://www.vinted.es/items/101-macbook-air-m2")
check("vinted status passed into description",
      "Estado Vinted: Muy bueno" in r["description"])
check("vinted brand passed into description",
      "Marca Vinted: Apple" in r["description"])
check("original description preserved", "Poco uso" in r["description"])
check("string price handled", records[1]["price"] == "220")

# roundtrip into a real (temp) database through the shared save path
with tempfile.TemporaryDirectory() as d:
    os.environ["HARDWARE_DEALS_DB"] = os.path.join(d, "v.db")
    import importlib, config
    importlib.reload(config)
    import database
    database.DB_FILE = str(config.DB_PATH)
    database.initialize_database()
    counts = vinted_scraper.save_catalog(CATALOG)
    check("inserted 2 on first save",
          counts["inserted"] == 2 and counts["updated"] == 0)
    counts = vinted_scraper.save_catalog(CATALOG)
    check("idempotent second save",
          counts["inserted"] == 0 and counts["updated"] == 0)
    conn = sqlite3.connect(database.DB_FILE); conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT source, price_amount FROM listings WHERE url LIKE '%101%'").fetchone()
    check("stored with vinted source and parsed price",
          row["source"] == "vinted" and row["price_amount"] == 750.0)
    conn.close()

# the live-fetch seam stays gated even when permission flag is on
os.environ["PERSONAL_DEALS_VINTED_PERMITTED"] = "1"
importlib.reload(config)
try:
    vinted_scraper.fetch_items("macbook")
    check("fetch_items gated", False)
except NotImplementedError:
    check("fetch_items raises NotImplementedError (no bypass)", True)
except PermissionError:
    check("fetch_items gated", False)
os.environ.pop("PERSONAL_DEALS_VINTED_PERMITTED")
importlib.reload(config)
try:
    vinted_scraper.fetch_items("macbook")
    check("permission error without flag", False)
except PermissionError:
    check("permission error without flag", True)

failed = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} vinted adapter checks passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
print("ALL VINTED ADAPTER CHECKS PASSED")
