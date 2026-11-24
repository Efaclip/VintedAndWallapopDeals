
import sqlite3
from pathlib import Path
import config

DB_FILE = config.DB_PATH

connection = sqlite3.connect(DB_FILE)
connection.row_factory = sqlite3.Row

query = """
SELECT
    l.id,
    l.title,
    n.category,
    n.brand,
    n.model,
    n.processor,
    n.ram_gb,
    n.storage_gb,
    n.screen_inches,
    c.condition
FROM listings l
JOIN normalized_listings n
    ON n.listing_id = l.id
LEFT JOIN listing_conditions c
    ON c.listing_id = l.id
WHERE n.category = 'laptop'
ORDER BY l.id
"""

rows = connection.execute(query).fetchall()

print("\n=== LAPTOP COMPARABLE DATA ===")

for row in rows:
    print(
        f"\nID {row['id']}: {row['title']}\n"
        f"  Brand: {row['brand']}\n"
        f"  Model: {row['model']}\n"
        f"  Processor: {row['processor']}\n"
        f"  RAM: {row['ram_gb']}\n"
        f"  Storage: {row['storage_gb']}\n"
        f"  Screen: {row['screen_inches']}\n"
        f"  Condition: {row['condition']}"
    )

connection.close()
