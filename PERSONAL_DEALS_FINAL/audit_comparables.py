
import sqlite3
from pathlib import Path
import config

DB_FILE = config.DB_PATH

connection = sqlite3.connect(DB_FILE)
connection.row_factory = sqlite3.Row

try:
    rows = connection.execute("""
        SELECT
            e.listing_id,
            l.title,
            e.fair_value,
            e.comparable_count,
            e.lowest_comparable,
            e.highest_comparable
        FROM price_estimates e
        JOIN listings l ON l.id = e.listing_id
        WHERE e.status = 'estimated'
        ORDER BY e.listing_id
    """).fetchall()

    print("=== ESTIMATED LISTINGS ===")

    for row in rows:
        print(f"\nTarget ID: {row['listing_id']}")
        print(f"Title: {row['title']}")
        print(f"Fair value: €{row['fair_value']}")
        print(f"Comparables: {row['comparable_count']}")
        print(
            f"Price range: €{row['lowest_comparable']} "
            f"- €{row['highest_comparable']}"
        )

        print("\nPotential comparable listings:")

        # Show other listings in the same category, brand and model.
        matches = connection.execute("""
            SELECT
                l.id,
                l.title,
                l.price_text,
                n.processor,
                n.ram_gb,
                n.storage_gb,
                c.condition
            FROM listings l
            JOIN normalized_listings n
                ON n.listing_id = l.id
            JOIN listing_conditions c
                ON c.listing_id = l.id
            JOIN normalized_listings target
                ON target.listing_id = ?
            WHERE n.category = target.category
              AND n.brand = target.brand
              AND n.model = target.model
              AND l.id != ?
            ORDER BY l.id
        """, (row["listing_id"], row["listing_id"])).fetchall()

        for match in matches:
            print(
                f"  ID {match['id']}: {match['title']} | "
                f"Price: {match['price_text']} | "
                f"CPU: {match['processor']} | "
                f"RAM: {match['ram_gb']} | "
                f"Storage: {match['storage_gb']} | "
                f"Condition: {match['condition']}"
            )

finally:
    connection.close()
