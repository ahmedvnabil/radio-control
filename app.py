"""
Radio Control — Operational panel over AzuraCast.

Keeps the AzuraCast API key on the server. Frontend talks only to this app's
/api/v1/* endpoints, which proxy to AzuraCast with the bearer token.
"""
from __future__ import annotations

import base64
import collections
import json
import logging
import os
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request

load_dotenv()

BASE_URL = os.environ.get("AZURACAST_BASE_URL", "https://radio.zad.tools").rstrip("/")
API_KEY = os.environ.get("AZURACAST_API_KEY", "")
DEBUG = os.environ.get("DEBUG", "false").lower() in ("1", "true", "yes")
QUEUE_BUILDER_INTERVAL = int(os.environ.get("QUEUE_BUILDER_INTERVAL", "30"))
DEFAULT_LANG = os.environ.get("DEFAULT_LANG", "ar")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

# ---- Local integrations (user's network) ----
# All optional — when unset, the related endpoint returns an empty/stub response.
GUIDE_BASE_URL = os.environ.get("GUIDE_BASE_URL", "").rstrip("/")    # ex: http://192.168.70.127
GUIDE_TOKEN = os.environ.get("GUIDE_TOKEN", "")                       # X-Guide-Token for protected ops
VOICEBOX_BASE_URL = os.environ.get("VOICEBOX_BASE_URL", "").rstrip("/")  # ex: http://192.168.70.194:11000
ACE_STEP_BASE_URL = os.environ.get("ACE_STEP_BASE_URL", "").rstrip("/")  # ex: http://192.168.70.164:3001
FREELLM_BASE_URL = os.environ.get("FREELLM_BASE_URL", "").rstrip("/")    # ex: https://freegetway.zad.tools
FREELLM_API_KEY = os.environ.get("FREELLM_API_KEY", "")
VOICEBOX_API_KEY = os.environ.get("VOICEBOX_API_KEY", "")               # api-key / Bearer for VoiceBox generation
ACE_STEP_TOKEN = os.environ.get("ACE_STEP_TOKEN", "")                   # bearer token for the songs.* service

# Cloudflare Access service-token credentials. The 3 zad.tools tunnels have an Access policy
# on /api/* — we pass these headers so the policy lets the container through.
CF_ACCESS_CLIENT_ID = os.environ.get("CF_ACCESS_CLIENT_ID", "")
CF_ACCESS_CLIENT_SECRET = os.environ.get("CF_ACCESS_CLIENT_SECRET", "")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev")
log = logging.getLogger("radio-control")

# File-based show personas live in agents/ — see agents_engine.py
from agents_engine import agents_bp, build_station_templates  # noqa: E402
from telegram_engine import telegram_bp, tick as _broadcast_tick  # noqa: E402
# Agent Studio (lyric writer / QC / Suno prompts) — merged from the agent-studio app.
from studio_engine import studio_bp  # noqa: E402

app.register_blueprint(agents_bp)
app.register_blueprint(telegram_bp)
app.register_blueprint(studio_bp)


# ---------- helpers ---------------------------------------------------------

# Cloudflare's bot protection 403s the default python-requests UA. The integration
# services (VoiceBox / songs / FreeLLM) sit behind Cloudflare tunnels, so all calls
# to them go through this session with a browser User-Agent.
INTEG_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_integ = requests.Session()
_integ.headers.update({"User-Agent": INTEG_UA})
if CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET:
    _integ.headers.update({
        "CF-Access-Client-Id": CF_ACCESS_CLIENT_ID,
        "CF-Access-Client-Secret": CF_ACCESS_CLIENT_SECRET,
    })


def _vb_headers() -> dict:
    """Auth headers for VoiceBox generation (profiles are public; synth needs the key)."""
    if not VOICEBOX_API_KEY:
        return {}
    return {"api-key": VOICEBOX_API_KEY, "Authorization": f"Bearer {VOICEBOX_API_KEY}"}


def _ace_headers() -> dict:
    """Auth header for the songs.* service."""
    return {"Authorization": f"Bearer {ACE_STEP_TOKEN}"} if ACE_STEP_TOKEN else {}


def _az(method: str, path: str, **kwargs: Any) -> requests.Response:
    url = urljoin(BASE_URL + "/", path.lstrip("/"))
    headers = kwargs.pop("headers", {}) or {}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    return requests.request(method, url, headers=headers, timeout=30, **kwargs)


def _ok(data: Any, meta: dict | None = None) -> tuple[Response, int]:
    return jsonify({"ok": True, "data": data, "error": None, "meta": meta or {}}), 200


def _err(message: str, code: str = "ERROR", status: int = 400) -> tuple[Response, int]:
    return jsonify({"ok": False, "data": None, "error": message, "code": code}), status


def _proxy(method: str, path: str, **kwargs) -> tuple[Response, int]:
    """Generic proxy: pass through, wrap in envelope."""
    r = _az(method, path, **kwargs)
    if not r.ok:
        return _err(r.text[:500], "AZURACAST_ERROR", r.status_code)
    try:
        return _ok(r.json())
    except ValueError:
        return _ok(r.text)


def _guide(method: str, path: str, **kwargs):
    """Call the local Guide backend if configured. Returns requests.Response or None."""
    if not GUIDE_BASE_URL:
        return None
    headers = kwargs.pop("headers", {}) or {}
    if GUIDE_TOKEN:
        headers["X-Guide-Token"] = GUIDE_TOKEN
    url = GUIDE_BASE_URL + path
    try:
        return _integ.request(method, url, headers=headers, timeout=30, **kwargs)
    except Exception as e:
        log.warning("guide call failed: %s", e)
        return None


# ---------- security --------------------------------------------------------

TOKENS_FILE = Path("agents/api-tokens.json")
_access_logs = collections.deque(maxlen=200)

def _load_tokens() -> list[dict]:
    if not TOKENS_FILE.exists():
        return []
    try:
        return json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []

def _save_tokens(tokens: list[dict]) -> None:
    TOKENS_FILE.write_text(json.dumps(tokens, indent=2, ensure_ascii=False), encoding="utf-8")

def _validate_token(given: str) -> tuple[bool, str]:
    if not given:
        return False, ""
    if GUIDE_TOKEN and given == GUIDE_TOKEN:
        return True, "Master Token"
    tokens = _load_tokens()
    for t in tokens:
        if t.get("token") == given:
            t["last_used"] = datetime.now(timezone.utc).isoformat()
            _save_tokens(tokens)
            return True, t.get("name", "Unknown Plugin")
    return False, ""

@app.before_request
def require_global_token():
    path = request.path
    
    # Allow CORS preflight and public paths
    if request.method == "OPTIONS":
        return
        
    public_paths = ("/p/", "/static/", "/healthz", "/docs", "/apiendpoints")
    if any(path.startswith(p) for p in public_paths) or path == "/docs/raw" or path == "/apiendpoints/raw":
        return

    # If NO token is configured globally (for local dev) and no tokens exist, allow all
    if not GUIDE_TOKEN and not _load_tokens():
        return

    given = request.headers.get("X-Guide-Token") or request.cookies.get("guide_token") or request.args.get("token")
    is_valid, token_name = _validate_token(given)

    if not is_valid:
        _access_logs.appendleft({
            "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "method": request.method,
            "path": path,
            "token": "Invalid/None",
            "ip": request.remote_addr or ""
        })
        if path.startswith("/api/"):
            return jsonify({"ok": False, "error": "unauthorized", "hint": "send X-Guide-Token header or set ?token=..."}), 401
        else:
            return "Unauthorized. Please login via the Security tab in <a href='/docs'>/docs</a>", 401

    # Log successful API request
    if path.startswith("/api/"):
        _access_logs.appendleft({
            "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "method": request.method,
            "path": path,
            "token": token_name,
            "ip": request.remote_addr or ""
        })


# ---------- views -----------------------------------------------------------

@app.get("/")
def index() -> str:
    return render_template(
        "index.html",
        base_url=BASE_URL,
        configured=bool(API_KEY),
        default_lang=DEFAULT_LANG,
    )


@app.get("/p/<station_short>")
def public_player(station_short: str) -> str:
    return render_template("player.html", base_url=BASE_URL, station_short=station_short)


@app.get("/docs")
def api_docs() -> str:
    # Serves the combined tabbed view
    return render_template("api-center.html")

@app.get("/docs/raw")
def api_docs_raw() -> str:
    # Serves the interactive explorer
    return render_template("api-explorer.html")


@app.get("/apiendpoints/raw")
def api_endpoints_list_raw() -> str:
    # Serves the generated list
    return render_template("apiendpoints.html")

@app.get("/apiendpoints")
def api_endpoints_redirect():
    # Redirect legacy URL to the new combined view
    from flask import redirect
    return redirect("/docs#list")


@app.get("/healthz")
def healthz() -> tuple[Response, int]:
    return jsonify({"ok": True}), 200


# ---------- API v1: Auth & Tokens -------------------------------------------

@app.get("/api/v1/auth/tokens")
def api_list_tokens():
    tokens = _load_tokens()
    # Mask the actual tokens except the first few characters for security
    safe_tokens = [
        {
            "id": t["id"],
            "name": t.get("name", "Unnamed"),
            "prefix": t["token"][:5] + "...",
            "created_at": t.get("created_at"),
            "last_used": t.get("last_used")
        } for t in tokens
    ]
    return _ok(safe_tokens)

