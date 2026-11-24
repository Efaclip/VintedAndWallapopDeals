
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from contextlib import closing
import config


DB_FILE = config.DB_PATH


def now_utc():
    """Return a consistent UTC timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_price(value):
    """
    Convert common price strings to a numeric euro amount.
    Return None if the price cannot be interpreted safely.

    Ambiguity policy: a single dot with exactly three trailing digits
    ("1.200") is treated as a European thousands separator, because in
    this project's 20-15000 EUR hardware range a decimal "1.200" is
    implausible. This is a domain heuristic, NOT universal locale
    handling; genuinely ambiguous strings outside the hardware price
    range may still mis-parse.
    """
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return None

    # Remove currency symbols, spaces and other text.
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[^\d,.\-]", "", text)

    if not re.search(r"\d", text):
        return None

    # Handle common European formats:
    # 1.234,56 -> 1234.56
    # 220,50   -> 220.50
    # 220.50   -> 220.50
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        parts = text.split(",")
        if len(parts[-1]) == 2:
            text = "".join(parts[:-1]) + "." + parts[-1]
        else:
            text = "".join(parts)
    elif text.count(".") > 1:
        parts = text.split(".")
        if len(parts[-1]) == 2:
            text = "".join(parts[:-1]) + "." + parts[-1]
        else:
            text = "".join(parts)
    elif text.count(".") == 1:
        # Single dot with exactly three trailing digits is a European
        # thousands separator in this price range ("1.200" -> 1200),
        # not a decimal. Two trailing digits stay decimal ("20.50").
        integer_part, fraction = text.split(".")
        if len(fraction) == 3 and integer_part.lstrip("-").isdigit():
            text = integer_part + fraction

    try:
        price = float(text)
        return price if price >= 0 else None
    except ValueError:
        return None


def connect():
    """Open the database and enable foreign-key checks."""
    connection = sqlite3.connect(DB_FILE)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database():
    """Create the tables if they don't already exist."""
    with closing(connect()) as connection, connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                title TEXT,
                price_text TEXT,
                price_amount REAL,
                description TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id INTEGER NOT NULL,
                price_text TEXT,
                price_amount REAL,
                observed_at TEXT NOT NULL,
                FOREIGN KEY (listing_id)
                    REFERENCES listings(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_listings_source
                ON listings(source);

            CREATE INDEX IF NOT EXISTS idx_price_history_listing
                ON price_history(listing_id, observed_at);
        """)


def save_listings(items, source="wallapop"):
    """
    Save listings from either:
      - a list of dictionaries containing a 'url' field, or
      - a dictionary keyed by URL, as in the existing scraper.

    Returns counts of inserted, updated and unchanged listings.
    """
    if isinstance(items, dict):
        records = [
            {"url": url, **item}
            for url, item in items.items()
        ]
    else:
        records = items

    counts = {"inserted": 0, "updated": 0, "unchanged": 0}

    with closing(connect()) as connection, connection:
        for item in records:
            url = str(item.get("url") or "").strip()
            if not url:
                continue

            title = item.get("title")
            price_text = item.get("price")
            if price_text is None:
                price_text = item.get("price_text")

            price_text = (
                str(price_text).strip()
                if price_text is not None else None
            )

            if price_text is not None:
                price_text = price_text.strip()
                if not price_text:
                    price_text = None
                    
            price_amount = parse_price(price_text)
            description = item.get("description")
            timestamp = now_utc()

            existing = connection.execute(
                """
                SELECT id, title, price_text, price_amount, description
                FROM listings
                WHERE url = ?
                """,
                (url,),
            ).fetchone()

            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO listings (
                        source, url, title, price_text, price_amount,
                        description, first_seen, last_seen
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source, url, title, price_text, price_amount,
                        description, timestamp, timestamp,
                    ),
                )
                listing_id = cursor.lastrowid

                connection.execute(
                    """
                    INSERT INTO price_history (
                        listing_id, price_text, price_amount, observed_at
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (listing_id, price_text, price_amount, timestamp),
                )
                counts["inserted"] += 1
                continue

            listing_id = existing["id"]

            # Keep previously collected information if a new scrape
            # returns an empty field.
            new_title = title or existing["title"]
            new_description = description or existing["description"]
            new_price_text = (
                price_text
                if price_text is not None
                else existing["price_text"]
            )
            new_price_amount = (
                price_amount
                if price_text is not None
                else existing["price_amount"]
            )

            changed = any([
                new_title != existing["title"],
                new_description != existing["description"],
                new_price_text != existing["price_text"],
                new_price_amount != existing["price_amount"],
            ])

            connection.execute(
                """
                UPDATE listings
                SET title = ?, price_text = ?, price_amount = ?,
                    description = ?, last_seen = ?
                WHERE id = ?
                """,
                (
                    new_title, new_price_text, new_price_amount,
                    new_description, timestamp, listing_id,
                ),
            )

            if (
                new_price_text != existing["price_text"]
                or new_price_amount != existing["price_amount"]
            ):
                connection.execute(
                    """
                    INSERT INTO price_history (
                        listing_id, price_text, price_amount, observed_at
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        listing_id, new_price_text,
                        new_price_amount, timestamp,
                    ),
                )

            counts["updated" if changed else "unchanged"] += 1

    return counts


def get_listing_count():
    """Return the number of unique listings stored."""
    with closing(connect()) as connection, connection:
        row = connection.execute(
            "SELECT COUNT(*) AS total FROM listings"
        ).fetchone()
        return row["total"]


if __name__ == "__main__":
    initialize_database()
    print(f"Database ready: {DB_FILE}")
    print(f"Stored unique listings: {get_listing_count()}")
