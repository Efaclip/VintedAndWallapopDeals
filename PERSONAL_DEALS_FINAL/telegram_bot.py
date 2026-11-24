"""
Telegram control panel (Stage E) - offline-testable core.

Everything here works WITHOUT a token or network: command parsing,
preference storage, alert formatting and the owner gate. The only piece
that needs credentials is deliver(), which stays a stub until the user
supplies a bot token via environment (never in code).

Owner gate: handle_message ignores every sender whose numeric Telegram
user ID is not config.TELEGRAM_OWNER_ID. Commands are private by design.
"""

import copy
import json
import urllib.error
import math
import sqlite3

import config

DB_FILE = config.DB_PATH

DEFAULT_PREFS = {
    "min_discount": None,     # percent
    "min_score": None,        # 0-100 quality score
    "min_confidence": None,   # 0.0-1.0
    "max_price": None,        # euros
    "categories": None,       # list of categories or None = all
    "sources": None,          # list of marketplaces or None = all
    "paused_until": None,     # ISO timestamp
    "digest": None,           # e.g. "daily"
    "watches": [],            # product watch terms
    "priority_order": [],     # most-prioritized categories first
    "update_offset": None,    # last processed getUpdates id + 1
}

HELP_TEXT = """Commands:
/min_discount 30 - minimum discount vs estimate (%)
/min_score 80 - minimum deal quality score
/min_confidence 0.85 - minimum estimate confidence
/max_price 300 - maximum asking price
/category gpu,laptop - choose categories
/source wallapop,vinted - choose marketplaces
/status - system status
/why ID - explain a deal
/pause 2h - pause alerts
/digest daily - daily summary
/watch RTX 4060 Ti - watch a product
/prioritize gpu - change alert priority (quality scores never change)
/reset - reset preferences
/help - show this help"""


class UnknownCommand(Exception):
    pass


def parse_command(text):
    """Split '/command arg arg' into (command, argument-string)."""
    text = (text or "").strip()
    if not text.startswith("/"):
        raise UnknownCommand(f"Not a command: {text!r}")
    parts = text.split(None, 1)
    command = parts[0].lstrip("/").lower().lstrip()
    argument = parts[1].strip() if len(parts) > 1 else ""
    return command, argument


MAX_PAUSE_HOURS = 24 * 30  # one month


def _parse_duration_hours(argument):
    """'2h' -> 2.0, '90m' -> 1.5, '1d' -> 24.0

    Raises ValueError for anything non-numeric, non-finite, non-positive
    or longer than MAX_PAUSE_HOURS."""
    argument = argument.strip().lower()
    unit = argument[-1] if argument else "h"
    try:
        number = float(argument[:-1] if unit in "hmd" else argument)
    except ValueError:
        raise ValueError(f"not a duration: {argument!r}")
    if not math.isfinite(number):
        raise ValueError(f"not a finite duration: {argument!r}")
    if unit == "m":
        hours = number / 60
    elif unit == "d":
        hours = number * 24
    else:
        hours = number
    if hours <= 0 or hours > MAX_PAUSE_HOURS:
        raise ValueError(f"duration out of range: {argument!r}")
    return hours