@app.post("/api/v1/auth/tokens")
def api_create_token():
    body = request.get_json(silent=True) or {}
    name = body.get("name", "Unnamed Plugin").strip()
    tokens = _load_tokens()
    new_token = {
        "id": "tkn_" + secrets.token_hex(6),
        "name": name,
        "token": "sk_" + secrets.token_urlsafe(32),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_used": None
    }
    tokens.append(new_token)
    _save_tokens(tokens)
    return _ok(new_token)  # Send the full token ONLY once on creation

@app.delete("/api/v1/auth/tokens/<token_id>")
def api_delete_token(token_id: str):
    tokens = _load_tokens()
    new_tokens = [t for t in tokens if t["id"] != token_id]
    if len(tokens) == len(new_tokens):
        return _err("Token not found", "NOT_FOUND", 404)
    _save_tokens(new_tokens)
    return _ok({"deleted": True})

@app.get("/api/v1/auth/logs")
def api_auth_logs():
    return _ok(list(_access_logs))

# ---------- API v1: meta ----------------------------------------------------

@app.get("/api/v1/config")
def api_config() -> tuple[Response, int]:
    return _ok({"azuracast_base_url": BASE_URL, "configured": bool(API_KEY), "default_lang": DEFAULT_LANG})


@app.get("/api/v1/integrations/health")
def api_integrations_health() -> tuple[Response, int]:
    """Diagnostic: which integration URLs are set, and can the container actually reach them?"""
    targets = {
        "voicebox": (VOICEBOX_BASE_URL, "/api/profiles"),
        "ace_step": (ACE_STEP_BASE_URL, "/api/songs?limit=1"),
        "freellm": (FREELLM_BASE_URL, "/v1/models"),
        "guide": (GUIDE_BASE_URL, "/api/radio/voices"),
    }
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
    cf_hdr = {"CF-Access-Client-Id": CF_ACCESS_CLIENT_ID,
              "CF-Access-Client-Secret": CF_ACCESS_CLIENT_SECRET} if (CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET) else {}
    out = {}
    for name, (base, probe) in targets.items():
        info = {"configured": bool(base), "base": base or None}
        if base:
            tests = [
                ("plain", {}),
                ("browser_ua", {"User-Agent": ua}),
                ("ua_plus_cf", {"User-Agent": ua, **cf_hdr}),
            ]
            for label, hdr in tests:
                try:
                    r = requests.get(base + probe, headers=hdr, timeout=8)
                    info[label] = r.status_code
                except Exception as e:
                    info[label] = type(e).__name__ + ": " + str(e)[:140]
        out[name] = info
    out["openai_key"] = bool(OPENAI_API_KEY)
    out["freellm_key"] = bool(FREELLM_API_KEY)
    out["cf_access_configured"] = bool(cf_hdr)
    return _ok(out)


# ---------- API v1: stations ------------------------------------------------

@app.get("/api/v1/stations")
def api_stations() -> tuple[Response, int]:
    r = _az("GET", "/api/admin/stations")
    if not r.ok:
        return _err(f"AzuraCast: {r.status_code}", "AZURACAST_ERROR", r.status_code)
    return _ok(r.json())


@app.post("/api/v1/stations")
def api_create_station() -> tuple[Response, int]:
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    if not name:
        return _err("name is required", "VALIDATION", 400)
    short = (payload.get("short_name") or name.lower().replace(" ", "_"))[:50]
    body = {
        "name": name,
        "short_name": short,
        "description": payload.get("description") or "",
        "frontend_type": "icecast",
        "backend_type": "liquidsoap",
        "enable_public_page": True,
        "enable_streamers": True,
        "enable_requests": True,
        "enable_on_demand": True,
    }
    r = _az("POST", "/api/admin/stations", json=body)
    if not r.ok:
        return _err(r.text[:500], "AZURACAST_ERROR", r.status_code)
    return _ok(r.json())


# ---------- API v1: media ---------------------------------------------------

@app.get("/api/v1/stations/<int:sid>/files")
def api_files(sid: int) -> tuple[Response, int]:
    r = _az("GET", f"/api/station/{sid}/files?per_page=500")
    if not r.ok:
        return _err(r.text[:500], "AZURACAST_ERROR", r.status_code)
    raw = r.json()
    rows = raw.get("rows", []) if isinstance(raw, dict) else (raw or [])
    items = []
    for it in rows:
        path = it.get("path") or ""
        region = path.split("/")[0] if "/" in path else "global"
        items.append({
            "id": it.get("id"),
            "path": path,
            "name": path.split("/")[-1],
            "title": it.get("title") or path.split("/")[-1],
            "artist": it.get("artist") or "",
            "album": it.get("album") or "",
            "genre": it.get("genre") or "",
            "length": it.get("length") or 0,
            "length_text": it.get("length_text") or "",
            "unique_id": it.get("unique_id"),
            "region": region,
            "art": it.get("art"),
            "playlists": [{"id": p.get("id"), "name": p.get("name")} for p in (it.get("playlists") or [])],
        })
    return _ok(items, meta={"total": raw.get("total"), "page": raw.get("page")} if isinstance(raw, dict) else None)


@app.post("/api/v1/stations/<int:sid>/upload")
def api_upload(sid: int) -> tuple[Response, int]:
    if "file" not in request.files:
        return _err("file is required", "VALIDATION", 400)
    f = request.files["file"]
    path = (request.form.get("path") or f.filename or "uploaded.mp3").strip()
    body = {"path": path, "file": base64.b64encode(f.read()).decode("ascii")}
    r = _az("POST", f"/api/station/{sid}/files", json=body, headers={"Content-Type": "application/json"})
    if not r.ok:
        return _err(r.text[:500], "AZURACAST_ERROR", r.status_code)
    return _ok(r.json())


@app.delete("/api/v1/stations/<int:sid>/files/<int:fid>")
def api_delete_file(sid: int, fid: int) -> tuple[Response, int]:
    return _proxy("DELETE", f"/api/station/{sid}/file/{fid}")


@app.post("/api/v1/stations/<int:sid>/files/bulk-assign")
def api_bulk_assign(sid: int) -> tuple[Response, int]:
    """Body: {file_ids: [...], playlist_ids: [...]}.
    AzuraCast's batch action expects `files` as PATHS and `playlists` as a list of IDs,
    so we resolve the selected ids to their storage paths first."""
    payload = request.get_json(silent=True) or {}
    file_ids = [int(x) for x in (payload.get("file_ids") or [])]
    playlist_ids = [int(x) for x in (payload.get("playlist_ids") or [])]
    if not file_ids or not playlist_ids:
        return _err("file_ids and playlist_ids required", "VALIDATION", 400)
    # id -> path map from the station file list
    fr = _az("GET", f"/api/station/{sid}/files?per_page=1000")
    if not fr.ok:
        return _err(fr.text[:300], "AZURACAST_ERROR", fr.status_code)
    raw = fr.json()
    rows = raw.get("rows", []) if isinstance(raw, dict) else (raw or [])
    id_to_path = {it.get("id"): it.get("path") for it in rows}
    paths = [id_to_path[i] for i in file_ids if id_to_path.get(i)]
    if not paths:
        return _err("no matching files", "VALIDATION", 400)
    body = {"do": "playlist", "playlists": playlist_ids, "files": paths, "dirs": []}
    r = _az("PUT", f"/api/station/{sid}/files/batch", json=body)
    if not r.ok:
        return _err(r.text[:500], "AZURACAST_ERROR", r.status_code)
    try:
        return _ok(r.json())
    except ValueError:
        return _ok({"assigned": len(paths)})


# ---------- API v1: playlists -----------------------------------------------

@app.get("/api/v1/stations/<int:sid>/playlists")
def api_playlists(sid: int) -> tuple[Response, int]:
    return _proxy("GET", f"/api/station/{sid}/playlists")


@app.post("/api/v1/stations/<int:sid>/playlists")
def api_create_playlist(sid: int) -> tuple[Response, int]:
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    if not name:
        return _err("name is required", "VALIDATION", 400)
    body = {
        "name": name,
        "type": payload.get("type", "default"),
        "source": "songs",
        "order": payload.get("order", "shuffle"),
        "is_enabled": True,
        "weight": int(payload.get("weight", 3)),
    }
    return _proxy("POST", f"/api/station/{sid}/playlists", json=body)


@app.put("/api/v1/stations/<int:sid>/playlists/<int:pid>")
def api_update_playlist(sid: int, pid: int) -> tuple[Response, int]:
    payload = request.get_json(silent=True) or {}
    return _proxy("PUT", f"/api/station/{sid}/playlist/{pid}", json=payload)


@app.delete("/api/v1/stations/<int:sid>/playlists/<int:pid>")
def api_delete_playlist(sid: int, pid: int) -> tuple[Response, int]:
    return _proxy("DELETE", f"/api/station/{sid}/playlist/{pid}")


# ---------- API v1: schedule ------------------------------------------------

@app.get("/api/v1/stations/<int:sid>/schedule")
def api_schedule(sid: int) -> tuple[Response, int]:
    """Pull full schedule (events) for visualisation."""
    r = _az("GET", f"/api/station/{sid}/schedule")
    if not r.ok:
        return _err(r.text[:500], "AZURACAST_ERROR", r.status_code)
    return _ok(r.json())


