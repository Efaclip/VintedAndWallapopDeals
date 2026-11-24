"""
Alert selection (Stage E glue).

Applies the user's stored Telegram preferences to scored deals and
decides what would notify RIGHT NOW:

  - /min_discount, /min_score, /min_confidence, /max_price
  - /category and /source filters
  - /pause suppression
  - dedup: a listing already alerted is not alerted again unless its
    price or quality score changed since the alert (alerts_log table)

Selection never changes quality scores; it only filters and orders.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import config
import deal_scorer

DB_FILE = config.DB_PATH


def create_alerts_log(connection):
    connection.execute("""
        CREATE TABLE IF NOT EXISTS alerts_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id INTEGER NOT NULL,
            quality_score REAL NOT NULL,
            price_amount REAL,
            alerted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (listing_id) REFERENCES listings(id)
        )
    """)


def _parse_ts(value):
    """Parse a stored timestamp. Legacy naive timestamps are assumed UTC,
    matching the source convention (database.now_utc writes UTC).
    Returns None for NULL/empty/malformed values."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_paused(prefs, now):
    until = _parse_ts(prefs.get("paused_until"))
    if until is None:
        return False
    return now < until


def _ai_derived_identity(deal):
    raw = deal.get("spec_sources")
    if not raw:
        return False
    try:
        sources = json.loads(raw)
    except (TypeError, ValueError):
        return False
    return sources.get("brand") == "ai" or sources.get("model") == "ai"


def _passes_filters(deal, prefs):
    if prefs.get("min_discount") is not None:
        if deal["discount_pct"] is None or deal["discount_pct"] < prefs["min_discount"]:
            return False
    if prefs.get("min_score") is not None:
        if deal["quality_score"] < prefs["min_score"]:
            return False
    if prefs.get("min_confidence") is not None:
        confidence = deal.get("estimate_confidence")
        if confidence is None or confidence < prefs["min_confidence"]:
            return False
    if prefs.get("max_price") is not None:
        if deal["price"] is None or deal["price"] > prefs["max_price"]:
            return False
    if prefs.get("categories"):
        if deal.get("category") not in prefs["categories"]:
            return False
    if prefs.get("sources"):
        if deal.get("source") not in prefs["sources"]:
            return False
    return True


def describe_specs(deal):
    """Human spec line from the identified fields (empty if unknown)."""
    parts = []
    for key, label in (
        ("brand", None), ("model", None), ("processor", None),
        ("gpu", None), ("ram_gb", "GB RAM"), ("storage_gb", "GB"),
        ("screen_inches", '\"'), ("edition", None),
    ):
        value = deal.get(key)
        if value is None:
            continue
        parts.append(f"{value}{label}" if label else str(value))
    return ", ".join(parts)


def _last_alert(connection, listing_id):
    return connection.execute("""
        SELECT quality_score, price_amount
        FROM alerts_log
        WHERE listing_id = ?
        ORDER BY id DESC
        LIMIT 1
    """, (listing_id,)).fetchone()


def select_alerts(connection, prefs, now=None):
    """
    Return (selected, suppressed) deals. Selected deals are ordered by
    alert priority, then quality score. Nothing is sent or logged here:
    recording happens in record_alerts after actual delivery.
    """
    now = now or datetime.now(timezone.utc)
    create_alerts_log(connection)

    spec_sources_select = (
        "n.spec_sources,"
        if "spec_sources" in {
            r[1] for r in connection.execute(
                "PRAGMA table_info(normalized_listings)"
            )
        }
        else "NULL AS spec_sources,"
    )
    rows = connection.execute(f"""
        SELECT
            l.id, l.title, l.url, l.source, l.price_amount AS price,
            l.last_seen,
            n.category, n.processor, n.gpu, n.ram_gb, n.storage_gb,
            n.screen_inches, n.edition, n.brand, n.model,
            {spec_sources_select}
            s.quality_score, s.discount_pct, s.fair_value,
            s.value_basis, s.estimate_confidence
        FROM deal_scores s
        JOIN listings l ON l.id = s.listing_id
        JOIN normalized_listings n ON n.listing_id = l.id
        JOIN price_estimates e ON e.listing_id = l.id
        WHERE e.status = 'estimated'
    """).fetchall()

    deals = [dict(row) for row in rows]

    if _is_paused(prefs, now):
        return [], deals

    selected = []
    suppressed = []

    staleness_cutoff = timedelta(days=config.ACTIVE_LISTING_DAYS)

    for deal in deals:
        # Eligibility guards (before user preferences):
        # - the ask must be BELOW the estimate (a deal, not just a score)
        # - a minimum quality floor user prefs can only raise
        # - the listing must have been seen recently
        if _ai_derived_identity(deal):
            # AI-guessed brand/model (confidence 0.30) can never back an
            # alert, even if a stale estimated row exists.
            suppressed.append(deal)
            continue
        if deal["discount_pct"] is None or deal["discount_pct"] <= 0:
            suppressed.append(deal)
            continue
        if deal["quality_score"] < config.MIN_ELIGIBLE_QUALITY:
            suppressed.append(deal)
            continue
        last_seen = _parse_ts(deal.get("last_seen"))
        if last_seen is None:
            # Unknown freshness: never alert on data we cannot date.
            suppressed.append(deal)
            continue
        if now - last_seen > staleness_cutoff:
            suppressed.append(deal)
            continue

        if not _passes_filters(deal, prefs):
            suppressed.append(deal)
            continue

        if _is_quarantined(connection, deal):
            # Uncertain prior send: automatic resend is blocked until a
            # person clears the quarantine.
            suppressed.append(deal)
            continue

        last = _last_alert(connection, deal["id"])
        if last is not None:
            unchanged = (
                last["quality_score"] == deal["quality_score"]
                and last["price_amount"] == deal["price"]
            )
            if unchanged:
                suppressed.append(deal)
                continue

        deal["priority"] = deal_scorer.alert_priority(deal, prefs)
        selected.append(deal)

    priority_rank = {"high": 0, "medium": 1, "low": 2}
    selected.sort(key=lambda d: (
        priority_rank.get(d["priority"], 3), -d["quality_score"]
    ))

    return selected, suppressed


