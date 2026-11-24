"""
Vinted adapter (Stage G).

What this module IS:
  - A complete mapping layer from Vinted catalog item JSON (the shape the
    public Vinted web/API endpoints return) into the shared listing
    record used by database.save_listings, plus catalog-level parsing and
    a save path for JSON the user supplies himself (e.g. an export).

What this module is NOT:
  - No live fetching. Vinted has blocked automated activity on this
    account before, and nothing here retries, bypasses or works around
    that. fetch_items() stays a gated seam: it requires
    PERSONAL_DEALS_VINTED_PERMITTED=1 (config.VINTED_COLLECTION_PERMITTED)
    AND still raises NotImplementedError until a permitted client exists.
  - Parsing/mapping is pure data transformation and is NOT gated: it
    touches no network and collects nothing.

Storage, normalization, estimation and scoring stay in the shared
pipeline; marketplace adapters stay independent of each other.
"""

import config

SOURCE_NAME = "vinted"


def _require_permission():
    if not config.VINTED_COLLECTION_PERMITTED:
        raise PermissionError(
            "Vinted collection is disabled. Enable it only after confirming "
            "collection is permitted and technically reliable "
            "(PERSONAL_DEALS_VINTED_PERMITTED=1)."
        )


def _price_text(raw):
    """Vinted returns price either as a string or an object."""
    price = raw.get("price")
    if isinstance(price, dict):
        return str(price.get("amount") or "")
    if price is None:
        return ""
    return str(price)


def _vinted_notes(raw):
    """Seller-independent structured fields, passed through as plain
    description lines so the deterministic condition detector and the
    normalizer see the same words a person would."""
    notes = []
    status = raw.get("status") or raw.get("condition")
    if status:
        notes.append(f"Estado Vinted: {status}")
    brand = raw.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("title") or brand.get("name")
    if brand:
        notes.append(f"Marca Vinted: {brand}")
    size = raw.get("size")
    if isinstance(size, dict):
        size = size.get("title") or size.get("name")
    if size:
        notes.append(f"Talla Vinted: {size}")
    return notes


def map_item(raw):
    """Map one raw Vinted item dict to the shared listing record."""
    url = (raw.get("url") or "").split("?")[0]
    description_parts = [raw.get("description") or ""]
    description_parts.extend(_vinted_notes(raw))
    return {
        "url": url,
        "title": raw.get("title") or "",
        "price": _price_text(raw),
        "description": "\n".join(p for p in description_parts if p),
    }


def parse_catalog(payload):
    """Extract the raw item list from a catalog response payload.

    Accepts the standard {"items": [...]} shape; anything else yields an
    empty list rather than guessing.
    """
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    return items if isinstance(items, list) else []


def map_catalog(payload):
    """Map a full catalog payload to shared listing records, skipping
    entries without a URL (they can never be stored or deduped)."""
    records = []
    for raw in parse_catalog(payload):
        if not isinstance(raw, dict):
            continue
        record = map_item(raw)
        if record["url"]:
            records.append(record)
    return records


def save_catalog(payload):
    """Store a user-supplied catalog payload. Returns save_listings
    counts. This ingests JSON the user already has; it collects nothing."""
    import database
    return database.save_listings(map_catalog(payload), source=SOURCE_NAME)


def fetch_items(search):
    """Live collection seam. Gated AND unimplemented: Vinted blocked
    automated activity on this account; no permitted client exists yet."""
    _require_permission()
    raise NotImplementedError(
        "Vinted live fetching is not implemented. The account previously "
        "hit an automated-activity block; a permitted client must be "
        "agreed before this seam is built."
    )