class PrefsStore:
    """User preferences persisted in SQLite."""

    def __init__(self, connection, autocommit=True):
        self.connection = connection
        connection.execute("""
            CREATE TABLE IF NOT EXISTS user_prefs (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        self._autocommit = autocommit

    def _commit(self):
        if self._autocommit:
            self.connection.commit()

    def get_all(self):
        # Deep copy: DEFAULT_PREFS contains mutable lists (watches,
        # priority_order). A shallow dict() lets /watch append into the
        # module default, polluting /reset and every fresh database.
        prefs = copy.deepcopy(DEFAULT_PREFS)
        for key, value in self.connection.execute(
            "SELECT key, value FROM user_prefs"
        ).fetchall():
            prefs[key] = json.loads(value)
        return prefs

    def set(self, key, value):
        if key not in DEFAULT_PREFS:
            raise UnknownCommand(f"Unknown preference: {key}")
        self.connection.execute(
            "INSERT INTO user_prefs (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
        self._commit()

    def reset(self):
        self.connection.execute("DELETE FROM user_prefs")
        self._commit()


def format_alert(deal):
    """Alert text with every field the roadmap requires."""
    lines = [
        f"[{deal.get('source', 'unknown')}] {deal.get('title', 'Unknown item')}",
        f"Specs: {deal.get('specs', 'not identified')}",
        f"Asking price: {deal.get('price')} EUR",
        f"Estimated market value: {deal.get('fair_value')} EUR "
        f"({deal.get('value_basis', config.VALUE_BASIS_ASKING)} basis)",
        f"Discount: {deal.get('discount_pct')}%",
        f"Deal score: {deal.get('quality_score')}",
        f"Confidence: {deal.get('estimate_confidence')}",
        f"Link: {deal.get('url')}",
        f"Why flagged: {deal.get('reason', 'meets your filters')}",
        "Note: data is from the last scrape; availability, working "
        "condition and seller claims are NOT verified.",
    ]
    return "\n".join(lines)


def _status_report(connection):
    def count(query):
        return connection.execute(query).fetchone()[0]

    estimates_query = (
        "SELECT COUNT(*) FROM price_estimates WHERE status='estimated'"
    )
    return "\n".join([
        "System status:",
        f"Listings: {count('SELECT COUNT(*) FROM listings')}",
        f"Estimates: {count(estimates_query)}",
        f"Scored deals: {count('SELECT COUNT(*) FROM deal_scores')}",
    ])


def _comparable_lines(connection, listing_id):
    """List the comparables behind an estimate, for traceability."""
    import price_estimator
    try:
        listings = price_estimator.load_listings(connection)
    except Exception:
        return ["Comparables unavailable (run normalize/classify first)."]
    target = next((l for l in listings if l["id"] == listing_id), None)
    if target is None:
        return []
    _prices, matches = price_estimator.find_comparables(target, listings)
    if not matches:
        return ["No comparable listings matched."]
    lines = ["Comparable listings used:"]
    for match in matches:
        lines.append(
            f"  #{match['id']} {match['title']} - {match['price']} EUR"
        )
    return lines


def _why_report(connection, listing_id):
    row = connection.execute("""
        SELECT l.title, l.price_amount, e.status, e.fair_value,
               e.comparable_count, e.lowest_comparable,
               e.highest_comparable, e.reason, e.estimate_confidence,
               s.quality_score, s.discount_pct
        FROM listings l
        LEFT JOIN price_estimates e ON e.listing_id = l.id
        LEFT JOIN deal_scores s ON s.listing_id = l.id
        WHERE l.id = ?
    """, (listing_id,)).fetchone()

    if row is None:
        return f"No listing with ID {listing_id}."

    (title, price, status, fair_value, comparable_count,
     lowest, highest, reason, confidence,
     quality_score, discount) = row

    lines = [f"Listing {listing_id}: {title}", f"Asking price: {price} EUR"]
    if status is None:
        lines.append("Not estimated yet (run normalize, classify, estimate).")
    else:
        lines += [
            f"Estimate status: {status}",
            f"Fair value: {fair_value} EUR (asking basis)",
            f"Comparables: {comparable_count} "
            f"(range {lowest} - {highest})",
            f"Confidence: {confidence}",
            f"Explanation: {reason}",
        ]
    if quality_score is not None:
        lines += [f"Quality score: {quality_score}", f"Discount: {discount}%"]
    if status == "estimated":
        lines += _comparable_lines(connection, listing_id)
    return "\n".join(lines)


def handle_message(sender_id, text, connection, owner_id=None, commit=True):
    """
    Entry point for every incoming Telegram message.

    Returns the reply text, or None when the sender is not the owner:
    unknown senders are ignored entirely. With commit=False the caller
    owns the transaction (process_updates commits the command effect
    together with the offset advance, atomically).
    """
    owner_id = owner_id if owner_id is not None else config.TELEGRAM_OWNER_ID

    if owner_id is None:
        return None  # no owner configured: the bot answers nobody
    if sender_id != owner_id:
        return None

    store = PrefsStore(connection, autocommit=commit)

    try:
        command, argument = parse_command(text)
    except UnknownCommand:
        return "Send /help to see the commands."

    if command == "help":
        return HELP_TEXT

    if command == "reset":
        store.reset()
        return "Preferences reset."

    if command == "status":
        return _status_report(connection)

    if command == "why":
        if not argument.isdigit():
            return "Usage: /why ID"
        return _why_report(connection, int(argument))

    if command == "pause":
        try:
            hours = _parse_duration_hours(argument or "1h")
        except (ValueError, OverflowError):
            return "Usage: /pause 2h (or 90m, 1d), up to 30 days"
        from datetime import datetime, timedelta, timezone
        until = datetime.now(timezone.utc) + timedelta(hours=hours)
        store.set("paused_until", until.isoformat(timespec="seconds"))
        return f"Alerts paused for {argument or '1h'}."

    if command == "digest":
        store.set("digest", argument or "daily")
        return f"Digest set to {argument or 'daily'}."

    if command == "watch":
        if not argument:
            return "Usage: /watch RTX 4060 Ti"
        prefs = store.get_all()
        watches = prefs["watches"]
        if argument not in watches:
            watches.append(argument)
        store.set("watches", watches)
        return f"Watching: {argument}"

    if command == "prioritize":
        if not argument:
            return "Usage: /prioritize gpu"
        prefs = store.get_all()
        order = [
            item.strip().lower()
            for item in argument.split(",")
            if item.strip()
        ]
        store.set("priority_order", order)
        return (
            f"Alert priority set to: {', '.join(order)}. "
            "Quality scores are unchanged."
        )

    RANGES = {
        "min_discount": (0.0, 100.0),
        "min_score": (0.0, 100.0),
        "min_confidence": (0.0, 1.0),
        "max_price": (0.0, 1_000_000.0),
    }

    if command in ("min_discount", "min_score", "min_confidence", "max_price"):
        try:
            value = float(argument)
        except ValueError:
            return f"Usage: /{command} NUMBER"
        low, high = RANGES[command]
        if not math.isfinite(value) or not (low <= value <= high):
            return f"{command} must be between {low:g} and {high:g}."
        store.set(command, value)
        return f"{command} set to {value:g}."

    if command in ("category", "source"):
        key = "categories" if command == "category" else "sources"
        # Stored lowercase: the selector compares lowercase values.
        values = [
            item.strip().lower()
            for item in argument.split(",")
            if item.strip()
        ]
        store.set(key, values or None)
        return f"{key} set to: {', '.join(values) if values else 'all'}."

    return "Unknown command. Send /help to see the commands."


class DeliveryUncertain(RuntimeError):
    """The send outcome cannot be proven (transport failure, rate limit,
    server error, malformed success body). Callers must NOT retry
    blindly: quarantine the deal and leave it for a human decision."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def _post(url, payload, timeout):
    """One HTTPS POST to the Telegram Bot API. Isolated so tests can
    substitute it; the only network call in this module lives here."""
    import json as _json
    import urllib.request
    request = urllib.request.Request(
        url,
        data=_json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return _json.loads(response.read().decode("utf-8"))


def deliver(chat_id, text, token=None):
    """
    Live delivery via the Telegram Bot API sendMessage endpoint.

    Credentials come only from PERSONAL_DEALS_TELEGRAM_TOKEN (or the
    token argument); nothing is hardcoded. Delivery is owner-only: when
    PERSONAL_DEALS_TELEGRAM_OWNER_ID is configured, any other chat_id is
    refused before any network call.

    Returns the Telegram message_id on success. Raises RuntimeError on
    missing credentials, owner mismatch, or an API-level failure, so a
    failed send can never be mistaken for a delivered alert (the caller
    records alerts_log only after this returns).
    """
    token = token or config.TELEGRAM_BOT_TOKEN
    if not token:
        raise RuntimeError(
            "Telegram token not configured; set PERSONAL_DEALS_TELEGRAM_TOKEN"
        )

    owner = config.TELEGRAM_OWNER_ID
    if owner is None:
        # Fail closed: without the owner's numeric id there is no safe
        # recipient, so nothing may be sent.
        raise RuntimeError(
            "Telegram owner id not configured; set "
            "PERSONAL_DEALS_TELEGRAM_OWNER_ID before any delivery."
        )
    if str(chat_id) != str(owner):
        raise RuntimeError(
            f"Refusing to deliver to chat {chat_id}: alerts go only to "
            "the configured owner."
        )

    try:
        reply = _post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            {
                "chat_id": int(chat_id),
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=config.TELEGRAM_TIMEOUT_SECONDS,
        )
    except DeliveryUncertain:
        raise
    except urllib.error.HTTPError as exc:
        if exc.code == 429 or exc.code >= 500:
            # Rate-limited or server-side failure: the request was very
            # likely NOT processed, but that is not provable - treat as
            # uncertain and honor Retry-After instead of repeating.
            retry_after = None
            try:
                retry_after = int(exc.headers.get("Retry-After", ""))
            except (TypeError, ValueError):
                retry_after = None
            raise DeliveryUncertain(
                f"Telegram HTTP {exc.code} (rate limit/server error); "
                "outcome not provably failed",
                retry_after=retry_after,
            ) from exc
        raise RuntimeError(
            f"Telegram API HTTP {exc.code}: confirmed refusal"
        ) from exc
    except Exception as exc:
        # Transport-level failure (timeout, reset, DNS): the API may or
        # may not have accepted the message. Never blind-retry this.
        raise DeliveryUncertain(
            f"Send outcome unknown (transport error): {exc}"
        ) from exc
    if not isinstance(reply, dict) or not reply.get("ok"):
        # The API answered and refused: a confirmed failure, safe to
        # report and retry later.
        raise RuntimeError(f"Telegram API rejected the send: {reply}")
    try:
        return reply["result"]["message_id"]
    except (KeyError, TypeError) as exc:
        # 200 OK with a malformed body: the message may well have been
        # accepted. NOT a confirmed failure.
        raise DeliveryUncertain(
            f"Malformed success body; outcome unknown: {reply!r:.200}"
        ) from exc


def get_updates(token, offset=None, timeout=None):
    """One getUpdates call (long-poll). Returns the raw result list.
    Same credentials rule as deliver(); offset acks processed updates."""
    timeout = timeout or config.TELEGRAM_TIMEOUT_SECONDS
    payload = {"timeout": 0, "allowed_updates": ["message"]}
    if offset is not None:
        payload["offset"] = offset
    reply = _post(
        f"https://api.telegram.org/bot{token}/getUpdates",
        payload,
        timeout=timeout,
    )
    if not reply.get("ok"):
        raise RuntimeError(f"Telegram getUpdates failed: {reply}")
    return reply["result"]


def process_updates(connection, token=None, owner_id=None):
    """Fetch pending commands once and answer them.

    Owner gate: messages from anyone else are ignored (silence), and
    without a configured owner id nothing is processed at all. Each
    command's effect commits atomically with its offset advance, so a
    committed command is applied exactly once and a restart never
    re-applies it; a crash before the commit replays it harmlessly
    (effects are idempotent upserts). Replies are best-effort: they
    are NOT part of that transaction, so a crash after the commit can
    lose a reply (never replay the command). Replies go through
    deliver(), inheriting its fail-closed and failure classification.

    Returns {"processed": n, "ignored": n, "replies": n}.
    """
    token = token or config.TELEGRAM_BOT_TOKEN
    if not token:
        raise RuntimeError(
            "Telegram token not configured; set PERSONAL_DEALS_TELEGRAM_TOKEN"
        )
    owner = owner_id if owner_id is not None else config.TELEGRAM_OWNER_ID
    if owner is None:
        raise RuntimeError(
            "Telegram owner id not configured; set "
            "PERSONAL_DEALS_TELEGRAM_OWNER_ID before polling commands."
        )

    store = PrefsStore(connection)
    store_noauto = PrefsStore(connection, autocommit=False)
    offset = store.get_all().get("update_offset")
    updates = get_updates(token, offset=offset)

    processed = ignored = replies = 0
    reply_failures = []
    for update in updates:
        update_id = update.get("update_id")
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        sender = (message.get("from") or {}).get("id")
        text = (message.get("text") or "").strip()

        def ack():
            # Persist the offset per update, only AFTER its handling
            # completed: a restart never re-processes an acked command.
            if update_id is not None:
                store.set("update_offset", update_id + 1)

        chat_id = chat.get("id")
        # Private chat with the owner ONLY: in a private chat the chat
        # id equals the user id. Anything else (groups, channels,
        # strangers) is ignored and acked so it can never poison the
        # offset - a command from a group must neither apply nor block
        # later commands.
        if chat_id != owner or sender != owner:
            ignored += 1
            ack()
            continue
        if not text.startswith("/"):
            ignored += 1
            ack()
            continue
        # Command effect and offset advance commit TOGETHER: a crash or
        # commit failure rolls both back, so the update is simply
        # reprocessed on the next poll (effects are idempotent upserts).
        # A crash after the commit but before the reply means the
        # command applied with no reply sent - never a double-apply.
        try:
            reply = handle_message(sender, text, connection,
                                   owner_id=owner, commit=False)
            if reply is None:
                ignored += 1
                ack()
                continue
            if update_id is not None:
                store_noauto.set("update_offset", update_id + 1)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        processed += 1
        # The command effect + offset are committed; the reply is a
        # notification. A failed reply must neither replay the command
        # nor block later updates - it is reported, not retried.
        try:
            deliver(chat_id, reply, token=token)
            replies += 1
        except DeliveryUncertain as exc:
            reply_failures.append((update_id, f"uncertain: {exc}"))
        except RuntimeError as exc:
            reply_failures.append((update_id, f"failed: {exc}"))
        ack()
    return {"processed": processed, "ignored": ignored,
            "replies": replies, "reply_failures": reply_failures}


if __name__ == "__main__":
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    # Owner gate demo
    print("stranger:", handle_message(111, "/status", connection, owner_id=42))
    print("owner:", handle_message(42, "/help", connection, owner_id=42)[:60], "...")
    print(handle_message(42, "/min_score 80", connection, owner_id=42))
    print(handle_message(42, "/watch RTX 4060 Ti", connection, owner_id=42))
    print(handle_message(42, "/prioritize gpu,laptop", connection, owner_id=42))
