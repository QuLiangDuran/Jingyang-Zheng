"""Token logging and automatic continuation of answers cut off at the output limit."""
import json
import sqlite3
from types import SimpleNamespace

from audit.db import connect
from audit.extract import add_document, extract_document
from audit.llm import CONTINUE_PROMPT, OpenAICompatClient, join_continuation, repair_seams

FULL = '{"source": null, "conditions": [], "observations": []}'


class Scripted:
    """Fake OpenAI client answering with (text, finish_reason, completion_tokens) in order."""

    def __init__(self, answers):
        self.answers, self.messages = list(answers), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._chat))

    def _chat(self, model, temperature, messages, **kw):
        self.messages.append(messages)
        text, finish, n = self.answers.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason=finish)],
                               usage=SimpleNamespace(prompt_tokens=100, completion_tokens=n))


def _client(answers, cont=2):
    fake = Scripted(answers)
    return OpenAICompatClient("http://x", "m", "k", max_output_tokens=32000, client=fake,
                              max_continuations=cont), fake


def test_cut_off_answer_is_continued_and_joined():
    c, fake = _client([(FULL[:20], "length", 16384), (FULL[20:], "stop", 50)])
    assert c.complete("sys", "paper") == FULL
    assert c.last_finish_reason == "stop"
    second = fake.messages[1]
    assert second[2] == {"role": "assistant", "content": FULL[:20]}
    assert second[3]["content"].startswith(CONTINUE_PROMPT)
    assert f"<<<{FULL[:20]}>>>" in second[3]["content"]               # quotes the exact end
    assert c.last_usage["completion_tokens"] == 16434
    assert [x["finish_reason"] for x in c.last_usage["calls"]] == ["length", "stop"]


def test_gives_up_after_max_continuations():
    c, _ = _client([("a" * 30, "length", 10)] * 3, cont=2)
    c.complete("sys", "paper")
    assert c.last_finish_reason == "length" and len(c.last_usage["calls"]) == 3


def test_join_drops_reopened_fence_and_repeated_overlap():
    partial = '{"observations": [{"metric_code": {"value": "photocurrent_density"'
    assert join_continuation(partial, '```json\n, "x": 1}]}') == partial + ', "x": 1}]}'
    assert join_continuation(partial, '{"value": "photocurrent_density", "y": 2}') == partial + ', "y": 2}'
    assert join_continuation("abc", "def") == "abcdef"                 # short overlap is not trimmed


def test_extraction_stores_usage_and_reports_tokens(conn):
    doc = add_document(conn, "p", "Paper text.", 200_000, None)
    c, _ = _client([(FULL[:20], "length", 16384), (FULL[20:], "stop", 50)])
    s = extract_document(conn, doc, c)
    assert s.failed_runs == 0 and s.calls == 2 and s.completion_tokens == 16434
    usage = json.loads(conn.execute("SELECT usage FROM extraction_runs").fetchone()[0])
    assert usage["max_tokens"] == 32000 and len(usage["calls"]) == 2


def test_still_cut_off_is_a_parse_error_with_token_counts(conn):
    doc = add_document(conn, "p", "Paper text.", 200_000, None)
    c, _ = _client([('{"source": null, "obs', "length", 16384)], cont=0)
    s = extract_document(conn, doc, c)
    assert s.failed_runs == 1 and "16384" in s.errors[0]


def test_old_database_gets_usage_column(tmp_path):
    db = tmp_path / "old.db"
    connect(db).close()
    raw = sqlite3.connect(db)
    raw.executescript("ALTER TABLE extraction_runs DROP COLUMN usage;")
    raw.close()
    conn = connect(db)
    assert "usage" in {r[1] for r in conn.execute("PRAGMA table_info(extraction_runs)")}


# The real seam from 2025_YUAN: cut inside a key, the continuation skipped the closing quote and colon.
OBS = '{"value": "BG11 medium", "quote": "cultivated in BG11 medium"}'


def _real_case():
    whole = '{"source": null, "conditions": [{"group_type": {"value": "WT"}, "medium_name": ' + OBS + '}], "observations": []}'
    cut = whole.index('"medium_name') + len('"medium_name')
    first = whole[:cut]
    second = whole[cut + 2:]                                          # model dropped '":'
    return whole, first, second


def test_repairs_the_real_seam():
    whole, first, second = _real_case()
    fixed, notes = repair_seams(first + second, [len(first)])
    assert json.loads(fixed) == json.loads(whole.replace('": {"value": "BG11', '":{"value": "BG11'))
    assert notes == [{"seam": len(first), "fixed": True, "inserted": '":', "context": notes[0]["context"]}]


def test_unfinished_answer_is_not_patched():
    whole, first, _ = _real_case()
    text = whole[:-30]                                                # simply not finished
    assert repair_seams(text, [len(first)]) == (text, [])


def test_error_away_from_the_seam_is_left_alone():
    whole, first, _ = _real_case()
    broken = whole.replace('"observations": []', '"observations": [,]')
    assert repair_seams(broken, [len(first)]) == (broken, [])


def test_valid_join_is_untouched():
    whole, first, _ = _real_case()
    assert repair_seams(whole, [len(first)]) == (whole, [])


def test_client_repairs_seam_and_records_it():
    whole, first, second = _real_case()
    c, _ = _client([(first, "length", 32000), (second, "stop", 100)])
    out = c.complete("sys", "paper")
    assert json.loads(out)["conditions"][0]["medium_name"]["value"] == "BG11 medium"
    assert c.last_usage["seams"] == [len(first)] and c.last_usage["seam_repairs"][0]["fixed"]


def test_reparse_recovers_a_run_broken_at_a_seam_without_api(conn):
    from audit.extract import reparse_document
    whole, first, second = _real_case()
    doc = add_document(conn, "p", "Text: cultivated in BG11 medium.", 200_000, None)
    c, _ = _client([(first, "length", 32000), (second, "stop", 100)])
    c.complete = lambda s, u: first + second          # simulate the old client: joined without repair
    c.last_usage = None
    extract_document(conn, doc, c)
    assert conn.execute("SELECT status FROM extraction_runs").fetchone()[0] == "parse_error"
    # old run stored no usage; give it the per-call lengths the old code recorded
    conn.execute("UPDATE extraction_runs SET usage=?", (json.dumps(
        {"calls": [{"chars": len(first)}, {"chars": len(second)}]}),))
    s = reparse_document(conn, doc)
    assert s.failed_runs == 0 and s.records == 1       # the condition (an empty paper record is added)
    assert conn.execute("SELECT COUNT(*) FROM records WHERE kind='source'").fetchone()[0] == 1
    runs = conn.execute("SELECT status, raw_response FROM extraction_runs ORDER BY id").fetchall()
    assert [r[0] for r in runs] == ["parse_error", "ok"] and runs[0][1] == first + second  # old raw untouched
    assert "BG11 medium" in conn.execute(
        "SELECT current_json FROM records WHERE kind='condition'").fetchone()[0]
    again = reparse_document(conn, doc)                 # latest run is ok now: nothing to do
    assert again.runs == 0
