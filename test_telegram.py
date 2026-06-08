"""Telegram broadcast engine tests — fake sender, no real network/API."""
import os, sys

os.environ["AZURACAST_API_KEY"] = ""
os.environ["WERKZEUG_RUN_MAIN"] = "false"
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.pop("TELEGRAM_BOT_TOKEN", None)   # import app with feature dormant (no bg thread)

import app as appmod
import telegram_engine as te

ok = True
def check(label, cond):
    global ok; ok = ok and cond
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")

# capture every send instead of hitting Telegram
sent = []
def fake_send(text, chat_id=None):
    sent.append({"text": text, "chat_id": chat_id})
    return {"sent": True, "chat_id": chat_id or "test-chan", "status": 200, "error": None}
te.send_telegram = fake_send

# controlled rules file (back up the real one, restore at the end)
backup = te.RULES_FILE.read_text(encoding="utf-8") if te.RULES_FILE.exists() else None
TEST_RULES = """\
channel: "@test"
rules:
  - id: test-morning
    trigger: show_start
    station: islamic
    show: morning
    enabled: true
    generate: false
    template: "TEST {station_name} / {show} / {date}"
    chat_id: ""
  - id: test-manual
    trigger: manual
    station: islamic
    show: night
    enabled: true
    generate: false
    template: "MANUAL {station_name}"
"""
te.RULES_FILE.write_text(TEST_RULES, encoding="utf-8")
DAY = "2099-01-01"

try:
    c = appmod.app.test_client()

    print("status / rules endpoints:")
    st = c.get("/api/v1/telegram/status").get_json()
    check("status ok, 2 rules, 2 enabled", st["ok"] and st["data"]["rules"] == 2 and st["data"]["enabled_rules"] == 2)
    rl = c.get("/api/v1/telegram/rules").get_json()
    check("rules GET returns raw+parsed", rl["ok"] and "test-morning" in rl["data"]["raw"])

    print("manual send (raw text):")
    r = c.post("/api/v1/telegram/send", json={"text": "مرحباً بالمتابعين"}).get_json()
    check("manual text sent", r["ok"] and r["data"]["sent"])
    check("captured the text", sent and sent[-1]["text"] == "مرحباً بالمتابعين")

    print("event scheduler (show_start):")
    os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"   # enable tick
    before = len(sent)
    fired = te.tick("0500", DAY)   # islamic/morning start_time is 0500
    check("show_start fired exactly once", len(fired) == 1 and len(sent) == before + 1)
    check("rendered template placeholders", "إذاعة القرآن" in sent[-1]["text"] and "morning" in sent[-1]["text"])
    again = te.tick("0500", DAY)   # same minute/day again
    check("claim prevents double-post (2 workers / re-tick)", again == [])
    none = te.tick("0600", DAY)    # wrong time
    check("no fire at non-matching time", none == [])

    print("fire endpoint (manual trigger of a rule):")
    fr = c.post("/api/v1/telegram/rules/test-manual/fire").get_json()
    check("fire by id works", fr["ok"] and fr["data"]["sent"] and "MANUAL" in fr["data"]["text"])
    nf = c.post("/api/v1/telegram/rules/does-not-exist/fire").get_json()
    check("unknown rule → NOT_FOUND", nf["ok"] is False and nf["code"] == "NOT_FOUND")

    print("dormant-without-token guard:")
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    check("tick no-ops without token", te.tick("0500", "2099-01-02") == [])
finally:
    # cleanup: state markers + restore rules file
    import glob
    for f in glob.glob(str(te.STATE_DIR / f"*__{DAY}.fired")):
        os.remove(f)
    if backup is not None:
        te.RULES_FILE.write_text(backup, encoding="utf-8")
    else:
        te.RULES_FILE.unlink(missing_ok=True)

print("\nRESULT:", "ALL PASS ✓" if ok else "SOME FAILED ✗")
sys.exit(0 if ok else 1)