@app.post("/api/v1/stations/<int:sid>/playlists/<int:pid>/schedule")
def api_add_schedule(sid: int, pid: int) -> tuple[Response, int]:
    """
    Add a schedule item to a playlist. Body:
    {start_time: 800, end_time: 1100, days: [1,2,3,4,5], loop_once: false}
    Times are HHMM integers (08:00 -> 800, 17:30 -> 1730).
    """
    payload = request.get_json(silent=True) or {}
    # Fetch existing playlist + its schedule items
    r = _az("GET", f"/api/station/{sid}/playlist/{pid}")
    if not r.ok:
        return _err(r.text[:500], "AZURACAST_ERROR", r.status_code)
    pl = r.json()
    existing = pl.get("schedule_items") or []
    new_item = {
        "start_time": int(payload.get("start_time") or 0),
        "end_time": int(payload.get("end_time") or 0),
        "start_date": payload.get("start_date"),
        "end_date": payload.get("end_date"),
        "days": payload.get("days") or [1, 2, 3, 4, 5, 6, 7],
        "loop_once": bool(payload.get("loop_once", False)),
    }
    existing.append(new_item)
    return _proxy("PUT", f"/api/station/{sid}/playlist/{pid}", json={"schedule_items": existing})


@app.delete("/api/v1/stations/<int:sid>/playlists/<int:pid>/schedule/<int:idx>")
def api_remove_schedule(sid: int, pid: int, idx: int) -> tuple[Response, int]:
    r = _az("GET", f"/api/station/{sid}/playlist/{pid}")
    if not r.ok:
        return _err(r.text[:500], "AZURACAST_ERROR", r.status_code)
    pl = r.json()
    existing = pl.get("schedule_items") or []
    if 0 <= idx < len(existing):
        existing.pop(idx)
    return _proxy("PUT", f"/api/station/{sid}/playlist/{pid}", json={"schedule_items": existing})


# ---------- API v1: queue + history -----------------------------------------

@app.get("/api/v1/stations/<int:sid>/queue")
def api_queue(sid: int) -> tuple[Response, int]:
    return _proxy("GET", f"/api/station/{sid}/queue")


@app.delete("/api/v1/stations/<int:sid>/queue/<int:qid>")
def api_skip_queue(sid: int, qid: int) -> tuple[Response, int]:
    return _proxy("DELETE", f"/api/station/{sid}/queue/{qid}")


@app.get("/api/v1/stations/<int:sid>/history")
def api_history(sid: int) -> tuple[Response, int]:
    return _proxy("GET", f"/api/station/{sid}/history")


@app.post("/api/v1/stations/<int:sid>/build-queue")
def api_build_queue(sid: int) -> tuple[Response, int]:
    results = []
    for _ in range(4):
        r = _az("PUT", f"/api/admin/debug/station/{sid}/nextsong")
        results.append({"ok": r.ok, "status": r.status_code})
    return _ok({"runs": results})


@app.post("/api/v1/stations/<int:sid>/skip")
def api_skip(sid: int) -> tuple[Response, int]:
    """Skip the current song. Triggers next from queue."""
    return _proxy("POST", f"/api/station/{sid}/backend/skip")


@app.post("/api/v1/stations/<int:sid>/restart")
def api_restart(sid: int) -> tuple[Response, int]:
    return _proxy("POST", f"/api/station/{sid}/restart")


# ---------- API v1: now playing ---------------------------------------------

@app.get("/api/v1/nowplaying")
def api_nowplaying() -> tuple[Response, int]:
    return _proxy("GET", "/api/nowplaying")


# ---------- API v1: streamers (DJs) -----------------------------------------

@app.get("/api/v1/stations/<int:sid>/streamers")
def api_streamers(sid: int) -> tuple[Response, int]:
    return _proxy("GET", f"/api/station/{sid}/streamers")


@app.post("/api/v1/stations/<int:sid>/streamers")
def api_create_streamer(sid: int) -> tuple[Response, int]:
    p = request.get_json(silent=True) or {}
    body = {
        "streamer_username": p.get("streamer_username"),
        "streamer_password": p.get("streamer_password"),
        "display_name": p.get("display_name"),
        "comments": p.get("comments") or "",
        "is_active": p.get("is_active", True),
    }
    return _proxy("POST", f"/api/station/{sid}/streamers", json=body)


@app.delete("/api/v1/stations/<int:sid>/streamers/<int:strid>")
def api_delete_streamer(sid: int, strid: int) -> tuple[Response, int]:
    return _proxy("DELETE", f"/api/station/{sid}/streamer/{strid}")


# ---------- API v1: webhooks ------------------------------------------------

@app.get("/api/v1/stations/<int:sid>/webhooks")
def api_webhooks(sid: int) -> tuple[Response, int]:
    return _proxy("GET", f"/api/station/{sid}/webhooks")


@app.post("/api/v1/stations/<int:sid>/webhooks")
def api_create_webhook(sid: int) -> tuple[Response, int]:
    p = request.get_json(silent=True) or {}
    return _proxy("POST", f"/api/station/{sid}/webhooks", json=p)


@app.delete("/api/v1/stations/<int:sid>/webhooks/<int:wid>")
def api_delete_webhook(sid: int, wid: int) -> tuple[Response, int]:
    return _proxy("DELETE", f"/api/station/{sid}/webhook/{wid}")


@app.post("/api/v1/stations/<int:sid>/webhooks/<int:wid>/test")
def api_test_webhook(sid: int, wid: int) -> tuple[Response, int]:
    return _proxy("PUT", f"/api/station/{sid}/webhook/{wid}/test")


# ---------- API v1: mounts --------------------------------------------------

@app.get("/api/v1/stations/<int:sid>/mounts")
def api_mounts(sid: int) -> tuple[Response, int]:
    return _proxy("GET", f"/api/station/{sid}/mounts")


@app.post("/api/v1/stations/<int:sid>/mounts")
def api_create_mount(sid: int) -> tuple[Response, int]:
    p = request.get_json(silent=True) or {}
    body = {
        "name": p.get("name") or "/extra.mp3",
        "display_name": p.get("display_name") or p.get("name"),
        "is_visible_on_public_pages": True,
        "is_default": False,
        "enable_autodj": p.get("enable_autodj", True),
        "autodj_format": p.get("autodj_format", "mp3"),
        "autodj_bitrate": int(p.get("autodj_bitrate", 128)),
    }
    return _proxy("POST", f"/api/station/{sid}/mounts", json=body)


@app.delete("/api/v1/stations/<int:sid>/mounts/<int:mid>")
def api_delete_mount(sid: int, mid: int) -> tuple[Response, int]:
    return _proxy("DELETE", f"/api/station/{sid}/mount/{mid}")


# ---------- API v1: listeners & analytics -----------------------------------

@app.get("/api/v1/stations/<int:sid>/listeners")
def api_listeners(sid: int) -> tuple[Response, int]:
    return _proxy("GET", f"/api/station/{sid}/listeners")


# ---------- day-builder persistence ----------------------------------------
# 24-hour block schedule per station, persisted as JSON on the agents/ volume so
# edits survive container redeploys and follow the user across browsers/devices.

_DAY_BUILDER_DIR = Path(__file__).resolve().parent / "agents" / "day_builder"


def _day_blocks_path(sid: int, date: str | None = None) -> Path:
    if date and all(c.isalnum() or c in "-" for c in date):
        return _DAY_BUILDER_DIR / f"{sid}_{date}.json"
    return _DAY_BUILDER_DIR / f"{sid}.json"


@app.get("/api/v1/stations/<int:sid>/day-blocks")
def api_day_blocks_get(sid: int):
    date = request.args.get("date")
    p = _day_blocks_path(sid, date)
    if not p.exists():
        return _ok([])
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return _ok(data if isinstance(data, list) else [])
    except Exception as e:
        log.warning("day-blocks read failed for sid=%s: %s", sid, e)
        return _ok([])


@app.route("/api/v1/stations/<int:sid>/day-blocks", methods=["POST", "PUT"])
def api_day_blocks_put(sid: int):
    date = request.args.get("date")
    body = request.get_json(silent=True) or {}
    blocks = body.get("blocks")
    if not isinstance(blocks, list):
        return _err("blocks must be a list", "BAD_PAYLOAD")
    # Sanity-validate each block — keep only the fields the frontend writes.
    clean: list[dict] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        try:
            clean.append({
                "id": str(b.get("id") or "")[:64],
                "type": str(b.get("type") or "")[:32],
                "title": str(b.get("title") or "")[:200],
                "startHour": max(0, min(23, int(b.get("startHour") or 0))),
                "durationMins": max(1, min(24 * 60, int(b.get("durationMins") or 30))),
                "data": b.get("data"),
                "showKey": str(b.get("showKey") or "")[:32] if b.get("showKey") else None,
            })
        except (TypeError, ValueError):
            continue
    _day_blocks_path(sid, date).write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    return _ok({"saved": True, "count": len(clean)})


@app.get("/api/v1/stations/<int:sid>/reports/listeners")
def api_listeners_report(sid: int) -> tuple[Response, int]:
    # AzuraCast listener chart lives under reports/overview/charts
    return _proxy("GET", f"/api/station/{sid}/reports/overview/charts")


@app.get("/api/v1/stations/<int:sid>/reports/best")
def api_best_songs(sid: int) -> tuple[Response, int]:
    return _proxy("GET", f"/api/station/{sid}/reports/overview/best-and-worst")


# ---------- API v1: requests ------------------------------------------------

@app.get("/api/v1/stations/<int:sid>/requestable")
def api_requestable(sid: int) -> tuple[Response, int]:
    return _proxy("GET", f"/api/station/{sid}/requests")


@app.get("/api/v1/stations/<int:sid>/request-log")
def api_request_log(sid: int) -> tuple[Response, int]:
    return _proxy("GET", f"/api/station/{sid}/reports/requests")


# ---------- API v1: podcasts ------------------------------------------------

