"use strict";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const STATUS = { pending: "待审核", approved: "已确认", rejected: "已拒绝", needs_resolution: "待解决" };
const ACTION = { approve: "确认", edit: "修改", reject: "拒绝", reopen: "重新打开", flag: "标记待解决" };
const KIND = { source: "论文", condition: "条件", observation: "观测值" };
const OPEN = ["pending", "needs_resolution"];

const state = { meta: null, records: [], currentId: null, record: null, docCache: {}, editing: null, showEmpty: false, pinned: null };

// ---------- api ----------
async function api(path, body) {
  const opts = body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {};
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data.detail;
    throw new Error(typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => x.msg).join("; ") : `HTTP ${res.status}`);
  }
  return data;
}

function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = isErr ? "err" : "";
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), isErr ? 5000 : 2000);
}

function reviewer() {
  const name = $("#reviewer").value.trim();
  if (!name) { $("#reviewer").focus(); throw new Error("请先在右上角填写审核人名字"); }
  return name;
}

async function act(fn) {
  try { await fn(); } catch (e) { toast(e.message, true); }
}

// ---------- list ----------
async function loadDocs() {
  const docs = await api("/api/documents");
  const sel = $("#f-doc");
  const keep = sel.value;
  sel.innerHTML = '<option value="">全部文档</option>' +
    docs.map((d) => `<option value="${d.id}">${esc(d.title)}（待审 ${d.pending}/${d.total}）</option>`).join("");
  sel.value = keep;
}

async function loadList(keepSelection = true) {
  const q = new URLSearchParams();
  if ($("#f-status").value) q.set("status", $("#f-status").value);
  if ($("#f-doc").value) q.set("document_id", $("#f-doc").value);
  if ($("#f-flag").value) q.set("flagged", $("#f-flag").value);
  if ($("#f-kind").value) q.set("kind", $("#f-kind").value);
  state.records = await api("/api/records?" + q);
  renderList();
  if (!keepSelection && state.records.length) select(state.records[0].id);
}

function renderList() {
  $("#counts").textContent = `${state.records.length} 条`;
  $("#list").innerHTML = state.records.map((r) => `
    <li data-id="${r.id}" class="${r.id === state.currentId ? "active" : ""}">
      <div class="line1">
        <span class="badge kind ${r.kind}">${KIND[r.kind]}</span>
        <strong title="#${r.id}">${esc(r.system_id)}</strong>
        <span class="badge ${r.status}">${STATUS[r.status]}</span>
        ${r.errors ? `<span class="badge error">${r.errors} 错误</span>` : ""}
        ${r.warnings ? `<span class="badge warning">${r.warnings} 警告</span>` : ""}
      </div>
      <div class="line2">${esc(r.summary)}</div>
    </li>`).join("") || '<li class="muted">没有记录</li>';
}

// ---------- detail ----------
async function select(id) {
  state.currentId = id;
  state.editing = null;
  renderList();
  state.record = await api(`/api/records/${id}`);
  const docId = state.record.document_id;
  if (!state.docCache[docId]) state.docCache[docId] = await api(`/api/documents/${docId}`);
  render(true);
}

function render(scroll) {
  renderSource(scroll);
  renderDetail();
}

function renderSource(scroll) {
  const rec = state.record;
  const doc = state.docCache[rec.document_id];
  $("#doc-title").textContent = "· " + doc.title;
  const text = doc.text;

  const ranges = rec.fields.map((f, i) => (f.highlight ? { s: f.highlight[0], e: f.highlight[1], i, label: f.label } : null)).filter(Boolean);
  const box = $("#source-text");
  box.classList.remove("muted");
  box.innerHTML = highlightHtml(text, ranges, rec.segment);  // static/highlight.js
  state.pinned = null;
  if (scroll) {
    const first = box.querySelector("mark") || box.querySelector(".seg");
    if (first) first.scrollIntoView({ block: "center" });
  }
}

// All quotes are shown faintly; only the hovered / clicked field's quote is highlighted.
function setActive(i) {
  const key = i == null ? null : String(i);
  document.querySelectorAll("#source-text mark").forEach((m) =>
    m.classList.toggle("active", key !== null && m.dataset.f.split(",").includes(key)));
  document.querySelectorAll("#detail-body .field").forEach((el) =>
    el.classList.toggle("active", key !== null && el.dataset.i === key));
}

