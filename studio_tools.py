"""
Bilingual (English/Arabic) tool registry for AI Agent Studio.

Ensures LLMs call deterministic Python code for counts and rhyme tail checks.
"""
import re
import json

_VOWELS = "aeiouy"


def _syllables_in_word(word: str) -> int:
    # Check if word contains Arabic characters
    is_arabic = any('\u0600' <= char <= '\u06FF' for char in word)
    if is_arabic:
        # If vocalized (contains tashkeel: Fatha, Damma, Kasra, Shadda, etc.)
        # count the number of harakat to approximate syllables.
        harakat = re.findall(r'[\u064E\u064F\u0650\u0651\u064B\u064C\u064D]', word)
        if harakat:
            return len(harakat)
        # Default fallback for unvocalized Arabic: count letters and divide by 2
        clean_ar = re.sub(r'[^\u0600-\u06FF]', '', word)
        return max(1, len(clean_ar) // 2)

    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 0
    count, prev_vowel = 0, False
    for ch in w:
        is_vowel = ch in _VOWELS
        if is_vowel and not prev_vowel:
            count += 1
        prev_vowel = is_vowel
    if w.endswith("e") and count > 1:  # crude silent-e correction
        count -= 1
    return max(1, count)


def _rhyme_tail(word: str) -> str:
    is_arabic = any('\u0600' <= char <= '\u06FF' for char in word)
    if is_arabic:
        # clean tashkeel for rhyming tail
        w = re.sub(r"[\u064B-\u0652\u0670]", "", word)
        w = re.sub(r"[^\u0600-\u06FF]", "", w)
        return w[-2:] if len(w) >= 2 else w

    w = re.sub(r"[^a-z]", "", word.lower())
    last = -1
    for i, ch in enumerate(w):
        if ch in _VOWELS:
            last = i
    return w[last:] if last >= 0 else w


def count_syllables(text: str) -> dict:
    per_line = []
    for line in text.splitlines():
        words = line.split()
        per_line.append({"line": line, "syllables": sum(_syllables_in_word(w) for w in words)})
    return {
        "per_line": per_line,
        "total": sum(o["syllables"] for o in per_line),
        "note": "Bilingual vowel heuristic",
    }


def check_rhyme(word_a: str, word_b: str) -> dict:
    ta, tb = _rhyme_tail(word_a), _rhyme_tail(word_b)
    return {
        "word_a": word_a,
        "word_b": word_b,
        "rhymes": ta == tb and len(ta) >= 2,
        "tail_a": ta,
        "tail_b": tb,
        "note": "suffix heuristic",
    }


def readability(text: str) -> dict:
    words = text.split()
    n = len(words) or 1
    syllables = sum(_syllables_in_word(w) for w in words)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return {
        "words": len(words),
        "lines": len(lines),
        "syllables": syllables,
        "avg_syllables_per_word": round(syllables / n, 2),
        "avg_words_per_line": round(len(words) / (len(lines) or 1), 2),
    }


REGISTRY = {
    "count_syllables": {
        "schema": {
            "name": "count_syllables",
            "description": "Count syllables per line of lyrics (Bilingual EN/AR). Use to verify meter/prosody consistency within a section.",
            "input_schema": {
                "type": "object",
                "properties": {"text": {"type": "string", "description": "Lyrics, newline-separated"}},
                "required": ["text"],
            },
        },
        "fn": lambda i: count_syllables(i["text"]),
    },
    "check_rhyme": {
        "schema": {
            "name": "check_rhyme",
            "description": "Check whether two words rhyme (suffix heuristic). Use on line-ending word pairs.",
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
            "input_schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
        "fn": lambda i: readability(i["text"]),
    },
}


def anthropic_tools(names: list[str]) -> list[dict]:
    return [REGISTRY[n]["schema"] for n in names if n in REGISTRY]


def execute(name: str, tool_input: dict) -> str:
    if name not in REGISTRY:
        raise ValueError(f"unknown tool: {name}")
    return json.dumps(REGISTRY[name]["fn"](tool_input), ensure_ascii=False)
