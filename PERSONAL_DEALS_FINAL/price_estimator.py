"""
Price estimator (Stage A).

Estimates a fair ASKING value for each listing from comparable listings
in the local database. Comparables must share category, brand, model and
condition, and must not conflict on any known technical specification.

Estimates are medians of asking prices, NOT confirmed sale prices
(config.VALUE_BASIS_ASKING). A future stage can add a sale-value basis
once a sold-price source exists.

Every listing gets a machine-readable reason_code plus a human reason,
so a run with zero estimates explains exactly why.
"""

import json
import re
import sqlite3
import statistics
from datetime import datetime, timezone

import config

DB_FILE = config.DB_PATH
MIN_COMPARABLES = config.MIN_COMPARABLES
ELIGIBLE_CATEGORIES = config.ELIGIBLE_CATEGORIES

# These specifications must match exactly.
SPEC_FIELDS = [
    "processor",
    "gpu",
    "ram_gb",
    "storage_gb",
    "storage_type",
    "screen_inches",
    "edition",
]

# At least one meaningful specification must be available.
ANCHOR_FIELDS = [
    "processor", "gpu", "ram_gb", "storage_gb",
    "screen_inches", "edition",
]

PRICE_FIELDS = [
    "price_amount",
    "numeric_price",
    "price_numeric",
    "price",
    "price_text",
]

# Machine-readable outcomes, used for run diagnostics and the scorer.
REASON_ESTIMATED = "estimated"
REASON_UNSUPPORTED_CATEGORY = "unsupported_category"
REASON_MISSING_BRAND_MODEL = "missing_brand_or_model"
REASON_FOR_PARTS = "for_parts"
REASON_UNKNOWN_CONDITION = "unknown_condition"
REASON_NO_ANCHOR = "no_configuration_anchor"
REASON_NO_PRICE = "no_valid_price"
REASON_INSUFFICIENT_COMPARABLES = "insufficient_comparables"


def parse_price(value):
    """Convert common Spanish/European price formats into a number.

    Ambiguity policy: a single dot with exactly three trailing digits
    ("1.200") is treated as a thousands separator (hardware-price
    heuristic, not universal locale handling).
    """

    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()

    if not text:
        return None

    # Remove currency symbols and other non-numeric characters.
    text = re.sub(r"[^\d,.\-]", "", text)

    if not text:
        return None

    # European format: 1.660,50 -> 1660.50
    if "." in text and "," in text:
        text = text.replace(".", "").replace(",", ".")

    # A comma followed by three digits is treated as a
    # thousands separator; otherwise, as a decimal separator.
    elif "," in text:
        if re.search(r",\d{3}$", text):
            text = text.replace(",", "")
        else:
            text = text.replace(",", ".")

    # Multiple dots: treat earlier dots as thousands separators.
    elif text.count(".") > 1:
        parts = text.split(".")
        text = "".join(parts[:-1]) + "." + parts[-1]

    # Single dot with exactly three trailing digits is a European
    # thousands separator in this price range ("1.200" -> 1200).
    elif text.count(".") == 1:
        integer_part, fraction = text.split(".")
        if len(fraction) == 3 and integer_part.lstrip("-").isdigit():
            text = integer_part + fraction

    try:
        price = float(text)
        return price if price > 0 else None
    except ValueError:
        return None


def get_columns(connection, table):
    return {
        row["name"]
        for row in connection.execute(
            f'PRAGMA table_info("{table}")'
        ).fetchall()
    }


def find_price_column(connection):
    columns = get_columns(connection, "listings")

    for candidate in PRICE_FIELDS:
        if candidate in columns:
            return candidate

    raise RuntimeError(
        "No recognized price column found in listings. "
        f"Available columns: {sorted(columns)}"
    )


def extract_year(title):
    match = re.search(r"\b(19\d{2}|20[0-2]\d)\b", title or "")
    return int(match.group(1)) if match else None


def has_reliable_generation(listing):
    processor = listing.get("processor")

    if not processor:
        return False

    processor = processor.strip()

    # Apple Silicon generations
    if re.fullmatch(
        r"M[1-9](?:\s+(?:Pro|Max|Ultra))?",
        processor,
        re.I,
    ):
        return True

    # Specific Intel Core processor models
    if re.search(
        r"\bCore\s+i[3579][- ]?\d{4,5}[A-Z]{0,2}\b",
        processor,
        re.I,
    ):
        return True

    # Specific Intel Core Ultra processor models
    if re.search(
        r"\bCore\s+Ultra\s+[579]\s+\d{3,4}[A-Z]{0,2}\b",
        processor,
        re.I,
    ):
        return True

    # Specific AMD Ryzen processor models
    if re.search(
        r"\bRyzen\s+[3579]\s+\d{4,5}",
        processor,
        re.I,
    ):
        return True

    return False


