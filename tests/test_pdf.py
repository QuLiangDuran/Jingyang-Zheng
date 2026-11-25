import pytest

from audit import documents
from audit.documents import clean_pdf_pages, load_document, load_pdf


def test_clean_joins_lines_and_hyphens_and_ligatures():
    pages = [["Cells were grown in BG-11 me-\ndium at 25 °C\nfor 96 h.", "Dry biomass was ﬁnal 1.42 g/L."]]
    text = clean_pdf_pages(pages)
    assert "Cells were grown in BG-11 medium at 25 °C for 96 h." in text
    assert "Dry biomass was final 1.42 g/L." in text
    assert text.count("\n\n") == 1                                   # one paragraph per block


def test_clean_keeps_hyphen_before_capital_or_digit():
    text = clean_pdf_pages([["indium tin oxide (ITO-\nCoated) anode and BG-\n11 medium"]])
    assert "ITO- Coated" in text and "BG- 11" in text                # not glued together


def test_clean_drops_running_headers_and_page_numbers():
    bodies = ["Cells were grown.", "Biomass rose.", "Power density fell.", "We conclude."]
    pages = [[f"Journal of Phycology {2020 + i}", bodies[i], "Table row 25 10", "Tail text.", str(i + 1)]
             for i in range(4)]
    text = clean_pdf_pages(pages)
    assert "Journal of Phycology" not in text
    assert "Power density fell." in text
    assert text.count("Table row 25 10") == 4                       # repeated body line is kept
    assert "\n\n4\n" not in text and not text.strip().endswith("4")


def test_clean_drops_draft_page_text_repeated_near_figures():
    real = "The average photocurrent of the HL7942 biofilm system reached 0.845 \u00b5A/cm2."
    pages = [["Results. " + real + " Other text follows here."],
             ["Life\u00a02025,\u00a015,\u00a0x\u00a0FOR\u00a0PEER\u00a0REVIEW\u00a0 7\u00a0 of\u00a0 13",
              real.replace(" ", "\u00a0") + " Figure 3. Output."]]
    text = clean_pdf_pages(pages)
    assert text.count("0.845") == 1
    assert "PEER REVIEW" not in text and "\u00a0" not in text
    assert "Figure 3. Output." in text and "Other text follows here." in text


def _make_pdf(path, pages):
    try:
        import pymupdf as fitz  # PyMuPDF can write PDFs too
    except ImportError:
        try:
            import fitz
        except ImportError:
            fitz = None
    if fitz is not None:
        doc = fitz.open()
        for lines in pages:
            page = doc.new_page()
            for i, ln in enumerate(lines):
                page.insert_text((72, 72 + 14 * i), ln, fontsize=11)
        doc.save(str(path))
        doc.close()
        return
    canvas = pytest.importorskip("reportlab.pdfgen.canvas")
    c = canvas.Canvas(str(path))
    for lines in pages:
        y = 800
        for ln in lines:
            c.drawString(72, y, ln)
            y -= 14
        c.showPage()
    c.save()


def test_load_pdf_reads_real_file(tmp_path):
    pdf = tmp_path / "paper.pdf"
    body = ["Methods. Chlorella vulgaris was grown in BG-11 medium at 25 C.",
            "The maximum power density was 2.1 mW m-2 for the control biofilm."]
    page2 = [f"Results paragraph {i}: dry biomass in group {i} was measured after 96 h of growth." for i in range(4)]
    _make_pdf(pdf, [body, page2])
    text = load_document(pdf)
    assert "maximum power density was 2.1 mW m-2" in text
    assert "Chlorella vulgaris" in text


def test_scanned_pdf_without_text_is_refused(tmp_path):
    pdf = tmp_path / "scan.pdf"
    _make_pdf(pdf, [[]])
    with pytest.raises(ValueError, match="scanned"):
        load_pdf(pdf)


def test_txt_still_loads_as_text(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes("line one\r\nline two".encode("utf-8"))  # bytes: write_text would add \r on Windows
    assert load_document(p) == "line one\nline two"


def test_sentence_split_never_loses_text():
    from audit.documents import _SENTENCE
    for para in ["Value was 0.845 uA. Next one!", "No end punctuation", "...", "e.g. 2.5 mg. ?", ""]:
        assert "".join(_SENTENCE.findall(para)) == para


def test_table_cells_that_are_bare_numbers_are_kept():
    blocks = ["Header line", "Table 1 Performance of individual cells", "Voc (mV)", "cell-1", "775", "12", "400",
              "cell-2", "730", "Body text continues here.", "Footer line"]
    text = clean_pdf_pages([blocks, ["Header line", "p2", "Footer line"], ["Header line", "p3", "Footer line"]])
    for v in ("775", "12", "400", "730"):
        assert f"\n\n{v}\n\n" in text


def test_clean_drops_acs_style_footer_block_so_a_quote_across_pages_matches():
    """ACS puts journal name, URL, DOI and page number as the LAST six lines of each page's text."""
    def page(n, top, bottom):
        filler = "\n".join(f"Body text {chr(65 + n)}{chr(65 + j)} continues here." for j in range(12))
        footer = ("ACS Sustainable Chemistry & Engineering\npubs.acs.org/journal/ascecg\nResearch Article\n"
                  f"https://doi.org/10.1021/acssuschemeng.4c00935\nACS Sustainable Chem. Eng. 2024, 12, 11120−11129\n{11120 + n}")
        return [top, filler, bottom, footer]
    pages = [page(1, "Title.", "Intro text ends here."),
             page(2, "Methods start.", "The pellet was spotted onto ITO"),
             page(3, "slide coating an area of 0.8 cm2. Next sentence.", "More methods."),
             page(4, "Results start.", "The end.")]
    text = clean_pdf_pages(pages)
    assert "pubs.acs.org" not in text and "Research Article" not in text and "doi.org" not in text
    from audit.textmatch import locate                                          # a quote across the page break matches
    assert locate(text, "spotted onto ITO slide coating an area of 0.8 cm2.") is not None
    assert "Intro text ends here.\n\nMethods start." in text                    # a finished sentence stays apart
    assert all(f"Body text {chr(65 + n)}D" in text for n in range(1, 5))         # body lines are kept