@app.get("/api/v1/stations/<int:sid>/podcasts")
def api_podcasts(sid: int) -> tuple[Response, int]:
    return _proxy("GET", f"/api/station/{sid}/podcasts")


@app.post("/api/v1/stations/<int:sid>/podcasts")
def api_create_podcast(sid: int) -> tuple[Response, int]:
    p = request.get_json(silent=True) or {}
    title = (p.get("title") or "").strip()
    description = (p.get("description") or "").strip()
    if not title:
        return _err("title is required", "VALIDATION", 400)
    if not description:
        return _err("description is required", "VALIDATION", 400)  # AzuraCast requires it
    body = {
        "title": title,
        "description": description,
        "language": p.get("language") or "ar",
        "author": p.get("author") or "",
        "email": p.get("email") or "",
        "explicit": bool(p.get("explicit", False)),
        "categories": p.get("categories") or [],
    }
    return _proxy("POST", f"/api/station/{sid}/podcasts", json=body)


# ---------- API v1: folder → playlist auto-assign ---------------------------

def _station_directories(sid: int) -> list[str]:
    """Distinct top-level folders present in the station's media library."""
    r = _az("GET", f"/api/station/{sid}/files?per_page=1000")
    if not r.ok:
        return []
    raw = r.json()
    rows = raw.get("rows", []) if isinstance(raw, dict) else (raw or [])
    dirs = set()
    for it in rows:
        path = it.get("path") or ""
        if "/" in path:
            dirs.add(path.split("/")[0])
    return sorted(dirs)


@app.get("/api/v1/stations/<int:sid>/directories")
def api_directories(sid: int) -> tuple[Response, int]:
    """List media folders so the UI can assign a whole folder to a playlist.
    (AzuraCast has no writable per-playlist folder rule via the public API; this
    offers the practical equivalent — a one-shot bulk assignment by directory.)"""
    return _ok(_station_directories(sid))


@app.get("/api/v1/stations/<int:sid>/playlists/<int:pid>/folders")
def api_playlist_folders(sid: int, pid: int) -> tuple[Response, int]:
    # Kept for compatibility; returns available directories to choose from.
    return _ok(_station_directories(sid))


@app.post("/api/v1/stations/<int:sid>/playlists/<int:pid>/assign-folder")
def api_assign_folder(sid: int, pid: int) -> tuple[Response, int]:
    """Body: {path: 'egypt'}. Assigns every file currently in that folder to this playlist."""
    p = request.get_json(silent=True) or {}
    path = (p.get("path") or "").strip().strip("/")
    if not path:
        return _err("path required", "VALIDATION", 400)
    body = {"do": "playlist", "playlists": [pid], "files": [], "dirs": [path]}
    r = _az("PUT", f"/api/station/{sid}/files/batch", json=body)
    if not r.ok:
        return _err(r.text[:500], "AZURACAST_ERROR", r.status_code)
    try:
        res = r.json()
    except ValueError:
        res = {}
    return _ok({"assigned_dir": path, "result": res})


# ---------- API v1: station settings ----------------------------------------

@app.delete("/api/v1/stations/<int:sid>")
def api_delete_station(sid: int) -> tuple[Response, int]:
    return _proxy("DELETE", f"/api/admin/station/{sid}")


@app.put("/api/v1/stations/<int:sid>/settings")
def api_update_station(sid: int) -> tuple[Response, int]:
    """Body is a subset of the AzuraCast station record (admin)."""
    p = request.get_json(silent=True) or {}
    allowed = {
        "name", "short_name", "description", "url", "genre", "timezone", "max_listeners",
        "enable_streamers", "enable_requests", "enable_public_page", "enable_on_demand",
        "enable_on_demand_download", "enable_hls", "request_delay", "request_threshold",
        "api_history_items", "is_enabled",
    }
    body = {k: v for k, v in p.items() if k in allowed}
    if "backend_config" in p:
        body["backend_config"] = p["backend_config"]
    if not body:
        return _err("no allowed fields", "VALIDATION", 400)
    return _proxy("PUT", f"/api/admin/station/{sid}", json=body)


# ---------- API v1: podcast episodes ----------------------------------------

@app.get("/api/v1/stations/<int:sid>/podcasts/<podcast_id>/episodes")
def api_episodes(sid: int, podcast_id: str) -> tuple[Response, int]:
    return _proxy("GET", f"/api/station/{sid}/podcast/{podcast_id}/episodes")


@app.post("/api/v1/stations/<int:sid>/podcasts/<podcast_id>/episodes")
def api_create_episode(sid: int, podcast_id: str) -> tuple[Response, int]:
    p = request.get_json(silent=True) or {}
    body = {
        "title": p.get("title"),
        "description": p.get("description") or "",
        "explicit": bool(p.get("explicit", False)),
    }
    return _proxy("POST", f"/api/station/{sid}/podcast/{podcast_id}/episodes", json=body)


# ---------- API v1: recordings ----------------------------------------------

@app.get("/api/v1/stations/<int:sid>/recordings")
def api_recordings(sid: int) -> tuple[Response, int]:
    # AzuraCast records live DJ broadcasts; list them across all streamers.
    r = _az("GET", f"/api/station/{sid}/streamers/broadcasts")
    if not r.ok:
        return _ok([])  # feature off or no broadcasts — degrade gracefully
    try:
        return _ok(r.json())
    except ValueError:
        return _ok([])


# ---------- API v1: branding ------------------------------------------------

@app.get("/api/v1/stations/<int:sid>")
def api_station_detail(sid: int) -> tuple[Response, int]:
    return _proxy("GET", f"/api/admin/station/{sid}")


@app.put("/api/v1/stations/<int:sid>/branding")
def api_update_branding(sid: int) -> tuple[Response, int]:
    p = request.get_json(silent=True) or {}
    branding = {
        "default_album_art_url": p.get("default_album_art_url"),
        "offline_text": p.get("offline_text"),
        "public_custom_css": p.get("public_custom_css"),
        "public_custom_js": p.get("public_custom_js"),
    }
    return _proxy("PUT", f"/api/admin/station/{sid}", json={"branding_config": branding})


# ---------- API v1: backups -------------------------------------------------

@app.get("/api/v1/backups")
def api_backups() -> tuple[Response, int]:
    return _proxy("GET", "/api/admin/backups")


@app.post("/api/v1/backups")
def api_run_backup() -> tuple[Response, int]:
    p = request.get_json(silent=True) or {}
    body = {"path": p.get("path") or f"backup-{int(time.time())}.zip", "exclude_media": p.get("exclude_media", False)}
    return _proxy("POST", "/api/admin/backups/run", json=body)


# ---------- API v1: Station templates (personas now live in agents/) --------
# The 4 station × 4 show personas used to be a hardcoded dict here. They now live
# as editable files under agents/<station>/<show>.md and are rebuilt at request
# time by agents_engine.build_station_templates() — edit a persona file and it
# takes effect on the next call, no restart. See also /api/v1/agents (edit API).

@app.get("/api/v1/templates")
def api_templates() -> tuple[Response, int]:
    return _ok([{"key": k, **v} for k, v in build_station_templates().items()])


# ---------- API v1: AI Copilot heuristics (no LLM, fast) -------------------

