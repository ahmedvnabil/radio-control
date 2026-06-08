"""
Telegram broadcasts to followers — flexible, file-configured.

You define *events* in an editable file (agents/broadcasts.yaml). Each rule has a
trigger:
  - show_start : auto-posts when that show's start_time hits (needs the scheduler
                 running + TELEGRAM_BOT_TOKEN set)
  - manual     : only fires from the "send" / "fire" endpoints (you trigger it)

Plus a manual send endpoint to post anything to the channel on demand.

Env:
  TELEGRAM_BOT_TOKEN   bot token from @BotFather  (no token → feature dormant)
  TELEGRAM_CHAT_ID     default channel/chat id (e.g. @my_channel or -1001234567890)

Fired-state is an atomic file claim under agents/.broadcast_state/, so 2 gunicorn
workers (or a redeploy mid-day) never double-post the same show_start rule.
"""
import os
import re
import time as _time
from pathlib import Path

import requests
import yaml
from flask import Blueprint, jsonify, request

AGENTS_DIR = (Path(__file__).resolve().parent / "agents").resolve()
RULES_FILE = AGENTS_DIR / "broadcasts.yaml"
STATE_DIR = AGENTS_DIR / ".broadcast_state"
TG_API = "https://api.telegram.org"

DEFAULT_RULES = """\
# Telegram broadcasts to followers — editable. Add/remove rules freely.
# trigger: show_start -> auto-posts when the show's start_time hits
#                        (needs the scheduler + TELEGRAM_BOT_TOKEN)
#          manual      -> only fires from the Send button / fire endpoint
# template placeholders: {script} {description} {station_name} {station} {show} {date}
channel: ""        # default chat/channel id; empty = use TELEGRAM_CHAT_ID env
rules:
  - id: islamic-morning
    trigger: show_start
    station: islamic
    show: morning
    enabled: false       # turn on when you're ready to go live
    generate: true        # generate the show script and include it
    template: |
      \U0001F4FB الآن على {station_name}
      برنامج: {description}

      {script}
    chat_id: ""          # optional override; empty = default channel
"""


def _ok(data=None, meta=None):
    return jsonify({"ok": True, "data": data, "error": None, "meta": meta or {}}), 200


def _err(message, code="ERROR", status=400):
    return jsonify({"ok": False, "data": None, "error": message, "code": code}), status


def _token():
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def _default_chat():
    return os.environ.get("TELEGRAM_CHAT_ID", "").strip()


# ---------- rules file ------------------------------------------------------

def ensure_rules_file():
    if not RULES_FILE.exists():
        AGENTS_DIR.mkdir(parents=True, exist_ok=True)
        RULES_FILE.write_text(DEFAULT_RULES, encoding="utf-8")


def load_rules() -> dict:
    if not RULES_FILE.exists():
        return {"channel": "", "rules": []}
    data = yaml.safe_load(RULES_FILE.read_text(encoding="utf-8")) or {}
    data.setdefault("channel", "")
    data.setdefault("rules", [])
    return data


def save_rules_raw(text: str):
    yaml.safe_load(text)  # validate before writing
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    RULES_FILE.write_text(text, encoding="utf-8")


# ---------- sending ---------------------------------------------------------

def send_telegram(text: str, chat_id: str | None = None) -> dict:
    token = _token()
    chat = (str(chat_id).strip() if chat_id else "") or load_rules().get("channel") or _default_chat()
    if not token:
        return {"sent": False, "error": "TELEGRAM_BOT_TOKEN not set", "code": "TG_DISABLED"}
    if not chat:
        return {"sent": False, "error": "no chat_id/channel configured", "code": "TG_NO_CHAT"}
    try:
        r = requests.post(
            f"{TG_API}/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
            timeout=20,
        )
        good = r.ok and (r.json().get("ok") is True)
        return {"sent": bool(good), "chat_id": chat, "status": r.status_code,
                "error": None if good else r.text[:200]}
    except Exception as e:
        return {"sent": False, "error": str(e)[:200], "code": "TG_ERROR"}


def _render(rule: dict, show: dict, date: str, station_name: str, script: str) -> str:
    # plain replace (not str.format) so emojis/Arabic braces never break it
    out = rule.get("template") or "{script}"
    for token, val in {
        "{script}": script,
        "{description}": show.get("description", ""),
        "{station_name}": station_name,
        "{station}": rule.get("station", ""),
        "{show}": rule.get("show", ""),
        "{date}": date,
    }.items():
        out = out.replace(token, str(val))
    return out.strip()


