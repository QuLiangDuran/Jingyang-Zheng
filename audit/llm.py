"""LLM clients. Anything with `.model`, `.base_url`, `.temperature` and
`.complete(system, user) -> str` can be used by the extractor. A client may also set
`.last_finish_reason` after each call ("length" = the output was cut off).

PDF input: a client with `supports_pdf = True` is called as `complete(system, user, pdf_path=...)`
and sends the original PDF together with the extracted text, so the model can see figures and
table layout. The text stays the reference for quotes (the review page highlights quotes in it).
`cleanup()`, if present, is called after a document is done (deletes uploaded files).
"""
import base64
import json
import re
from pathlib import Path
from typing import Protocol

from .config import Settings


class LLMClient(Protocol):
    model: str
    base_url: str | None
    temperature: float

    def complete(self, system: str, user: str) -> str: ...


CONTINUE_PROMPT = ("Your previous answer was cut off at the output limit. Continue the JSON exactly from "
                   "the last character you wrote. Do not repeat anything, do not restart, no commentary, "
                   "no code fence.")
TAIL_CHARS = 80


def continue_prompt(text: str) -> str:
    """The continuation request, quoting the exact end of the answer so the model resumes at the
    very next character (it tends to skip to the next JSON token after a cut inside a key)."""
    tail = text[-TAIL_CHARS:]
    return (f"{CONTINUE_PROMPT}\nYour answer so far ends with exactly these characters (between <<< >>>):\n"
            f"<<<{tail}>>>\nYour reply must start with the character that comes right after them.")


# Short patches tried at a seam where the model dropped a few characters (e.g. the ": after a key).
SEAM_PATCHES = ["", '"', ":", '":', '": ', ",", '",', '"}', "}", "]", '"]', "},", '"},', " ", '":"', ':"',
                ":{", '":{']


def _json_progress(body: str) -> int:
    """How far json.loads gets: len(body) + 1 if it parses, else the error position."""
    try:
        json.loads(body)
        return len(body) + 1
    except json.JSONDecodeError as e:
        return e.pos


def repair_seams(text: str, seams: list[int]) -> tuple[str, list[dict]]:
    """Fix JSON broken exactly at continuation seams (character offsets in `text`).

    For each seam where parsing fails right at the join, try the short patches above and keep the
    one that lets the parser get furthest (shortest on ties), only if it gets clearly past the seam.
    Errors anywhere else are left alone, and so is an answer that is simply unfinished.
    Returns the text and one note per seam that was patched or could not be fixed."""
    notes: list[dict] = []
    a = text.find("{")
    if a == -1:
        return text, notes
    shift = 0
    for seam in sorted(seams):
        s = seam + shift
        b = text.rfind("}")
        body_start, body = a, text[a:b + 1]
        pos = _json_progress(body) + body_start
        if pos > len(text) - 1 or b < s:  # parses, or the seam is not inside the JSON body
            continue
        # The parser may only notice a few characters later (a key cut open swallows the next
        # quote), so errors up to 40 characters after the seam count as seam problems.
        if not (s - 3 <= pos <= s + 40):  # broken elsewhere: not a seam problem
            continue
        best, best_pos = None, pos
        for patch in SEAM_PATCHES:
            cand = text[:s] + patch + text[s:]
            cb = cand.rfind("}")
            p = _json_progress(cand[a:cb + 1]) + a
            if p > best_pos + 20 or (best is not None and p == best_pos and len(patch) < len(best)):
                best, best_pos = patch, p
        if best is None:
            notes.append({"seam": seam, "fixed": False, "context": text[max(0, s - 30):s + 30]})
            continue
        text = text[:s] + best + text[s:]
        shift += len(best)
        notes.append({"seam": seam, "fixed": True, "inserted": best,
                      "context": text[max(0, s - 30):s + len(best) + 30]})
    return text, notes


def join_continuation(partial: str, more: str) -> str:
    """Append a continuation, dropping a code fence the model may reopen and text it repeated."""
    more = re.sub(r"^\s*```(?:json)?\s*", "", more)
    for k in range(min(len(partial), len(more), 400), 19, -1):  # repeated overlap of 20+ chars
        if partial.endswith(more[:k]):
            return partial + more[k:]
    return partial + more