def same_specifications(target, candidate):
    # Laptop matching: use the year when both listings provide it.
    if target["category"] == "laptop":
        target_year = extract_year(target["title"])
        candidate_year = extract_year(candidate["title"])

        if target_year is not None and candidate_year is not None:
            if target_year != candidate_year:
                return False

        # If either year is missing, require a reliable processor generation.
        elif not (
            has_reliable_generation(target)
            and has_reliable_generation(candidate)
        ):
            return False

    # At least one meaningful specification must be known
    # on both listings and match exactly.
    matching_anchor_found = False

    for field in SPEC_FIELDS:
        target_value = target[field]
        candidate_value = candidate[field]

        # Missing information cannot establish a match.
        if target_value is None or candidate_value is None:
            if target_value != candidate_value:
                return False
            continue

        # Any conflicting known specification rejects the match.
        if target_value != candidate_value:
            return False

        if field in ANCHOR_FIELDS:
            matching_anchor_found = True

    return matching_anchor_found


def has_configuration_anchor(listing):
    return any(listing[field] is not None for field in ANCHOR_FIELDS)


def estimate_confidence(prices):
    """Heuristic confidence for an estimate, tunable in config."""
    n = len(prices)
    base = 0.0
    for threshold, confidence in config.CONFIDENCE_BY_COMPARABLES:
        if n >= threshold:
            base = confidence
            break
    if base and n >= 2:
        median = statistics.median(prices)
        if median and (max(prices) - min(prices)) / median > config.SPREAD_PENALTY_THRESHOLD:
            base *= config.SPREAD_PENALTY_FACTOR
    return round(base, 3)


def ai_derived_identity(listing):
    """True when the listing's brand or model came from the local AI.

    AI-derived identity fields are allowed to describe a listing but can
    never seed a strict comparable pool: one wrong AI guess would pollute
    every median built on it. Deterministic reliability gates stay exact.
    """
    raw = listing.get("spec_sources")
    if not raw:
        return False
    try:
        sources = json.loads(raw)
    except (TypeError, ValueError):
        return False
    return sources.get("brand") == "ai" or sources.get("model") == "ai"


def ai_derived_fields(listing, fields):
    """AI-sourced fields among the given ones (confidence 0.30)."""
    raw = listing.get("spec_sources")
    if not raw:
        return []
    try:
        sources = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [f for f in fields if sources.get(f) == "ai"]


def find_comparables(target, all_listings):
    """Return the listings that qualify as comparables for target."""
    condition = target["condition"] or "unknown"
    prices = []
    matches = []

    for candidate in all_listings:
        if candidate["id"] == target["id"]:
            continue

        if candidate["category"] != target["category"]:
            continue

        if candidate["brand"] != target["brand"]:
            continue

        if candidate["model"] != target["model"]:
            continue

        if not candidate["brand"] or not candidate["model"]:
            continue

        if ai_derived_identity(candidate):
            continue

        if candidate["condition"] != condition:
            continue

        if candidate["condition"] in {"unknown", "for_parts"}:
            continue

        if candidate["price"] is None:
            continue

        # Ignore obviously invalid prices for this initial estimator.
        if (
            candidate["price"] < config.MIN_VALID_PRICE
            or candidate["price"] > config.MAX_VALID_PRICE
        ):
            continue

        if not same_specifications(target, candidate):
            continue

        prices.append(candidate["price"])
        matches.append(candidate)

    return prices, matches