function scrollToField(i) {
  state.pinned = state.pinned === i ? null : i;  // click again to unpin
  setActive(state.pinned);
  const m = [...document.querySelectorAll("#source-text mark")].find((x) => x.dataset.f.split(",").includes(String(i)));
  if (m && state.pinned !== null) m.scrollIntoView({ block: "center", behavior: "smooth" });
}

function fieldHtml(r, f, i, fl) {
  const editing = state.editing === f.name;
  const open = OPEN.includes(r.status);
  return `
    <div class="field" data-name="${f.name}" data-i="${i}">
      <div class="head">
        <span class="label" title="${esc(fieldDesc(f.name))}">${esc(f.label)}</span>
        ${f.required ? `<span class="muted" title="${f.electrical_only ? "电学数据必填" : "必填"}">*${f.electrical_only ? "⚡" : ""}</span>` : ""}
        ${f.edited ? '<span class="badge edited">已修改</span>' : ""}
        ${fl.map((x) => `<span class="badge ${x.level}" title="${esc(x.message)}">${esc(x.code)}</span>`).join("")}
        ${open && !editing ? `<button data-edit="${f.name}">修改</button>` : ""}
      </div>
      <div class="value ${f.value ? "" : "empty"}">${f.value ? esc(f.value) : "（空）"}</div>
      ${f.quote ? `<div class="quote ${f.highlight ? "" : "notfound"}" data-goto="${i}">“${esc(f.quote)}”${f.highlight ? "" : " — 原文中找不到"}</div>` : ""}
      ${f.edited ? `<div class="was">AI 原值：${esc(f.proposed_value ?? "（空）")}${f.proposed_quote !== f.quote ? ` · 原引文：“${esc(f.proposed_quote ?? "")}”` : ""}</div>` : ""}
      ${editing ? editorHtml(f) : ""}
    </div>`;
}

function fieldDesc(name) {
  const m = state.meta.fields.find((x) => x.name === name);
  return m ? m.description : "";
}

function linksHtml(items, title) {
  if (!items.length) return "";
  return `<div class="muted">${title}</div><ul class="links">${items.map((x) => `
    <li data-goto-record="${x.id}"><span class="badge kind ${x.kind}">${KIND[x.kind]}</span>
      <strong>${esc(x.system_id)}</strong> <span class="badge ${x.status}">${STATUS[x.status]}</span>
      <span class="muted">${esc(x.summary)}</span></li>`).join("")}</ul>`;
}

