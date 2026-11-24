"""
Deal scorer (Stage C).

Produces a QUALITY score for each deal: how good the opportunity is,
independent of the user's current alert preferences. Alert PRIORITY is
computed separately (alert_priority) from user preferences and never
changes the quality score. Prioritizing GPUs over laptops must not move
any deal's quality number.

Quality components (weights in config.SCORE_WEIGHTS):
  - discount: asking price vs estimated fair value
  - price_reliability: estimate confidence from the estimator
  - spec_match: how completely the listing's specs were identified
  - condition: classified condition of the item
  - comparables: quantity backing the estimate
"""

import json
import sqlite3

import config

DB_FILE = config.DB_PATH

CONDITION_QUALITY = {
    "new": 1.00,
    "like_new": 0.85,
    "used": 0.70,
    "for_parts": 0.00,
    "unknown": 0.30,
}

# Discount at which the discount component saturates (40% below estimate).
DISCOUNT_SATURATION = 40.0

PRIORITY_LEVELS = ("low", "medium", "high")


def discount_pct(price, fair_value):
    """Positive when the ask is below the estimate, negative above it."""
    if not fair_value or fair_value <= 0 or price is None:
        return None
    return round((fair_value - price) / fair_value * 100, 2)


def clamp(value, low=0.0, high=1.0):
    return max(low, min(high, value))


def discount_component(discount):
    if discount is None:
        return 0.0
    return clamp(discount / DISCOUNT_SATURATION)


def spec_match_component(normalized):
    """Fraction of anchor specs identified, penalized by conflicts."""
    anchor_fields = (
        "processor", "gpu", "ram_gb", "storage_gb",
        "screen_inches", "edition",
    )
    known = sum(1 for field in anchor_fields if normalized.get(field) is not None)
    base = known / len(anchor_fields)
    conflicts = normalized.get("conflicts") or {}
    if conflicts:
        base *= 0.5
    return round(base, 3)


def condition_component(condition):
    return CONDITION_QUALITY.get(condition or "unknown", 0.30)


def comparables_component(comparable_count):
    return clamp((comparable_count or 0) / 10)


def price_reliability_component(estimate):
    confidence = (estimate or {}).get("estimate_confidence")
    if confidence is None:
        return 0.0
    return clamp(confidence)


def score_deal(listing, estimate, normalized):
    """
    Quality score from 0 to 100. Returns None when the listing has no
    usable estimate: an unestimated listing has no quality signal.
    """
    estimate = estimate or {}
    if estimate.get("status") != "estimated" or not estimate.get("fair_value"):
        return None

    discount = discount_pct(listing.get("price"), estimate["fair_value"])

    # An ask at or above the estimate is not a deal. The record is kept
    # (so /why can still explain it) but its quality is zero and the
    # alert selector will never pick it up.
    overpriced = discount is not None and discount <= 0

    components = {
        "discount": discount_component(discount),
        "price_reliability": price_reliability_component(estimate),
        "spec_match": spec_match_component(normalized),
        "condition": condition_component(estimate.get("condition")),
        "comparables": comparables_component(estimate.get("comparable_count")),
    }

    weights = config.SCORE_WEIGHTS
    total_weight = sum(weights.values())
    quality = sum(
        components[name] * weights.get(name, 0) for name in components
    ) / total_weight

    return {
        "quality_score": 0.0 if overpriced else round(quality * 100, 1),
        "overpriced": overpriced,
        "discount_pct": discount,
        "fair_value": estimate["fair_value"],
        "value_basis": estimate.get("value_basis", config.VALUE_BASIS_ASKING),
        "estimate_confidence": estimate.get("estimate_confidence"),
        "components": components,
    }


def alert_priority(deal, prefs):
    """
    PRIORITY: how urgently to notify, from user preferences only.

    Reads (never writes) the deal's quality_score. Category priorities and
    watch terms come from prefs; they reorder notifications, they do not
    change quality.
    """
    if deal is None:
        return None

    quality = deal["quality_score"]
    category = deal.get("category")
    title = (deal.get("title") or "").lower()

    priority_order = prefs.get("priority_order") or []
    watch_terms = [term.lower() for term in (prefs.get("watches") or [])]

    level = 0  # low
    if quality >= (prefs.get("min_score") or 0):
        level = 1  # medium
    if category and category in priority_order:
        level += 1
    if any(term in title for term in watch_terms):
        level += 1

    return PRIORITY_LEVELS[min(level, len(PRIORITY_LEVELS) - 1)]


