"""Recall check (v10 #3): result sentences that no observation quotes. No API call.

Prompt changes can make the model drop a whole kind of result (v9 lost detachment, chloroplast
counts and FDA activity on VIC). This check does not depend on the model: it scans the Results /
Discussion part of the text for sentences that state a measured value (a number with a unit or %,
an approximate "~" value, or a "±" value) and reports those that no observation's quote (or
evidence_excerpt) overlaps. The count of uncovered sentences also measures recall between
prompt versions (`python run.py recall <doc>`).
"""
import re
from functools import lru_cache

from .documents import _SENTENCE
from .textmatch import locate

_HEAD = r"(?:^|■)[ \t]*(?:\d+(?:\.\d+)*\.?[ \t]*)?"
_START = re.compile(_HEAD + r"results\b", re.I | re.M)
_END = re.compile(_HEAD + r"(?:conclusions?|references|acknowledg\w*|associated content|author information)\b",
                  re.I | re.M)
_REFS = re.compile(_HEAD + r"references\b", re.I | re.M)

_UNIT = (r"(?:%|‰|°\s?C|[pnµμmkM]?(?:A|V|W|Ω|M|g|L|mol)\b|cm[2²]|cm[−-]\s?[12²]|m[2²]\b|mm\b|[nµμ]m\b"
         r"|cells?\b|h\b|min\b|s\b|days?\b|lux\b|-?fold\b)")
_VALUE = re.compile(r"\d(?:[.,]\d+)?\s?" + _UNIT          # 5 mV, 0.3 μA, 1%, 20 °C
                    + r"|[~∼≈]\s?\d"                       # ∼8−12, ~100
                    + r"|\d\s?±\s?\d")                     # 0.22 ± 0.04


def result_region(text: str) -> tuple[int, int]:
    """(start, end) of Results/Discussion; without a Results heading, everything before References."""
    m = _START.search(text)
    if m:
        start = m.start()
        e = _END.search(text, m.end())
        return start, e.start() if e else len(text)
    r = _REFS.search(text)
    return 0, r.start() if r else len(text)


@lru_cache(maxsize=8)
def result_sentences(text: str) -> tuple[tuple[int, int], ...]:
    """Offsets of the sentences in the result region that state a measured value."""
    a, b = result_region(text)
    out = []
    pos = a
    for para in re.split(r"(\n\s*\n)", text[a:b]):
        for m in _SENTENCE.finditer(para):
            s, e = pos + m.start(), pos + m.end()
            while s < e and text[s].isspace():
                s += 1
            if s < e and _VALUE.search(text[s:e]):
                out.append((s, e))
        pos += len(para)
    return tuple(out)


def coverage(text: str, quotes: list[str]) -> tuple[int, list[tuple[int, int]]]:
    """(number of result sentences, the ones that no quote overlaps)."""
    spans = [sp for q in quotes if q for sp in [locate(text, q)] if sp]
    sents = result_sentences(text)
    uncovered = [(s, e) for s, e in sents if not any(qs < e and s < qe for qs, qe in spans)]
    return len(sents), uncovered


def observation_quotes(contents) -> list[str]:
    """Every quote of the given observations, plus their evidence_excerpt (a quote kept as a value)."""
    out = []
    for c in contents:
        for name, fv in c.fields.items():
            if fv.quote:
                out.append(fv.quote)
            if name == "evidence_excerpt" and fv.value:
                out.append(fv.value)
    return out