def estimate_listing(target, all_listings):
    condition = target["condition"] or "unknown"

    def result(reason_code, reason, prices=None):
        prices = prices or []
        estimated = reason_code == REASON_ESTIMATED

        return {
            "status": (
                "estimated" if estimated
                else "excluded" if reason_code in (
                    REASON_UNSUPPORTED_CATEGORY, REASON_FOR_PARTS,
                )
                else "insufficient_data"
            ),
            "reason_code": reason_code,
            "condition": condition,
            "fair_value": (
                round(statistics.median(prices), 2) if estimated else None
            ),
            "value_basis": config.VALUE_BASIS_ASKING if estimated else None,
            "estimate_confidence": (
                estimate_confidence(prices) if estimated else None
            ),
            "comparable_count": len(prices),
            "lowest_comparable": min(prices) if prices else None,
            "highest_comparable": max(prices) if prices else None,
            "reason": reason,
        }

    if target["category"] not in ELIGIBLE_CATEGORIES:
        return result(REASON_UNSUPPORTED_CATEGORY, "Unsupported category")

    ai_fields = ai_derived_fields(target, ["brand", "model", *ANCHOR_FIELDS])
    if ai_fields:
        return result(
            "ai_derived_fields",
            "AI-derived fields (confidence 0.30) cannot support an "
            "estimate: " + ", ".join(ai_fields),
        )

    if not target["brand"] or not target["model"]:
        return result(
            REASON_MISSING_BRAND_MODEL,
            "Brand or model is missing",
        )

    if condition == "for_parts":
        return result(REASON_FOR_PARTS, "Faulty or for-parts listing")

    if condition == "unknown":
        return result(REASON_UNKNOWN_CONDITION, "Condition is unknown")

    if not has_configuration_anchor(target):
        return result(
            REASON_NO_ANCHOR,
            "Not enough specifications to identify comparable items",
        )

    if target["price"] is None:
        return result(REASON_NO_PRICE, "Listing has no valid price")

    prices, _matches = find_comparables(target, all_listings)

    if len(prices) < MIN_COMPARABLES:
        return result(
            REASON_INSUFFICIENT_COMPARABLES,
            f"Only {len(prices)} comparable listings; "
            f"at least {MIN_COMPARABLES} required",
            prices,
        )

    return result(
        REASON_ESTIMATED,
        "Median of matching asking prices from the local database",
        prices,
    )


def ensure_column(connection, table, column, definition):
    columns = get_columns(connection, table)
    if column not in columns:
        connection.execute(
            f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}'
        )


def create_estimates_table(connection):
    connection.execute("""
        CREATE TABLE IF NOT EXISTS price_estimates (
            listing_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL,
            condition TEXT,
            fair_value REAL,
            comparable_count INTEGER NOT NULL DEFAULT 0,
            lowest_comparable REAL,
            highest_comparable REAL,
            reason TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (listing_id) REFERENCES listings(id)
        )
    """)
    # Upgrades for the Stage A extensions.
    ensure_column(connection, "price_estimates", "value_basis", "TEXT")
    ensure_column(connection, "price_estimates", "estimate_confidence", "REAL")
    ensure_column(connection, "price_estimates", "reason_code", "TEXT")


def save_estimate(connection, listing_id, result):
    connection.execute("""
        INSERT INTO price_estimates (
            listing_id,
            status,
            condition,
            fair_value,
            comparable_count,
            lowest_comparable,
            highest_comparable,
            reason,
            value_basis,
            estimate_confidence,
            reason_code,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(listing_id) DO UPDATE SET
            status = excluded.status,
            condition = excluded.condition,
            fair_value = excluded.fair_value,
            comparable_count = excluded.comparable_count,
            lowest_comparable = excluded.lowest_comparable,
            highest_comparable = excluded.highest_comparable,
            reason = excluded.reason,
            value_basis = excluded.value_basis,
            estimate_confidence = excluded.estimate_confidence,
            reason_code = excluded.reason_code,
            updated_at = CURRENT_TIMESTAMP
    """, (
        listing_id,
        result["status"],
        result["condition"],
        result["fair_value"],
        result["comparable_count"],
        result["lowest_comparable"],
        result["highest_comparable"],
        result["reason"],
        result["value_basis"],
        result["estimate_confidence"],
        result["reason_code"],
    ))