def create_quarantine(connection):
    connection.execute("""
        CREATE TABLE IF NOT EXISTS delivery_quarantine (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id INTEGER NOT NULL,
            quality_score REAL NOT NULL,
            price_amount REAL,
            reason TEXT,
            quarantined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (listing_id) REFERENCES listings(id)
        );
    """)
    connection.commit()


def quarantine_delivery(connection, deal, reason):
    """Durably block an uncertain-outcome send from automatic resend.

    A send whose outcome is unknown (transport failure after the request
    left) must never be silently retried by the next scheduled run: if
    it actually landed, that retry would be a duplicate. The deal stays
    quarantined until a person checks Telegram and clears it.
    """
    create_quarantine(connection)
    connection.execute("""
        INSERT INTO delivery_quarantine (listing_id, quality_score,
                                         price_amount, reason)
        VALUES (?, ?, ?, ?)
    """, (deal["id"], deal["quality_score"], deal["price"], reason))
    connection.commit()


def clear_quarantine(connection, listing_id=None):
    """Person-verified release. Returns the number cleared."""
    create_quarantine(connection)
    if listing_id is None:
        count = connection.execute(
            "SELECT COUNT(*) FROM delivery_quarantine").fetchone()[0]
        connection.execute("DELETE FROM delivery_quarantine")
    else:
        count = connection.execute(
            "SELECT COUNT(*) FROM delivery_quarantine WHERE listing_id=?",
            (listing_id,)).fetchone()[0]
        connection.execute(
            "DELETE FROM delivery_quarantine WHERE listing_id=?",
            (listing_id,))
    connection.commit()
    return count


def _is_quarantined(connection, deal):
    create_quarantine(connection)
    row = connection.execute("""
        SELECT 1 FROM delivery_quarantine
        WHERE listing_id = ? AND quality_score = ? AND price_amount IS ?
        LIMIT 1
    """, (deal["id"], deal["quality_score"], deal["price"])).fetchone()
    return row is not None


def record_alerts(connection, deals):
    """Record delivered alerts so repeats are deduped. Idempotent per
    (listing, score, price): re-recording an unchanged deal adds no row."""
    create_alerts_log(connection)
    recorded = 0
    for deal in deals:
        last = _last_alert(connection, deal["id"])
        if (
            last is not None
            and last["quality_score"] == deal["quality_score"]
            and last["price_amount"] == deal["price"]
        ):
            continue
        connection.execute("""
            INSERT INTO alerts_log (listing_id, quality_score, price_amount)
            VALUES (?, ?, ?)
        """, (deal["id"], deal["quality_score"], deal["price"]))
        recorded += 1
    connection.commit()
    return recorded


if __name__ == "__main__":
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE listings (
            id INTEGER PRIMARY KEY, source TEXT, url TEXT, title TEXT,
            price_text TEXT, price_amount REAL, description TEXT,
            first_seen TEXT, last_seen TEXT
        );
        CREATE TABLE normalized_listings (
            listing_id INTEGER PRIMARY KEY, category TEXT, brand TEXT,
            model TEXT, processor TEXT, gpu TEXT, ram_gb INTEGER,
            storage_gb INTEGER, storage_type TEXT, screen_inches REAL,
            edition TEXT, normalized_at TEXT, spec_sources TEXT,
            spec_confidence TEXT, conflicts TEXT
        );
        CREATE TABLE deal_scores (
            listing_id INTEGER PRIMARY KEY, quality_score REAL,
            discount_pct REAL, fair_value REAL, value_basis TEXT,
            estimate_confidence REAL, components TEXT, scored_at TEXT
        );
        INSERT INTO listings VALUES
            (1, 'wallapop', 'u1', 'RTX 4070 SUPER', '', 430, '', 't', 't'),
            (2, 'wallapop', 'u2', 'MacBook Pro M4', '', 1500, '', 't', 't');
        INSERT INTO normalized_listings (listing_id, category) VALUES
            (1, 'gpu'), (2, 'laptop');
        INSERT INTO deal_scores VALUES
            (1, 82, 12.0, 490, 'asking', 0.8, '{}', 't'),
            (2, 40, 1.0, 1515, 'asking', 0.6, '{}', 't');
    """)
    prefs = {"min_score": 50, "categories": ["gpu"], "watches": []}
    selected, suppressed = select_alerts(connection, prefs)
    print("selected:", [(d["id"], d["priority"]) for d in selected])
    print("suppressed:", [d["id"] for d in suppressed])
    print("recorded:", record_alerts(connection, selected))
    again, suppressed2 = select_alerts(connection, prefs)
    print("second run selected (dedup):", [d["id"] for d in again])
