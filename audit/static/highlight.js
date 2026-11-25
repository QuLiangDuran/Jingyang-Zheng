"use strict";
// Pure helper: paint quote highlights over the source text. No DOM access, so it can be
// tested from Node (tests/test_highlight.py). The visible text must stay exactly the source.

function escHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ranges: [{ s, e, i, label }] -- [s, e) char offsets of field i's quote.
// seg: { start, end } of the record's segment (light background).
function highlightHtml(text, ranges, seg) {
  const cuts = new Set([0, text.length, seg.start, seg.end]);
  ranges.forEach((r) => { cuts.add(r.s); cuts.add(r.e); });
  const pts = [...cuts].filter((p) => p >= 0 && p <= text.length).sort((a, b) => a - b);

  let html = "";
  for (let k = 0; k < pts.length - 1; k++) {
    const a = pts[k], b = pts[k + 1];
    let piece = escHtml(text.slice(a, b));
    const cover = ranges.filter((r) => r.s <= a && r.e >= b);
    if (cover.length) {
      piece = `<mark class="${cover.length > 1 ? "multi" : ""}" data-f="${cover.map((c) => c.i).join(",")}" ` +
        `title="${escHtml(cover.map((c) => c.label).join(" / "))}">${piece}</mark>`;
    }
    if (a >= seg.start && b <= seg.end) piece = `<span class="seg">${piece}</span>`;
    html += piece;
  }
  return html;
}

if (typeof module !== "undefined") module.exports = { highlightHtml, escHtml };
