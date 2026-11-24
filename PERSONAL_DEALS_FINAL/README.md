# PERSONAL_DEALS - Personal Hardware Deals Finder

Python pipeline that collects second-hand hardware listings, identifies the
hardware, estimates its market value from comparable listings, scores the
deals and prepares Telegram alerts.

## Pipeline order (run in this order, every session)

1. `wallapop_scraper.py` - collect listings (Playwright, interactive)
2. `normalize_database.py` - extract structured specs (brand, model, CPU, GPU, RAM, storage, screen)
3. `classify_conditions.py` - classify condition (new / like_new / used / for_parts / unknown)
4. `price_estimator.py` - estimate fair asking value from comparables
5. `deal_scorer.py` - score deal quality (0-100)

`scheduler.py` plans what each cycle should do; Telegram alerts come last.

## Architecture

| File | Stage | Role | State |
|---|---|---|---|
| wallapop_scraper.py | collect | Playwright scraper, search + description refresh | working (user-run) |
| database.py | store | SQLite, dedup by URL, price history | working |
| normalizer.py | identify | deterministic spec extraction + per-spec confidence | extended this build |
| normalize_database.py | identify | batch normalization into normalized_listings | working |
| condition_detector.py | condition | keyword rules, conservative conflict resolution | working |
| classify_conditions.py | condition | batch classification | working |
| price_estimator.py | estimate | median of >=3 comparable ASKS; value_basis + confidence + reason codes + run diagnostics | upgraded this build |
| deal_scorer.py | score | quality score separate from alert priority | new this build |
| telegram_bot.py | alerts | command parser, prefs, owner gate, alert format, /why with comparable traceability; live send needs your token | new this build (offline core) |
| alert_selector.py | alerts | applies stored filters, pause, dedup; never changes quality scores | new this build |
| ai_assistant.py | identify | ambiguity router; local model only for ambiguous listings | new this build (endpoint needed) |
| scheduler.py | automate | cycle planner: description refreshes, rechecks, inactive throttling | new this build |
| run_pipeline.py | automate | offline end-to-end runner: normalize -> classify -> estimate -> score -> select -> SIMULATED delivery (nothing sent) | new this build |
| vinted_scraper.py | ingest | maps Vinted catalog JSON to the shared schema; live fetch gated OFF (prior automated-activity block) | extended this build | new this build (gated) |
| config.py | all | central thresholds, paths, credentials from env | new this build |

## Key design rules

- Estimates are medians of ASKING prices (`value_basis = "asking"`), not
  confirmed sale prices. A sale-value basis needs a sold-price source.
- Deal QUALITY score never changes because of alert PRIORITY preferences.
  `/prioritize gpu` reorders notifications only.
- Telegram commands are answered only from the owner's numeric user ID.
- The local AI model only sees listings the deterministic pipeline flags
  as ambiguous. It fills gaps; it never overwrites deterministic results.
- Vinted collection stays off until you confirm it is permitted.

## Configuration (environment variables, never in code)

- `PERSONAL_DEALS_TELEGRAM_TOKEN` - your bot token (enables live sending)
- `PERSONAL_DEALS_TELEGRAM_OWNER_ID` - your numeric Telegram user ID
- `PERSONAL_DEALS_AI_ENDPOINT` - local model endpoint (Stage D)
- `PERSONAL_DEALS_VINTED_PERMITTED=1` - enables the Vinted adapter
- `HARDWARE_DEALS_DB` - database path override (used by the tests)

## Tests

    python3 normalizer.py                 # extraction self-tests
    python3 condition_detector.py         # condition self-tests
    python3 ai_assistant.py               # router demo
    python3 scheduler.py                  # planner demo
    python3 telegram_bot.py               # command demo
    HARDWARE_DEALS_DB=/tmp/pd_test.db python3 tests/test_pipeline.py
    HARDWARE_DEALS_DB=/tmp/pd_neg.db python3 tests/test_negative.py
    HARDWARE_DEALS_DB=/tmp/pd_rev.db python3 tests/test_review.py
    python3 tests/test_scraper_fixtures.py

End-to-end offline run against a real database COPY (never the original):

    cp hardware_deals.db /tmp/working_copy.db
    HARDWARE_DEALS_DB=/tmp/working_copy.db python3 run_pipeline.py

The pipeline test builds a sample database and checks 25 behaviours
end to end (estimation, reason codes, scoring, quality/priority
separation, scheduler, owner gate, alert format, AI routing).

## Still needs you

- Real `hardware_deals.db` to validate estimates on live data.
- Telegram bot token + your numeric user ID for live alerts.
- AI endpoint if you want Stage D active.
- Termux/phone execution wrapper (last step, per your roadmap).
- Vinted: your confirmation that collection is permitted.

## Local AI assist (Stage D, opt-in)

Deterministic rules always run first. The local AI only re-examines
listings the rules left ambiguous, and only when you explicitly enable it:

    python3 normalize_database.py --ai
    HARDWARE_DEALS_AI=1 python3 normalize_database.py
    PERSONAL_DEALS_AI_ENDPOINT=http://localhost:11434 ... python3 normalize_database.py --ai

- Fails closed: without a reachable endpoint nothing changes.
- Never overwrites a deterministic result.
- Every AI-filled field is marked `spec_sources.<field> = "ai"` and
  carries a low confidence (0.30). Listings whose brand or model came
  from the AI are excluded from strict comparable pools in the price
  estimator, so a wrong AI guess can never pollute a median. They can
  still receive an estimate from a deterministic pool.


## Vinted (Stage G)

The adapter parses and maps Vinted catalog JSON into the shared schema
and can ingest JSON you supply (save_catalog). LIVE FETCHING IS NOT
IMPLEMENTED: Vinted previously blocked automated activity on this
account, so fetch_items stays gated (PERSONAL_DEALS_VINTED_PERMITTED=1)
and raises NotImplementedError until a permitted client is agreed.

## Live delivery safety (Telegram)

- `deliver()` fails closed: no token or no owner id = no network call.
- Every confirmed send is recorded in alerts_log BEFORE the next send.
- A send whose outcome cannot be proven (transport failure, HTTP 429/5xx,
  malformed success body) is quarantined in `delivery_quarantine`: no
  scheduled run will retry it automatically. After checking Telegram,
  clear it with `alert_selector.clear_quarantine(connection)` (or pass a
  listing id). Confirmed API refusals (HTTP 4xx) are reported and safe
  to retry on the next run.
- Rate limit: a 429 stops the batch and honors Retry-After.
- Honest limits: there is an unavoidable crash window between Telegram
  confirming a send and the local record commit - Telegram offers no
  idempotency key. A crash inside that window can duplicate one alert.
  The same applies to quarantine: a crash between an uncertain send and
  the quarantine write leaves no trace, so the next run can resend an
  alert that actually arrived.
- Rate-limit (429) recovery: the deal is quarantined because a 429 does
  not prove non-delivery. Telegram's retry_after only tells you when to
  ask again, not what happened. Once the rate-limit window has passed,
  release the deal with alert_selector.clear_quarantine(connection).
  WARNING: a quarantined deal has no confirmed alert record, so dedup
  CANNOT protect it - if the original attempt actually reached
  Telegram, clearing the quarantine will send that alert again. Check
  your Telegram chat first and only clear deals you did not receive.