def record_run_summary(connection, reason_counts):
    """Persist a per-run diagnostic summary so runs can be compared."""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS estimation_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT NOT NULL,
            reason_counts TEXT NOT NULL
        )
    """)
    connection.execute(
        "INSERT INTO estimation_runs (run_at, reason_counts) VALUES (?, ?)",
        (
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            json.dumps(reason_counts, sort_keys=True),
        ),
    )


def load_listings(connection):
    listing_columns = get_columns(connection, "listings")
    normalized_columns = get_columns(connection, "normalized_listings")

    required_normalized = {
        "listing_id", "category", "brand", "model",
        *SPEC_FIELDS,
    }

    missing = required_normalized - normalized_columns

    if missing:
        raise RuntimeError(
            f"Missing normalized columns: {sorted(missing)}. "
            "Run normalize_database.py first."
        )

    if "listing_id" not in get_columns(connection, "listing_conditions"):
        raise RuntimeError(
            "listing_conditions table is missing listing_id. "
            "Run classify_conditions.py first."
        )

    price_column = find_price_column(connection)

    if "title" not in listing_columns:
        raise RuntimeError("Listings table has no title column")

    # Read listings and their normalized specifications.
    spec_sources_select = (
        "n.spec_sources,"
        if "spec_sources" in normalized_columns
        else "NULL AS spec_sources,"
    )
    rows = connection.execute(f"""
        SELECT
            l.id,
            l.title,
            l."{price_column}" AS raw_price,
            n.category,
            n.brand,
            n.model,
            n.processor,
            n.gpu,
            n.ram_gb,
            n.storage_gb,
            n.storage_type,
            n.screen_inches,
            n.edition,
            {spec_sources_select}
            c.condition
        FROM listings l
        JOIN normalized_listings n
            ON n.listing_id = l.id
        LEFT JOIN listing_conditions c
            ON c.listing_id = l.id
    """).fetchall()

    listings = []

    for row in rows:
        item = dict(row)
        item["price"] = parse_price(item.pop("raw_price"))
        listings.append(item)

    return listings


def get_estimate(connection, listing_id):
    """Fetch the stored estimate for one listing, or None."""
    row = connection.execute("""
        SELECT *
        FROM price_estimates
        WHERE listing_id = ?
    """, (listing_id,)).fetchone()
    return dict(row) if row else None


def run_estimator(connection=None):
    owns_connection = connection is None
    if owns_connection:
        if not DB_FILE.exists():
            raise FileNotFoundError(f"Database not found: {DB_FILE}")
        connection = sqlite3.connect(DB_FILE)
        connection.row_factory = sqlite3.Row

    try:
        listings = load_listings(connection)

        create_estimates_table(connection)

        counts = {
            "estimated": 0,
            "insufficient_data": 0,
            "excluded": 0,
        }
        reason_counts = {}

        for target in listings:
            estimate = estimate_listing(target, listings)

            save_estimate(connection, target["id"], estimate)
            counts[estimate["status"]] += 1
            reason_counts[estimate["reason_code"]] = (
                reason_counts.get(estimate["reason_code"], 0) + 1
            )

        record_run_summary(connection, reason_counts)

        if owns_connection:
            connection.commit()

        print("\n=== PRICE ESTIMATOR RESULTS ===")
        print(f"Listings examined: {len(listings)}")

        for status, count in counts.items():
            print(f"{status}: {count}")

        print("\n=== WHY LISTINGS WERE NOT ESTIMATED ===")
        for reason_code, count in sorted(
            reason_counts.items(), key=lambda item: -item[1]
        ):
            print(f"{reason_code}: {count}")

        print("\n=== SAMPLE ESTIMATES ===")

        samples = connection.execute("""
            SELECT
                l.id,
                l.title,
                n.category,
                e.condition,
                e.status,
                e.fair_value,
                e.value_basis,
                e.estimate_confidence,
                e.comparable_count,
                e.lowest_comparable,
                e.highest_comparable,
                e.reason
            FROM price_estimates e
            JOIN listings l ON l.id = e.listing_id
            JOIN normalized_listings n
                ON n.listing_id = l.id
            WHERE e.status != 'excluded'
            ORDER BY
                CASE e.status
                    WHEN 'estimated' THEN 0
                    ELSE 1
                END,
                l.id DESC
            LIMIT 15
        """).fetchall()

        for row in samples:
            print(f"\nID: {row['id']}")
            print(f"Title: {row['title']}")
            print(f"Category: {row['category']}")
            print(f"Condition: {row['condition']}")
            print(f"Status: {row['status']}")
            print(f"Fair value: {row['fair_value']}")
            print(f"Value basis: {row['value_basis']}")
            print(f"Confidence: {row['estimate_confidence']}")
            print(f"Comparables: {row['comparable_count']}")
            print(
                f"Comparable range: "
                f"{row['lowest_comparable']} - "
                f"{row['highest_comparable']}"
            )
            print(f"Reason: {row['reason']}")

        print("\nPrice estimates saved successfully.")

    except Exception:
        if owns_connection:
            connection.rollback()
        raise

    finally:
        if owns_connection:
            connection.close()


if __name__ == "__main__":
    run_estimator()
