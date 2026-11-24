"""
Offline end-to-end pipeline runner.

Wires the whole post-scrape chain in one command:

    normalize -> classify -> estimate -> score -> select -> deliver

DELIVERY IS SIMULATED BY DEFAULT: alerts are formatted and printed,
never sent, and nothing is recorded in alerts_log. Live sending is
implemented (telegram_bot.deliver via the Bot API) and only runs with
the explicit --live flag plus configured credentials
(PERSONAL_DEALS_TELEGRAM_TOKEN and PERSONAL_DEALS_TELEGRAM_OWNER_ID);
alerts go only to the owner's chat and are recorded in alerts_log only
after a confirmed send.

The scrape step is NOT part of this runner: collection stays a separate
user-run step (wallapop_scraper.py), so this runner never touches the
network except live Telegram delivery under --live.

Usage:
    python3 run_pipeline.py                 # dry run, config.DB_PATH
    HARDWARE_DEALS_DB=/path.db python3 run_pipeline.py
    python3 run_pipeline.py --live          # real sends (credentials needed)
"""

import sqlite3
import sys

import config
import normalize_database
import classify_conditions
import price_estimator
import deal_scorer
import alert_selector
import telegram_bot


def describe_specs(deal):
    """Human-readable specs from the normalized fields.

    Only fields actually extracted are shown; nothing is guessed.
    """
    parts = []
    identity = " ".join(
        p for p in (deal.get("brand"), deal.get("model")) if p
    )
    if identity:
        parts.append(identity)
    processor = str(deal["processor"]) if deal.get("processor") else None
    gpu = str(deal["gpu"]) if deal.get("gpu") else None
    if processor:
        parts.append(processor)
    # Skip the GPU field when it restates the identity or processor
    # (GPU listings often put the chip name in model, processor and gpu).
    identity_l = identity.lower()
    if gpu and gpu.lower() not in identity_l and (
        not processor or gpu.lower() not in processor.lower()
    ):
        parts.append(f"GPU {gpu}")
    if deal.get("ram_gb"):
        # On GPU listings the extracted GB is the card's memory, not RAM.
        label = "VRAM" if deal.get("category") == "gpu" else "RAM"
        parts.append(f"{deal['ram_gb']}GB {label}")
    if deal.get("storage_gb"):
        parts.append(f"{deal['storage_gb']}GB storage")
    if deal.get("screen_inches"):
        parts.append(f"{deal['screen_inches']}\"")
    if deal.get("edition"):
        parts.append(str(deal["edition"]))
    return ", ".join(parts) if parts else "not identified"


def run(connection, use_ai=None, live=False):
    if use_ai is None:
        use_ai = normalize_database.ai_opt_in_enabled()
    print("=== STEP 1/5: normalize ===")
    normalize_database.main(use_ai=use_ai)

    print("\n=== STEP 2/5: classify conditions ===")
    classify_conditions.classify_database()

    print("\n=== STEP 3/5: estimate prices ===")
    price_estimator.run_estimator()

    print("\n=== STEP 4/5: score deals ===")
    deal_scorer.score_database()

    print("\n=== STEP 5/5: select + simulated delivery ===")
    prefs = telegram_bot.PrefsStore(connection).get_all()
    selected, suppressed = alert_selector.select_alerts(connection, prefs)

    print(f"Selected for alert: {len(selected)}")
    print(f"Suppressed: {len(suppressed)}")

    delivered = []
    send_failures = []
    send_uncertain = []
    for deal in selected:
        specs = describe_specs(deal)
        alert = telegram_bot.format_alert({
            "source": deal.get("source"),
            "title": deal.get("title"),
            "specs": specs,
            "price": deal.get("price"),
            "fair_value": deal.get("fair_value"),
            "discount_pct": deal.get("discount_pct"),
            "quality_score": deal.get("quality_score"),
            "estimate_confidence": deal.get("estimate_confidence"),
            "url": deal.get("url"),
            "reason": f"passes your filters (priority {deal.get('priority')})",
        })
        if live:
            try:
                message_id = telegram_bot.deliver(
                    config.TELEGRAM_OWNER_ID, alert
                )
            except telegram_bot.DeliveryUncertain as exc:
                # Outcome unknown: do NOT retry in this run, do NOT
                # record as sent - and quarantine durably so the NEXT
                # scheduled run cannot silently retry either. Released
                # only by a person after checking Telegram.
                alert_selector.quarantine_delivery(connection, deal, str(exc))
                send_uncertain.append((deal["id"], str(exc)))
                if exc.retry_after is not None:
                    # Rate-limited: honor the backoff and stop the batch
                    # rather than hammering the API with the rest.
                    remaining = len(selected) - len(delivered) - len(send_failures) - len(send_uncertain)
                    print(f"Rate-limited (Retry-After {exc.retry_after}s): "
                          f"stopping batch, {remaining} alert(s) not attempted.")
                    break
                print(f"\n--- ALERT OUTCOME UNCERTAIN (listing "
                      f"{deal['id']}, NOT retried) ---")
                print(alert)
                continue
            except RuntimeError as exc:
                # Confirmed refusal/failure: recorded as failed, the rest
                # of the batch continues.
                send_failures.append((deal["id"], str(exc)))
                print(f"\n--- ALERT FAILED (listing {deal['id']}: "
                      f"{exc}) ---")
                print(alert)
                continue
            # Durable BEFORE the next send: a crash after this line can
            # never re-send a confirmed alert.
            alert_selector.record_alerts(connection, [deal])
            delivered.append({**deal, "message_id": message_id})
            print(f"\n--- ALERT SENT (message {message_id}) ---")
        else:
            print("\n--- ALERT (simulated, NOT sent) ---")
        print(alert)

    if live:
        print(f"\nLIVE RUN COMPLETE: {len(delivered)} sent+recorded, "
              f"{len(send_failures)} failed, "
              f"{len(send_uncertain)} uncertain (not retried).")
    else:
        # Dry runs record NOTHING in alerts_log: a simulated alert must
        # never dedup-suppress a later real delivery.
        print("\nDry run: alerts are NOT recorded in alerts_log, so "
              "future live delivery is never deduped against this "
              "simulation.")
        print("DRY RUN COMPLETE: nothing was sent (use --live with "
              "credentials for real delivery).")

    return {"selected": len(selected), "suppressed": len(suppressed),
            "delivered": len(delivered),
            "send_failures": send_failures,
            "send_uncertain": send_uncertain}


def main():
    if not config.DB_PATH.exists():
        print(f"Database not found: {config.DB_PATH}", file=sys.stderr)
        print("Run wallapop_scraper.py first, or point HARDWARE_DEALS_DB "
              "at an existing database.", file=sys.stderr)
        sys.exit(1)

    live = "--live" in sys.argv[1:]
    connection = sqlite3.connect(config.DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        run(connection, live=live)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
