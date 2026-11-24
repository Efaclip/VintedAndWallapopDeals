"""Telegram live-delivery transport: payload shape, owner-only gate,
credential handling, API failure surfacing. _post is stubbed; no network."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("PERSONAL_DEALS_TELEGRAM_OWNER_ID", "123456")
import importlib
import config
importlib.reload(config)
import telegram_bot
importlib.reload(telegram_bot)

def rebind_db(path):
    """Modules freeze config.DB_PATH at import; rebind after env change."""
    import database, normalize_database, classify_conditions
    import price_estimator, deal_scorer, alert_selector, telegram_bot
    from pathlib import Path
    for mod in (database, alert_selector, classify_conditions,
                deal_scorer, price_estimator, telegram_bot):
        mod.DB_FILE = Path(str(path))
    normalize_database.DB_PATH = Path(str(path))

RESULTS = []
def check(name, ok):
    RESULTS.append((name, bool(ok)))
    print(("PASS" if ok else "FAIL"), name)

sent = []
def fake_post(url, payload, timeout):
    sent.append((url, payload, timeout))
    return {"ok": True, "result": {"message_id": 777}}
telegram_bot._post = fake_post

mid = telegram_bot.deliver(123456, "alert text", token="tok123")
url, payload, timeout = sent[-1]
check("returns message_id", mid == 777)
check("url carries token", url == "https://api.telegram.org/bot123/sendMessage".replace("bot123", "bottok123"))
check("payload shape", payload == {"chat_id": 123456, "text": "alert text",
                                   "disable_web_page_preview": True})
check("timeout passed", timeout == config.TELEGRAM_TIMEOUT_SECONDS)

# owner gate: non-owner refused BEFORE any network call
try:
    telegram_bot.deliver(999, "x", token="tok123")
    check("non-owner refused", False)
except RuntimeError as e:
    check("non-owner refused", "owner" in str(e).lower())
check("no network call on refusal", len(sent) == 1)

# missing token
try:
    telegram_bot.deliver(123456, "x", token=None)
    check("missing token raises", False)
except RuntimeError as e:
    check("missing token raises", "PERSONAL_DEALS_TELEGRAM_TOKEN" in str(e))

# API-level failure surfaces, never silent
def failing_post(url, payload, timeout):
    return {"ok": False, "description": "chat not found"}
telegram_bot._post = failing_post
try:
    telegram_bot.deliver(123456, "x", token="tok123")
    check("api failure raises", False)
except RuntimeError as e:
    check("api failure raises", "chat not found" in str(e))

# owner unset: fail closed, nothing may be sent
config.TELEGRAM_OWNER_ID = None
importlib.reload(telegram_bot)
telegram_bot._post = fake_post
calls_before = len(sent)
try:
    telegram_bot.deliver(555, "x", token="tok123")
    check("owner unset fails closed", False)
except RuntimeError as e:
    check("owner unset fails closed", "OWNER_ID" in str(e))
check("no network call when failing closed", len(sent) == calls_before)
config.TELEGRAM_OWNER_ID = 123456
importlib.reload(telegram_bot)

# transport error -> DeliveryUncertain, never a blind retry
def flaky_post(url, payload, timeout):
    raise ConnectionError("timed out")
telegram_bot._post = flaky_post
try:
    telegram_bot.deliver(123456, "x", token="tok123")
    check("transport error -> DeliveryUncertain", False)
except telegram_bot.DeliveryUncertain:
    check("transport error -> DeliveryUncertain", True)
except RuntimeError:
    check("transport error -> DeliveryUncertain", False)

failed = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} telegram delivery checks passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
print("ALL TELEGRAM DELIVERY CHECKS PASSED")

# --- Live pipeline mode: send then record, only after confirmed send ---
import sqlite3, tempfile
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "live.db")
    os.environ["HARDWARE_DEALS_DB"] = db
    os.environ["PERSONAL_DEALS_TELEGRAM_OWNER_ID"] = "123456"
    os.environ["PERSONAL_DEALS_TELEGRAM_TOKEN"] = "tok123"
    importlib.reload(config)
    import database
    rebind_db(config.DB_PATH)
    database.initialize_database()
    conn = sqlite3.connect(db)
    # 4 identical deterministic listings; one priced below the median.
    for i, price in enumerate([500.0, 520.0, 540.0, 380.0]):
        conn.execute(
            "INSERT INTO listings (source,title,description,price_amount,url,"
            "first_seen,last_seen) VALUES ('wallapop',?,?,?,?,datetime('now'),"
            "datetime('now'))",
            ("KFA2 GeForce RTX 4070 SUPER 12GB",
             "Tarjeta usada en buen estado", price,
             f"http://x/{i}"))
    conn.commit(); conn.close()

    importlib.reload(telegram_bot)
    sends = []
    telegram_bot._post = lambda url, payload, timeout: (
        sends.append(payload) or {"ok": True, "result": {"message_id": 900 + len(sends)}})
    import run_pipeline
    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
    out = run_pipeline.run(conn, use_ai=False, live=True)
    # Both below-median listings are eligible (380 deeply, 500 slightly).
    check("live run delivered the eligible alerts",
          out["delivered"] == 2 and len(sends) == 2)
    check("live alerts went to the owner chat",
          all(s["chat_id"] == 123456 for s in sends))
    logged = conn.execute(
        "SELECT listing_id, price_amount FROM alerts_log ORDER BY price_amount").fetchall()
    check("alerts_log records exactly the delivered alerts",
          len(logged) == 2 and [r[1] for r in logged] == [380.0, 500.0])
    # Second live run: same deals, same score+price -> deduped, no resend.
    out2 = run_pipeline.run(conn, use_ai=False, live=True)
    check("unchanged deals not resent", out2["delivered"] == 0 and len(sends) == 2)

    # Mid-batch failure: deal order is by priority/score, so force one
    # send to fail and one to be uncertain; the rest must still land and
    # every confirmed send must be recorded BEFORE the next send.
    conn.execute("UPDATE listings SET price_amount=360.0 WHERE url='http://x/0'")
    conn.execute("UPDATE listings SET price_amount=500.0 WHERE url='http://x/1'")
    conn.commit()
    state = {"n": 0}
    def flaky_batch(url, payload, timeout):
        state["n"] += 1
        if state["n"] == 1:
            return {"ok": True, "result": {"message_id": 1000 + state["n"]}}
        if state["n"] == 2:
            raise ConnectionError("socket reset")
    telegram_bot._post = flaky_batch
    out3 = run_pipeline.run(conn, use_ai=False, live=True)
    check("batch: confirmed send recorded despite later failures",
          out3["delivered"] == 1)
    check("batch: uncertain outcome not retried, not recorded",
          len(out3["send_uncertain"]) == 1 and len(out3["send_failures"]) == 0)
    # confirmed-rejection path: fresh deal state, all sends rejected
    import alert_selector as _as
    _as.clear_quarantine(conn)
    conn.execute("UPDATE listings SET price_amount=370.0 WHERE url='http://x/0'")
    conn.commit()
    telegram_bot._post = lambda url, payload, timeout: {
        "ok": False, "description": "confirmed rejection"}
    out3b = run_pipeline.run(conn, use_ai=False, live=True)
    check("batch: confirmed failure classified",
          len(out3b["send_failures"]) >= 1 and out3b["delivered"] == 0)
    logged = conn.execute(
        "SELECT price_amount FROM alerts_log ORDER BY price_amount").fetchall()
    check("batch: log has old 2 + newly confirmed 1",
          len(logged) == 3)
    # Restart after the partial batch: the confirmed send must dedup.
    telegram_bot._post = lambda url, payload, timeout: {
        "ok": True, "result": {"message_id": 2000}}
    out4 = run_pipeline.run(conn, use_ai=False, live=True)
    rows = conn.execute(
        "SELECT listing_id, quality_score, price_amount FROM alerts_log"
    ).fetchall()
    check("restart: no (listing, score, price) ever re-sent",
          len(rows) == len(set(tuple(r) for r in rows)) and out4["delivered"] >= 1)
    conn.close()

print(f"\n{sum(1 for _, ok in RESULTS if ok)}/{len(RESULTS)} total delivery checks passed")
if not all(ok for _, ok in RESULTS):
    sys.exit(1)
print("ALL DELIVERY + LIVE-MODE CHECKS PASSED")

# --- Inbound polling: owner gate, replies, offset persistence ---
import sqlite3, tempfile
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "poll.db")
    os.environ["HARDWARE_DEALS_DB"] = db
    os.environ["PERSONAL_DEALS_TELEGRAM_OWNER_ID"] = "123456"
    os.environ["PERSONAL_DEALS_TELEGRAM_TOKEN"] = "tok123"
    importlib.reload(config)
    import database
    rebind_db(config.DB_PATH)
    database.initialize_database()
    importlib.reload(telegram_bot)

    updates = {"result": [
        {"update_id": 50, "message": {"message_id": 1,
            "chat": {"id": 999}, "from": {"id": 999},
            "text": "/min_discount 99"}},          # stranger: ignored
        {"update_id": 51, "message": {"message_id": 2,
            "chat": {"id": 123456}, "from": {"id": 123456},
            "text": "/min_discount 30"}},          # owner: processed
        {"update_id": 52, "message": {"message_id": 3,
            "chat": {"id": 123456}, "from": {"id": 123456},
            "text": "hola"}},                      # not a command
    ]}
    posts = []
    def poll_post(url, payload, timeout):
        posts.append((url, payload))
        if "getUpdates" in url:
            return {"ok": True, **updates}
        return {"ok": True, "result": {"message_id": 1}}
    telegram_bot._post = poll_post

    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
    res = telegram_bot.process_updates(conn)
    check("poll: owner command processed, stranger ignored",
          res == {"processed": 1, "ignored": 2, "replies": 1,
                  "reply_failures": []})
    pref = telegram_bot.PrefsStore(conn).get_all()
    check("poll: min_discount persisted for owner",
          pref["min_discount"] == 30)
    check("poll: offset persisted past last update", pref["update_offset"] == 53)
    reply_posts = [p for u, p in posts if "sendMessage" in u]
    check("poll: exactly one reply, to owner chat",
          len(reply_posts) == 1 and reply_posts[0]["chat_id"] == 123456)

    # restart: offset sent to API, already-processed updates not re-answered
    updates["result"] = []
    res2 = telegram_bot.process_updates(conn)
    offset_sent = [p for u, p in posts if "getUpdates" in u][-1].get("offset")
    check("poll: restart uses stored offset, no replies",
          offset_sent == 53 and res2["replies"] == 0)
    conn.close()

print(f"\n{sum(1 for _, ok in RESULTS if ok)}/{len(RESULTS)} total delivery checks passed")
if not all(ok for _, ok in RESULTS):
    sys.exit(1)
print("ALL DELIVERY + POLLING CHECKS PASSED")

# --- Quarantine: uncertain sends stay blocked across restarts ---
import sqlite3, tempfile
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "q.db")
    os.environ["HARDWARE_DEALS_DB"] = db
    importlib.reload(config)
    import database
    rebind_db(config.DB_PATH)
    database.initialize_database()
    conn = sqlite3.connect(db)
    for i, price in enumerate([500.0, 520.0, 540.0, 380.0]):
        conn.execute(
            "INSERT INTO listings (source,title,description,price_amount,url,"
            "first_seen,last_seen) VALUES ('wallapop',?,?,?,?,datetime('now'),"
            "datetime('now'))",
            ("KFA2 GeForce RTX 4070 SUPER 12GB",
             "Tarjeta usada en buen estado", price, f"http://q/{i}"))
    conn.commit(); conn.close()
    importlib.reload(telegram_bot)
    import run_pipeline
    # every send's outcome is uncertain
    telegram_bot._post = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("reset"))
    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
    out = run_pipeline.run(conn, use_ai=False, live=True)
    check("uncertain sends quarantined, none recorded",
          out["delivered"] == 0 and len(out["send_uncertain"]) >= 1)
    q = conn.execute("SELECT COUNT(*) FROM delivery_quarantine").fetchone()[0]
    check("quarantine persisted", q == len(out["send_uncertain"]))
    # next run with a HEALTHY api: quarantined deals are NOT retried
    telegram_bot._post = lambda url, payload, timeout: {"ok": True, "result": {"message_id": 1}}
    out2 = run_pipeline.run(conn, use_ai=False, live=True)
    check("healthy rerun does not resend quarantined deals",
          out2["delivered"] == 0)
    # person verifies Telegram, clears, then the deal flows again
    import alert_selector
    cleared = alert_selector.clear_quarantine(conn)
    out3 = run_pipeline.run(conn, use_ai=False, live=True)
    check("after human clear, deal delivers once",
          cleared >= 1 and out3["delivered"] >= 1)
    conn.close()

print(f"\n{sum(1 for _, ok in RESULTS if ok)}/{len(RESULTS)} total delivery checks passed")
if not all(ok for _, ok in RESULTS):
    sys.exit(1)
print("ALL DELIVERY + POLLING + QUARANTINE CHECKS PASSED")

# --- V7-review cases: group poison, nonfatal reply, HTTP classes ---
import sqlite3, tempfile, urllib.error, io
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "poll2.db")
    os.environ["HARDWARE_DEALS_DB"] = db
    importlib.reload(config)
    import database
    rebind_db(config.DB_PATH)
    database.initialize_database()
    importlib.reload(telegram_bot)
    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row

    # group poison: owner sends a command FROM A GROUP - must not apply,
    # must not block the next command, offset must advance.
    updates = {"result": [
        {"update_id": 60, "message": {"message_id": 1,
            "chat": {"id": -100}, "from": {"id": 123456},
            "text": "/min_score 99"}},
        {"update_id": 61, "message": {"message_id": 2,
            "chat": {"id": 123456}, "from": {"id": 123456},
            "text": "/min_score 70"}},
    ]}
    posts = []
    def poll_post2(url, payload, timeout):
        posts.append((url, payload))
        if "getUpdates" in url:
            return {"ok": True, **updates}
        return {"ok": True, "result": {"message_id": 1}}
    telegram_bot._post = poll_post2
    res = telegram_bot.process_updates(conn)
    prefs = telegram_bot.PrefsStore(conn).get_all()
    check("group command ignored, private command applied",
          res["processed"] == 1 and res["ignored"] == 1
          and prefs["min_score"] == 70)
    check("group poison cannot stall offset", prefs["update_offset"] == 62)

    # nonfatal reply error: two commands, first reply fails - both
    # commands apply, both acked, failure reported, second reply sent.
    updates["result"] = [
        {"update_id": 62, "message": {"message_id": 3,
            "chat": {"id": 123456}, "from": {"id": 123456},
            "text": "/min_score 60"}},
        {"update_id": 63, "message": {"message_id": 4,
            "chat": {"id": 123456}, "from": {"id": 123456},
            "text": "/min_discount 25"}},
    ]
    state = {"n": 0}
    def flaky_reply(url, payload, timeout):
        state["n"] += 1
        if "getUpdates" in url:
            return {"ok": True, **updates}
        if state["n"] == 2:
            raise ConnectionError("reset")
        return {"ok": True, "result": {"message_id": 1}}
    telegram_bot._post = flaky_reply
    res = telegram_bot.process_updates(conn)
    prefs = telegram_bot.PrefsStore(conn).get_all()
    check("reply failure: both commands applied + acked",
          prefs["min_score"] == 60 and prefs["min_discount"] == 25
          and prefs["update_offset"] == 64)
    check("reply failure: reported, one reply sent, not retried",
          len(res["reply_failures"]) == 1 and res["replies"] == 1)
    conn.close()

# HTTP classification (no DB needed)
def http_error(code):
    return urllib.error.HTTPError(
        url="u", code=code, msg="m", hdrs={"Retry-After": "30"} if code == 429 else {}, fp=io.BytesIO(b"{}"))

os.environ["PERSONAL_DEALS_TELEGRAM_OWNER_ID"] = "123456"
importlib.reload(config); importlib.reload(telegram_bot)
telegram_bot._post = lambda *a, **k: (_ for _ in ()).throw(http_error(429))
try:
    telegram_bot.deliver(123456, "x", token="t")
    check("429 -> uncertain", False)
except telegram_bot.DeliveryUncertain as e:
    check("429 -> uncertain with retry_after", e.retry_after == 30)
telegram_bot._post = lambda *a, **k: (_ for _ in ()).throw(http_error(500))
try:
    telegram_bot.deliver(123456, "x", token="t")
    check("500 -> uncertain", False)
except telegram_bot.DeliveryUncertain:
    check("500 -> uncertain", True)
telegram_bot._post = lambda *a, **k: (_ for _ in ()).throw(http_error(400))
try:
    telegram_bot.deliver(123456, "x", token="t")
    check("400 -> confirmed refusal", False)
except RuntimeError as e:
    check("400 -> confirmed refusal", not isinstance(e, telegram_bot.DeliveryUncertain))
telegram_bot._post = lambda *a, **k: {"ok": True}  # malformed success
try:
    telegram_bot.deliver(123456, "x", token="t")
    check("malformed ok -> uncertain", False)
except telegram_bot.DeliveryUncertain:
    check("malformed ok -> uncertain", True)

print(f"\n{sum(1 for _, ok in RESULTS if ok)}/{len(RESULTS)} total delivery checks passed")
if not all(ok for _, ok in RESULTS):
    sys.exit(1)
print("ALL DELIVERY + POLLING + QUARANTINE + HTTP-CLASS CHECKS PASSED")

# --- V8-review cases: /reset isolation + atomic effect/offset commit ---
import copy
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "poll3.db")
    os.environ["HARDWARE_DEALS_DB"] = db
    importlib.reload(config)
    import database
    rebind_db(config.DB_PATH)
    database.initialize_database()
    importlib.reload(telegram_bot)
    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row

    # reset isolation: /watch then /reset must leave watches empty AND
    # the module default unpolluted (deepcopy regression).
    telegram_bot._post = lambda *a, **k: {"ok": True, "result": {"message_id": 1}}
    conn2 = sqlite3.connect(db); conn2.row_factory = sqlite3.Row
    telegram_bot.handle_message(123456, "/watch rtx 4060", conn2, owner_id=123456)
    check("watch applies", telegram_bot.PrefsStore(conn2).get_all()["watches"] == ["rtx 4060"])
    telegram_bot.handle_message(123456, "/reset", conn2, owner_id=123456)
    check("reset clears watches", telegram_bot.PrefsStore(conn2).get_all()["watches"] == [])
    check("module default unpolluted", telegram_bot.DEFAULT_PREFS["watches"] == [])
    with tempfile.TemporaryDirectory() as d2:
        fresh = sqlite3.connect(os.path.join(d2, "fresh.db"))
        fresh.execute("CREATE TABLE IF NOT EXISTS user_prefs (key TEXT PRIMARY KEY, value TEXT)")
        check("fresh DB not polluted",
              telegram_bot.PrefsStore(fresh).get_all()["watches"] == [])
        fresh.close()

    # atomicity: if the commit carrying effect+offset fails, BOTH roll
    # back - the update is reprocessed on the next poll.
    updates = {"result": [
        {"update_id": 70, "message": {"message_id": 5,
            "chat": {"id": 123456}, "from": {"id": 123456},
            "text": "/min_score 55"}},
    ]}
    state = {"fail_commit": True}
    class FlakyConn(sqlite3.Connection):
        def commit(self):
            if state["fail_commit"]:
                state["fail_commit"] = False
                raise sqlite3.OperationalError("disk full (simulated)")
            return super().commit()
    flaky = sqlite3.connect(db, factory=FlakyConn)
    flaky.row_factory = sqlite3.Row
    telegram_bot._post = lambda *a, **k: {"ok": True, **updates} if "getUpdates" in a[0] else {"ok": True, "result": {"message_id": 1}}
    try:
        telegram_bot.process_updates(flaky)
        check("commit failure propagates", False)
    except sqlite3.OperationalError:
        check("commit failure propagates", True)
    prefs = telegram_bot.PrefsStore(conn2).get_all()
    check("commit failure: effect AND offset rolled back",
          prefs["min_score"] != 55 and prefs["update_offset"] is None)
    # next poll reprocesses the same update successfully
    telegram_bot.process_updates(flaky)
    prefs = telegram_bot.PrefsStore(conn2).get_all()
    check("reprocess after rollback applies once",
          prefs["min_score"] == 55 and prefs["update_offset"] == 71)
    flaky.close(); conn2.close(); conn.close()

print(f"\n{sum(1 for _, ok in RESULTS if ok)}/{len(RESULTS)} total delivery checks passed")
if not all(ok for _, ok in RESULTS):
    sys.exit(1)
print("ALL DELIVERY + POLLING + QUARANTINE + HTTP-CLASS + ATOMIC CHECKS PASSED")
