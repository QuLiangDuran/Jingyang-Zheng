"""Locate a quote inside the source text, tolerant of trivial formatting differences."""
import re
import unicodedata
from functools import lru_cache

_TRANSLATE = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "−": "-", " ": " ",
})


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Return normalized text and, for each normalized char, its index in `text`.

    Normalization: NFKC per char, unify quotes/dashes, collapse whitespace runs
    to one space, casefold.
    """
    out: list[str] = []
    index: list[int] = []
    prev_space = False
    for i, ch in enumerate(text):
        for c in unicodedata.normalize("NFKC", ch).translate(_TRANSLATE).casefold():
            if c.isspace():
                if prev_space:
                    continue
                c = " "
                prev_space = True
            else:
                prev_space = False
            out.append(c)
            index.append(i)
    return "".join(out), index


def normalize(text: str) -> str:
    return normalize_with_map(text)[0].strip()


@lru_cache(maxsize=8)
def _normalized_doc(text: str) -> tuple[str, list[int]]:
    """Documents are long and checked field by field; normalize each one only once."""
    return normalize_with_map(text)


def locate(text: str, quote: str | None, prefer_from: int = 0) -> tuple[int, int] | None:
    """Return (start, end) char offsets of `quote` in `text`, or None.

    If the quote occurs several times, the first occurrence at/after
    `prefer_from` wins (falls back to the first occurrence overall).
    """
    if not quote or not quote.strip():
        return None
    norm_text, idx = _normalized_doc(text)
    norm_quote = normalize(quote)
    if not norm_quote:
        return None
    hits = [m.start() for m in re.finditer(re.escape(norm_quote), norm_text)]
    if not hits:
        return None
    pos = next((h for h in hits if idx[h] >= prefer_from), hits[0])
    return idx[pos], idx[pos + len(norm_quote) - 1] + 1


_NUM_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)*(?:[eE][-+]?\d+)?")
# "1.2 × 10^6", "1.2x10^-3", "3.4e5" (after normalize, which casefolds)
_SCI_RE = re.compile(r"([-+]?\d+(?:\.\d+)?)\s*(?:[×x*·]\s*10\s*\^?\s*|e)([-+]?\d+)")
_SCI_RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*~\s*(\d+(?:\.\d+)?)\s*[×x*·]\s*10\s*\^?\s*([-+]?\d+)")
_SUPERSCRIPT = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺", "0123456789-+")


def _prep_number_text(s: str) -> str:
    # "10⁻⁶" -> "10^-6" before NFKC would turn it into the ambiguous "10-6"
    s = re.sub(r"10([⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+)", lambda m: "10^" + m.group(1).translate(_SUPERSCRIPT), s)
    return normalize(s)


def parse_number(s: str) -> float | None:
    s = _prep_number_text(s).replace(" ", "")
    if re.fullmatch(r"[-+]?\d{1,3}(,\d{3})+(\.\d+)?", s):
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        pass
    m = _SCI_RE.fullmatch(s)
    return float(m.group(1)) * 10 ** int(m.group(2)) if m else None


def numbers_in_value(s: str) -> list[float]:
    """Numbers in a reported value such as "0.97 ± 0.05", "1.2–1.5", "1.2 × 10⁶ cells/mL".
    Unlike numbers_in, scientific notation is one number and "a-b" is a range (b is not negative)."""
    s = re.sub(r"(?<=\d)\s*-\s*(?=\d)", " ~ ", _prep_number_text(s))  # range dash, not a minus sign
    found: list[float] = []

    def sci_range(m):  # "1–2 × 10^6": the multiplier applies to both ends
        scale = 10 ** int(m.group(3))
        found.extend([float(m.group(1)) * scale, float(m.group(2)) * scale])
        return " "

    s = _SCI_RANGE_RE.sub(sci_range, s)
    found += [float(m.group(1)) * 10 ** int(m.group(2)) for m in _SCI_RE.finditer(s)]
    s = _SCI_RE.sub(" ", s)
    for m in _NUM_RE.finditer(s):
        n = parse_number(m.group())
        if n is not None:
            found.append(n)
    return found


def numbers_in(s: str) -> list[float]:
    found = []
    for m in _NUM_RE.finditer(normalize(s)):
        n = parse_number(m.group())
        if n is not None:
            found.append(n)
    return found
