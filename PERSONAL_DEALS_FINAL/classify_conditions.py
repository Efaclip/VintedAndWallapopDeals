
import sqlite3
from pathlib import Path
from condition_detector import detect_condition
import config

DB_FILE = config.DB_PATH


def classify_database():
    connection = sqlite3.connect(DB_FILE)
    connection.row_factory = sqlite3.Row

    try:
        # Only classify devices and components.
        eligible_categories = {
            "laptop", "phone", "tablet", "console", "desktop",
            "gpu", "cpu", "ram", "storage", "motherboard", "monitor"
        }

        eligible_listings = connection.execute("""
            SELECT l.id, l.title, l.description, n.category
            FROM listings l
            JOIN normalized_listings n ON n.listing_id = l.id
        """).fetchall()

        listings = [
            listing for listing in eligible_listings
            if listing["category"] in eligible_categories
        ]        

        

        
        connection.execute("""
            CREATE TABLE IF NOT EXISTS listing_conditions (
                listing_id INTEGER PRIMARY KEY,
                condition TEXT NOT NULL,
                confidence REAL NOT NULL,
                matched_text TEXT,
                condition_conflict INTEGER NOT NULL DEFAULT 0,
                title_condition TEXT,
                description_condition TEXT,
                title_matched_text TEXT,
                description_matched_text TEXT,
                classified_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (listing_id) REFERENCES listings(id)
            )
        """)

        # Safely upgrade databases created with the older schema.
        existing_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(listing_conditions)"
            ).fetchall()
        }

        new_columns = {
            "condition_conflict": (
                "INTEGER NOT NULL DEFAULT 0"
            ),
            "title_condition": "TEXT",
            "description_condition": "TEXT",
            "title_matched_text": "TEXT",
            "description_matched_text": "TEXT",
        }

        for column, definition in new_columns.items():
            if column not in existing_columns:
                connection.execute(
                    f"ALTER TABLE listing_conditions "
                    f"ADD COLUMN {column} {definition}"
                )

        counts = {}

        for listing in listings:
            result = detect_condition(
                title=listing["title"] or "",
                description=listing["description"] or "",
            )

            
            connection.execute("""
                INSERT INTO listing_conditions (
                    listing_id,
                    condition,
                    confidence,
                    matched_text,
                    condition_conflict,
                    title_condition,
                    description_condition,
                    title_matched_text,
                    description_matched_text,
                    classified_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(listing_id) DO UPDATE SET
                    condition = excluded.condition,
                    confidence = excluded.confidence,
                    matched_text = excluded.matched_text,
                    condition_conflict = excluded.condition_conflict,
                    title_condition = excluded.title_condition,
                    description_condition = excluded.description_condition,
                    title_matched_text = excluded.title_matched_text,
                    description_matched_text = excluded.description_matched_text,
                    classified_at = CURRENT_TIMESTAMP
            """, (
                listing["id"],
                result["condition"],
                result["confidence"],
                result["matched_text"],
                int(result["conflict"]),
                result["title_condition"],
                result["description_condition"],
                result["title_matched_text"],
                result["description_matched_text"],
            ))

            condition = result["condition"]
            counts[condition] = counts.get(condition, 0) + 1

            if result["conflict"]:
                print(
                    f"CONFLICT: ID {listing['id']} | "
                    f"Title: {result['title_condition']} | "
                    f"Description: {result['description_condition']} | "
                    f"Selected: {result['condition']}"
                )

        connection.commit()

        print("\n=== CONDITION CLASSIFICATION RESULTS ===")
        print(f"Listings processed: {len(listings)}")

        for condition, count in sorted(counts.items()):
            print(f"{condition}: {count}")

        print("\n=== SAMPLE RESULTS ===")

        samples = connection.execute("""
            SELECT
                l.id,
                l.title,
                n.category,
                c.condition,
                c.confidence,
                c.matched_text
            FROM listings l
            JOIN normalized_listings n
                ON n.listing_id = l.id
            JOIN listing_conditions c
                ON c.listing_id = l.id
            WHERE n.category != 'accessory'
            ORDER BY l.id DESC
            LIMIT 15
        """).fetchall()

        for row in samples:
            print(f"\nID: {row['id']}")
            print(f"Title: {row['title']}")
            print(f"Category: {row['category']}")
            print(f"Condition: {row['condition']}")
            print(f"Confidence: {row['confidence']}")
            print(f"Matched phrase: {row['matched_text']}")

        print("\nCondition results saved successfully.")

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


if __name__ == "__main__":
    classify_database()
