"""Token report per extraction run: what the API billed vs. what was saved.

Cumulative API output = every completion token of every call (continuations and hidden reasoning
included). Saved effective output = the field values and quotes that ended up in the records of
that run, counted once per distinct (field, value, quote). Token counts of saved text are
estimates (characters / CHARS_PER_TOKEN); API counts come from the provider.
"""
import json
import sqlite3

CHARS_PER_TOKEN = 3.3  # JSON with English text, measured on our DeepSeek runs


def run_report(conn: sqlite3.Connection, run) -> dict:
    u = json.loads(run["usage"]) if run["usage"] else {}
    calls = u.get("calls") or []
    out = sum(c.get("completion_tokens") or 0 for c in calls)
    inp = sum(c.get("prompt_tokens") or 0 for c in calls)
    cached = [c.get("cached_tokens") for c in calls]
    reasoning = [c.get("reasoning_tokens") for c in calls]
    visible_chars = len(run["raw_response"] or "")
    seen, saved_chars, unique_chars = set(), 0, 0
    for r in conn.execute("SELECT proposed_json FROM records WHERE run_id=?", (run["id"],)):
        for k, fv in json.loads(r["proposed_json"])["fields"].items():
            s = (fv.get("value") or "") + (fv.get("quote") or "")
            if not s:
                continue
            saved_chars += len(s)
            key = (k, fv.get("value"), fv.get("quote"))
            if key not in seen:
                seen.add(key)
                unique_chars += len(s)
    reasoning_known = None if all(v is None for v in reasoning) else sum(v or 0 for v in reasoning)
    reasoning_est = max(0, out - round(visible_chars / CHARS_PER_TOKEN))
    unique_tok = round(unique_chars / CHARS_PER_TOKEN)
    return {
        "run": run["id"], "document": run["document_id"], "prompt_version": run["prompt_version"],
        "status": run["status"], "calls": len(calls), "continuations": max(0, len(calls) - 1),
        "prompt_tokens": inp,
        "cached_tokens": None if all(v is None for v in cached) else sum(v or 0 for v in cached),
        "api_output_tokens": out,
        "reasoning_tokens": reasoning_known,
        "reasoning_tokens_estimated": reasoning_est if reasoning_known is None else None,
        "answer_chars": visible_chars,
        "saved_chars": saved_chars, "saved_unique_chars": unique_chars,
        "saved_unique_tokens_est": unique_tok,
        "effective_share": (unique_tok / out) if out else None,
    }


def report(conn: sqlite3.Connection, document_id: int | None = None) -> list[dict]:
    q = "SELECT * FROM extraction_runs" + (" WHERE document_id=?" if document_id else "") + " ORDER BY id"
    return [run_report(conn, r) for r in conn.execute(q, (document_id,) if document_id else ())]


def format_report(rows: list[dict]) -> str:
    lines = []
    for r in rows:
        hit = f"{r['cached_tokens'] / r['prompt_tokens']:.0%}" if r["cached_tokens"] is not None and r["prompt_tokens"] else "not reported"
        rea = (f"{r['reasoning_tokens']:,}" if r["reasoning_tokens"] is not None
               else f"~{r['reasoning_tokens_estimated']:,} (estimated; provider did not report it)")
        share = f"{r['effective_share']:.0%}" if r["effective_share"] is not None else "-"
        lines += [
            f"run {r['run']}  document {r['document']}  prompt {r['prompt_version']}  {r['status']}",
            f"  calls {r['calls']} (continuations {r['continuations']})",
            f"  input  {r['prompt_tokens']:,} tokens, cache hits {r['cached_tokens'] if r['cached_tokens'] is not None else '-'} ({hit})",
            f"  API output (cumulative) {r['api_output_tokens']:,} tokens; hidden reasoning {rea}",
            f"  answer text {r['answer_chars']:,} chars; saved {r['saved_chars']:,} chars, "
            f"{r['saved_unique_chars']:,} distinct (~{r['saved_unique_tokens_est']:,} tokens)",
            f"  effective output = {share} of cumulative API output",
        ]
    return "\n".join(lines) or "No extraction runs."