function renderDetail() {
  const r = state.record;
  const flagsByField = {};
  r.flags.forEach((f) => (flagsByField[f.field ?? "_"] ||= []).push(f));
  const recordFlags = flagsByField["_"] || [];

  // Group fields; hide empty optional fields unless asked (~100 fields per condition would bury the useful ones).
  const groups = [];
  r.fields.forEach((f, i) => {
    const fl = flagsByField[f.name] || [];
    const visible = state.showEmpty || f.value || f.quote || f.edited || fl.length || state.editing === f.name ||
      (f.required && (!f.electrical_only || r.is_electrical));
    if (!visible) return;
    let g = groups.find((x) => x.name === f.group);
    if (!g) groups.push((g = { name: f.group, items: [] }));
    g.items.push(fieldHtml(r, f, i, fl));
  });
  const hidden = r.fields.length - groups.reduce((n, g) => n + g.items.length, 0);
  const fieldsHtml = groups.map((g) => `
    <details class="group" open><summary>${esc(g.name)} <span class="muted">(${g.items.length})</span></summary>
      ${g.items.join("")}</details>`).join("");

  let actions = "";
  if (OPEN.includes(r.status)) {
    actions = `
      <div class="row"><input id="a-note" placeholder="备注（确认时可选）/ 原因（拒绝、待解决时必填）"></div>
      <div class="row">
        <button class="primary" id="btn-approve">确认 ✓</button>
        <button class="danger" id="btn-reject">拒绝 ✕</button>
        ${r.status === "pending" ? '<button id="btn-flag">待解决 ?</button>' : '<button id="btn-reopen">退回待审核</button>'}
      </div>`;
  } else {
    actions = `
      <div class="row"><input id="a-note" placeholder="重新打开的原因（必填）"></div>
      <div class="row"><button id="btn-reopen">重新打开</button></div>`;
  }

  $("#detail-body").classList.remove("muted");
  $("#detail-body").innerHTML = `
    <div class="card">
      <div class="status-line">
        <span class="badge kind ${r.kind}">${KIND[r.kind]}</span>
        <strong>${esc(r.system_id)}</strong>
        <span class="muted">#${r.id}</span>
        <span class="badge ${r.status}">${STATUS[r.status]}</span>
        ${r.kind !== "source" && r.is_electrical ? '<span class="badge kind">⚡ 电学</span>' : ""}
        ${r.reviewer && r.status !== "pending" ? `<span class="muted">${esc(r.reviewer)} · ${esc(fmt(r.reviewed_at))}</span>` : ""}
      </div>
      <div class="muted">文档：${esc(r.document_title)}${r.segment.count > 1 ? ` · 第 ${r.segment.idx + 1}/${r.segment.count} 块` : ""}${r.experiment_id ? ` · 实验 ${esc(r.experiment_id)}` : ""}${r.arm_id ? ` · 组 ${esc(r.arm_id)}` : ""}</div>
      ${recordFlags.length ? `<ul class="flags">${recordFlags.map((x) => `<li class="${x.level}">${esc(x.message)}</li>`).join("")}</ul>` : ""}
      ${linksHtml(r.parents, "所属")}
      ${linksHtml(r.children, r.kind === "source" ? "条件" : "观测值")}
    </div>
    <div class="card">
      <h3>字段（点引文可在原文中定位；* 必填，*⚡ 电学数据必填）</h3>
      <div class="toolbar"><label><input type="checkbox" id="t-empty" ${state.showEmpty ? "checked" : ""}>
        显示空的可选字段</label>${hidden ? `<span class="muted">已隐藏 ${hidden} 个</span>` : ""}</div>
      ${fieldsHtml}
    </div>
    <div class="card actions"><h3>审核</h3>${actions}</div>
    <div class="card"><h3>审核日志</h3>${logHtml(r.log)}</div>
    <div class="card">
      <details><summary>AI 提取信息</summary>
        <div class="muted">模型 ${esc(r.run.model)} · 提示词 ${esc(r.run.prompt_version)} · 字段 ${esc(r.run.schema_version)} · ${esc(fmt(r.run.created_at))}</div>
        ${r.run.error ? `<div class="muted">解析说明：${esc(r.run.error)}</div>` : ""}
        <pre>${esc(r.run.raw_response)}</pre>
      </details>
    </div>`;
  setActive(state.pinned);
}

function editorHtml(f) {
  const isLink = f.filled_by === "link";
  const opts = f.choices ? [...f.choices, "other", "unspecified"] : null;
  const valueInput = opts
    ? `<select id="e-value"><option value=""></option>${opts.map((c) =>
        `<option ${c === f.value ? "selected" : ""}>${esc(c)}</option>`).join("")}${f.value && !opts.includes(f.value)
        ? `<option selected>${esc(f.value)}（不在选项里）</option>` : ""}</select>
       <div class="muted">临时选项，待 Carly 确认</div>`
    : `<input id="e-value" value="${esc(f.value ?? "")}">`;
  return `
    <div class="editor">
      <label>新值${isLink ? "（关联标签：填对方记录的 key）" : ""}</label>
      ${valueInput}
      ${isLink ? "" : `
      <label>引文（可在左侧原文中选中文字后点“填入选中原文”）</label>
      <textarea id="e-quote" rows="3">${esc(f.quote ?? "")}</textarea>
      <div class="row"><button id="e-pick">填入选中原文</button></div>`}
      <label>修改原因（必填）</label>
      <input id="e-reason">
      <div class="row">
        <button class="primary" id="e-save">保存修改</button>
        <button id="e-cancel">取消</button>
      </div>
    </div>`;
}

function logHtml(log) {
  if (!log.length) return '<div class="muted">暂无</div>';
  return `<ul class="log">${log.map((l) => `
    <li><strong>${esc(l.reviewer)}</strong> ${ACTION[l.action] ?? l.action}
      ${l.field ? `<code>${esc(l.field)}</code>：${esc(l.old_value ?? "（空）")} → ${esc(l.new_value ?? "（空）")}` : ""}
      ${l.reason ? `<div class="muted">原因：${esc(l.reason)}</div>` : ""}
      <div class="muted">${esc(fmt(l.created_at))}</div></li>`).join("")}</ul>`;
}

function fmt(iso) {
  return iso ? new Date(iso).toLocaleString() : "";
}