@app.get("/api/v1/stations/<int:sid>/copilot")
def api_copilot(sid: int) -> tuple[Response, int]:
    """Analyze the station and return actionable suggestions. No LLM cost."""
    suggestions = []
    try:
        station = _az("GET", f"/api/admin/station/{sid}").json()
        playlists = _az("GET", f"/api/station/{sid}/playlists").json() or []
        queue = _az("GET", f"/api/station/{sid}/queue").json() or []
        history = _az("GET", f"/api/station/{sid}/history").json() or []
        streamers = _az("GET", f"/api/station/{sid}/streamers").json() or []
        webhooks = _az("GET", f"/api/station/{sid}/webhooks").json() or []
        mounts = _az("GET", f"/api/station/{sid}/mounts").json() or []
        files = _az("GET", f"/api/station/{sid}/files?per_page=500").json()
        files_count = (files.get("total") if isinstance(files, dict) else 0) or 0
    except Exception as e:
        return _err(f"data fetch failed: {str(e)[:200]}", "COPILOT_ERROR", 502)

    enabled_pls = [p for p in playlists if p.get("is_enabled")]
    empty_enabled = [p for p in enabled_pls if (p.get("num_songs") or 0) == 0]
    has_scheduled = sum(1 for p in enabled_pls if (p.get("schedule_items") or []))

    # 1. Empty queue
    if len(queue) == 0:
        suggestions.append({
            "id": "build_queue",
            "badge": "خطة اليوم", "badgeClass": "danger",
            "desc": "طابور البث فارغ تماماً! دعنا نملأ الساعات القادمة فوراً أو نطلق الـ Pipeline لبناء كتل إذاعية مبتكرة.",
            "action": {"method": "POST", "url": f"/api/v1/stations/{sid}/build-queue"},
        })

    # 2. No playlists with songs
    if not any((p.get("num_songs") or 0) > 0 for p in enabled_pls):
        suggestions.append({
            "id": "upload_media",
            "badge": "المكتبة الموسيقية", "badgeClass": "danger",
            "desc": "المكتبة الموسيقية صامتة وخالية! ما رأيك في استيراد أغانٍ جديدة من استوديو التلحين (ACE-Step) أو رفع مقاطع حصرية؟",
            "action": {"goto_tab": "media"},
        })

    # 3. Enabled but empty playlists
    for p in empty_enabled[:3]:
        suggestions.append({
            "id": f"fill_pl_{p.get('id')}",
            "badge": "فقرة مفقودة", "badgeClass": "warn",
            "desc": f"القائمة الموسيقية '{p.get('name')}' جاهزة للبث ولكنها تفتقر للمحتوى الموسيقي. ارفع إليها أغانٍ أو خصص لها نصوصاً.",
            "action": {"goto_tab": "playlists"},
        })

    # 4. No schedule yet
    if has_scheduled == 0 and len(enabled_pls) >= 2:
        suggestions.append({
            "id": "add_schedule",
            "badge": "تطوير البث", "badgeClass": "warn",
            "desc": "خريطة البث فارغة اليوم! وزّع فترات اليوم الإذاعي (صباحي، يومي، مسائي) لتناوب تلقائي بين المواد الغنائية والإخبارية.",
            "action": {"goto_tab": "schedule"},
        })

    # 5. Streamers feature on but no streamers
    if station.get("enable_streamers") and not streamers:
        suggestions.append({
            "id": "add_streamer",
            "badge": "بث مباشر", "badgeClass": "info",
            "desc": "استغل قدرتك على المقاطعة الحية! أنشئ حساب مذيع (DJ) لتمكين المذيعين أو بوتات الصوت الذكية من قطع البث المعتاد بفقرات حية.",
            "action": {"goto_tab": "broadcast"},
        })

    # 6. No webhooks
    if not webhooks:
        suggestions.append({
            "id": "add_webhook",
            "badge": "تفاعل الجمهور", "badgeClass": "muted",
            "desc": "جمهورك غائب عن جديدك! اربط ويب هوك (Discord/Slack) لإشعار المستمعين تلقائياً بمجرد انطلاق فقرة إذاعية ذكية جديدة.",
            "action": {"goto_tab": "broadcast"},
        })

    # 7. Only one mount (suggest HLS)
    if len(mounts) == 1:
        suggestions.append({
            "id": "add_mount",
            "badge": "جودة البث", "badgeClass": "muted",
            "desc": "سهّل الوصول للجميع! أضف نقطة بث رديفة (Mount) بجودة AAC أو HLS لتمكين المستمعين من الاستماع بسلاسة عبر الجوال.",
            "action": {"goto_tab": "broadcast"},
        })

    # 8. Few playlists overall
    if len(enabled_pls) < 3 and files_count >= 10:
        suggestions.append({
            "id": "more_playlists",
            "badge": "القدرات المخفية", "badgeClass": "info",
            "desc": "تجنب رتابة البث! وزع أغانيك على 3 قوائم متخصصة (صباحي هادئ، حماسي بعد الظهر، كلاسيك مسائي) لخلق إيقاع راديو متوازن.",
            "action": {"goto_tab": "playlists"},
        })

    # 9. AI block scheduling suggestion (New)
    today_str = time.strftime("%Y-%m-%d")
    try:
        p_today = _day_blocks_path(sid, today_str)
        has_today_blocks = False
        if p_today.exists():
            blocks = json.loads(p_today.read_text(encoding="utf-8"))
            if len(blocks) > 0:
                has_today_blocks = True
        if not has_today_blocks:
            suggestions.append({
                "id": "schedule_day_blocks",
                "badge": "توجيه إبداعي", "badgeClass": "warn",
                "desc": f"اليوم الإذاعي ({today_str}) غير مجدول بالكتل الزمنية الذكية. خطط كتل اليوم الآن في قسم الجدولة لعرضها على الخط الزمني.",
                "action": {"goto_tab": "schedule"},
            })
    except Exception:
        pass

    # 10. AI Personas / Script generation (New)
    suggestions.append({
        "id": "generate_ai_scripts",
        "badge": "شخصية إذاعية", "badgeClass": "info",
        "desc": "استخدم استوديو الكلمات لكتابة نصوص للمذيع الآلي لليوم، ثم استخدم شخصيات الراديو (Personas) لبث روح تفاعلية حية بين الأغاني.",
        "action": {"goto_tab": "studio"},
    })

    # 11. Snippet Recorder suggestion (New)
    suggestions.append({
        "id": "record_stream_snippets",
        "badge": "لمسة حية", "badgeClass": "muted",
        "desc": "سجّل فواصل إذاعية مميزة (Station IDs) مباشرة من البث المشغل بالأسفل وأعد رفعها لمكتبتك الموسيقية لربط الفقرات بذكاء.",
        "action": {"goto_tab": "live"},
    })

    # Score: weight by severity, base 100 minus penalties
    score = 100
    score -= sum({"danger": 20, "warn": 12, "info": 6, "muted": 3}.get(s.get("badgeClass", "muted"), 5) for s in suggestions)
    score = max(0, min(100, score))

    return _ok({
        "score": score,
        "status_text": "ممتاز" if score >= 90 else ("جيد" if score >= 70 else ("يحتاج تحسين" if score >= 50 else "ضعيف")),
        "suggestions": suggestions,
        "totals": {"playlists": len(playlists), "enabled": len(enabled_pls), "files": files_count, "queue": len(queue), "history": len(history)},
    })


# ---------- API v1: Local services (VoiceBox / ACE-Step / Guide / FreeLLM) ----

@app.get("/api/v1/voices")
def api_voices() -> tuple[Response, int]:
    # Prefer the Guide proxy (handles fallback); else direct VoiceBox; else stub.
    r = _guide("GET", "/api/radio/voices")
    if r is not None and r.ok:
        body = r.json()
        profiles = body.get("profiles") if isinstance(body, dict) else body
        return _ok(profiles or [])
    if VOICEBOX_BASE_URL:
        for path in ("/api/profiles", "/profiles"):
            try:
                vr = _integ.get(VOICEBOX_BASE_URL + path, headers=_vb_headers(), timeout=10)
                if vr.ok:
                    return _ok(vr.json())
            except Exception:
                continue
    return _ok([
        {"id": "voice_default_male", "name": "صوت رجالي افتراضي", "lang": "ar", "provider": "stub"},
        {"id": "voice_default_female", "name": "صوت نسائي افتراضي", "lang": "ar", "provider": "stub"},
    ])


@app.get("/api/v1/music-library")
def api_music_library() -> tuple[Response, int]:
    limit = int(request.args.get("limit", "30"))
    # 1) Guide proxy if configured
    r = _guide("GET", f"/api/music-brand/songs?limit={limit}")
    if r is not None and r.ok:
        body = r.json()
        songs = body.get("songs") if isinstance(body, dict) else body
        return _ok(songs or [], meta={"source": (body.get("source") if isinstance(body, dict) else None)})
    # 2) Direct songs.* service (needs ACE_STEP_TOKEN; Cloudflare needs the browser UA)
    if ACE_STEP_BASE_URL:
        for path in (f"/api/songs?limit={limit}", f"/api/music-brand/songs?limit={limit}"):
            try:
                sr = _integ.get(ACE_STEP_BASE_URL + path, headers=_ace_headers(), timeout=12)
                if sr.ok:
                    body = sr.json()
                    songs = body.get("songs") if isinstance(body, dict) else body
                    return _ok(songs or [], meta={"source": "ace_step"})
            except Exception:
                continue
    return _ok([])


@app.post("/api/v1/music-library/inject")
def api_inject_music() -> tuple[Response, int]:
    """Download an ACE-Step song and upload it directly to AzuraCast via our own /files endpoint."""
    p = request.get_json(silent=True) or {}
    audio_url = (p.get("audio_url") or "").strip()
    song_id = (p.get("song_id") or "").strip()
    title = (p.get("title") or "track").strip()
    sid = int(p.get("station_id") or 1)
    if not audio_url:
        return _err("audio_url required", "VALIDATION", 400)
    # ACE-Step container hosts the audio. Try several common URL forms.
    candidates = []
    if audio_url.startswith("http"):
        candidates.append(audio_url)
    elif ACE_STEP_BASE_URL:
        candidates.append(ACE_STEP_BASE_URL + (audio_url if audio_url.startswith("/") else "/" + audio_url))
    elif GUIDE_BASE_URL:
        # Try the typical ACE-Step IP-based forms used by the guide app's inject-song
        for base in ("http://192.168.70.164:3001", "http://192.168.70.164:3000", "http://192.168.70.164"):
            candidates.append(base + (audio_url if audio_url.startswith("/") else "/" + audio_url))
    content = None
    used = None
    for url in candidates:
        try:
            rr = _integ.get(url, headers=_ace_headers(), timeout=20)
            if rr.ok and len(rr.content) > 1024:
                content = rr.content
                used = url
                break
        except Exception:
            continue
    if not content:
        return _err("could not fetch audio from ACE-Step", "ACE_FETCH", 502)
    # Upload to AzuraCast via JSON base64 endpoint
    safe_title = "".join(c for c in title if c.isalnum() or c in (" ", "_", "-")).strip().replace(" ", "_") or "track"
    filename = f"acestep_{song_id or int(time.time())}_{safe_title}.mp3"
    body = {"path": f"generated/{filename}", "file": base64.b64encode(content).decode("ascii")}
    r = _az("POST", f"/api/station/{sid}/files", json=body, headers={"Content-Type": "application/json"})
    if not r.ok:
        return _err(r.text[:500], "AZURACAST_ERROR", r.status_code)
    return _ok({"filename": filename, "source": used, "azuracast": r.json()})


