"""
Generic agent runner for AI Agent Studio.
Loads agent markdown files from agents/studio_agents/ with hot reload.
"""
import os
import re
from pathlib import Path
import frontmatter

AGENTS_DIR = (Path(__file__).resolve().parent / "agents" / "studio_agents").resolve()
DEFAULT_MODEL = "claude-opus-4-8"
MAX_TURNS = 8  # safety cap on the tool-use loop


class AgentError(Exception):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


def ensure_agents_dir():
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)


def _path(name: str) -> Path:
    ensure_agents_dir()
    # Safe validation for names
    if not re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", name):
        raise AgentError("invalid agent name", "BAD_NAME")
    p = (AGENTS_DIR / f"{name}.md").resolve()
    if p.parent != AGENTS_DIR:
        raise AgentError("path traversal blocked", "BAD_PATH")
    return p


def list_agents() -> list[dict]:
    ensure_agents_dir()
    agents = []
    for f in sorted(AGENTS_DIR.glob("*.md")):
        try:
            m = frontmatter.load(f).metadata
            agents.append(
                {
                    "name": m.get("name", f.stem),
                    "label_en": m.get("label_en", m.get("name", f.stem)),
                    "label_ar": m.get("label_ar", ""),
                    "model": m.get("model", DEFAULT_MODEL),
                    "tools": m.get("tools", []) or [],
                    "description": m.get("description", ""),
                }
            )
        except Exception:
            pass
    return agents


def load_raw(name: str) -> dict:
    p = _path(name)
    if not p.exists():
        raise AgentError("agent not found", "NOT_FOUND")
    return {"name": name, "content": p.read_text(encoding="utf-8")}


def save_raw(name: str, content: str) -> None:
    try:
        frontmatter.loads(content)  # validate it parses before writing
    except Exception as e:
        raise AgentError(f"invalid frontmatter: {e}", "BAD_FRONTMATTER")
    _path(name).write_text(content, encoding="utf-8")


def default_template(name: str) -> str:
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


def create_raw(name: str, content: str | None = None) -> dict:
    if _path(name).exists():
        raise AgentError("agent already exists", "EXISTS")
    save_raw(name, content or default_template(name))
    return {"name": name}


def delete_raw(name: str) -> None:
    p = _path(name)
    if not p.exists():
        raise AgentError("agent not found", "NOT_FOUND")
    p.unlink()


def _load_agent(name: str):
    p = _path(name)
    if not p.exists():
        raise AgentError("agent not found", "NOT_FOUND")
    post = frontmatter.load(p)  # read at request time -> hot reload
    return post.metadata, post.content


def run_agent(name: str, user_input: str) -> dict:
    from studio_tools import anthropic_tools, execute

    meta, system = _load_agent(name)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise AgentError("ANTHROPIC_API_KEY not set in environment", "NO_API_KEY")

    import anthropic

    client = anthropic.Anthropic()
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
                    result = execute(block.name, block.input)
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
