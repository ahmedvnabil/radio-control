"""
File-based show agents.

The 4 station templates × 4 shows used to be a hardcoded STATION_TEMPLATES dict in
app.py. They now live as editable files under agents/:

    agents/stations.json            # ordered [{key, name, description}]
    agents/<station>/<show>.md      # frontmatter (times, user_prompt_template, model,
                                    # temperature) + body = the persona system_prompt

`build_station_templates()` rebuilds the exact structure app.py used to serve, reading
the files AT REQUEST TIME — so editing a persona file takes effect on the next call,
no restart. The Blueprint exposes read/edit endpoints so a UI can manage personas.
"""
import json
import re
from pathlib import Path

import frontmatter
from flask import Blueprint, jsonify, request

AGENTS_DIR = (Path(__file__).resolve().parent / "agents").resolve()
SHOW_ORDER = ["morning", "afternoon", "evening", "night"]
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


# ---------- envelope (local, to avoid importing app.py → circular) ----------

def _ok(data=None, meta=None):
    return jsonify({"ok": True, "data": data, "error": None, "meta": meta or {}}), 200


def _err(message, code="ERROR", status=400):
    return jsonify({"ok": False, "data": None, "error": message, "code": code}), status


# ---------- file helpers ----------------------------------------------------

def _time(v) -> str:
    """Times are 4-char HHMM strings. Coerce ints/loose YAML back to '0830' form."""
    if v is None or v == "":
        return ""
    return str(v).zfill(4)


def _stations_meta() -> list:
    f = AGENTS_DIR / "stations.json"
    if not f.exists():
        return []
    return json.loads(f.read_text(encoding="utf-8"))


def _show_files(station_dir: Path):
    files = {p.stem: p for p in station_dir.glob("*.md")}
    ordered = [s for s in SHOW_ORDER if s in files] + sorted(k for k in files if k not in SHOW_ORDER)
    return [(k, files[k]) for k in ordered]


def _agent_path(station: str, show: str) -> Path:
    if not (NAME_RE.match(station) and NAME_RE.match(show)):
        raise ValueError("invalid name")
    p = (AGENTS_DIR / station / f"{show}.md").resolve()
    if p.parent.parent != AGENTS_DIR:  # block traversal outside agents/<station>/
        raise ValueError("path traversal blocked")
    return p


def build_station_templates() -> dict:
    """Rebuild the STATION_TEMPLATES structure from files (hot reload)."""
    out = {}
    for st in _stations_meta():
        sdir = AGENTS_DIR / st["key"]
        if not sdir.is_dir():
            continue
        shows = {}
        for shkey, path in _show_files(sdir):
            m = frontmatter.load(path)
            shows[shkey] = {
                "description": m.get("description", ""),
                "start_time": _time(m.get("start_time")),
                "end_time": _time(m.get("end_time")),
                "system_prompt": m.content.strip(),
                "user_prompt_template": m.get("user_prompt_template", ""),
                "model": m.get("model", "") or "",
                "temperature": m.get("temperature", 0.6),
            }
        out[st["key"]] = {"name": st["name"], "description": st["description"], "shows": shows}
    return out


def list_agents() -> list:
    """Flat list for an editor UI."""
    items = []
    for skey, sdata in build_station_templates().items():
        for shkey, sh in sdata["shows"].items():
            items.append({
                "station": skey, "station_name": sdata["name"],
                "show": shkey, "description": sh["description"],
                "start_time": sh["start_time"], "end_time": sh["end_time"],
                "model": sh["model"], "temperature": sh["temperature"],
            })
    return items


# ---------- blueprint -------------------------------------------------------

agents_bp = Blueprint("agents", __name__)


@agents_bp.get("/api/v1/agents")
def api_agents_list():
    return _ok(list_agents())


@agents_bp.get("/api/v1/agents/<station>/<show>")
def api_agent_get(station, show):
    try:
        p = _agent_path(station, show)
    except ValueError as e:
        return _err(str(e), "BAD_NAME")
    if not p.exists():
        return _err("agent not found", "NOT_FOUND", 404)
    return _ok({"station": station, "show": show, "content": p.read_text(encoding="utf-8")})


@agents_bp.put("/api/v1/agents/<station>/<show>")
def api_agent_put(station, show):
    try:
        p = _agent_path(station, show)
    except ValueError as e:
        return _err(str(e), "BAD_NAME")
    content = (request.get_json(silent=True) or {}).get("content", "")
    if not content.strip():
        return _err("content is required", "EMPTY")
    try:
        frontmatter.loads(content)  # validate before writing
    except Exception as e:
        return _err(f"invalid frontmatter: {e}", "BAD_FRONTMATTER")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return _ok({"station": station, "show": show, "saved": True})
