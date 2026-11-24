
import sqlite3
from pathlib import Path
import config

db = config.DB_PATH
connection = sqlite3.connect(db)

print("=== ALL NORMALIZED CATEGORIES ===")
for row in connection.execute("""
    SELECT category, COUNT(*)
    FROM normalized_listings
    GROUP BY category
    ORDER BY COUNT(*) DESC
"""):
    print(f"{row[0]}: {row[1]}")

print("\n=== NON-LAPTOP LISTINGS ===")
for row in connection.execute("""
    SELECT l.id, l.title, n.category
    FROM listings l
    JOIN normalized_listings n ON n.listing_id = l.id
    WHERE n.category != 'laptop'
    ORDER BY l.id
"""):
    print(f"{row[0]} | {row[2]} | {row[1]}")

print("\n=== POSSIBLE MISCLASSIFICATIONS ===")
for row in connection.execute("""
    SELECT l.id, l.title, n.category
    FROM listings l
    JOIN normalized_listings n ON n.listing_id = l.id
    WHERE lower(l.title) LIKE '%iphone%'
       OR lower(l.title) LIKE '%galaxy%'
       OR lower(l.title) LIKE '%rtx%'
       OR lower(l.title) LIKE '%playstation%'
       OR lower(l.title) LIKE '%xbox%'
       OR lower(l.title) LIKE '%ssd%'
       OR lower(l.title) LIKE '%ryzen%'
    ORDER BY l.id
"""):
    print(f"{row[0]} | {row[2]} | {row[1]}")

connection.close()
