import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone

from normalizer import normalize_listing
from ai_assistant import resolve_listing, LocalModelClient
import config

DB_PATH = config.DB_PATH

# Bump whenever normalizer/condition logic changes: cached rows are only
# valid for the code version + AI mode + exact source text that made
# them. A code upgrade or an AI-mode toggle recomputes affected rows.
NORMALIZER_VERSION = "3"


def ensure_column(connection, table, column, definition):
    columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
    if column not in columns:
        connection.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}')


def main(use_ai=False):
    """Batch-normalize every stored listing.

    use_ai=False (default): deterministic rules only. use_ai=True routes
    listings through the local AI assistant (Stage D), which acts only on
    ambiguous listings, fails closed, and never overwrites deterministic
    results. Without a configured endpoint the flag is a no-op.
    """
    client = LocalModelClient() if use_ai else None
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS normalized_listings (
                listing_id INTEGER PRIMARY KEY,
                category TEXT, brand TEXT, model TEXT, processor TEXT, gpu TEXT,
                ram_gb INTEGER, storage_gb INTEGER, storage_type TEXT,
                screen_inches REAL, edition TEXT, normalized_at TEXT NOT NULL,
                spec_sources TEXT NOT NULL DEFAULT '{}',
                spec_confidence TEXT NOT NULL DEFAULT '{}',
                conflicts TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (listing_id) REFERENCES listings(id) ON DELETE CASCADE
            )
        """)
        ensure_column(connection, "normalized_listings", "source_hash", "TEXT")
        ensure_column(connection, "normalized_listings", "ai_state", "TEXT NOT NULL DEFAULT 'none'")
        ensure_column(connection, "normalized_listings", "spec_sources", "TEXT NOT NULL DEFAULT '{}'")
        ensure_column(connection, "normalized_listings", "spec_confidence", "TEXT NOT NULL DEFAULT '{}'")
        ensure_column(connection, "normalized_listings", "conflicts", "TEXT NOT NULL DEFAULT '{}'")

        listings = connection.execute("""
            SELECT id, source, url, title, description, price_amount
            FROM listings ORDER BY id
        """).fetchall()
        processed, skipped, errors = 0, 0, []
        for listing in listings:
            try:
                # Provenance cache: a listing whose source text is
                # unchanged keeps its existing row, INCLUDING AI fills.
                # A changed title/description recomputes, so stale AI
                # answers never survive new content.
                digest = hashlib.sha256(
                    NORMALIZER_VERSION.encode()
                    + b"\x00"
                    + (listing["title"] or "").encode("utf-8")
                    + b"\x00"
                    + (listing["description"] or "").encode("utf-8")
                ).hexdigest()
                existing = connection.execute(
                    "SELECT source_hash, ai_state FROM normalized_listings "
                    "WHERE listing_id = ?",
                    (listing["id"],),
                ).fetchone()
                if existing and existing["source_hash"] == digest:
                    # Fresh for this code version + source text. An
                    # AI-enabled run may still top up a row the AI never
                    # saw; a deterministic rerun never wipes AI fills.
                    ai_state = existing["ai_state"]
                    if (
                        client is None
                        or not client.enabled
                        or ai_state in ("filled", "attempted")
                    ):
                        skipped += 1
                        continue

                if client is not None and client.enabled:
                    result = resolve_listing(
                        listing["title"] or "",
                        listing["description"] or "",
                        client=client,
                    )
                    # resolve_listing's "source" is the AI provenance;
                    # capture it before the marketplace name overwrites.
                    provenance = result.get("source", "deterministic")
                    result["price"] = listing["price_amount"]
                    result["url"] = listing["url"]
                    result["source"] = listing["source"]
                else:
                    result = normalize_listing(
                        title=listing["title"] or "",
                        description=listing["description"] or "",
                        price=listing["price_amount"], url=listing["url"], source=listing["source"]
                    )
                    provenance = "deterministic"
                source = provenance
                ai_state = (
                    "filled" if source == "deterministic+ai"
                    else "attempted" if source.startswith("deterministic(ai_")
                    else "none"
                )
                connection.execute("""
                    INSERT INTO normalized_listings (
                        listing_id, category, brand, model, processor, gpu,
                        ram_gb, storage_gb, storage_type, screen_inches, edition,
                        normalized_at, spec_sources, spec_confidence, conflicts,
                        source_hash, ai_state
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(listing_id) DO UPDATE SET
                        category=excluded.category, brand=excluded.brand, model=excluded.model,
                        processor=excluded.processor, gpu=excluded.gpu, ram_gb=excluded.ram_gb,
                        storage_gb=excluded.storage_gb, storage_type=excluded.storage_type,
                        screen_inches=excluded.screen_inches, edition=excluded.edition,
                        normalized_at=excluded.normalized_at, spec_sources=excluded.spec_sources,
                        spec_confidence=excluded.spec_confidence,
                        conflicts=excluded.conflicts,
                        source_hash=excluded.source_hash,
                        ai_state=excluded.ai_state
                """, (
                    listing["id"], result["category"], result["brand"], result["model"],
                    result["processor"], result["gpu"], result["ram_gb"], result["storage_gb"],
                    result["storage_type"], result["screen_inches"], result["edition"],
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    json.dumps(result["spec_sources"], ensure_ascii=False),
                    json.dumps(result.get("spec_confidence", {}), ensure_ascii=False),
                    json.dumps(result["conflicts"], ensure_ascii=False),
                    digest,
                    ai_state,
                ))
                processed += 1
            except Exception as exc:
                errors.append(f"Listing ID {listing['id']}: {exc}")

        connection.commit()
        print("\\n=== DESCRIPTION-AWARE NORMALIZATION ===")
        print(f"Listings found: {len(listings)}")
        print(f"Unchanged (kept, incl. AI fills): {skipped}")
        print(f"Successfully normalized: {processed}")
        print(f"Errors: {len(errors)}")
        print("\\n=== SAMPLE RESULTS ===")
        rows = connection.execute("""
            SELECT l.id, l.title, n.category, n.model, n.processor, n.ram_gb,
                   n.storage_gb, n.spec_sources, n.conflicts
            FROM listings l JOIN normalized_listings n ON n.listing_id=l.id
            ORDER BY l.id DESC LIMIT 12
        """).fetchall()
        for row in rows:
            print(f"\\nID {row['id']}: {row['title']}")
            print(f"  Category/model: {row['category']} / {row['model']}")
            print(f"  Processor: {row['processor']} | RAM: {row['ram_gb']} GB | Storage: {row['storage_gb']} GB")
            print(f"  Sources: {row['spec_sources']}")
            print(f"  Conflicts: {row['conflicts']}")
        if errors:
            print("\\n=== ERRORS ===")
            print("\\n".join(errors))
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def ai_opt_in_enabled(argv=None):
    """The local-AI path is strictly opt-in.

    Enable it with --ai on the command line, HARDWARE_DEALS_AI=1 in the
    environment, or AI_ENABLED=True in config.py. Deterministic
    normalization always runs first; the AI only sees listings the rules
    left ambiguous, fails closed, and marks every field it fills with
    spec_sources '<field>': 'ai' so those listings can never seed a
    strict comparable pool.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--ai" in argv:
        return True
    env = os.environ.get("HARDWARE_DEALS_AI", "").strip().lower()
    if env in {"1", "true", "yes", "on"}:
        return True
    return bool(getattr(config, "AI_ENABLED", False))


if __name__ == "__main__":
    use_ai = ai_opt_in_enabled()
    if use_ai:
        print("AI assist ENABLED via opt-in (ambiguous listings only; "
              "AI fields are marked spec_sources='ai').")
    main(use_ai=use_ai)