def _get(obj, name):
    if obj is None:
        return None
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def usage_summary(u) -> dict:
    """Token counts of one API call. Providers name the cache and reasoning counts differently:
    OpenAI-style prompt_tokens_details.cached_tokens / completion_tokens_details.reasoning_tokens,
    DeepSeek-style prompt_cache_hit_tokens / prompt_cache_miss_tokens. The full usage object is
    kept in "raw" so nothing a provider reports is lost."""
    pd, cd = _get(u, "prompt_tokens_details"), _get(u, "completion_tokens_details")
    cached = _get(pd, "cached_tokens")
    if cached is None:
        cached = _get(u, "prompt_cache_hit_tokens")
    raw = None
    if u is not None:
        try:
            raw = u.model_dump() if hasattr(u, "model_dump") else u
            raw = json.loads(json.dumps(raw, default=lambda o: vars(o) if hasattr(o, "__dict__") else str(o)))
        except Exception:
            raw = None
    return {"prompt_tokens": _get(u, "prompt_tokens"), "cached_tokens": cached,
            "completion_tokens": _get(u, "completion_tokens"), "reasoning_tokens": _get(cd, "reasoning_tokens"),
            "raw": raw}


def usage_totals(calls: list[dict]) -> dict:
    def tot(k):
        vals = [c.get(k) for c in calls]
        return None if all(v is None for v in vals) else sum(v or 0 for v in vals)
    return {"prompt_tokens": tot("prompt_tokens"), "cached_tokens": tot("cached_tokens"),
            "completion_tokens": tot("completion_tokens") or 0, "reasoning_tokens": tot("reasoning_tokens"),
            "continuations": max(0, len(calls) - 1)}