// ---------- actions ----------
async function after(updated, msg, advance) {
  state.record = updated;
  state.editing = null;
  toast(msg);
  await Promise.all([loadList(), loadDocs()]);
  if (advance) {
    const next = state.records.find((x) => x.status === "pending" && x.id !== updated.id);
    if (next && $("#f-status").value === "pending") return select(next.id);
  }
  render(false);
}

$("#detail-body").addEventListener("click", (ev) => act(async () => {
  const t = ev.target;
  const r = state.record;
  if (t.dataset.goto !== undefined) return scrollToField(Number(t.dataset.goto));
  const link = t.closest("[data-goto-record]");
  if (link) return select(Number(link.dataset.gotoRecord));
  if (t.id === "t-empty") { state.showEmpty = t.checked; renderDetail(); return; }
  if (t.dataset.edit) { state.editing = t.dataset.edit; renderDetail(); $("#e-value").focus(); return; }
  if (t.id === "e-cancel") { state.editing = null; renderDetail(); return; }
  if (t.id === "e-pick") {
    const sel = window.getSelection().toString().trim();
    if (!sel) throw new Error("请先在左侧原文中选中一段文字");
    $("#e-quote").value = sel;
    return;
  }
  if (t.id === "e-save") {
    const f = r.fields.find((x) => x.name === state.editing);
    const value = $("#e-value").value, quote = $("#e-quote") ? $("#e-quote").value : (f.quote ?? "");
    const body = { reviewer: reviewer(), field: f.name, reason: $("#e-reason").value };
    if (value.trim() !== (f.value ?? "")) body.value = value;
    if (quote.trim() !== (f.quote ?? "")) body.quote = quote;
    return after(await api(`/api/records/${r.id}/edit`, body), "已保存修改", false);
  }
  if (t.id === "btn-approve")
    return after(await api(`/api/records/${r.id}/approve`, { reviewer: reviewer(), note: $("#a-note").value }), "已确认", true);
  if (t.id === "btn-reject")
    return after(await api(`/api/records/${r.id}/reject`, { reviewer: reviewer(), reason: $("#a-note").value }), "已拒绝", true);
  if (t.id === "btn-flag")
    return after(await api(`/api/records/${r.id}/flag`, { reviewer: reviewer(), reason: $("#a-note").value }), "已标记待解决", true);
  if (t.id === "btn-reopen")
    return after(await api(`/api/records/${r.id}/reopen`, { reviewer: reviewer(), reason: $("#a-note").value }), "已重新打开", false);
}));

// Hover a field (right) or a highlight (left) to light up just that field's quote.
$("#detail-body").addEventListener("mouseover", (ev) => {
  const f = ev.target.closest(".field[data-i]");
  setActive(f ? Number(f.dataset.i) : state.pinned);
});
$("#detail-body").addEventListener("mouseleave", () => setActive(state.pinned));
$("#source-text").addEventListener("mouseover", (ev) => {
  const m = ev.target.closest("mark");
  setActive(m ? Number(m.dataset.f.split(",")[0]) : state.pinned);
});
$("#source-text").addEventListener("mouseleave", () => setActive(state.pinned));
$("#source-text").addEventListener("click", (ev) => {
  const m = ev.target.closest("mark");
  if (!m) return;
  const i = Number(m.dataset.f.split(",")[0]);
  state.pinned = i;
  setActive(i);
  const el = document.querySelector(`#detail-body .field[data-i="${i}"]`);
  if (el) el.scrollIntoView({ block: "center", behavior: "smooth" });
});

// Keep the text selection when clicking the "pick" button.
$("#detail-body").addEventListener("mousedown", (ev) => { if (ev.target.id === "e-pick") ev.preventDefault(); });

$("#list").addEventListener("click", (ev) => {
  const li = ev.target.closest("li[data-id]");
  if (li) act(() => select(Number(li.dataset.id)));
});

["#f-doc", "#f-kind", "#f-status", "#f-flag"].forEach((s) => $(s).addEventListener("change", () => act(() => loadList(false))));

$("#reviewer").addEventListener("input", () => {
  try { localStorage.setItem("reviewer", $("#reviewer").value); } catch {}
});

// ---------- boot ----------
act(async () => {
  try { $("#reviewer").value = localStorage.getItem("reviewer") || ""; } catch {}
  state.meta = await api("/api/meta");
  await loadDocs();
  await loadList(false);
});