@app.post("/api/v1/voicebox/speak")
def api_voicebox_speak() -> tuple[Response, int]:
    """Generate TTS via VoiceBox and upload result to AzuraCast as a media file."""
    p = request.get_json(silent=True) or {}
    text = (p.get("text") or "").strip()
    voice = (p.get("voice") or "").strip()
    sid = int(p.get("station_id") or 1)
    if not text:
        return _err("text required", "VALIDATION", 400)
    if not VOICEBOX_BASE_URL and not GUIDE_BASE_URL:
        return _err("VoiceBox not configured", "AI_DISABLED", 503)
    # Try Guide proxy first (if it exposes a synth endpoint), else direct VoiceBox
    audio_bytes = None
    if GUIDE_BASE_URL:
        try:
            h = {"X-Guide-Token": GUIDE_TOKEN} if GUIDE_TOKEN else {}
            rr = _integ.post(GUIDE_BASE_URL + "/api/radio/voicebox/speak", json={"text": text, "voice": voice}, headers=h, timeout=60)
            if rr.ok and rr.headers.get("content-type", "").startswith("audio/"):
                audio_bytes = rr.content
        except Exception:
            audio_bytes = None
    if not audio_bytes and VOICEBOX_BASE_URL:
        try:
            rr = _integ.post(VOICEBOX_BASE_URL + "/api/synth", json={"text": text, "voice": voice}, headers=_vb_headers(), timeout=60)
            if rr.ok:
                audio_bytes = rr.content
        except Exception:
            audio_bytes = None
    if not audio_bytes:
        return _err("voicebox call failed", "VOICEBOX_ERROR", 502)
    safe = "".join(c for c in text[:32] if c.isalnum() or c in " _-").strip().replace(" ", "_") or "voice"
    filename = f"voice_{int(time.time())}_{safe}.mp3"
    body = {"path": f"voices/{filename}", "file": base64.b64encode(audio_bytes).decode("ascii")}
    r = _az("POST", f"/api/station/{sid}/files", json=body, headers={"Content-Type": "application/json"})
    if not r.ok:
        return _err(r.text[:500], "AZURACAST_ERROR", r.status_code)
    return _ok({"filename": filename, "bytes": len(audio_bytes)})


