"""
Agent Studio engine — file-based AI agents over Flask.

Merged from the standalone agent-studio project (FastAPI → Flask Blueprint).
Provides 8 endpoints under /api/v1/studio/* for editing markdown agents and
running them through Anthropic Claude with deterministic tools.

Agents live in agents/_studio/<name>.md with YAML frontmatter:

    ---
    name: lyric-writer
    label_en: Lyric Writer
    label_ar: كاتب الكلمات
    description: ...
    model: claude-opus-4-8
    tools: [count_syllables, check_rhyme]
    temperature: 1.0
    max_tokens: 2048
    ---
    <system prompt as the markdown body>

Hot-reload: files are read at request time, no restart needed.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from pathlib import Path

import frontmatter
from flask import Blueprint, jsonify, request

log = logging.getLogger("studio")


# ---------- paths -----------------------------------------------------------

STUDIO_DIR = (Path(__file__).resolve().parent / "agents" / "_studio").resolve()
DEFAULT_MODEL = "claude-sonnet-4-6"  # available on Meridian; api.anthropic.com aliases it too
MAX_TURNS = 8
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class AgentError(Exception):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


def _path(name: str) -> Path:
    p = (STUDIO_DIR / f"{name}.md").resolve()
    if p.parent != STUDIO_DIR:
        raise AgentError("path traversal blocked", "BAD_PATH")
    return p


# ---------- tool registry (deterministic — LLM must NOT guess these) -------

_VOWELS = "aeiouy"


def _syllables_in_word(word: str) -> int:
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 0
    count, prev_vowel = 0, False
    for ch in w:
        is_vowel = ch in _VOWELS
        if is_vowel and not prev_vowel:
            count += 1
        prev_vowel = is_vowel
    if w.endswith("e") and count > 1:
        count -= 1
    return max(1, count)


def _rhyme_tail(word: str) -> str:
    w = re.sub(r"[^a-z]", "", word.lower())
    last = -1
    for i, ch in enumerate(w):
        if ch in _VOWELS:
            last = i
    return w[last:] if last >= 0 else w


def count_syllables(text: str) -> dict:
    per_line = []
    for line in text.splitlines():
        words = [w for w in re.split(r"\s+", line) if w]
        per_line.append({
            "line": line,
            "syllables": sum(_syllables_in_word(w) for w in words),
            "words": len(words),
        })
    total = sum(p["syllables"] for p in per_line)
    return {"per_line": per_line, "total_syllables": total, "total_lines": len(per_line)}


def check_rhyme(word_a: str, word_b: str) -> dict:
    ta, tb = _rhyme_tail(word_a), _rhyme_tail(word_b)
    return {
        "word_a": word_a, "word_b": word_b,
        "rhyme_tail_a": ta, "rhyme_tail_b": tb,
        "rhymes": ta == tb and len(ta) >= 2,
    }


def readability(text: str) -> dict:
    sentences = [s for s in re.split(r"[.!?\n]+", text) if s.strip()]
    words = [w for w in re.split(r"\s+", text) if w]
    chars = len(text)
    avg_word_len = (sum(len(w) for w in words) / len(words)) if words else 0
    avg_sentence_len = (len(words) / len(sentences)) if sentences else 0
    return {
        "characters": chars, "words": len(words), "sentences": len(sentences),
        "avg_word_length": round(avg_word_len, 2),
        "avg_sentence_length": round(avg_sentence_len, 2),
    }


REGISTRY = {
    "count_syllables": {
        "schema": {
            "name": "count_syllables",
            "description": "Count syllables per line and totals. Use to enforce song meter.",
            "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        },
        "fn": lambda i: count_syllables(i["text"]),
    },
    "check_rhyme": {
        "schema": {
            "name": "check_rhyme",
            "description": "Check whether two words rhyme by trailing-vowel comparison.",
            "input_schema": {
                "type": "object",
                "properties": {"word_a": {"type": "string"}, "word_b": {"type": "string"}},
                "required": ["word_a", "word_b"],
            },
        },
        "fn": lambda i: check_rhyme(i["word_a"], i["word_b"]),
    },
    "readability": {
        "schema": {
            "name": "readability",
            "description": "Basic readability/structure stats for a block of text.",
            "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        },
        "fn": lambda i: readability(i["text"]),
    },
}


def anthropic_tools(names: list[str]) -> list[dict]:
    return [REGISTRY[n]["schema"] for n in names if n in REGISTRY]


def execute_tool(name: str, tool_input: dict) -> str:
    if name not in REGISTRY:
        raise ValueError(f"unknown tool: {name}")
    result = REGISTRY[name]["fn"](tool_input)
    return json.dumps(result, ensure_ascii=False)


# ---------- agent CRUD ------------------------------------------------------

def list_studio_agents() -> list[dict]:
    if not STUDIO_DIR.exists():
        return []
    agents = []
    for f in sorted(STUDIO_DIR.glob("*.md")):
        m = frontmatter.load(f).metadata
        agents.append({
            "name": m.get("name", f.stem),
            "label_en": m.get("label_en", m.get("name", f.stem)),
            "label_ar": m.get("label_ar", ""),
            "model": m.get("model", DEFAULT_MODEL),
            "tools": m.get("tools", []) or [],
            "description": m.get("description", ""),
        })
    return agents


def load_studio_raw(name: str) -> dict:
    p = _path(name)
    if not p.exists():
        raise AgentError("agent not found", "NOT_FOUND")
    return {"name": name, "content": p.read_text(encoding="utf-8")}


def save_studio_raw(name: str, content: str) -> None:
    try:
        frontmatter.loads(content)
    except Exception as e:
        raise AgentError(f"invalid frontmatter: {e}", "BAD_FRONTMATTER")
    STUDIO_DIR.mkdir(parents=True, exist_ok=True)
    _path(name).write_text(content, encoding="utf-8")


def _default_template(name: str) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"label_en: {name}\n"
        'label_ar: ""\n'
        'description: ""\n'
        "model: claude-sonnet-4-6\n"
        "tools: []\n"
        "temperature: 0.7\n"
        "max_tokens: 1024\n"
        "---\n"
        "You are a helpful assistant. Describe this agent's job, rules, and output format here.\n"
    )


def create_studio_raw(name: str, content: str | None = None) -> dict:
    if _path(name).exists():
        raise AgentError("agent already exists", "EXISTS")
    save_studio_raw(name, content or _default_template(name))
    return {"name": name}


def delete_studio_raw(name: str) -> None:
    p = _path(name)
    if not p.exists():
        raise AgentError("agent not found", "NOT_FOUND")
    p.unlink()


def _load_studio_agent(name: str):
    p = _path(name)
    if not p.exists():
        raise AgentError("agent not found", "NOT_FOUND")
    post = frontmatter.load(p)
    return post.metadata, post.content


def run_studio_agent(name: str, user_input: str) -> dict:
    meta, system = _load_studio_agent(name)
    import anthropic

    # Two providers supported, in order:
    #   1. Meridian proxy (ANTHROPIC_BASE_URL set, no key needed — Claude Code SDK proxy
    #      on local CT 125 with OAuth profile synced from the Mac keychain). Passes a
    #      dummy key to bypass the SDK's own validation; the proxy does its own auth.
    #   2. Anthropic direct (ANTHROPIC_API_KEY set, no base URL — api.anthropic.com).
    base = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    # Browser-like UA — required when Meridian is reached through a Cloudflare tunnel
    # with Bot Fight Mode on. Otherwise CF returns a JS challenge page → PermissionDenied.
    browser_ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    default_headers = {"User-Agent": browser_ua}
    if base:
        client = anthropic.Anthropic(base_url=base, api_key=key or "meridian-passthrough",
                                     default_headers=default_headers)
    elif key:
        client = anthropic.Anthropic(api_key=key, default_headers=default_headers)
    else:
        raise AgentError("set ANTHROPIC_BASE_URL (Meridian) or ANTHROPIC_API_KEY", "NO_API_KEY")
    model = meta.get("model", DEFAULT_MODEL)
    tool_names = meta.get("tools", []) or []
    tools = anthropic_tools(tool_names)
    temperature = float(meta.get("temperature", 1.0))
    max_tokens = int(meta.get("max_tokens", 2048))

    messages = [{"role": "user", "content": user_input}]
    trace: list[dict] = []
    usage = {"input_tokens": 0, "output_tokens": 0}

    for _ in range(MAX_TURNS):
        kwargs = dict(model=model, system=system, messages=messages,
                      max_tokens=max_tokens, temperature=temperature)
        if tools:
            kwargs["tools"] = tools
        resp = client.messages.create(**kwargs)
        usage["input_tokens"] += resp.usage.input_tokens
        usage["output_tokens"] += resp.usage.output_tokens
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "tool_use":
            tool_results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                try:
                    result = execute_tool(block.name, block.input)
                    is_error = False
                except Exception as e:
                    result, is_error = f"error: {e}", True
                trace.append({"tool": block.name, "input": block.input,
                              "output": result, "error": is_error})
                tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                     "content": result, "is_error": is_error})
            messages.append({"role": "user", "content": tool_results})
            continue

        text = "".join(b.text for b in resp.content if b.type == "text")
        return {"output": text, "trace": trace, "model": model, "usage": usage}

    raise AgentError("max turns reached without a final answer", "MAX_TURNS")


# ---------- in-process rate limiter (sliding window) -----------------------

class _RateLimiter:
    def __init__(self, limit: int, window: float = 60.0):
        self.limit = limit
        self.window = window
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window
        q = self.hits[key]
        while q and q[0] <= cutoff:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


def _rate_limit_per_min() -> int:
    try:
        return int(os.environ.get("STUDIO_RATE_LIMIT_PER_MIN", "30"))
    except ValueError:
        return 30


_limiter: _RateLimiter | None = None


def _get_limiter() -> _RateLimiter:
    global _limiter
    if _limiter is None or _limiter.limit != _rate_limit_per_min():
        _limiter = _RateLimiter(_rate_limit_per_min())
    return _limiter


# ---------- envelope (local — no app.py import) -----------------------------

def _ok(data=None, meta=None):
    return jsonify({"ok": True, "data": data, "error": None, "meta": meta or {}}), 200


def _err(message, code="ERROR", status=400):
    return jsonify({"ok": False, "data": None, "error": message, "code": code}), status


# ---------- blueprint -------------------------------------------------------

studio_bp = Blueprint("studio", __name__)


@studio_bp.get("/api/v1/studio/config")
def api_studio_config():
    return _ok({
        "anthropic_configured": bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_BASE_URL")),
        "provider": "meridian" if os.environ.get("ANTHROPIC_BASE_URL") else ("anthropic" if os.environ.get("ANTHROPIC_API_KEY") else None),
        "rate_limit_per_min": _rate_limit_per_min(),
        "tools": list(REGISTRY.keys()),
    })


@studio_bp.get("/api/v1/studio/agents")
def api_studio_list():
    return _ok(list_studio_agents())


@studio_bp.post("/api/v1/studio/agents")
def api_studio_create():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not NAME_RE.match(name):
        return _err("invalid agent name", "BAD_NAME")
    try:
        return _ok(create_studio_raw(name, body.get("content")))
    except AgentError as e:
        status = 409 if e.code == "EXISTS" else 400
        return _err(str(e), e.code or "CREATE_FAILED", status)


@studio_bp.get("/api/v1/studio/agents/<name>")
def api_studio_get(name):
    if not NAME_RE.match(name):
        return _err("invalid agent name", "BAD_NAME")
    try:
        return _ok(load_studio_raw(name))
    except AgentError as e:
        return _err(str(e), e.code or "NOT_FOUND", 404)


@studio_bp.put("/api/v1/studio/agents/<name>")
def api_studio_put(name):
    if not NAME_RE.match(name):
        return _err("invalid agent name", "BAD_NAME")
    content = (request.get_json(silent=True) or {}).get("content", "")
    if not content.strip():
        return _err("content is required", "EMPTY")
    try:
        save_studio_raw(name, content)
        return _ok({"name": name, "saved": True})
    except AgentError as e:
        return _err(str(e), e.code or "SAVE_FAILED")


@studio_bp.delete("/api/v1/studio/agents/<name>")
def api_studio_delete(name):
    if not NAME_RE.match(name):
        return _err("invalid agent name", "BAD_NAME")
    try:
        delete_studio_raw(name)
        return _ok({"name": name, "deleted": True})
    except AgentError as e:
        status = 404 if e.code == "NOT_FOUND" else 400
        return _err(str(e), e.code or "DELETE_FAILED", status)


@studio_bp.post("/api/v1/studio/run")
def api_studio_run():
    body = request.get_json(silent=True) or {}
    name = (body.get("agent") or "").strip()
    user_input = (body.get("input") or "").strip()
    if not NAME_RE.match(name):
        return _err("invalid agent name", "BAD_NAME")
    if not user_input:
        return _err("input is required", "EMPTY_INPUT")
    # Rate limit by client IP
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
    if not _get_limiter().allow(ip):
        return _err("rate limit exceeded", "RATE_LIMITED", 429)
    try:
        return _ok(run_studio_agent(name, user_input))
    except AgentError as e:
        status = 503 if e.code == "NO_API_KEY" else 400
        return _err(str(e), e.code or "RUN_FAILED", status)
    except Exception as e:  # noqa: BLE001 — convert SDK/transport failures to clean JSON
        # When Meridian (claude.zad.tools / CT 125) or Anthropic is unreachable the SDK
        # raises APIConnectionError / APITimeoutError / PermissionDeniedError / InternalServerError.
        # Degrade gracefully instead of bubbling a 500 HTML page to the UI.
        etype = type(e).__name__
        transient = any(k in etype for k in (
            "APITimeout", "APIConnection", "InternalServerError",
            "PermissionDenied", "ServiceUnavailable", "RateLimit", "Overloaded",
        ))
        if transient:
            log.warning("studio/run provider error (%s): %s", etype, str(e)[:200])
            return _err(
                "مزوّد الذكاء (Meridian/Claude) غير متاح مؤقتاً — حاول بعد لحظات. "
                "AI provider temporarily unavailable — please retry shortly.",
                "PROVIDER_UNAVAILABLE", 502,
            )
        log.exception("studio/run unexpected error")
        return _err(f"unexpected error: {etype}", "RUN_FAILED", 500)
