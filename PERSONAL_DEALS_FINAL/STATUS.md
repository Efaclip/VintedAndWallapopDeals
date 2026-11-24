# Build status - 2026-11-02 overnight build

Precise inventory of what is IMPLEMENTED AND TESTED vs what is a
SCAFFOLD/STUB still needing the user. Nothing below claims the roadmap
is finished.

## Implemented and tested (offline, sample/fixture data)

| Component | What works | Proof |
|---|---|---|
| config.py | central thresholds, DB path override, env-only credentials, Vinted flag OFF by default | imported by all suites |
| database.py (extended) | dedup by URL, price history, non-destructive upserts, European price parsing incl. single-dot thousands ("1.200" = 1200 - fixed this build) | negative suite, fixture suite |
| normalizer.py (extended) | original 37 self-tests + new coverage: MateBook, LG gram, Galaxy Book, Chromebook, Redmi/POCO/OnePlus/Moto, iPhone SE, Switch 2, Intel Arc; per-spec confidence | normalizer self-tests |
| normalize_database.py | persists spec_confidence alongside spec_sources/conflicts | pipeline suite |
| condition_detector.py | unchanged, 10/10 self-tests | condition self-tests |
| price_estimator.py (upgraded) | reason codes per listing, value_basis="asking", estimate confidence, per-run diagnostics table, schema migration from his original table preserving legacy rows, idempotent re-runs | pipeline + negative suites |
| deal_scorer.py (new) | quality score 0-100, stored components, idempotent; alert_priority() provably never mutates quality | pipeline + negative suites |
| alert_selector.py (new) | applies /min_discount /min_score /min_confidence /max_price /category /source, pause suppression, dedup via alerts_log, re-alert on price/score change | negative suite |
| telegram_bot.py core (new) | full command table parsed + persisted, /status /why /reset /help, owner gate: silence for strangers on every command, silence when no owner configured | negative suite (all command paths) |
| ai_assistant.py router (new) | deterministic-first, ambiguity detection, clean disabled behavior, AI fills gaps only | pipeline suite |
| scheduler.py planner (new) | incomplete-description refresh ordering, active rechecks, inactive throttling | pipeline suite |
| vinted_scraper.py gate (new) | every entry point raises PermissionError while disabled; mapping shape defined | manual run |
| wallapop_scraper.py | UNCHANGED; extract_description verified against fixtures (markers, short/empty pages); card price parsing verified | fixture suite |

## Scaffolds / stubs - NOT operational integrations

| Component | State | What is missing |
|---|---|---|
| Telegram live delivery | deliver() is REAL (Bot API sendMessage, owner-only, fail-closed without owner id, confirmed sends recorded before the next send, uncertain outcomes never blind-retried); run_pipeline --live sends. INBOUND POLLING implemented as process_updates() (getUpdates, owner-gated, offset persisted so restarts never re-process, replies via deliver()); NOT YET DAEMONIZED: no always-on loop or webhook runs it - he (or the scheduler) must invoke it on a schedule once credentials exist | user's bot token, his numeric Telegram user ID, an inbound polling loop or webhook for commands |
| Local AI | LocalModelClient exists; no endpoint configured, no model bundled, AI path untested with a real model | user's local endpoint (PERSONAL_DEALS_AI_ENDPOINT) |
| Vinted collection | adapter shape only; fetch_items() not implemented; disabled by design | user's confirmation collection is permitted + a working client; site access restrictions apply |
| Termux/phone execution | out of scope per user instruction ("last steps") | on-device wrapper, background execution |
| Real-data validation | all tests use sample/fixture data | his hardware_deals.db uploaded to Drive |
| Live Wallapop scraping | not attempted this build; access restrictions unchanged | he runs the scraper from his environment |
| Sale-value basis | value_basis field reserves "sale"; no sold-price source exists | a real sold-price data source |

## Code-review round (fixed and regression-tested)

- Overpriced asks (discount <= 0) score 0 and can never alert; the alert
  selector additionally requires discount_pct > 0, a config quality floor
  (MIN_ELIGIBLE_QUALITY, documented), a LIVE estimate join and a fresh
  last_seen (config.ACTIVE_LISTING_DAYS) before user filters even apply.
- Stale deal_scores rows are purged whenever the underlying estimate is
  no longer "estimated" (tested: condition flips -> estimate lost ->
  score gone -> no alert).
- Telegram input validation: /pause overflow/negative rejected, NaN and
  out-of-range numbers rejected and never stored, /category /source
  /prioritize values normalized to lowercase.
- AI assistant fails closed (timeout, non-JSON, non-object replies ->
  deterministic result unchanged with ai_error provenance); AI values
  accepted only when the normalizer's own vocabulary extracts them
  (canonicalization guard), marked ai provenance + low confidence (0.30);
  category fills only "other". The AI is now wired into
  normalize_database as an OPT-IN flag (use_ai=True); default stays
  deterministic-only and no endpoint means a strict no-op (both paths
  tested). No endpoint is configured, so the AI has never run live.
- Dry runs record NOTHING in alerts_log, so simulated alerts can never
  dedup-suppress a future live delivery.
- Freshness policy (explicit, configurable): alerts require a parseable
  last_seen within config.ACTIVE_LISTING_DAYS; NULL/empty/malformed
  freshness suppresses; legacy naive timestamps are assumed UTC (the
  source convention). paused_until gets the same parsing.
- Alerts carry the identified specs (brand/model/processor/gpu/RAM/
  storage/screen) and a standing note that availability, working
  condition and seller claims are NOT verified.
- Migration test rebuilt: tempdir, real populated schema, unprocessed
  legacy row asserted byte-preserved, processed row asserted upserted.
- run_pipeline.py added: the runnable end-to-end loop (minus scraping
  and minus live delivery, both deliberately out).

## Test inventory (all passing, logs in test_logs/)

- normalizer.py self-tests: 37 PASS / 0 FAIL (original suite preserved)
- condition_detector.py self-tests: 10/10
- tests/test_pipeline.py: 25/25 (end-to-end on sample DB)
- tests/test_negative.py: 33/33 (bad prices, malformed specs, self/duplicate
  comparables, spec conflicts, year/generic-CPU rules, condition exclusions,
  old-schema migration, non-destructive re-runs, idempotence, filters,
  pause, dedup, owner gate on every command)
- tests/test_review.py: 28/28 (all code-review fixes above)
- tests/test_scraper_fixtures.py: 10/10 (no browser, no network)

Run everything:

    python3 normalizer.py && python3 condition_detector.py
    HARDWARE_DEALS_DB=/tmp/pd1.db python3 tests/test_pipeline.py
    HARDWARE_DEALS_DB=/tmp/pd2.db python3 tests/test_negative.py
    python3 tests/test_scraper_fixtures.py