def generate_script_text(show: dict, date: str) -> dict:
    """Generate a show script from a show persona: FreeLLM → OpenAI fallback.

    Returns {script, provider, model}. Raises RuntimeError if no LLM is configured.
    Reused by /api/v1/generate-script and the Telegram broadcaster.
    """
    sys_prompt = show.get("system_prompt") or "أنت مذيع إذاعي عربي."
    usr_template = show.get("user_prompt_template") or "اكتب فقرة قصيرة ليوم {date}."
    user_prompt = usr_template.replace("{date}", date)
    temperature = float(show.get("temperature") or 0.6)
    model_override = show.get("model") or ""

    if FREELLM_BASE_URL:
        try:
            rr = _integ.post(
                FREELLM_BASE_URL + "/v1/chat/completions",
                json={
                    "model": model_override or "auto:fast",
                    "messages": [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": temperature,
                },
                headers={"Authorization": f"Bearer {FREELLM_API_KEY}"} if FREELLM_API_KEY else {},
                timeout=60,
            )
            if rr.ok:
                j = rr.json()
                text = (j.get("choices") or [{}])[0].get("message", {}).get("content", "")
                return {"script": text, "provider": "freellm", "model": j.get("model")}
            log.warning("freellm HTTP %s — body: %s", rr.status_code, rr.text[:300])
        except Exception as e:
            log.warning("freellm exception: %s", e)

    if OPENAI_API_KEY:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        resp = client.chat.completions.create(
            model=model_override or OPENAI_MODEL,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
        )
        return {"script": resp.choices[0].message.content, "provider": "openai", "model": model_override or OPENAI_MODEL}

    raise RuntimeError("no LLM configured")


@app.post("/api/v1/generate-script")
def api_generate_script() -> tuple[Response, int]:
    """Generate a show script using the show's prompts (FreeLLM → OpenAI)."""
    p = request.get_json(silent=True) or {}
    show = p.get("show") or {}
    if p.get("model") and not show.get("model"):  # preserve top-level model override
        show = {**show, "model": p["model"]}
    date = p.get("date") or time.strftime("%Y-%m-%d")
    try:
        return _ok(generate_script_text(show, date))
    except RuntimeError as e:
        return _err(str(e), "AI_DISABLED", 503)
    except Exception as e:
        return _err(f"ai error: {str(e)[:200]}", "AI_ERROR", 502)


@app.get("/api/v1/weather")
def api_weather() -> tuple[Response, int]:
    city = request.args.get("city") or "Cairo"
    r = _guide("GET", f"/api/radio/weather?city={city}")
    if r is not None and r.ok:
        return _ok(r.json())
    # Direct wttr.in fallback
    try:
        wr = requests.get(f"https://wttr.in/{city}?format=j1&lang=ar", headers={"User-Agent": "radio-control/1.0"}, timeout=8)
        if wr.ok:
            data = wr.json()
            current = (data.get("current_condition") or [{}])[0]
            return _ok({
                "city": city, "ok": True,
                "current": {
                    "temp_c": current.get("temp_C"),
                    "description_ar": (current.get("lang_ar") or [{}])[0].get("value") if current.get("lang_ar") else current.get("weatherDesc", [{}])[0].get("value"),
                    "feels_like_c": current.get("FeelsLikeC"),
                    "humidity": current.get("humidity"),
                },
            })
    except Exception as e:
        log.warning("wttr fail: %s", e)
    return _ok({"city": city, "current": {"temp_c": None, "description_ar": "—"}})


@app.get("/api/v1/scripts")
def api_scripts_list() -> tuple[Response, int]:
    """Proxy the Guide's stored radio scripts registry."""
    params = request.args.to_dict()
    r = _guide("GET", "/api/radio/scripts", params=params)
    if r is None:
        return _ok({})
    if not r.ok:
        return _err(r.text[:500], "GUIDE_ERROR", r.status_code)
    return _ok(r.json())


@app.post("/api/v1/scripts")
def api_scripts_save() -> tuple[Response, int]:
    """Save/update a generated or edited talk show script in the registry."""
    body = request.get_json(silent=True) or {}
    r = _guide("POST", "/api/radio/scripts", json=body)
    if r is None:
        return _err("Guide integration not configured", "GUIDE_UNAVAILABLE", 503)
    if not r.ok:
        return _err(r.text[:500], "GUIDE_ERROR", r.status_code)
    return _ok(r.json())


@app.delete("/api/v1/scripts")
def api_scripts_delete() -> tuple[Response, int]:
    """Delete a generated talk show script from the registry."""
    params = request.args.to_dict()
    r = _guide("DELETE", "/api/radio/scripts", params=params)
    if r is None:
        return _err("Guide integration not configured", "GUIDE_UNAVAILABLE", 503)
    if not r.ok:
        return _err(r.text[:500], "GUIDE_ERROR", r.status_code)
    return _ok(r.json())


@app.get("/api/v1/radio/stations")
def api_radio_stations_proxy() -> tuple[Response, int]:
    """Proxy configured AI radio stations from the Guide."""
    r = _guide("GET", "/api/radio/stations")
    if r is None:
        return _ok({"stations": {}})
    if not r.ok:
        return _err(r.text[:500], "GUIDE_ERROR", r.status_code)
    return _ok(r.json())


@app.post("/api/v1/radio/stations")
def api_radio_stations_save_proxy() -> tuple[Response, int]:
    """Proxy station configuration save/update to the Guide."""
    body = request.get_json(silent=True) or {}
    r = _guide("POST", "/api/radio/stations", json=body)
    if r is None:
        return _err("Guide integration not configured", "GUIDE_UNAVAILABLE", 503)
    if not r.ok:
        return _err(r.text[:500], "GUIDE_ERROR", r.status_code)
    return _ok(r.json())


@app.delete("/api/v1/radio/stations/<shortcode>")
def api_radio_stations_delete_proxy(shortcode: str) -> tuple[Response, int]:
    """Proxy station configuration delete to the Guide."""
    r = _guide("DELETE", f"/api/radio/stations/{shortcode}")
    if r is None:
        return _err("Guide integration not configured", "GUIDE_UNAVAILABLE", 503)
    if not r.ok:
        return _err(r.text[:500], "GUIDE_ERROR", r.status_code)
    return _ok(r.json())


@app.post("/api/v1/run/<action_id>")
def api_run_action(action_id: str) -> tuple[Response, int]:
    """Proxy the Guide's background action runner (e.g. run-radio-pipeline)."""
    body = request.get_json(silent=True) or {}
    r = _guide("POST", f"/api/run/{action_id}", json=body)
    if r is None:
        return _err("Guide integration not configured", "GUIDE_UNAVAILABLE", 503)
    if not r.ok:
        return _err(r.text[:500], "GUIDE_ERROR", r.status_code)
    return _ok(r.json())


@app.get("/api/v1/runs/<run_id>")
def api_run_status(run_id: str) -> tuple[Response, int]:
    """Proxy the Guide's background action status/output logs."""
    r = _guide("GET", f"/api/runs/{run_id}")
    if r is None:
        return _err("Guide integration not configured", "GUIDE_UNAVAILABLE", 503)
    if not r.ok:
        return _err(r.text[:500], "GUIDE_ERROR", r.status_code)
    return _ok(r.json())


# ---------- API v1: AI assistant --------------------------------------------

# Tool registry — each tool is a small Python function that hits AzuraCast.
# The LLM gets these as tool/function definitions; we dispatch by name.

def _tool_get_station_status(station_id: int) -> dict:
    s = _az("GET", f"/api/admin/station/{station_id}").json()
    np = _az("GET", f"/api/nowplaying/{station_id}").json()
    return {
        "name": s.get("name"),
        "is_enabled": s.get("is_enabled"),
        "is_online": np.get("is_online") if isinstance(np, dict) else None,
        "current_song": (np.get("now_playing") or {}).get("song", {}).get("text") if isinstance(np, dict) else None,
        "listeners": (np.get("listeners") or {}).get("current") if isinstance(np, dict) else 0,
    }


def _tool_get_media(station_id: int, region: str | None = None) -> dict:
    r = _az("GET", f"/api/station/{station_id}/files?per_page=200").json()
    rows = r.get("rows", []) if isinstance(r, dict) else []
    items = []
    for it in rows:
        path = it.get("path", "")
        reg = path.split("/")[0] if "/" in path else "global"
        if region and reg != region:
            continue
        items.append({"id": it.get("id"), "title": it.get("title"), "artist": it.get("artist"), "region": reg, "length": it.get("length")})
    return {"count": len(items), "items": items[:30]}


def _tool_get_playlists(station_id: int) -> dict:
    r = _az("GET", f"/api/station/{station_id}/playlists").json()
    return {"playlists": [{"id": p.get("id"), "name": p.get("name"), "is_enabled": p.get("is_enabled"), "weight": p.get("weight"), "num_songs": p.get("num_songs"), "schedule_items": p.get("schedule_items") or []} for p in (r or [])]}


def _tool_create_playlist(station_id: int, name: str, weight: int = 3, type_: str = "default") -> dict:
    r = _az("POST", f"/api/station/{station_id}/playlists", json={"name": name, "type": type_, "source": "songs", "order": "shuffle", "is_enabled": True, "weight": weight}).json()
    return {"created": bool(r.get("id")), "playlist": r}


def _tool_schedule_playlist(station_id: int, playlist_id: int, start_time: int, end_time: int, days: list[int]) -> dict:
    cur = _az("GET", f"/api/station/{station_id}/playlist/{playlist_id}").json()
    existing = cur.get("schedule_items") or []
    existing.append({"start_time": start_time, "end_time": end_time, "days": days, "loop_once": False})
    r = _az("PUT", f"/api/station/{station_id}/playlist/{playlist_id}", json={"schedule_items": existing})
    return {"ok": r.ok, "count": len(existing)}


def _tool_build_queue(station_id: int) -> dict:
    results = [_az("PUT", f"/api/admin/debug/station/{station_id}/nextsong").ok for _ in range(4)]
    return {"runs": sum(results), "total": len(results)}


def _tool_skip_song(station_id: int) -> dict:
    r = _az("POST", f"/api/station/{station_id}/backend/skip")
    return {"ok": r.ok}


def _tool_get_top_songs(station_id: int) -> dict:
    r = _az("GET", f"/api/station/{station_id}/reports/best").json()
    rows = r if isinstance(r, list) else (r.get("songs") if isinstance(r, dict) else []) or []
    return {"songs": [{"title": (s.get("song") or {}).get("text"), "plays": s.get("plays")} for s in rows[:10]]}


def _tool_get_history(station_id: int, limit: int = 10) -> dict:
    r = _az("GET", f"/api/station/{station_id}/history").json()
    rows = r if isinstance(r, list) else []
    return {"history": [{"song": (h.get("song") or {}).get("text"), "played_at": h.get("played_at"), "playlist": h.get("playlist")} for h in rows[:limit]]}


def _tool_set_station_setting(station_id: int, **kwargs) -> dict:
    allowed = {"max_listeners", "enable_streamers", "enable_requests", "enable_public_page", "enable_on_demand", "is_enabled", "description"}
    body = {k: v for k, v in kwargs.items() if k in allowed}
    if not body:
        return {"ok": False, "error": "no allowed fields"}
    r = _az("PUT", f"/api/admin/station/{station_id}", json=body)
    return {"ok": r.ok, "applied": list(body.keys())}


def _tool_get_day_blocks(station_id: int, date: str) -> dict:
    p = _day_blocks_path(station_id, date)
    if not p.exists():
        return {"blocks": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {"blocks": data}
    except Exception as e:
        return {"error": str(e)}


def _tool_save_day_block(station_id: int, date: str, type_: str, title: str, start_hour: int, duration_mins: int = 60, show_key: str | None = None) -> dict:
    p = _day_blocks_path(station_id, date)
    blocks = []
    if p.exists():
        try:
            blocks = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    import random
    import time
    new_id = f"b_{int(time.time())}_{random.randint(1000, 9999)}"
    blocks.append({
        "id": new_id,
        "type": type_,
        "title": title,
        "startHour": start_hour,
        "durationMins": duration_mins,
        "data": None,
        "showKey": show_key
    })
    p.write_text(json.dumps(blocks, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"saved": True, "block_id": new_id}


def _tool_get_script(station: str, date: str, show: str) -> dict:
    r = _guide("GET", "/api/radio/scripts", params={"station": station, "date": date, "show": show})
    if r and r.ok:
        return {"script": r.json()}
    return {"error": "Script not found or connection error"}


def _tool_save_script(station: str, date: str, show: str, script: str) -> dict:
    body = {
        "station": station,
        "date": date,
        "show": show,
        "script": script,
        "date_str": date
    }
    r = _guide("POST", "/api/radio/scripts", json=body)
    if r and r.ok:
        return {"saved": True}
    return {"error": r.text if r else "Guide connection error"}


def _tool_run_radio_pipeline(station: str, date: str, show: str = "all", mode: str = "full") -> dict:
    args = ["--station", station, "--date", date, "--show", show]
    if mode == "text":
        args.append("--text-only")
    elif mode == "audio":
        args.append("--audio-only")
    r = _guide("POST", "/api/run/run-radio-pipeline", json={"args": args})
    if r and r.ok:
        return {"started": True, "run_id": r.json().get("run_id")}
    return {"started": False, "error": r.text if r else "Guide connection error"}


TOOL_REGISTRY = {
    "get_station_status": _tool_get_station_status,
    "get_media": _tool_get_media,
    "get_playlists": _tool_get_playlists,
    "create_playlist": _tool_create_playlist,
    "schedule_playlist": _tool_schedule_playlist,
    "build_queue": _tool_build_queue,
    "skip_song": _tool_skip_song,
    "get_top_songs": _tool_get_top_songs,
    "get_history": _tool_get_history,
    "set_station_setting": _tool_set_station_setting,
    "get_day_blocks": _tool_get_day_blocks,
    "save_day_block": _tool_save_day_block,
    "get_script": _tool_get_script,
    "save_script": _tool_save_script,
    "run_radio_pipeline": _tool_run_radio_pipeline,
}

TOOL_SPECS = [
    {"type": "function", "function": {"name": "get_station_status", "description": "Get current station status: is it online, current song, listener count.", "parameters": {"type": "object", "properties": {"station_id": {"type": "integer"}}, "required": ["station_id"]}}},
    {"type": "function", "function": {"name": "get_media", "description": "List media files in the station, optionally filtered by region (egypt/gulf/levant/maghreb/global).", "parameters": {"type": "object", "properties": {"station_id": {"type": "integer"}, "region": {"type": "string"}}, "required": ["station_id"]}}},
    {"type": "function", "function": {"name": "get_playlists", "description": "List station playlists with their weights, enabled state, song counts, and schedule blocks.", "parameters": {"type": "object", "properties": {"station_id": {"type": "integer"}}, "required": ["station_id"]}}},
    {"type": "function", "function": {"name": "create_playlist", "description": "Create a new playlist in the station.", "parameters": {"type": "object", "properties": {"station_id": {"type": "integer"}, "name": {"type": "string"}, "weight": {"type": "integer", "default": 3}, "type_": {"type": "string", "enum": ["default", "once_per_hour", "once_per_x_songs", "once_per_x_minutes"], "default": "default"}}, "required": ["station_id", "name"]}}},
    {"type": "function", "function": {"name": "schedule_playlist", "description": "Add a weekly schedule block to a playlist. Time is HHMM integer (08:00 → 800, 17:30 → 1730). days uses ISO 1=Mon ... 7=Sun.", "parameters": {"type": "object", "properties": {"station_id": {"type": "integer"}, "playlist_id": {"type": "integer"}, "start_time": {"type": "integer"}, "end_time": {"type": "integer"}, "days": {"type": "array", "items": {"type": "integer"}}}, "required": ["station_id", "playlist_id", "start_time", "end_time", "days"]}}},
    {"type": "function", "function": {"name": "build_queue", "description": "Force-populate the AutoDJ queue with 4 next songs.", "parameters": {"type": "object", "properties": {"station_id": {"type": "integer"}}, "required": ["station_id"]}}},
    {"type": "function", "function": {"name": "skip_song", "description": "Skip the currently playing song.", "parameters": {"type": "object", "properties": {"station_id": {"type": "integer"}}, "required": ["station_id"]}}},
    {"type": "function", "function": {"name": "get_top_songs", "description": "Get the most-played songs report.", "parameters": {"type": "object", "properties": {"station_id": {"type": "integer"}}, "required": ["station_id"]}}},
    {"type": "function", "function": {"name": "get_history", "description": "Get recent play history.", "parameters": {"type": "object", "properties": {"station_id": {"type": "integer"}, "limit": {"type": "integer", "default": 10}}, "required": ["station_id"]}}},
    {"type": "function", "function": {"name": "set_station_setting", "description": "Update station settings (max_listeners, enable_streamers, enable_requests, enable_public_page, enable_on_demand, is_enabled, description).", "parameters": {"type": "object", "properties": {"station_id": {"type": "integer"}, "max_listeners": {"type": "integer"}, "enable_streamers": {"type": "boolean"}, "enable_requests": {"type": "boolean"}, "enable_public_page": {"type": "boolean"}, "enable_on_demand": {"type": "boolean"}, "is_enabled": {"type": "boolean"}, "description": {"type": "string"}}, "required": ["station_id"]}}},
    {"type": "function", "function": {"name": "get_day_blocks", "description": "Get all scheduled timeline day blocks for a station and date.", "parameters": {"type": "object", "properties": {"station_id": {"type": "integer"}, "date": {"type": "string", "description": "Date in YYYY-MM-DD format"}}, "required": ["station_id", "date"]}}},
    {"type": "function", "function": {"name": "save_day_block", "description": "Add a new block to the timeline.", "parameters": {"type": "object", "properties": {"station_id": {"type": "integer"}, "date": {"type": "string", "description": "Date in YYYY-MM-DD format"}, "type_": {"type": "string", "enum": ["music", "weather", "voice", "ai-show"]}, "title": {"type": "string"}, "start_hour": {"type": "integer", "description": "0 to 23 start hour"}, "duration_mins": {"type": "integer", "default": 60}, "show_key": {"type": "string", "description": "Show identifier morning/daily/afternoon/evening/night"}}, "required": ["station_id", "date", "type_", "title", "start_hour"]}}},
    {"type": "function", "function": {"name": "get_script", "description": "Retrieve a generated show script by station, date, and show slot.", "parameters": {"type": "object", "properties": {"station": {"type": "string", "description": "Station short name (e.g. egypt, motivation)"}, "date": {"type": "string", "description": "Date in YYYY-MM-DD format"}, "show": {"type": "string", "description": "Show identifier morning/daily/afternoon/evening/night"}}, "required": ["station", "date", "show"]}}},
    {"type": "function", "function": {"name": "save_script", "description": "Save or update a show script.", "parameters": {"type": "object", "properties": {"station": {"type": "string", "description": "Station short name"}, "date": {"type": "string", "description": "Date in YYYY-MM-DD format"}, "show": {"type": "string"}, "script": {"type": "string"}}, "required": ["station", "date", "show", "script"]}}},
    {"type": "function", "function": {"name": "run_radio_pipeline", "description": "Trigger the background radio generation pipeline to generate/broadcast shows.", "parameters": {"type": "object", "properties": {"station": {"type": "string", "description": "Station short name"}, "date": {"type": "string", "description": "Date in YYYY-MM-DD format"}, "show": {"type": "string", "default": "all", "description": "Show identifier or 'all'"}, "mode": {"type": "string", "enum": ["full", "text", "audio"], "default": "full"}}, "required": ["station", "date"]}}},
]


def _ai_system_prompt(lang: str, context: dict) -> str:
    if lang == "ar":
        return (
            "أنت مساعد ذكي للوحة تحكم راديو على AzuraCast. تتحدث العربية بشكل افتراضي. "
            "تستطيع تنفيذ إجراءات حقيقية عبر الأدوات المتاحة. كن مختصراً ومباشراً. "
            "بعد تنفيذ أي إجراء، أخبر المستخدم بالنتيجة بسطر واحد. "
            "السياق الحالي: " + json.dumps(context, ensure_ascii=False)
        )
    return (
        "You are an AI assistant for an AzuraCast radio control panel. Be concise. "
        "You can perform real actions via the provided tools. After each action, report the outcome in one sentence. "
        "Current context: " + json.dumps(context)
    )


@app.post("/api/v1/ai/chat")
def api_ai_chat() -> tuple[Response, int]:
    if not OPENAI_API_KEY:
        return _err("OPENAI_API_KEY not configured", "AI_DISABLED", 503)
    try:
        from openai import OpenAI
    except ImportError:
        return _err("openai package not installed", "AI_DISABLED", 503)

    payload = request.get_json(silent=True) or {}
    messages_in = payload.get("messages") or []
    context = payload.get("context") or {}
    lang = payload.get("lang", DEFAULT_LANG)

    client = OpenAI(api_key=OPENAI_API_KEY)
    messages = [{"role": "system", "content": _ai_system_prompt(lang, context)}] + messages_in

    # Allow up to 5 tool-call iterations
    for _ in range(5):
        try:
            resp = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages,
                tools=TOOL_SPECS,
                tool_choice="auto",
                temperature=0.4,
            )
        except Exception as e:
            return _err(f"OpenAI error: {str(e)[:200]}", "AI_ERROR", 502)

        msg = resp.choices[0].message
        messages.append({"role": "assistant", "content": msg.content, "tool_calls": [{"id": t.id, "type": "function", "function": {"name": t.function.name, "arguments": t.function.arguments}} for t in (msg.tool_calls or [])] or None})

        if not msg.tool_calls:
            return _ok({"reply": msg.content, "messages": messages_in + [{"role": "assistant", "content": msg.content}]})

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:
                args = {}
            handler = TOOL_REGISTRY.get(name)
            if not handler:
                result = {"error": f"unknown tool {name}"}
            else:
                try:
                    result = handler(**args)
                except Exception as e:
                    result = {"error": str(e)[:200]}
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result, ensure_ascii=False)})

    return _err("Too many tool iterations", "AI_LOOP", 502)