class OpenAICompatClient:
    """Works with any OpenAI-compatible chat completions endpoint."""

    def __init__(self, base_url: str, model: str, api_key: str, temperature: float = 0.0,
                 max_output_tokens: int | None = None, pdf_input: str = "off", client=None,
                 max_continuations: int = 2, reasoning_effort: str | None = None,
                 extra_body: dict | None = None, json_mode: bool = False,
                 image_dpi: int = 150, image_pages: str = "figures", cache_control: bool = False):
        if not model or not api_key:
            raise ValueError("LLM_MODEL and LLM_API_KEY must be set in .env")
        self.base_url = base_url
        self.model = model
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.last_finish_reason: str | None = None
        self.max_continuations = max_continuations
        self.last_usage: dict | None = None
        self.reasoning_effort = reasoning_effort
        self.extra_body = extra_body
        self.json_mode = json_mode
        # images: pages rendered locally (pdf_images.py) and sent as images beside the text
        self.image_dpi = image_dpi
        self.image_pages = image_pages
        # Mark the system prompt and the paper (last user block) with cache_control, so continuations
        # and the next paper reuse them at the cached price (explicit cache; Qwen / Anthropic style).
        self.cache_control = cache_control
        # file_id: upload once through the Files API and refer to it by id (OpenAI);
        # inline: send the PDF as base64 in the message (providers without a Files API);
        # off: text only.
        self.pdf_input = pdf_input
        self.supports_pdf = pdf_input != "off"
        self.last_pdf_ref: str | None = None
        self._uploaded: dict[tuple, str] = {}  # (path, mtime, size) -> file_id
        if client is None:
            from openai import OpenAI
            client = OpenAI(base_url=base_url, api_key=api_key)
        self._client = client

    @classmethod
    def from_settings(cls, s: Settings) -> "OpenAICompatClient":
        return cls(s.llm_base_url, s.llm_model, s.llm_api_key, s.llm_temperature, s.llm_max_output_tokens,
                   s.llm_pdf_input, max_continuations=s.llm_max_continuations,
                   reasoning_effort=s.llm_reasoning_effort, extra_body=s.llm_extra_body,
                   json_mode=s.llm_json_mode, image_dpi=s.llm_image_dpi,
                   image_pages=s.llm_image_pages, cache_control=s.llm_cache_control)

    def request_options(self) -> dict:
        """Request parameters besides model/messages; also stored with each run."""
        kw: dict = {}
        if self.max_output_tokens:
            kw["max_tokens"] = self.max_output_tokens
        if self.reasoning_effort:
            kw["reasoning_effort"] = self.reasoning_effort
        if self.json_mode:
            kw["response_format"] = {"type": "json_object"}
        if self.extra_body:
            kw["extra_body"] = self.extra_body
        return kw

    def _pdf_parts(self, pdf_path: str | Path) -> list[dict]:
        if self.pdf_input != "images":
            return [self._pdf_part(pdf_path)]
        from .pdf_images import page_images
        p = Path(pdf_path)
        pages = page_images(p, dpi=self.image_dpi, pages=self.image_pages)
        self.last_pdf_ref = (f"images:{p.name} pages={','.join(str(n) for n, _ in pages) or 'none'} "
                             f"dpi={self.image_dpi}")
        parts: list[dict] = []
        for n, png in pages:
            parts.append({"type": "text", "text": f"[PDF page {n}]"})
            parts.append({"type": "image_url",
                          "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}})
        return parts

    def _messages(self, system: str, user: str, pdf_path) -> list[dict]:
        parts = self._pdf_parts(pdf_path) if pdf_path and self.supports_pdf else []
        if not self.cache_control:
            content = parts + [{"type": "text", "text": user}] if parts else user
            return [{"role": "system", "content": system}, {"role": "user", "content": content}]
        mark = {"type": "ephemeral"}
        return [{"role": "system", "content": [{"type": "text", "text": system, "cache_control": mark}]},
                {"role": "user", "content": parts + [{"type": "text", "text": user, "cache_control": mark}]}]

    def _pdf_part(self, pdf_path: str | Path) -> dict:
        p = Path(pdf_path)
        if self.pdf_input == "inline":
            data = base64.b64encode(p.read_bytes()).decode("ascii")
            self.last_pdf_ref = f"inline:{p.name}"
            return {"type": "file", "file": {"filename": p.name, "file_data": f"data:application/pdf;base64,{data}"}}
        st = p.stat()
        key = (str(p.resolve()), st.st_mtime_ns, st.st_size)
        if key not in self._uploaded:  # long papers are sent in chunks: upload once
            with p.open("rb") as fh:
                try:
                    self._uploaded[key] = self._client.files.create(file=(p.name, fh), purpose="user_data").id
                except Exception as e:
                    raise RuntimeError(f"PDF upload through the Files API failed ({type(e).__name__}: {e}). "
                                       "If this provider has no Files API, set LLM_PDF_INPUT=inline "
                                       "(or off) in .env.") from e
        self.last_pdf_ref = f"file_id:{self._uploaded[key]}"
        return {"type": "file", "file": {"file_id": self._uploaded[key]}}

    def complete(self, system: str, user: str, pdf_path: str | Path | None = None) -> str:
        kwargs = self.request_options()
        self.last_pdf_ref = None
        messages = self._messages(system, user, pdf_path)
        calls, text, seams = [], "", []
        for i in range(self.max_continuations + 1):
            resp = self._client.chat.completions.create(
                model=self.model, temperature=self.temperature, messages=messages, **kwargs)
            part = resp.choices[0].message.content or ""
            finish = resp.choices[0].finish_reason
            calls.append({**usage_summary(getattr(resp, "usage", None)), "finish_reason": finish,
                          "chars": len(part), "continuation": i > 0})
            if i:
                seams.append(len(text))
                text = join_continuation(text, part)
            else:
                text = part
            if finish != "length":
                break
            # Cut off at the output limit: ask the model to go on from where it stopped.
            messages = messages[:2] + [{"role": "assistant", "content": text},
                                       {"role": "user", "content": continue_prompt(text)}]
        text, repairs = repair_seams(text, seams) if seams else (text, [])
        self.last_finish_reason = finish
        opts = {k: v for k, v in kwargs.items() if k != "max_tokens"}
        self.last_usage = {"calls": calls, "max_tokens": self.max_output_tokens, "seams": seams,
                           "seam_repairs": repairs, "request_options": opts, **usage_totals(calls)}
        return text

    def cleanup(self) -> None:
        """Delete files uploaded for this document from the provider (best effort)."""
        for file_id in self._uploaded.values():
            try:
                self._client.files.delete(file_id)
            except Exception:
                pass
        self._uploaded.clear()


class ReplayClient:
    """Returns canned responses in order. For tests and the demo."""

    def __init__(self, responses: list[str], model: str = "replay"):
        self.responses = list(responses)
        self.model = model
        self.base_url = None
        self.temperature = 0.0
        self.last_finish_reason: str | None = None
        self.calls: list[tuple[str, str]] = []
        self.supports_pdf = False
        self.pdf_paths: list = []

    def complete(self, system: str, user: str, pdf_path=None) -> str:
        self.calls.append((system, user))
        self.pdf_paths.append(pdf_path)
        if not self.responses:
            return '{"source": null, "conditions": [], "observations": []}'
        return self.responses.pop(0)