def fire_rule(rule: dict, date: str | None = None) -> dict:
    """Build the message for a rule (optionally generating the script) and send it."""
    date = date or _time.strftime("%Y-%m-%d")
    from agents_engine import build_station_templates
    tpl = build_station_templates()
    sdata = tpl.get(rule.get("station", ""), {})
    show = (sdata.get("shows", {}) or {}).get(rule.get("show", ""), {})
    station_name = sdata.get("name", rule.get("station", ""))
    script = ""
    if rule.get("generate") and show:
        try:
            import app  # lazy → avoids circular import
            script = app.generate_script_text(show, date).get("script", "")
        except Exception as e:
            return {"sent": False, "error": f"generate failed: {str(e)[:160]}", "code": "GEN_FAILED"}
    text = _render(rule, show, date, station_name, script)
    result = send_telegram(text, rule.get("chat_id"))
    result["text"] = text
    return result


# ---------- scheduler hook (multi-worker safe) ------------------------------

def _claim(rule_id: str, today: str) -> bool:
    """Atomically claim a (rule, day) so only one worker fires it."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(rule_id))
    marker = STATE_DIR / f"{safe}__{today}.fired"
    try:
        fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        return False


def tick(now_hhmm: str, today: str) -> list:
    """Called by the scheduler each minute. Fires due show_start rules once/day."""
    fired = []
    if not _token():
        return fired
    from agents_engine import build_station_templates
    tpl = build_station_templates()
    for rule in load_rules().get("rules", []):
        if rule.get("trigger") != "show_start" or not rule.get("enabled"):
            continue
        show = (tpl.get(rule.get("station", ""), {}).get("shows", {}) or {}).get(rule.get("show", ""))
        if not show or show.get("start_time") != now_hhmm:
            continue
        if not _claim(rule.get("id", "rule"), today):
            continue  # another worker already fired it
        res = fire_rule(rule, today)
        fired.append({"id": rule.get("id"), **res})
    return fired


# ---------- blueprint -------------------------------------------------------

telegram_bp = Blueprint("telegram", __name__)


@telegram_bp.get("/api/v1/telegram/status")
def api_tg_status():
    data = load_rules()
    return _ok({
        "configured": bool(_token()),
        "default_chat": bool(data.get("channel") or _default_chat()),
        "rules": len(data.get("rules", [])),
        "enabled_rules": sum(1 for r in data.get("rules", []) if r.get("enabled")),
    })


@telegram_bp.get("/api/v1/telegram/rules")
def api_tg_rules_get():
    ensure_rules_file()
    return _ok({"raw": RULES_FILE.read_text(encoding="utf-8"), "parsed": load_rules()})


@telegram_bp.put("/api/v1/telegram/rules")
def api_tg_rules_put():
    content = (request.get_json(silent=True) or {}).get("content", "")
    if not content.strip():
        return _err("content required", "EMPTY")
    try:
        save_rules_raw(content)
    except Exception as e:
        return _err(f"invalid YAML: {str(e)[:160]}", "BAD_YAML")
    return _ok({"saved": True})


@telegram_bp.post("/api/v1/telegram/send")
def api_tg_send():
    """Manual send: {text} for raw text, or {station, show[, date, template]} to generate."""
    p = request.get_json(silent=True) or {}
    if p.get("text"):
        res = send_telegram(p["text"], p.get("chat_id"))
    elif p.get("station") and p.get("show"):
        rule = {"station": p["station"], "show": p["show"], "generate": p.get("generate", True),
                "template": p.get("template") or "{script}", "chat_id": p.get("chat_id", "")}
        res = fire_rule(rule, p.get("date"))
    else:
        return _err("provide 'text' or 'station'+'show'", "BAD_REQUEST")
    return _ok(res) if res.get("sent") else _err(res.get("error", "send failed"), res.get("code", "TG_ERROR"), 502)


@telegram_bp.post("/api/v1/telegram/rules/<rid>/fire")
def api_tg_fire(rid):
    rule = next((r for r in load_rules().get("rules", []) if r.get("id") == rid), None)
    if not rule:
        return _err("rule not found", "NOT_FOUND", 404)
    res = fire_rule(rule, request.args.get("date"))
    return _ok(res) if res.get("sent") else _err(res.get("error", "send failed"), res.get("code", "TG_ERROR"), 502)