# ---------- Studio API (Merged) --------------------------------

import studio_runner  # noqa: E402


@app.get("/api/v1/studio/agents")
def api_studio_agents_list():
    return _ok(studio_runner.list_agents())


@app.post("/api/v1/studio/agents")
def api_studio_agent_create():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    if not name:
        return _err("name required", "VALIDATION")
    try:
        return _ok(studio_runner.create_raw(name, payload.get("content")))
    except studio_runner.AgentError as e:
        return _err(str(e), e.code or "CREATE_FAILED", 409 if e.code == "EXISTS" else 400)


@app.get("/api/v1/studio/agents/<name>")
def api_studio_agent_get(name):
    try:
        return _ok(studio_runner.load_raw(name))
    except studio_runner.AgentError as e:
        return _err(str(e), e.code or "NOT_FOUND", 404)


@app.put("/api/v1/studio/agents/<name>")
def api_studio_agent_put(name):
    payload = request.get_json(silent=True) or {}
    content = payload.get("content") or ""
    if not content.strip():
        return _err("content is required", "EMPTY")
    try:
        studio_runner.save_raw(name, content)
        return _ok({"name": name, "saved": True})
    except studio_runner.AgentError as e:
        return _err(str(e), e.code or "SAVE_FAILED")


@app.delete("/api/v1/studio/agents/<name>")
def api_studio_agent_delete(name):
    try:
        studio_runner.delete_raw(name)
        return _ok({"name": name, "deleted": True})
    except studio_runner.AgentError as e:
        return _err(str(e), e.code or "NOT_FOUND", 404)


@app.post("/api/v1/studio/run")
def api_studio_run():
    payload = request.get_json(silent=True) or {}
    agent = payload.get("agent")
    user_input = payload.get("input")
    if not agent:
        return _err("agent required", "VALIDATION")
    if not user_input or not user_input.strip():
        return _err("input is required", "EMPTY_INPUT")
    try:
        return _ok(studio_runner.run_agent(agent, user_input))
    except studio_runner.AgentError as e:
        status = 503 if e.code == "NO_API_KEY" else 400
        return _err(str(e), e.code or "RUN_FAILED", status)

# ---------- background queue keeper -----------------------------------------

def _queue_keeper_loop() -> None:
    STATE_DIR = Path("agents/.queue_state")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            if API_KEY:
                r = _az("GET", "/api/admin/stations")
                if r.ok:
                    for st in r.json() or []:
                        if not st.get("is_enabled"):
                            continue
                        sid = st["id"]
                        # Claim this timestamp bucket to prevent other workers from double-calling nextsong
                        now_bucket = int(time.time() / QUEUE_BUILDER_INTERVAL)
                        marker = STATE_DIR / f"{sid}_{now_bucket}.lock"
                        try:
                            fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                            os.close(fd)
                            # Clean up old lock files
                            for f in STATE_DIR.glob(f"{sid}_*.lock"):
                                try:
                                    bucket_part = int(f.stem.split("_")[1])
                                    if bucket_part < now_bucket - 2:
                                        f.unlink()
                                except Exception:
                                    pass
                        except FileExistsError:
                            continue  # Already claimed by another worker
                        _az("PUT", f"/api/admin/debug/station/{sid}/nextsong")
        except Exception as e:
            log.warning("queue keeper: %s", e)
        time.sleep(QUEUE_BUILDER_INTERVAL)


def _start_queue_keeper() -> None:
    if os.environ.get("WERKZEUG_RUN_MAIN") == "false":
        return
    if not API_KEY:
        return
    threading.Thread(target=_queue_keeper_loop, name="queue-keeper", daemon=True).start()


def _broadcast_loop() -> None:
    """Fire due show_start Telegram rules — checked once per minute."""
    last = None
    while True:
        try:
            now = time.strftime("%H%M")
            if now != last:
                last = now
                _broadcast_tick(now, time.strftime("%Y-%m-%d"))
        except Exception as e:
            log.warning("broadcaster: %s", e)
        time.sleep(20)


def _start_broadcaster() -> None:
    if os.environ.get("WERKZEUG_RUN_MAIN") == "false":
        return
    if not os.environ.get("TELEGRAM_BOT_TOKEN", "").strip():
        return  # dormant until a bot token is configured
    threading.Thread(target=_broadcast_loop, name="tg-broadcaster", daemon=True).start()


_start_queue_keeper()
_start_broadcaster()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "4180"))
    app.run(host="0.0.0.0", port=port, debug=DEBUG)
