"""
Scheduler (Stage F) - planning core, offline-testable.

Decides WHAT work is due (searches to run, descriptions to refresh,
listings to re-check) without performing any network access itself. The
Termux/background execution wrapper comes later; this module is the
testable brain it will call.

Cadence rules from the roadmap:
  - maintain price history (handled by database.save_listings)
  - avoid repeatedly downloading unchanged descriptions
  - refresh incomplete descriptions
  - reduce checking frequency for inactive listings
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import config

DB_FILE = config.DB_PATH

# A description shorter than this counts as incomplete.
MIN_DESCRIPTION_LENGTH = 40


def _parse_ts(value):
    """Parse a stored timestamp; legacy naive values are assumed UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def plan_work(connection, now=None, base_interval_hours=6):
    """
    Return the work plan for one scheduler cycle:

      description_refreshes: listings whose saved description is missing
          or too short, most incomplete first.
      rechecks: active listings due for a price/availability re-check.
      inactive: listings not seen for config.ACTIVE_LISTING_DAYS days;
          they are re-checked config.INACTIVE_CHECK_MULTIPLIER times
          less often.
    """
    now = now or datetime.now(timezone.utc)

    rows = connection.execute("""
        SELECT id, url, title, description, first_seen, last_seen
        FROM listings
        ORDER BY id
    """).fetchall()

    description_refreshes = []
    rechecks = []
    inactive = []

    active_interval = timedelta(hours=base_interval_hours)
    inactive_interval = timedelta(
        hours=base_interval_hours * config.INACTIVE_CHECK_MULTIPLIER
    )
    inactivity_cutoff = now - timedelta(days=config.ACTIVE_LISTING_DAYS)

    for row in rows:
        listing = dict(row)
        description = listing.get("description") or ""
        last_seen = _parse_ts(listing.get("last_seen"))

        if len(description.strip()) < MIN_DESCRIPTION_LENGTH:
            description_refreshes.append(listing)

        is_inactive = last_seen is not None and last_seen < inactivity_cutoff
        interval = inactive_interval if is_inactive else active_interval
        due = last_seen is None or (now - last_seen) >= interval

        if is_inactive:
            if due:
                inactive.append(listing)
        elif due:
            rechecks.append(listing)

    # Most incomplete descriptions first.
    description_refreshes.sort(
        key=lambda item: len((item.get("description") or "").strip())
    )

    return {
        "description_refreshes": description_refreshes,
        "rechecks": rechecks,
        "inactive_rechecks": inactive,
    }


def run_pipeline_order():
    """Documented run order for one full pipeline pass."""
    return [
        "wallapop_scraper.py      (collect; never concurrently)",
        "normalize_database.py    (re-normalize new/changed listings)",
        "classify_conditions.py   (re-classify conditions)",
        "price_estimator.py       (re-estimate values)",
        "deal_scorer.py           (score deals)",
        "telegram alerts          (deliver deals passing filters)",
    ]


if __name__ == "__main__":
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("""
        CREATE TABLE listings (
            id INTEGER PRIMARY KEY, source TEXT, url TEXT, title TEXT,
            price_text TEXT, price_amount REAL, description TEXT,
            first_seen TEXT, last_seen TEXT
        )
    """)
    now = datetime.now(timezone.utc)
    fmt = lambda delta: (now - delta).isoformat(timespec="seconds")
    connection.executemany(
        "INSERT INTO listings VALUES (?, 'wallapop', ?, ?, '', NULL, ?, ?, ?)",
        [
            (1, "u1", "RTX 4070", "", fmt(timedelta(days=1)), fmt(timedelta(hours=1))),
            (2, "u2", "MacBook Pro M4", "short", fmt(timedelta(days=2)), fmt(timedelta(hours=8))),
            (3, "u3", "Old PC", "a" * 100, fmt(timedelta(days=90)), fmt(timedelta(days=45))),
        ],
    )
    plan = plan_work(connection, now=now)
    print("description_refreshes:", [item["id"] for item in plan["description_refreshes"]])
    print("rechecks:", [item["id"] for item in plan["rechecks"]])
    print("inactive_rechecks:", [item["id"] for item in plan["inactive_rechecks"]])
    print("\n".join(run_pipeline_order()))
