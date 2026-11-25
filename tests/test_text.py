from audit.documents import segment_text
from audit.textmatch import locate, numbers_in, parse_number


def test_locate_exact_and_offsets():
    text = "Alpha beta.\n\nThe hardness was 335 HV here."
    s, e = locate(text, "hardness was 335 HV")
    assert text[s:e] == "hardness was 335 HV"


def test_locate_tolerates_whitespace_quotes_dashes_and_fullwidth():
    text = "It is “strong”\n  and 10–20 ℃ wide, ＡＢＣ."
    s, e = locate(text, 'is "strong" and 10-20 °C wide, abc')
    assert text[s:e] == "is “strong”\n  and 10–20 ℃ wide, ＡＢＣ"


def test_locate_prefers_occurrence_in_segment():
    text = "x 352 HV y. later: 352 HV z."
    assert locate(text, "352 HV", prefer_from=10)[0] == text.rindex("352 HV")
    assert locate(text, "352 HV")[0] == text.index("352 HV")


def test_locate_missing():
    assert locate("abc", "xyz") is None
    assert locate("abc", "") is None
    assert locate("abc", None) is None


def test_numbers():
    assert parse_number("1,234.5") == 1234.5
    assert parse_number("abc") is None
    assert numbers_in("from 8.4 μm to 15.1 μm") == [8.4, 15.1]


def test_segments_cover_paragraphs_and_respect_max():
    text = "A" * 50 + "\n\n" + "B" * 50 + "\n\n" + "C" * 50 + "\n"
    segs = segment_text(text, 110)
    assert [(text[s.start:s.end][0], text[s.start:s.end][-1]) for s in segs] == [("A", "B"), ("C", "C")]
    assert all(s.end - s.start <= 110 for s in segs)
    # one oversize paragraph stays whole
    assert len(segment_text("X" * 500, 100)) == 1
