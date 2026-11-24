"""
Local AI assistant (Stage D).

Deterministic rules always run first. A small LOCAL language model is
consulted ONLY for listings the deterministic pipeline finds ambiguous:
unclear titles, missing models, conflicting specifications, unusual items.

No model is bundled. Set PERSONAL_DEALS_AI_ENDPOINT to a local server
(for example a llama.cpp/ollama-style /completion endpoint) to enable it.
Without an endpoint the assistant reports "disabled" and changes nothing.
"""

import json
import urllib.request

import config
from normalizer import normalize_listing

# Categories the AI may set when the normalizer returns "other".
KNOWN_CATEGORIES = {
    "laptop", "phone", "tablet", "console", "desktop",
    "gpu", "cpu", "ram", "storage", "motherboard", "monitor",
    "accessory",
}

# Confidence attached to AI-filled fields. Deliberately low: the price
# estimator matches on exact strings, so AI-derived values must never
# look as trustworthy as deterministic extraction.
AI_FIELD_CONFIDENCE = 0.30

# Categories where a missing model makes a listing genuinely ambiguous.
DEVICE_CATEGORIES = {
    "laptop", "phone", "tablet", "console", "desktop",
    "gpu", "cpu", "monitor",
}


def ambiguity_reasons(normalized):
    """Why the deterministic pipeline wants a second opinion, if it does."""
    reasons = []

    category = normalized.get("category")

    if category == "other":
        reasons.append("unrecognized_category")

    if category in DEVICE_CATEGORIES and not normalized.get("model"):
        reasons.append("model_missing")

    if category in DEVICE_CATEGORIES and not normalized.get("brand"):
        reasons.append("brand_missing")

    if normalized.get("conflicts"):
        reasons.append("conflicting_specs")

    processor = normalized.get("processor")
    if category == "laptop" and processor in ("i3", "i5", "i7", "i9"):
        reasons.append("generic_processor")

    return reasons


def needs_ai(normalized):
    return bool(ambiguity_reasons(normalized))


class LocalModelClient:
    """Minimal client for a local completion endpoint. Offline-safe."""

    def __init__(self, endpoint=None, timeout=None):
        self.endpoint = endpoint if endpoint is not None else config.AI_ENDPOINT
        self.timeout = timeout or config.AI_TIMEOUT_SECONDS

    @property
    def enabled(self):
        return bool(self.endpoint)

    def complete(self, prompt):
        if not self.enabled:
            raise RuntimeError(
                "AI endpoint not configured; set PERSONAL_DEALS_AI_ENDPOINT"
            )
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps({"prompt": prompt}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))


IDENTIFY_PROMPT = """Identify the hardware product in this second-hand listing.
Reply with JSON only: {{"brand": ..., "model": ..., "category": ...}}.
If unsure, use null. Title: {title}
Description: {description}"""


def resolve_listing(title, description="", client=None):
    """
    Deterministic normalization first; AI only if ambiguous and enabled.

    Returns the normalized listing plus provenance:
      source: "deterministic" | "deterministic+ai" | "deterministic(ai_unavailable)"
      ambiguity: list of reasons the AI would have been / was consulted
    """
    normalized = normalize_listing(title=title, description=description)
    reasons = ambiguity_reasons(normalized)

    result = dict(normalized)
    result["ambiguity"] = reasons

    if not reasons:
        result["source"] = "deterministic"
        return result

    client = client or LocalModelClient()

    if not client.enabled:
        result["source"] = "deterministic(ai_unavailable)"
        return result

    # Fail closed: one bad model reply must never abort a batch. Any
    # transport error, non-JSON content or non-object JSON falls back
    # to the unchanged deterministic result with explicit provenance.
    try:
        response = client.complete(
            IDENTIFY_PROMPT.format(title=title, description=description)
        )
        suggestion = json.loads(response.get("content", ""))
        if not isinstance(suggestion, dict):
            raise ValueError("AI reply is not a JSON object")
    except Exception:
        result["source"] = "deterministic(ai_error)"
        return result

    # Canonicalization guard: an AI value is accepted only if the
    # normalizer ITSELF extracts that same value from the suggestion.
    # This keeps AI output inside the deterministic vocabulary, so
    # invented spellings cannot create bogus comparable pools.
    filled = []
    for field in ("brand", "model"):
        proposed = suggestion.get(field)
        if normalized.get(field) is not None or not proposed:
            continue
        probe = normalize_listing(title=str(proposed))
        canonical = probe.get(field)
        if canonical is None:
            continue  # not in the deterministic vocabulary: reject
        result[field] = canonical
        filled.append(field)

    # Category: the normalizer returns "other" (never None) for
    # unrecognized listings, so "other" is the fillable state here.
    proposed_category = suggestion.get("category")
    if (
        normalized.get("category") == "other"
        and isinstance(proposed_category, str)
        and proposed_category.lower() in KNOWN_CATEGORIES
    ):
        result["category"] = proposed_category.lower()
        filled.append("category")

    for field in filled:
        result.setdefault("spec_sources", {})[field] = "ai"
        result.setdefault("spec_confidence", {})[field] = AI_FIELD_CONFIDENCE
    result["ai_fields"] = filled

    result["source"] = "deterministic+ai" if filled else "deterministic(ai_rejected)"
    return result


if __name__ == "__main__":
    samples = [
        ("MacBook Pro M4 16GB", "En perfecto estado"),
        ("Ordenador muy bueno", "Tiene mucha memoria y va rapido"),
        ("Portatil i5", ""),
    ]
    for title, description in samples:
        outcome = resolve_listing(title, description)
        print(f"\n{title}")
        print(f"  source: {outcome['source']}")
        print(f"  ambiguity: {outcome['ambiguity']}")
        print(f"  brand/model: {outcome.get('brand')} / {outcome.get('model')}")