def create_scores_table(connection):
    connection.execute("""
        CREATE TABLE IF NOT EXISTS deal_scores (
            listing_id INTEGER PRIMARY KEY,
            quality_score REAL NOT NULL,
            discount_pct REAL,
            fair_value REAL,
            value_basis TEXT,
            estimate_confidence REAL,
            components TEXT NOT NULL DEFAULT '{}',
            scored_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (listing_id) REFERENCES listings(id)
        )
    """)


def score_database(connection=None):
    """Score every listing with a usable estimate. Returns counts."""
    owns_connection = connection is None
    if owns_connection:
        if not DB_FILE.exists():
            raise FileNotFoundError(f"Database not found: {DB_FILE}")
        connection = sqlite3.connect(DB_FILE)
        connection.row_factory = sqlite3.Row

    try:
        rows = connection.execute("""
            SELECT
                l.id,
                l.title,
                l.price_amount AS price,
                n.category,
                n.processor,
                n.gpu,
                n.ram_gb,
                n.storage_gb,
                n.screen_inches,
                n.edition,
                n.conflicts,
                e.status,
                e.fair_value,
                e.value_basis,
                e.estimate_confidence,
                e.comparable_count,
                e.condition
            FROM listings l
            JOIN normalized_listings n ON n.listing_id = l.id
            JOIN price_estimates e ON e.listing_id = l.id
        """).fetchall()

        create_scores_table(connection)

        scored = 0
        skipped = 0

        for row in rows:
            listing = {"id": row["id"], "title": row["title"], "price": row["price"]}
            estimate = {
                "status": row["status"],
                "fair_value": row["fair_value"],
                "value_basis": row["value_basis"],
                "estimate_confidence": row["estimate_confidence"],
                "comparable_count": row["comparable_count"],
                "condition": row["condition"],
            }
            normalized = {
                "processor": row["processor"],
                "gpu": row["gpu"],
                "ram_gb": row["ram_gb"],
                "storage_gb": row["storage_gb"],
                "screen_inches": row["screen_inches"],
                "edition": row["edition"],
                "conflicts": json.loads(row["conflicts"] or "{}"),
            }

            result = score_deal(listing, estimate, normalized)

            if result is None:
                skipped += 1
                continue

            connection.execute("""
                INSERT INTO deal_scores (
                    listing_id, quality_score, discount_pct, fair_value,
                    value_basis, estimate_confidence, components, scored_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(listing_id) DO UPDATE SET
                    quality_score = excluded.quality_score,
                    discount_pct = excluded.discount_pct,
                    fair_value = excluded.fair_value,
                    value_basis = excluded.value_basis,
                    estimate_confidence = excluded.estimate_confidence,
                    components = excluded.components,
                    scored_at = CURRENT_TIMESTAMP
            """, (
                row["id"],
                result["quality_score"],
                result["discount_pct"],
                result["fair_value"],
                result["value_basis"],
                result["estimate_confidence"],
                json.dumps(result["components"]),
            ))
            scored += 1

        # Invalidate scores whose estimate is no longer usable: a stale
        # score must never survive the estimate that justified it.
        cursor = connection.execute("""
            DELETE FROM deal_scores
            WHERE listing_id NOT IN (
                SELECT listing_id FROM price_estimates
                WHERE status = 'estimated'
            )
        """)
        purged = cursor.rowcount

        if owns_connection:
            connection.commit()

        print("\n=== DEAL SCORER RESULTS ===")
        print(f"Listings examined: {len(rows)}")
        print(f"Scored: {scored}")
        print(f"Skipped (no usable estimate): {skipped}")

        return {"examined": len(rows), "scored": scored, "skipped": skipped}

    except Exception:
        if owns_connection:
            connection.rollback()
        raise

    finally:
        if owns_connection:
            connection.close()


if __name__ == "__main__":
    score_database()
