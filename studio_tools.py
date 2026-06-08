"""
Bilingual (English/Arabic) tool registry for AI Agent Studio.

Ensures LLMs call deterministic Python code for counts and rhyme tail checks.
"""
import re
import json

_VOWELS = "aeiouy"

# Arabic Unicode helpers ----------------------------------------------------
_AR_SHORT_VOWELS = "\u064E\u064F\u0650"          # fatha, damma, kasra
_AR_TANWIN = "\u064B\u064C\u064D"                 # fathatan, dammatan, kasratan
_AR_SHADDA = "\u0651"                              # gemination (adds a closed syllable)
_AR_SUKUN = "\u0652"                               # vowel-less marker
_AR_LONG_VOWELS = "\u0627\u0648\u064A\u0649\u0670"  # alif, waw, ya, alif maqsura, superscript alif
_AR_TATWEEL = "\u0640"                             # kashida (decorative, ignore)
_AR_TASHKEEL = "\u064B-\u0652\u0670\u0653-\u0655"  # full diacritic range


def _ar_is(word: str) -> bool:
    return any('\u0600' <= ch <= '\u06FF' for ch in word)


def _ar_normalize(word: str) -> str:
    """Strip tashkeel/tatweel and fold alef + ta-marbuta variants for rhyme matching."""
    w = re.sub(f"[{_AR_TASHKEEL}{_AR_TATWEEL}]", "", word)
    w = re.sub(r"[^\u0600-\u06FF]", "", w)
    w = re.sub(r"[\u0622\u0623\u0625\u0671]", "\u0627", w)  # \u0622 \u0623 \u0625 \u0671 \u2192 \u0627
    w = w.replace("\u0629", "\u0647")                        # \u0629 \u2192 \u0647 (rhyme-equivalent ending)
    w = w.replace("\u0649", "\u064A")                        # \u0649 \u2192 \u064A
    return w


def _syllables_in_word(word: str) -> int:
    if _ar_is(word):
        # Vocalized text: each short vowel / tanwin is a syllable nucleus; shadda
        # closes a syllable (counts once more). This is the linguistically correct path.
        nuclei = len(re.findall(f"[{_AR_SHORT_VOWELS}{_AR_TANWIN}]", word))
        nuclei += len(re.findall(f"[{_AR_SHADDA}]", word))
        if nuclei:
            return max(1, nuclei)
        # Unvocalized fallback: long vowels (\u0627 \u0648 \u064A) are definite nuclei; each remaining
        # consonant pair carries ~one short vowel. Far closer to real meter than letters//2.
        bare = _ar_normalize(word)
        if not bare:
            return 0
        long_v = len(re.findall(f"[{_AR_LONG_VOWELS}]", bare))
        consonants = len(bare) - long_v
        return max(1, long_v + round(consonants / 2))

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
    if _ar_is(word):
        # Arabic qafiya: the rhyme hinges on the last sounded letter (rawi) plus its
        # preceding nucleus. Use the normalized last 2 letters; if the word ends in a
        # long vowel, pull one more letter so e.g. "\u0633\u0644\u0627\u0645\u064F"/"\u0643\u0644\u0627\u0645\u064F" tails align on the rawi.
        w = _ar_normalize(word)
        if len(w) >= 2 and w[-1] in _AR_LONG_VOWELS and len(w) >= 3:
            return w[-3:]
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
        "note": "Bilingual: EN vowel-group heuristic · AR harakat/long-vowel nuclei (+shadda)",
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
