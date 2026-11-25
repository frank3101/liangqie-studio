/* 亮切工坊网页版 —— 前端逻辑 */
"use strict";

/* ================= 基础工具 ================= */
async function api(url, body, method) {
  const opt = body !== undefined
    ? { method: method || "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body) }
    : { method: method || "GET" };
  const r = await fetch(url, opt);
  if (r.status === 401) { location.href = "/login"; throw new Error("未登录"); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    if (r.status === 400 && data.detail) { alertMsg("提示", data.detail); }
    else { alertMsg("出错", (data.detail || r.statusText) + "（" + r.status + "）"); }
    throw new Error(data.detail || r.statusText);
  }
  return data;
}

function showModal(id) { document.getElementById(id).classList.add("show"); }
function hideModal(id) { document.getElementById(id).classList.remove("show"); }
function alertMsg(title, msg) {
  document.getElementById("alert-title").textContent = title;
  document.getElementById("alert-msg").textContent = msg;
  showModal("alert-modal");
}

/* 日志 */
function appendLog(channel, entry) {
  const box = document.getElementById("log-" + channel);
  if (!box) return;
  const tag = { warn: "⚠ ", error: "✖ ", ok: "✔ " }[entry.level] || "";
  const div = document.createElement("div");
  if (entry.level && entry.level !== "info") div.className = entry.level;
  div.textContent = `[${entry.ts}] ${tag}${entry.msg}`;
  box.appendChild(div);
  while (box.children.length > 600) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
}

/* WebSocket：日志 + 远程确认 */
function connectWs() {
  const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  ws.onmessage = (ev) => {
    const d = JSON.parse(ev.data);
    if (d.type === "log") appendLog(d.channel, d);
    else if (d.type === "confirm") showConfirm(d);
    else if (d.type === "confirm_closed") closeConfirm(d.id);
  };
  ws.onclose = (ev) => {
    if (ev.code === 1008) { location.href = "/login"; return; }  // 会话失效
    setTimeout(connectWs, 2000);
  };
}

/* 远程确认（后台任务弹的「是/否」） */
let confirmId = null;
function showConfirm(d) {
  confirmId = d.id;
  document.getElementById("confirm-msg").textContent = d.msg;
  showModal("confirm-modal");
}
function closeConfirm(id) {
  if (confirmId === id) { confirmId = null; hideModal("confirm-modal"); }
}
function confirmAnswer(ok) {
  if (confirmId) api("/api/confirm/" + confirmId, { ok }).catch(() => {});
  confirmId = null;
  hideModal("confirm-modal");
}

/* 弹窗是否被确认弹窗占用（避免盖住） */
function confirmBusy() { return confirmId !== null; }

/* 页面切换 */
const pageInit = {};
let currentPage = "accounts";
document.querySelectorAll(".nav-item[data-page], .side-link[data-page]").forEach(el => {
  if (el.dataset.page) el.addEventListener("click", () => showPage(el.dataset.page));
});
/* 折叠分组（电商违规申诉）：点父项展开并进第一个子页，再点收起 */
document.querySelectorAll(".nav-parent").forEach(el => {
  el.addEventListener("click", () => {
    const group = el.closest(".nav-group");
    const child = group.querySelector(".nav-item.sub");
    if (group.classList.contains("open") && group.querySelector(".nav-item.sub.active")) {
      group.classList.remove("open");
    } else {
      group.classList.add("open");
      if (child) showPage(child.dataset.page);
    }
  });
});
function showPage(key) {
  currentPage = key;
  document.querySelectorAll(".nav-item, .side-link").forEach(el =>
    el.classList.toggle("active", el.dataset.page === key));
  document.querySelectorAll(".page").forEach(el =>
    el.classList.toggle("active", el.id === "page-" + key));
  const sub = document.querySelector(`.nav-item.sub[data-page="${key}"]`);
  if (sub) sub.closest(".nav-group").classList.add("open");
  document.querySelectorAll(".nav-parent").forEach(p => {
    const g = p.closest(".nav-group");
    p.classList.toggle("active-child", !!g.querySelector(".nav-item.sub.active"));
  });
  if (pageInit[key]) pageInit[key]();
}

/* ================= 菜单栏 展示/隐藏（手机上抽屉式） ================= */
const SB_MOBILE = window.matchMedia("(max-width: 860px)");
const sbLayout = document.getElementById("layout");
const sbMask = document.getElementById("sb-mask");
function sbCloseDrawer() {
  sbLayout.classList.remove("sb-open");
  sbMask.classList.remove("show");
}
function sbToggle() {
  if (SB_MOBILE.matches) {
    sbLayout.classList.toggle("sb-open");
    sbMask.classList.toggle("show", sbLayout.classList.contains("sb-open"));
  } else {
    sbLayout.classList.toggle("sb-hidden");
    try { localStorage.setItem("sb_hidden",
      sbLayout.classList.contains("sb-hidden") ? "1" : "0"); } catch (e) {}
  }
}
document.getElementById("sb-toggle").addEventListener("click", sbToggle);
sbMask.addEventListener("click", sbCloseDrawer);
/* 手机上点导航后自动收起抽屉 */
document.querySelectorAll(".nav-item, .side-link").forEach(el =>
  el.addEventListener("click", () => { if (SB_MOBILE.matches) sbCloseDrawer(); }));
/* 恢复桌面端上次的收起状态 */
try { if (localStorage.getItem("sb_hidden") === "1") sbLayout.classList.add("sb-hidden"); } catch (e) {}
/* 手机/桌面切换时清理状态 */
if (SB_MOBILE.addEventListener) SB_MOBILE.addEventListener("change", sbCloseDrawer);

/* ================= 全局状态轮询 ================= */
let G = { accounts: [], env: {}, services: {} };
async function pollState() {
  try {
    G = await api("/api/state");
    renderAccounts();
    updateEnv();
    updateBadges();
  } catch (e) { /* 服务重启中 */ }
}
function updateEnv() {
  const el = document.getElementById("env-label");
  const env = G.env || {};
  if (env.chrome_found) {
    el.textContent = "浏览器：已找到\n" + (env.chrome_name || "chromium");
    el.classList.remove("warn");
  } else {
    el.textContent = "浏览器：未找到\n请在设置里指定";
    el.classList.add("warn");
  }
}
function updateBadges() {
  const s = G.services || {};
  const set = (id, run, word) => {
    const el = document.getElementById(id);
    el.textContent = run ? (word || "进行中") : "空闲";
    el.classList.toggle("on", !!run);
  };
  set("mat-badge", s.material);
  set("pub-badge", s.publish);
  set("ap-badge", s.appeal);
  set("byn-badge", s.buyin);
  set("byn-badge2", s.buyin);
}

/* ================= 账号管理 ================= */
const accSelected = new Set();
function renderAccounts() {
  const tb = document.getElementById("acc-tbody");
  const sel = new Set(accSelected);
  tb.innerHTML = "";
  for (const a of (G.accounts || [])) {
    const tr = document.createElement("tr");
    tr.className = "selectable" + (sel.has(a.id) ? " selected" : "");
    tr.dataset.id = a.id;
    let login = "—";
    if (a.login_state === "logged_in") login = ("已登录 " + (a.nickname || "")).trim();
    else if (a.login_state === "logged_out") login = "未登录";
    else if (a.login_state === "unknown") login = "待确认";
    // 百应申诉状态（来自 /api/state 的 buyin 字段）
    const by = (G.buyin || {})[a.name] || {};
    let byTxt = "未启用", byCls = "by-idle";
    if (by.enabled !== false) {
      byTxt = by.login_state || "未检查";
      byCls = by.state_cls === "ok" ? "by-ok"
            : by.state_cls === "bad" ? "by-bad"
            : by.state_cls === "new" ? "by-new" : "by-idle";
      if (by.mat_ok === false) byTxt += " · 缺材料";
    }
    const fansTxt = a.fans_text || "";
    tr.innerHTML = `
      <td>${esc(a.name)}</td><td>${esc(a.note || "")}</td>
      <td class="center" style="color:${fansTxt ? "var(--text)" : "var(--muted)"}"
          title="${esc(a.fans_at ? "采集于 " + a.fans_at : "还没有粉丝数，开浏览器后点「检测登录」采集")}">${esc(fansTxt || "—")}</td>
      <td style="color:${a.running ? "var(--ok)" : "var(--text)"}">${a.running ? "运行中" : "未打开"}</td>
      <td>${esc(login)}</td>
      <td class="${byCls}" title="${esc(by.last_result || "")}">${esc(byTxt)}</td>
      <td class="center">${by.today || 0}</td>
      <td style="color:var(--muted)">${esc(a.profile)}</td>`;
    tr.addEventListener("click", (ev) => {
      if (ev.ctrlKey || ev.metaKey) { sel.has(a.id) ? sel.delete(a.id) : sel.add(a.id); }
      else { sel.clear(); sel.add(a.id); }
      accSelected.clear(); sel.forEach(x => accSelected.add(x));
      tb.querySelectorAll("tr").forEach(r =>
        r.classList.toggle("selected", sel.has(r.dataset.id)));
    });
    tr.addEventListener("dblclick", () => api("/api/accounts/open", { id: a.id }));
    tb.appendChild(tr);
  }
}
function accIds() { return [...accSelected]; }
function accByIds() {
  return (G.accounts || []).filter(a => accSelected.has(a.id));
}
function accBulk(action) {
  const ids = accIds();
  if (!ids.length) { alertMsg("提示", "请先在列表里选中账号（可按住 Ctrl 多选）"); return; }
  if (action === "delete") {
    if (!window.confirm(`确定删除选中的 ${ids.length} 个账号？（只从列表移除，不删磁盘上的登录数据）`)) return;
    api("/api/accounts/delete", { ids }).then(pollState);
    return;
  }
  if (action === "edit") {
    if (ids.length !== 1) { alertMsg("提示", "编辑请一次只选一个账号"); return; }
    accAddDialog(accByIds()[0]);
    return;
  }
  for (const a of accByIds()) {
    if (action === "open") api("/api/accounts/open", { id: a.id }).catch(() => {});
    else if (action === "check") api("/api/accounts/check", { id: a.id }).catch(() => {});
    else if (action === "close") api("/api/accounts/close", { id: a.id }).catch(() => {});
  }
}

async function accBuyinCheck() {
  const names = accByIds().map(a => a.name);
  if (!names.length) { alertMsg("提示", "先在列表里选中账号（可按住 Ctrl 多选）"); return; }
  await bynCheck(names);
}

let accEditingId = null;
function accAddDialog(account) {
  accEditingId = account ? account.id : null;
  document.getElementById("acc-modal-title").textContent = account ? "编辑账号" : "新增账号";
  document.getElementById("acc-name").value = account ? account.name : "";
  document.getElementById("acc-note").value = account ? (account.note || "") : "";
  document.getElementById("acc-profile").value = account ? (account.profile || "") : "";
  document.getElementById("acc-port").value = account && account.port ? account.port : "";
  showModal("acc-modal");
}
async function accSave() {
  const payload = {
    name: document.getElementById("acc-name").value.trim(),
    note: document.getElementById("acc-note").value.trim(),
    profile: document.getElementById("acc-profile").value.trim(),
    port: document.getElementById("acc-port").value.trim(),
  };
  try {
    if (accEditingId) await api("/api/accounts/edit", { id: accEditingId, ...payload });
    else await api("/api/accounts/add", payload);
    hideModal("acc-modal");
    accEditingId = null;
    pollState();
  } catch (e) { /* api 已弹提示 */ }
}

async function accImportDialog() {
  const data = await api("/api/accounts/scan_profiles");
  const list = document.getElementById("import-list");
  list.innerHTML = "";
  if (!data.dirs.length) { list.innerHTML = '<div class="muted">没找到可导入的 profile（有 Local State 或 Default 目录的才算）</div>'; }
  data.dirs.forEach((d, i) => {
    const row = document.createElement("label");
    row.className = "chk"; row.style.display = "flex"; row.style.padding = "5px 6px";
    row.innerHTML = `<input type="checkbox" checked data-dir="${esc(d)}"> <span style="margin-left:6px">${esc(d)}</span>`;
    list.appendChild(row);
  });
  showModal("import-modal");
}
async function importSave() {
  const dirs = [...document.querySelectorAll("#import-list input:checked")].map(x => x.dataset.dir);
  if (!dirs.length) { alertMsg("提示", "先勾选要导入的账号"); return; }
  await api("/api/accounts/import", { dirs });
  hideModal("import-modal");
  pollState();
}

function gotoVnc() { showPage("vnc"); loadVnc(false); }

/* ================= 素材处理（采集 + 剪辑） ================= */
let MAT = null;
const pSel = new Set();
pageInit.material = () => { refreshMaterial(); };

async function refreshMaterial() {
  MAT = await api("/api/material");
  document.getElementById("mat-out").value = MAT.out_root;
  document.getElementById("mat-max").value = MAT.cfg.max_videos;
  document.getElementById("mat-autoclip").checked = !!MAT.cfg.auto_clip;
  document.getElementById("mat-autocopy").checked = MAT.cfg.auto_copy !== false;
  document.getElementById("mat-count").value = (MAT.cfg.clip || {}).count || 1;
  document.getElementById("mat-face").checked = !!(MAT.cfg.clip || {}).reject_face;
  document.getElementById("mat-still").checked = !!(MAT.cfg.clip || {}).reject_still;
  renderProducts();
  renderMatRows();
  matButtons();
}

function matButtons() {
  const run = MAT && MAT.running;
  ["mat-btn-one", "mat-btn-collect", "mat-btn-clip"].forEach(id =>
    document.getElementById(id).classList.toggle("disabled", !!run));
}

function renderProducts() {
  const tb = document.getElementById("ptable-tbody");
  tb.innerHTML = "";
  const items = (MAT && MAT.products) || [];
  if (!items.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 3;
    td.style.textAlign = "center";
    td.style.color = "var(--muted)";
    td.style.cursor = "pointer";
    tr.className = "ptable-empty";
    td.textContent = "（空）点这里手动添加商品；或用右侧按钮「从橱窗导入商品」「从选品车导入商品」";
    td.addEventListener("click", pAddText);
    tr.appendChild(td);
    tb.appendChild(tr);
    updatePInfo();
    return;
  }
  items.forEach((it, i) => {
    const tr = document.createElement("tr");
    tr.className = pSel.has(i) ? "selected" : "";
    tr.innerHTML = `<td class="center">${i + 1}</td><td class="editable" data-k="pid">${esc(it.pid)}</td>
      <td class="editable" data-k="title">${esc(it.title)}</td>`;
    tr.addEventListener("click", (ev) => {
      if (ev.target.classList.contains("editable") && ev.detail >= 2) return;
      if (ev.ctrlKey || ev.metaKey) { pSel.has(i) ? pSel.delete(i) : pSel.add(i); }
      else { pSel.clear(); pSel.add(i); }
      [...tb.children].forEach((r, ri) => r.classList.toggle("selected", pSel.has(ri)));
      updatePInfo();
    });
    tr.querySelectorAll(".editable").forEach(cell => {
      cell.addEventListener("dblclick", () => pEditCell(i, cell.dataset.k, cell));
    });
    tb.appendChild(tr);
  });
  updatePInfo();
}
function updatePInfo() {
  const n = (MAT && MAT.products || []).length;
  document.getElementById("ptable-info").textContent =
    n ? `共 ${n} 个商品，已选 ${pSel.size} 个` : "清单是空的";
  document.getElementById("ptable-del").classList.toggle("on", pSel.size > 0);
}
function pEditCell(i, key, cell) {
  if (cell.querySelector("input")) return;
  const old = cell.textContent;
  cell.innerHTML = "";
  const input = document.createElement("input");
  input.value = old;
  cell.appendChild(input);
  input.focus(); input.select();
  const commit = async () => {
    const val = input.value.trim();
    cell.textContent = val || old;
    if (val && val !== old) {
      await api("/api/material/products", { action: "edit", index: i, pid: key === "pid" ? val : null, title: key === "title" ? val : null });
      refreshMaterial();
    }
  };
  input.addEventListener("keydown", ev => {
    if (ev.key === "Enter") commit();
    if (ev.key === "Escape") { cell.textContent = old; }
  });
  input.addEventListener("blur", commit);
}
async function pDelSelected() {
  if (!pSel.size) return;
  await api("/api/material/products", { action: "delete", indexes: [...pSel] });
  pSel.clear();
  refreshMaterial();
}
function pAddText() {
  const text = window.prompt("每行一个商品：「商品ID 标题」（或直接贴带 id= 的商品链接，可多行）");
  if (!text || !text.trim()) return;
  api("/api/material/products", { action: "add_text", text }).then(refreshMaterial)
    .catch(() => {});
}

async function matSaveOut() {
  const v = document.getElementById("mat-out").value.trim();
  if (!v || (MAT && v === MAT.out_root)) return;
  if (!(await fsCheck(v, "输出目录"))) return;
  await api("/api/material/settings", { out_root: v });
  refreshMaterial();
}
async function matSaveSettings() {
  await api("/api/material/settings", {
    max_videos: +document.getElementById("mat-max").value || 5,
    auto_clip: document.getElementById("mat-autoclip").checked,
    auto_copy: document.getElementById("mat-autocopy").checked,
    clip_count: +document.getElementById("mat-count").value || 1,
    reject_face: document.getElementById("mat-face").checked,
    reject_still: document.getElementById("mat-still").checked,
  });
  refreshMaterial();
}
async function matAct(action) { await api("/api/material/" + action, {}); setTimeout(refreshMaterial, 800); }
async function matAct2(url, body, then) { await api(url, body); if (then) then(); }
function matCollect(oneClick) {
  if (MAT && MAT.running) return;
  api("/api/material/" + (oneClick ? "collect_one" : "collect"), {})
    .then(d => { if (d.msg) HUB_log("material", d.msg); setTimeout(refreshMaterial, 600); })
    .catch(() => {});
}
async function matImportShopwindow() {
  if (MAT && MAT.running) return;
  const d = await api("/api/material/import_shopwindow", {});
  if (d.ok === false) alertMsg("提示", d.msg || "启动失败");
  setTimeout(refreshMaterial, 600);
}
async function matImportCart() {
  if (MAT && MAT.running) return;
  const d = await api("/api/material/import_selection_cart", {});
  if (d.ok === false) alertMsg("提示", d.msg || "启动失败");
  setTimeout(refreshMaterial, 600);
}
async function matCartCleanup() {
  if (MAT && MAT.running) return;
  const d = await api("/api/material/cart_cleanup", {});
  if (d.ok === false) { alertMsg("提示", d.msg || "启动失败"); return; }
  alertMsg("提示", d.msg || "已开始清理");
  setTimeout(refreshMaterial, 600);
}
async function matCleanEmpty() {
  const d = await api("/api/material/clean_empty", { dry: true });
  if (d.ok === false) { alertMsg("提示", d.msg); return; }
  const names = (d.names || []).join("\n  · ");
  if (!window.confirm(`发现 ${d.names.length} 个空壳文件夹（无毛料、无成品）：\n\n  · ${names}\n\n删除后不可恢复，确定全部删除？`)) {
    return;
  }
  await api("/api/material/clean_empty", {});
  refreshMaterial();
}
async function matCleanNoClip() {
  if (MAT && MAT.running) { alertMsg("提示", "素材任务正在运行：先「停止」，等任务结束后再清空。"); return; }
  const d = await api("/api/material/clean_no_clip", { dry: true });
  if (d.ok === false) { alertMsg("提示", d.msg); return; }
  const names = d.names || [];
  const shown = names.slice(0, 15).map(n => "  · " + n).join("\n")
    + (names.length > 15 ? `\n  … 还有 ${names.length - 15} 个` : "");
  const raw = (d.with_raw && d.with_raw.length)
    ? `\n\n其中 ${d.with_raw.length} 个还有毛料（共 ${d.raw_total} 条视频），删掉后要重新采集。`
    : "";
  if (!window.confirm(`发现 ${names.length} 个没有成品的商品文件夹：\n\n${shown}${raw}\n\n整目录删除，不可恢复，确定全部清空？`)) {
    return;
  }
  const r = await api("/api/material/clean_no_clip", {});
  if (r.ok === false) { alertMsg("提示", r.msg || "清空失败"); return; }
  HUB_log("material", `已清空无成品文件夹 ${r.removed || 0} 个` + (r.failed ? `，失败 ${r.failed} 个` : ""));
  refreshMaterial();
}
function renderMatRows() {
  const tb = document.getElementById("mat-tbody");
  tb.innerHTML = "";
  for (const r of (MAT && MAT.rows) || []) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${esc(r.pid)}</td><td>${esc((r.title || "").slice(0, 40))}</td>
      <td>${esc(r.state || "")}</td><td>${r.videos ?? ""}</td><td>${r.clips ?? ""}</td>`;
    tr.className = "selectable";
    tr.addEventListener("dblclick", () => fsBrowse(r.pid));
    tr.addEventListener("contextmenu", (ev) => {
      ev.preventDefault();
      if (window.confirm(`删除商品 ${r.pid} 的整个目录？\n\n${r.title || ""}\n\n毛料、成品、商品资料会一并删除，不可恢复！`)) {
        api("/api/material/delete_product/" + encodeURIComponent(r.pid), {})
          .then(refreshMaterial).catch(() => {});
      }
    });
    tb.appendChild(tr);
  }
}

/* ================= 作品发布 ================= */
let PUB = null;
const pubSel = new Set();
// 切到本页就自动重扫素材目录（商品ID/成品列表不会再是空的）
pageInit.publish = () => {
  refreshPublish(false, true).catch(() => refreshPublish(false, false).catch(() => {}));
};

async function refreshPublish(keepLog, rescan) {
  // rescan 走 POST（/api/publish/rescan 内部会重扫目录，条数没变不写日志）
  PUB = await api(rescan ? "/api/publish/rescan" : "/api/publish", rescan ? {} : undefined);
  document.getElementById("pub-out").value = PUB.out_root;
  document.getElementById("pub-cart").checked = !!PUB.cfg.cart;
  document.getElementById("pub-cartfail").value = PUB.cfg.cart_fail_policy || "skip_item";
  document.getElementById("pub-dry").checked = !!PUB.cfg.dry_run;
  document.getElementById("pub-appeal").checked = !!PUB.cfg.appeal_after_publish;
  document.getElementById("pub-interval").value = PUB.cfg.interval_min || 30;
  renderPubAccounts(true);
  renderPubTasks();
  renderPubTopics();
  pubButtons();
}

function pubButtons() {
  const run = PUB && PUB.running;
  ["pub-btn-gen", "pub-btn-go", "pub-btn-edit"].forEach(id =>
    document.getElementById(id).classList.toggle("disabled", !!run));
}

function renderPubAccounts(autoAll) {
  const tb = document.getElementById("pub-acc-tbody");
  tb.innerHTML = "";
  const accts = (PUB && PUB.accounts) || [];
  if (autoAll && accts.length && !pubSel.size) accts.forEach(a => pubSel.add(a.name));
  for (const a of accts) {
    const tr = document.createElement("tr");
    tr.className = "selectable" + (pubSel.has(a.name) ? " selected" : "");
    tr.dataset.name = a.name;
    tr.innerHTML = `<td>${esc(a.name)}</td><td>${esc(a.nick || "")}</td>
      <td class="center">${a.today}</td><td class="center editable">${a.limit}</td>
      <td class="center">${a.left}</td>
      <td class="center editable" style="color:${a.cover === "需设封面" ? "var(--accent)" : "inherit"}">${a.cover}</td>`;
    tr.addEventListener("click", (ev) => {
      if (ev.ctrlKey || ev.metaKey) { pubSel.has(a.name) ? pubSel.delete(a.name) : pubSel.add(a.name); }
      else { pubSel.clear(); pubSel.add(a.name); }
      repaintPubAccounts();
    });
    tr.querySelectorAll(".editable").forEach((cell, ci) => {
      cell.addEventListener("dblclick", () => {
        if (ci === 1) pubToggleCover([a.name]);      // 封面列
        else pubEditLimitDialog([a.name], a.limit);  // 上限列
      });
    });
    tb.appendChild(tr);
  }
  repaintPubAccounts();
}
function repaintPubAccounts() {
  document.querySelectorAll("#pub-acc-tbody tr").forEach(tr =>
    tr.classList.toggle("selected", pubSel.has(tr.dataset.name)));
}
function pubSelectAll() {
  (PUB.accounts || []).forEach(a => pubSel.add(a.name));
  repaintPubAccounts();
}
function pubSelectNone() { pubSel.clear(); repaintPubAccounts(); }
async function pubToggleCover(names) {
  const list = names || [...pubSel];
  if (!list.length) { alertMsg("提示", "先在账号表里选中账号，再点「切换封面」"); return; }
  await api("/api/publish/toggle_cover", { names: list });
  refreshPublish();
}
function pubEditLimit() {
  const list = [...pubSel];
  if (!list.length) { alertMsg("提示", "先在账号表里选中账号，再点「设置上限」"); return; }
  const first = (PUB.accounts || []).find(a => list.includes(a.name));
  pubEditLimitDialog(list, first ? first.limit : 10);
}
function pubEditLimitDialog(names, cur) {
  const v = window.prompt(`给 ${names.join("、")} 设置每日发布上限：`, cur || 10);
  if (v === null) return;
  const val = parseInt(v, 10);
  if (!(val >= 1 && val <= 99)) { alertMsg("提示", "上限要是 1~99 的数字"); return; }
  api("/api/publish/edit_limit", { names, limit: val }).then(refreshPublish);
}
function pubOpenBrowsers() {
  const list = [...pubSel];
  if (!list.length) { alertMsg("提示", "先选账号"); return; }
  api("/api/publish/open_browsers", { names: list });
}
async function pubSaveOut() {
  const v = document.getElementById("pub-out").value.trim();
  if (!v || (PUB && v === PUB.out_root)) return;
  if (!(await fsCheck(v, "素材目录"))) return;
  PUB = await api("/api/publish/out_root", { path: v });
  renderPubAccounts(); renderPubTasks();
}
async function pubReload(showLog) {
  PUB = await api("/api/publish/reload", {});
  renderPubAccounts(); renderPubTasks(); pubButtons();
}
async function pubSaveSettings() {
  await api("/api/publish/settings", {
    cart: document.getElementById("pub-cart").checked,
    cart_fail_policy: document.getElementById("pub-cartfail").value,
    dry_run: document.getElementById("pub-dry").checked,
    appeal_after_publish: document.getElementById("pub-appeal").checked,
    interval_min: +document.getElementById("pub-interval").value || 30,
  });
  refreshPublish();
}
function renderPubTopics() {
  const fx = (PUB && PUB.fixed_topics) || [];
  const sel = document.getElementById("pub-topics");
  const vals = ["5 品类（AI 全定）", `${5 - fx.length} 品类 + 固定话题`];
  sel.innerHTML = vals.map(v => `<option>${v}</option>`).join("");
  const useFixed = PUB && PUB.cfg.use_fixed_topics;
  sel.selectedIndex = useFixed ? 1 : 0;
  document.getElementById("pub-fixed-lbl").textContent =
    useFixed ? fx.map(t => "#" + t).join(" ") : "";
}
function pubTopicModeChange() {
  const useFixed = document.getElementById("pub-topics").selectedIndex === 1;
  document.getElementById("pub-fixed-lbl").textContent =
    useFixed ? (PUB.fixed_topics || []).map(t => "#" + t).join(" ") : "";
  api("/api/publish/settings", { use_fixed_topics: useFixed }).then(refreshPublish);
}
function pubFixedTopicsDialog() {
  document.getElementById("topics-input").value =
    (PUB.fixed_topics || []).map(t => "#" + t).join(" ");
  showModal("topics-modal");
}
async function topicsSave() {
  try {
    const d = await api("/api/publish/fixed_topics", { topics: document.getElementById("topics-input").value });
    PUB.fixed_topics = d.fixed_topics;
    hideModal("topics-modal");
    renderPubTopics();
  } catch (e) { /* api 弹过 */ }
}

async function pubGenCopy() {
  if (PUB.running) return;
  const overwrite = window.confirm("要覆盖已有文案吗？\n\n「确定」= 全部重新生成\n「取消」= 只补缺文案的商品（推荐）");
  await api("/api/publish/settings", {
    cart: document.getElementById("pub-cart").checked,
    cart_fail_policy: document.getElementById("pub-cartfail").value,
    dry_run: document.getElementById("pub-dry").checked,
    appeal_after_publish: document.getElementById("pub-appeal").checked,
    interval_min: +document.getElementById("pub-interval").value || 30,
  });
  await api("/api/publish/gen_copy", { overwrite });
  pubButtons();
}
// 剪贴板兼容复制：navigator.clipboard 只在 HTTPS/localhost 可用（通过 IP+HTTP 访问 NAS 时没有），
// 降级用 execCommand('copy')（老 API，HTTP 页面也能用），都失败才返回 false
async function copyTextToClipboard(t) {
  try {
    await navigator.clipboard.writeText(t);
    return true;
  } catch (e) {}
  try {
    const ta = document.createElement("textarea");
    ta.value = t;
    ta.style.cssText = "position:fixed;left:-9999px;top:0;opacity:0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    ta.setSelectionRange(0, t.length);
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  } catch (e) {
    return false;
  }
}

async function pubCopyPrompt() {
  try {
    const d = await api("/api/publish/prompt");
    const ok = await copyTextToClipboard(d.text);
    if (ok) {
      alertMsg("已复制提示词", `已复制 ${d.pids} 个商品的提示词（${d.chars} 字）到剪贴板。\n\n1. 打开 DeepSeek / 豆包对话框，粘贴发送\n2. 把它回复的 JSON 整段复制\n3. 回到这里点「粘贴导入文案」`);
    } else {
      document.getElementById("paste-text").value = d.text;
      showModal("paste-modal");
      alertMsg("复制失败", "浏览器剪贴板不可用，已把提示词放进「粘贴导入文案」窗口，请手动全选复制。");
    }
  } catch (e) {}
}
function pubPasteDialog() {
  document.getElementById("paste-msg").textContent = "";
  showModal("paste-modal");
}
async function pasteImport() {
  const text = document.getElementById("paste-text").value.trim();
  if (!text) { document.getElementById("paste-msg").textContent = "还没粘贴内容"; return; }
  try {
    const d = await api("/api/publish/paste_copies", { text });
    document.getElementById("paste-msg").textContent =
      `导入成功：${d.imported} 个商品，写入 ${d.written} 条` + (d.missed ? `，${d.missed} 个 id 对不上` : "");
    PUB = await api("/api/publish/reload", {});
    renderPubAccounts(); renderPubTasks();
    setTimeout(() => hideModal("paste-modal"), 1200);
  } catch (e) {}
}

function renderPubTasks() {
  const tb = document.getElementById("pub-tbody");
  tb.innerHTML = "";
  for (const t of (PUB && PUB.tasks) || []) {
    const tr = document.createElement("tr");
    tr.className = "selectable";
    let cls = "";
    if (t.state.startsWith("已发布") || t.state === "演练通过") cls = "tag-ok";
    else if (t.state.startsWith("失败")) cls = "tag-fail";
    else if (t.state === "发布中") cls = "tag-doing";
    if (cls) tr.classList.add(cls);
    tr.innerHTML = `<td>${esc(t.pid)}</td><td>${esc((t.title || "").slice(0, 30))}</td>
      <td>${esc(t.video_name)}</td><td>${esc((t.copy_title || "—").slice(0, 30))}</td>
      <td>${esc(t.state)}</td>`;
    tr.dataset.key = t.key;
    tr.addEventListener("dblclick", () => fsBrowse(t.dir));
    tr.addEventListener("contextmenu", (ev) => {
      ev.preventDefault();
      if (window.confirm(`删除商品ID ${t.pid} 的整个文件夹？\n\n毛料、成品、文案会一并删除，不可恢复。`)) {
        api("/api/publish/delete_task", { key: t.key }).then(refreshPublish);
      }
    });
    tb.appendChild(tr);
  }
}

async function pubEditCopy() {
  if (!lastPubRow) { alertMsg("提示", "先选中一条作品"); return; }
  const key = lastPubRow;
  const d = await api("/api/publish/copy/" + encodeURIComponent(key));
  document.getElementById("copy-task-title").textContent = "商品：" + (d.title || "").slice(0, 40);
  document.getElementById("copy-title").value = d.copy_title || "";
  document.getElementById("copy-intro").value = d.copy_intro || "";
  document.getElementById("copy-title").dataset.key = key;
  showModal("copy-modal");
}
let lastPubRow = null;
async function copySave() {
  const key = document.getElementById("copy-title").dataset.key;
  const title = document.getElementById("copy-title").value.trim();
  const intro = document.getElementById("copy-intro").value.trim();
  if (!title || !intro) { alertMsg("提示", "标题和简介都不能为空"); return; }
  await api("/api/publish/copy/" + encodeURIComponent(key), { title, intro });
  hideModal("copy-modal");
  refreshPublish();
}

async function pubClearPublished() {
  if (PUB && PUB.running) { alertMsg("提示", "发布任务进行中，请先停止再清除"); return; }
  const d = await api("/api/publish/clear_published", {});
  if (d.ok === false) { alertMsg("提示", d.msg); return; }
  alertMsg("清除已发布", `已删除 ${d.removed} 个商品目录` + (d.failed && d.failed.length ? `，失败 ${d.failed.length} 个` : ""));
  refreshPublish();
}
function pubOpenDir() { fsBrowse(PUB ? PUB.out_root : ""); }

async function pubStart() {
  if (PUB && PUB.running) return;
  const names = [...pubSel];
  if (!names.length) { alertMsg("提示", "先选择参与发布的账号"); return; }
  // 保存设置
  await api("/api/publish/settings", {
    cart: document.getElementById("pub-cart").checked,
    cart_fail_policy: document.getElementById("pub-cartfail").value,
    dry_run: document.getElementById("pub-dry").checked,
    appeal_after_publish: document.getElementById("pub-appeal").checked,
    interval_min: +document.getElementById("pub-interval").value || 30,
  });
  const mode = document.querySelector('input[name="pub-mode"]:checked').value;
  let when = document.getElementById("pub-when").value.trim();
  try {
    const d = await api("/api/publish/preview", { accounts: names, mode, when, interval_min: +document.getElementById("pub-interval").value || 30 });
    if (!window.confirm(d.tip + "\n\n开始吗？")) return;
    const r = await api("/api/publish/start", { accounts: names, mode, when, interval_min: d.interval_min });
    if (r.ok === false) alertMsg("提示", r.msg);
  } catch (e) {}
}
function initPubDefaults() {
  // 默认时间 = 1 小时后（10 分钟取整），与桌面版一致
  const t = new Date(Date.now() + 3600 * 1000);
  t.setMinutes(Math.floor(t.getMinutes() / 10) * 10, 0, 0);
  const pad = n => String(n).padStart(2, "0");
  document.getElementById("pub-when").value =
    `${t.getFullYear()}-${pad(t.getMonth() + 1)}-${pad(t.getDate())} ${pad(t.getHours())}:${pad(t.getMinutes())}`;
  document.getElementById("pub-table").addEventListener("click", (ev) => {
    const tr = ev.target.closest("tr");
    if (!tr || !tr.dataset.key) return;
    document.querySelectorAll("#pub-tbody tr").forEach(r => r.classList.toggle("selected", r === tr));
    lastPubRow = tr.dataset.key;
  });
}

/* ================= 作品申诉 ================= */
let AP = null;
let AP_dirty = false;   // 用户改过设置区但还没保存：轮询期间不覆盖设置项
const apSel = new Set();
pageInit.appeal = () => { refreshAppeal(); };

async function refreshAppeal() {
  AP = await api("/api/appeal");
  // 轮询每 4 秒重跑本函数；用户正在编辑（有焦点）或已改动未保存（脏标记）时，
  // 不覆盖设置输入框，避免刚选好的定时申诉时间被服务器旧值冲掉
  const SET_IDS = ["ap-alldays", "ap-confirm", "ap-sched", "ap-sched-time",
                   "ap-link", "ap-mat", "ap-wait", "ap-review", "ap-interval", "ap-total"];
  for (const id of SET_IDS) {
    const el = document.getElementById(id);
    if (!el.dataset.dirtyHook) {           // 挂一次改动监听，标记脏状态
      el.dataset.dirtyHook = "1";
      el.addEventListener("input", () => { AP_dirty = true; });
      el.addEventListener("change", () => { AP_dirty = true; });
    }
  }
  const idle = id => !AP_dirty
                     && document.activeElement !== document.getElementById(id);
  document.getElementById("ap-rules").textContent = AP.rules_summary;
  if (idle("ap-alldays")) document.getElementById("ap-alldays").checked = !!AP.cfg["处理非今日作品"];
  if (idle("ap-confirm")) document.getElementById("ap-confirm").checked = !!AP.cfg["提交前人工确认"];
  if (idle("ap-sched")) document.getElementById("ap-sched").checked = !!AP.cfg["定时申诉"];
  if (idle("ap-sched-time")) document.getElementById("ap-sched-time").value = AP.cfg["定时申诉时间"] || "";
  if (idle("ap-link")) document.getElementById("ap-link").value = AP.cfg["站外链接"] || "";
  if (idle("ap-mat")) document.getElementById("ap-mat").value = AP.material_root;
  if (idle("ap-wait")) document.getElementById("ap-wait").value = AP.cfg["发布后等待秒"] ?? 90;
  if (idle("ap-review")) document.getElementById("ap-review").value = AP.cfg["审核中再等秒"] ?? 30;
  if (idle("ap-interval")) document.getElementById("ap-interval").value = AP.cfg["轮询间隔秒"] ?? 30;
  if (idle("ap-total")) document.getElementById("ap-total").value = AP.cfg["检查总时长秒"] ?? 720;
  renderApAccounts();
  apButtons();
}
function renderApAccounts() {
  const tb = document.getElementById("ap-acc-tbody");
  tb.innerHTML = "";
  for (const a of (AP && AP.accounts) || []) {
    const tr = document.createElement("tr");
    tr.className = "selectable" + (apSel.has(a.name) ? " selected" : "");
    tr.dataset.name = a.name;
    tr.innerHTML = `<td>${esc(a.name)}</td><td>${esc(a.nick || "")}</td>
      <td class="center">${esc(a.mat)}</td><td class="center">${a.hist}</td>`;
    tr.addEventListener("click", (ev) => {
      if (ev.ctrlKey || ev.metaKey) { apSel.has(a.name) ? apSel.delete(a.name) : apSel.add(a.name); }
      else { apSel.clear(); apSel.add(a.name); }
      repaintApAccounts();
    });
    tb.appendChild(tr);
  }
  repaintApAccounts();
}
function repaintApAccounts() {
  document.querySelectorAll("#ap-acc-tbody tr").forEach(tr =>
    tr.classList.toggle("selected", apSel.has(tr.dataset.name)));
}
function apSelectAll(all) {
  apSel.clear();
  if (all) (AP.accounts || []).forEach(a => apSel.add(a.name));
  repaintApAccounts();
}
function apButtons() {
  const run = AP && AP.running;
  ["ap-btn-scan", "ap-btn-go", "ap-btn-queue"].forEach(id =>
    document.getElementById(id).classList.toggle("disabled", !!run));
}
async function apSaveSettings() {
  const mroot = document.getElementById("ap-mat").value.trim();
  if (mroot && !(await fsCheck(mroot, "材料根目录"))) {
    if (!window.confirm("材料根目录不可用，仍要保存其它设置吗？\n（材料根目录会保持原样，改好后请重新保存）")) return;
    document.getElementById("ap-mat").value = (AP && AP.material_root) || "";
  }
  await api("/api/appeal/settings", {
    "站外链接": document.getElementById("ap-link").value.trim(),
    "材料根目录": document.getElementById("ap-mat").value.trim(),
    "处理非今日作品": document.getElementById("ap-alldays").checked,
    "提交前人工确认": document.getElementById("ap-confirm").checked,
    "定时申诉": document.getElementById("ap-sched").checked,
    "定时申诉时间": document.getElementById("ap-sched-time").value.trim(),
    "发布后等待秒": +document.getElementById("ap-wait").value || 90,
    "审核中再等秒": +document.getElementById("ap-review").value || 30,
    "轮询间隔秒": +document.getElementById("ap-interval").value || 30,
    "检查总时长秒": +document.getElementById("ap-total").value || 720,
  });
  AP_dirty = false;      // 保存成功后解除脏标记，轮询恢复回显
  refreshAppeal();
}
function apNames() { return [...apSel]; }
async function apAction(action, body) { await api("/api/appeal/" + action, body || {}); setTimeout(refreshAppeal, 500); }
function apScan() {
  if (!apNames().length) { alertMsg("提示", "先选账号（Ctrl 可多选）"); return; }
  apAction("scan", { names: apNames() });
}
async function apAppeal() {
  if (!apNames().length) { alertMsg("提示", "先选账号（Ctrl 可多选）"); return; }
  const d = await api("/api/appeal/appeal", { names: apNames() });
  if (d.ok === false) { alertMsg("不能申诉", d.msg); return; }
  if (d.confirm && !window.confirm(d.msg)) return;
  await apAction("appeal_confirmed", { names: apNames() });
  apButtons();
}
async function apQueue() {
  const d = await api("/api/appeal/queue", {});
  if (d.ok === false) { alertMsg("复查队列", d.msg); return; }
  if (d.confirm && !window.confirm(d.msg)) return;
  await apAction("queue_confirmed", {});
  apButtons();
}
function apOpenBrowsers() {
  if (!apNames().length) { alertMsg("提示", "先选账号"); return; }
  apAction("open_browsers", { names: apNames() });
}

/* ---- 申诉规则编辑器 ---- */
function apRulesDialog() {
  const list = document.getElementById("rules-list");
  list.innerHTML = "";
  (AP.rules || []).forEach(r => addRuleRow(r));
  if (!(AP.rules || []).length) addRuleRow({});
  showModal("rules-modal");
}
function addRuleRow(rule) {
  const list = document.getElementById("rules-list");
  const card = document.createElement("div");
  card.className = "card pad"; card.style.marginBottom = "8px";
  card.innerHTML = `
    <div class="form-row" style="margin:0">
      <span class="muted">名称</span><input class="txt" data-k="名称" style="width:90px" value="${esc(rule["名称"] || "")}">
      <span class="muted">违规原因关键词（多个用逗号分隔）</span>
      <input class="txt flex" data-k="关键词" value="${esc(rule["关键词"] || "")}">
      <span class="btn small danger" data-del>删除</span>
    </div>
    <div class="form-row" style="margin-top:6px">
      <span class="muted">材料子目录</span><input class="txt" data-k="材料子目录" style="width:160px" value="${esc(rule["材料子目录"] || "")}">
      <span class="muted">（空 = 材料根目录/账号XX）</span>
    </div>
    <div class="muted" style="margin-top:6px">申诉文案（命中这条规则时填进申诉框的话术）</div>
    <textarea rows="3" data-k="描述">${esc(rule["描述"] || "")}</textarea>`;
  card.querySelector("[data-del]").addEventListener("click", () => card.remove());
  list.appendChild(card);
}
async function rulesSave() {
  const rules = [];
  document.querySelectorAll("#rules-list .card").forEach(card => {
    const get = k => {
      const el = card.querySelector(`[data-k="${k}"]`);
      return el ? (el.tagName === "TEXTAREA" ? el.value : el.value) : "";
    };
    rules.push({ "名称": get("名称"), "关键词": get("关键词"),
                 "描述": get("描述"), "材料子目录": get("材料子目录") });
  });
  const emptyDesc = rules.filter(r => (r["关键词"] || r["描述"]) && !r["描述"]);
  if (emptyDesc.length) {
    if (!window.confirm("这些规则还没填申诉文案：\n" + emptyDesc.map(r => r["名称"] || r["关键词"].slice(0, 8)).join("、") +
        "\n\n没文案的规则命中后会被拒绝提交（不会瞎申诉）。仍要保存吗？")) return;
  }
  try {
    AP = await api("/api/appeal/rules", { rules });
    hideModal("rules-modal");
  } catch (e) {}
}

/* ================= 全局设置 / 企微互通 ================= */
/* ================= 电商违规申诉（百应后台） ================= */
let BY = null;
let BY_dirty = false;        // 设置区有未保存的改动：轮询不覆盖输入框
let BY_MAT_ACC = "";         // 材料页当前选中的账号
let BY_MATS = null;
let BY_LOG_ACC = "";

pageInit["buyin-mat"] = () => { refreshBuyin(true); };
pageInit["buyin-set"] = () => { refreshBuyin(true); };
pageInit["buyin-log"] = () => { refreshBuyin(false).then(bynLoadRecords); };

async function refreshBuyin(loadMats) {
  try {
    BY = await api("/api/buyin");
  } catch (e) { return; }
  const names = (BY.accounts || []).map(a => a.name);
  if (!BY_MAT_ACC || names.indexOf(BY_MAT_ACC) < 0) BY_MAT_ACC = names[0] || "";
  renderBymAccounts();
  renderBysSettings();
  renderBysAccounts();
  renderBylStats();
  renderBylAccounts();
  if (loadMats) bymLoadMats();
}

/* ---- 申诉材料 ---- */
function renderBymAccounts() {
  const box = document.getElementById("bym-acc-row");
  box.innerHTML = '<span class="muted">账号</span>';
  for (const a of (BY && BY.accounts) || []) {
    const b = document.createElement("span");
    b.className = "btn small" + (a.name === BY_MAT_ACC ? " primary" : "");
    b.textContent = (a.name + " " + (a.note || "")).trim();
    b.onclick = () => { BY_MAT_ACC = a.name; renderBymAccounts(); bymLoadMats(); };
    box.appendChild(b);
  }
}

async function bymLoadMats() {
  if (!BY_MAT_ACC) return;
  BY_MATS = await api("/api/buyin/materials?name=" + encodeURIComponent(BY_MAT_ACC));
  renderBymMats();
}

function renderBymMats() {
  if (!BY_MATS) return;
  const put = (group, boxId, cntId) => {
    const wrap = document.getElementById(boxId);
    const items = BY_MATS[group] || [];
    document.getElementById(cntId).textContent = items.length ? items.length + " 张" : "（还没上传）";
    wrap.innerHTML = "";
    if (!items.length) {
      wrap.innerHTML = `<div class="mat-empty">这个号还没有 ${group} 材料，点右上角「上传图片」</div>`;
      return;
    }
    for (const it of items) {
      const url = `/api/buyin/materials/${encodeURIComponent(BY_MAT_ACC)}/${group}/${encodeURIComponent(it.name)}`;
      const d = document.createElement("div");
      d.className = "mat-item";
      const isPdf = /\.pdf$/i.test(it.name);
      d.innerHTML = (isPdf ? '<div class="mat-pdf">PDF</div>' : '<img alt="">')
        + `<div class="mat-name" title="${esc(it.name)}">${esc(it.name)}</div>`
        + '<span class="mat-del">删除</span>';
      if (!isPdf) {
        const img = d.querySelector("img");
        img.src = url;
        img.onclick = () => window.open(url, "_blank");
      }
      d.querySelector(".mat-del").onclick = () => bymDelete(group, it.name);
      wrap.appendChild(d);
    }
  };
  put("box1", "bym-box1", "bym-c1");
  put("box2", "bym-box2", "bym-c2");
}

async function bymUpload(group, input) {
  const files = input.files;
  if (!files || !files.length) return;
  if (!BY_MAT_ACC) { alertMsg("提示", "先选账号"); return; }
  const fd = new FormData();
  for (const f of files) fd.append("files", f);
  input.value = "";
  try {
    const r = await fetch(`/api/buyin/materials/${encodeURIComponent(BY_MAT_ACC)}/${group}`,
                          { method: "POST", body: fd });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { alertMsg("上传失败", d.detail || r.statusText); return; }
    bymLoadMats();
    refreshBuyin(false);
  } catch (e) { alertMsg("上传失败", String(e)); }
}

async function bymDelete(group, name) {
  if (!window.confirm(`确定删除 ${group}/${name}？`)) return;
  await api(`/api/buyin/materials/${encodeURIComponent(BY_MAT_ACC)}/${group}/${encodeURIComponent(name)}`,
            {}, "DELETE");
  bymLoadMats();
  refreshBuyin(false);
}

/* ---- 申诉设置 ---- */
const BY_SET_IDS = ["byn-auto", "byn-interval", "byn-daily", "byn-daily-time", "byn-retry",
                    "byn-appeal", "byn-confirm", "byn-push", "byn-reason"];

function renderBysSettings() {
  if (!BY) return;
  for (const id of BY_SET_IDS) {
    const el = document.getElementById(id);
    if (el && !el.dataset.dirtyHook) {
      el.dataset.dirtyHook = "1";
      el.addEventListener("input", () => { BY_dirty = true; });
      el.addEventListener("change", () => { BY_dirty = true; });
    }
  }
  const idle = id => !BY_dirty && document.activeElement !== document.getElementById(id);
  const c = BY.cfg || {};
  if (idle("byn-auto")) document.getElementById("byn-auto").checked = !!c["自动巡检"];
  if (idle("byn-interval")) document.getElementById("byn-interval").value = c["检查间隔秒"] ?? 600;
  if (idle("byn-daily")) document.getElementById("byn-daily").checked = !!c["每日定时检查"];
  if (idle("byn-daily-time")) document.getElementById("byn-daily-time").value = c["定时检查时间"] || "";
  if (idle("byn-retry")) document.getElementById("byn-retry").value = c["失败重试上限"] ?? 5;
  if (idle("byn-appeal")) document.getElementById("byn-appeal").checked = !!c["自动申诉"];
  if (idle("byn-confirm")) document.getElementById("byn-confirm").checked = !!c["提交前人工确认"];
  if (idle("byn-push")) document.getElementById("byn-push").checked = !!c["企微通知"];
  if (idle("byn-reason")) document.getElementById("byn-reason").value = c["统一申诉理由"] || "";
}

async function bynSaveSettings() {
  try {
    await api("/api/buyin/settings", {
      "自动巡检": document.getElementById("byn-auto").checked,
      "检查间隔秒": +document.getElementById("byn-interval").value || 600,
      "每日定时检查": document.getElementById("byn-daily").checked,
      "定时检查时间": document.getElementById("byn-daily-time").value.trim(),
      "失败重试上限": +document.getElementById("byn-retry").value || 5,
      "自动申诉": document.getElementById("byn-appeal").checked,
      "提交前人工确认": document.getElementById("byn-confirm").checked,
      "企微通知": document.getElementById("byn-push").checked,
      "统一申诉理由": document.getElementById("byn-reason").value.trim(),
    });
    BY_dirty = false;
    refreshBuyin(false);
  } catch (e) { /* api 已弹提示 */ }
}

function renderBysAccounts() {
  if (!BY) return;
  const tb = document.getElementById("bys-tbody");
  const ae = document.activeElement;
  if (ae && ae.closest && ae.closest("#bys-tbody")) return;   // 正在这一行编辑，别重绘
  tb.innerHTML = "";
  const tip = document.getElementById("bys-tip");
  if (tip) {
    tip.textContent = "材料列 = box1/box2 图片数；某个号没有材料时命中违规会被跳过（不会空申诉）。"
      + "「打开百应」会在浏览器画面里打开违规处置页，没登录就在那里扫码。";
  }
  for (const a of BY.accounts || []) {
    const tr = document.createElement("tr");
    tr.dataset.name = a.name;
    const cls = a.state_cls === "ok" ? "by-ok"
              : a.state_cls === "bad" ? "by-bad"
              : a.state_cls === "new" ? "by-new" : "by-idle";
    tr.innerHTML = `
      <td>${esc(a.name)}</td><td>${esc(a.note || "")}</td>
      <td class="center"><input type="checkbox" class="by-en" ${a.enabled ? "checked" : ""}></td>
      <td class="center"><input type="number" class="spin by-iv" min="0" value="${a.interval_own || 0}"
           title="0 = 跟随全局 ${a.interval} 秒"></td>
      <td><input type="text" class="by-rs" value="${esc(a.reason || "")}" placeholder="跟随统一理由"></td>
      <td class="center">${a.mat1}/${a.mat2}</td>
      <td class="${cls}" title="${esc(a.last_result || "")}">${esc(a.login_state)}</td>
      <td class="center">${a.today}</td>
      <td><span class="btn small" data-act="check">立即检查</span>
          <span class="btn small" data-act="open">打开百应</span></td>`;
    tr.querySelectorAll("input").forEach(inp =>
      inp.addEventListener("change", () => bynSaveAcc(a.name)));
    tr.querySelector('[data-act="check"]').onclick = () => bynCheck([a.name]);
    tr.querySelector('[data-act="open"]').onclick = () =>
      api("/api/buyin/open_page", { name: a.name }).catch(() => {});
    tb.appendChild(tr);
  }
}

async function bynSaveAcc(name) {
  const tr = document.querySelector(`#bys-tbody tr[data-name="${name}"]`);
  if (!tr) return;
  try {
    await api("/api/buyin/account", {
      name,
      enabled: tr.querySelector(".by-en").checked,
      interval: +tr.querySelector(".by-iv").value || 0,
      reason: tr.querySelector(".by-rs").value.trim(),
    });
    refreshBuyin(false);
  } catch (e) { /* api 已弹提示 */ }
}

/* ---- 运行日志 ---- */
function renderBylStats() {
  if (!BY) return;
  const parts = (BY.accounts || []).map(a => `${esc(a.name)} ${a.today} 条`);
  document.getElementById("byl-stats").innerHTML =
    `<b>今日申诉</b>（${esc(BY.today_scope)}）：${parts.join(" · ") || "—"}，合计 <b>${BY.today_total} 条</b>`
    + `<br><span class="muted">材料目录 ${esc(BY.materials_root)} · 默认间隔 ${BY.interval_default} 秒</span>`;
}

function renderBylAccounts() {
  if (!BY) return;
  const sel = document.getElementById("byl-acc");
  const cur = sel.value || BY_LOG_ACC;
  sel.innerHTML = "";
  for (const a of BY.accounts || []) {
    const o = document.createElement("option");
    o.value = a.name;
    o.textContent = (a.name + " " + (a.note || "")).trim();
    sel.appendChild(o);
  }
  if (cur) sel.value = cur;
  BY_LOG_ACC = sel.value || BY_MAT_ACC;
}

async function bynLoadRecords() {
  const sel = document.getElementById("byl-acc");
  const name = (sel && sel.value) || BY_LOG_ACC;
  if (!name) return;
  BY_LOG_ACC = name;
  const d = await api("/api/buyin/records?name=" + encodeURIComponent(name));
  const tb = document.getElementById("byl-tbody");
  tb.innerHTML = "";
  document.getElementById("byl-count").textContent = `共 ${d.items.length} 条，今日成功 ${d.today} 条`;
  if (!d.items.length) {
    tb.innerHTML = '<tr><td colspan="5" class="muted" style="text-align:center; padding:18px">还没有记录</td></tr>';
    return;
  }
  for (const r of d.items) {
    const tr = document.createElement("tr");
    if (/成功/.test(r.result || "")) tr.className = "tag-ok";
    tr.innerHTML = `<td>${esc(r.order_id)}</td><td>${esc(r.reason)}</td>
      <td>${esc(r.result)}</td><td>${esc(r.found_at)}</td><td>${esc(r.ok_at)}</td>`;
    tb.appendChild(tr);
  }
}

async function bynCheck(names) {
  try {
    const d = await api("/api/buyin/check", { names: names || [] });
    alertMsg("电商申诉检查", `已开始检查 ${d.count} 个账号。进度看「电商违规申诉 → 运行日志」。`);
    setTimeout(() => refreshBuyin(false), 1200);
  } catch (e) { /* api 已弹提示 */ }
}

async function bynStop() {
  await api("/api/buyin/stop", {});
}

/* 百应页停留时轻量轮询（6 秒），输入中的框不会被覆盖 */
setInterval(() => {
  if (currentPage && currentPage.indexOf("buyin") === 0) refreshBuyin(false).catch(() => {});
}, 6000);

/* ================= 设置（独立页面） ================= */
/* 服务商预设：都是 OpenAI 兼容接口（/chat/completions），换服务商只是换 base + model */
const LLM_PROVIDERS = [
  {
    id: "deepseek", name: "DeepSeek（便宜、效果好）",
    base: "https://api.deepseek.com/v1",
    models: [["deepseek-flash", "deepseek-flash（快，推荐写文案）"],
             ["deepseek-chat", "deepseek-chat（V3.2 通用）"],
             ["deepseek-reasoner", "deepseek-reasoner（推理更强，慢一些）"]],
    tip: '去 <a href="https://platform.deepseek.com/api_keys" target="_blank">platform.deepseek.com → API Keys</a> 创建一个 Key，充 10 块能用很久。',
  },
  {
    id: "zhipu", name: "智谱 GLM（有免费模型）",
    base: "https://open.bigmodel.cn/api/paas/v4",
    models: [["glm-4.5-flash", "glm-4.5-flash（免费，推荐先试）"],
             ["glm-4.5-air", "glm-4.5-air（轻量）"],
             ["glm-4.6", "glm-4.6（旗舰，效果最好）"],
             ["glm-4-plus", "glm-4-plus（上一代旗舰）"]],
    tip: '去 <a href="https://bigmodel.cn/usercenter/apikeys" target="_blank">bigmodel.cn → 用户中心 → API Keys</a> 创建；glm-4.5-flash 免费额度够日常写文案。',
  },
  {
    id: "siliconflow", name: "硅基流动（部分模型免费）",
    base: "https://api.siliconflow.cn/v1",
    models: [["Qwen/Qwen2.5-7B-Instruct", "Qwen2.5-7B（免费）"],
             ["Qwen/Qwen3-8B", "Qwen3-8B"],
             ["Qwen/Qwen3-32B", "Qwen3-32B"],
             ["deepseek-ai/DeepSeek-V3", "DeepSeek-V3（第三方托管）"]],
    tip: '去 <a href="https://cloud.siliconflow.cn/account/ak" target="_blank">cloud.siliconflow.cn → API 密钥</a> 创建；带「免费」的模型不扣费。',
  },
  {
    id: "dashscope", name: "阿里百炼 · 通义千问",
    base: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    models: [["qwen-flash", "qwen-flash（最便宜）"],
             ["qwen-plus", "qwen-plus（均衡推荐）"],
             ["qwen-max", "qwen-max（效果最好）"],
             ["qwen-turbo", "qwen-turbo（老版快模型）"]],
    tip: '去 <a href="https://bailian.console.aliyun.com/" target="_blank">百炼控制台</a> 开通后创建 API Key，新用户有免费额度。',
  },
  {
    id: "moonshot", name: "月之暗面 Kimi",
    base: "https://api.moonshot.cn/v1",
    models: [["kimi-k2-0905-preview", "kimi-k2（旗舰）"],
             ["moonshot-v1-8k", "moonshot-v1-8k（短文本，便宜）"],
             ["moonshot-v1-32k", "moonshot-v1-32k（长文本）"]],
    tip: '去 <a href="https://platform.moonshot.cn/console/api-keys" target="_blank">platform.moonshot.cn → API Keys</a> 创建。',
  },
  {
    id: "hunyuan", name: "腾讯混元",
    base: "https://api.hunyuan.cloud.tencent.com/v1",
    models: [["hunyuan-turbos-latest", "hunyuan-turbos（快，推荐）"],
             ["hunyuan-t1-latest", "hunyuan-t1（深度推理）"],
             ["hunyuan-lite", "hunyuan-lite（免费额度）"]],
    tip: '去 <a href="https://console.cloud.tencent.com/hunyuan/api-key" target="_blank">腾讯云 → 混元大模型 → API Key</a> 创建。',
  },
  {
    id: "ark", name: "火山方舟 · 豆包",
    base: "https://ark.cn-beijing.volces.com/api/v3",
    models: [["doubao-seed-1-6-flash-250828", "doubao-seed-1.6-flash（便宜快）"],
             ["doubao-seed-1-6-250615", "doubao-seed-1.6（均衡）"],
             ["doubao-1-5-pro-32k-250115", "doubao-1.5-pro-32k"]],
    tip: '去 <a href="https://console.volcengine.com/ark" target="_blank">火山方舟控制台</a> 开通并创建 API Key；模型名以控制台「接入点」为准，对不上就选「自定义」手填。',
  },
  {
    id: "openai", name: "OpenAI",
    base: "https://api.openai.com/v1",
    models: [["gpt-4.1-mini", "gpt-4.1-mini（便宜快，推荐）"],
             ["gpt-4.1", "gpt-4.1（效果最好）"],
             ["gpt-4o-mini", "gpt-4o-mini"],
             ["gpt-4o", "gpt-4o"]],
    tip: '去 <a href="https://platform.openai.com/api-keys" target="_blank">platform.openai.com → API keys</a> 创建；国内直连需要代理，可在「企微互通」外的服务器环境里配代理。',
  },
  {
    id: "custom", name: "自定义（OpenAI 兼容接口）",
    base: "",
    models: [],
    tip: '任何 <b>OpenAI 兼容</b>接口都能用：接口地址填到 <b>/v1</b> 结尾（能拼出 <b>/v1/chat/completions</b> 就对了），模型名照服务商文档填。',
  },
];

function llmPreset(id) {
  return LLM_PROVIDERS.find(p => p.id === id) ||
         LLM_PROVIDERS[LLM_PROVIDERS.length - 1];
}
function llmGuessProvider(base) {
  const b = String(base || "").trim().replace(/\/+$/, "").toLowerCase();
  if (!b) return "deepseek";
  const hit = LLM_PROVIDERS.find(p => p.base && p.base.replace(/\/+$/, "").toLowerCase() === b);
  return hit ? hit.id : "custom";
}
function llmFillProviders(sel) {
  const el = document.getElementById("set-llm-provider");
  el.innerHTML = LLM_PROVIDERS.map(p => `<option value="${p.id}">${p.name}</option>`).join("");
  el.value = sel;
}
let LLM_MODEL_CACHE = {};        // base_url -> 服务商返回的完整模型 id 列表
let llmSyncBusy = false;

function llmEl(id) { return document.getElementById(id); }
function llmModelHint(txt) { llmEl("set-llm-model-hint").textContent = txt || ""; }

/* 模型是纯文本输入框，datalist 只给候选（推荐在前，服务商返回的在后）；
   想填什么就填什么，官网新出的模型不用等我们更新 */
function llmFillModelHints(pairs, extra) {
  const seen = new Set(), html = [];
  (pairs || []).forEach(([v, t]) => {
    if (!seen.has(v)) { seen.add(v); html.push(`<option value="${v}">${t}</option>`); }
  });
  (extra || []).forEach(v => {
    if (!seen.has(v)) { seen.add(v); html.push(`<option value="${v}"></option>`); }
  });
  llmEl("set-llm-model-list").innerHTML = html.join("");
}
/* 切换服务商：自动填接口地址、换默认模型与候选、更新「去哪拿 Key」的说明 */
function llmApplyProvider(pid, keepModel) {
  const p = llmPreset(pid);
  const custom = pid === "custom";
  const cached = LLM_MODEL_CACHE[(p.base || "").replace(/\/+$/, "")];
  llmEl("set-llm-base").value = custom ? "" : p.base;
  llmEl("set-llm-base").placeholder = custom ? "https://your-api.example.com/v1" : "";
  llmEl("set-llm-base-hint").textContent =
    custom ? "填到 /v1 结尾，能拼出 /v1/chat/completions 就对了" : "选服务商自动填好，一般不用改";
  llmFillModelHints(p.models, cached);
  const first = p.models.length ? p.models[0][0] : "";
  llmEl("set-llm-model").value = keepModel || first;
  llmEl("set-llm-model").placeholder = custom ? "填模型名，如 gpt-4o-mini"
    : `填模型名，如 ${first || "模型 id"}`;
  llmEl("set-llm-tip").innerHTML = p.tip;
  llmModelHint(cached ? `已读取 ${cached.length} 个模型；输入框里打字会提示候选，也可以直接手填`
                      : "模型名可以直接手填；点「同步模型列表」可从服务商拉全部模型当候选");
  llmUpdateWarn();
}
function llmProviderChanged() {
  llmApplyProvider(llmEl("set-llm-provider").value, "");
  llmSyncModels(false);
}
function llmPickedModel() {
  return llmEl("set-llm-model").value.trim();
}
function llmPickedBase() {
  return llmEl("set-llm-base").value.trim();
}
function llmUpdateWarn() {
  const el = llmEl("set-llm-warn");
  const on = llmEl("set-llm-on").checked;
  const key = llmEl("set-llm-key").value.trim();
  el.textContent = (!on || key) ? "" : "⚠ 还没填 Key，现在生成的是本地模板文案";
}

/* 从服务商 GET /models 拉完整模型列表，塞进 datalist（同接口地址只拉一次） */
async function llmSyncModels(manual) {
  const base = llmPickedBase().replace(/\/+$/, "");
  const key = llmEl("set-llm-key").value.trim();
  if (!base) { if (manual) llmModelHint("先填接口地址（要能拼出 /models 的那种）"); return; }
  if (!key) { if (manual) llmModelHint("填了 API Key 才能读服务商的模型列表；模型名也可以直接手填"); return; }
  if (LLM_MODEL_CACHE[base]) {
    if (manual) llmModelHint(`已读取 ${LLM_MODEL_CACHE[base].length} 个模型；输入框里打字会提示候选`);
    return;
  }
  if (llmSyncBusy) return;
  llmSyncBusy = true;
  llmModelHint("正在从服务商读取模型列表…");
  try {
    const d = await api("/api/settings/llm_models", { base_url: base, api_key: key });
    if (!d.ok) {
      llmModelHint("读取失败：" + (d.msg || "服务商没给原因") + "；模型名可以直接手填");
      return;
    }
    LLM_MODEL_CACHE[base] = d.models;
    llmFillModelHints(llmPreset(llmEl("set-llm-provider").value).models, d.models);
    llmModelHint(`已读取 ${d.count} 个模型；输入框里打字会提示候选`);
  } catch (e) {
    llmModelHint("读取失败：" + (e && e.message ? e.message : e));
  } finally {
    llmSyncBusy = false;
  }
}

async function loadSettings() {
  const d = await api("/api/settings");
  document.getElementById("set-chrome").value = d.chrome_path || "";
  document.getElementById("set-chrome-found").textContent = d.chrome_found || "未找到";
  document.getElementById("set-shots").value = d.screenshots_dir || "";
  document.getElementById("set-shots-cur").textContent = d.screenshots_dir_resolved || "";
  document.getElementById("set-llm-on").checked = d.llm.enabled;
  document.getElementById("set-llm-key").value = d.llm.api_key || "";
  const pid = llmGuessProvider(d.llm.base_url);
  llmFillProviders(pid);
  llmApplyProvider(pid, d.llm.model || "");
  if (pid === "custom") document.getElementById("set-llm-base").value = d.llm.base_url || "";
  llmUpdateWarn();
  document.getElementById("set-llm-result").textContent = "";
  document.getElementById("set-saved").textContent = "";
  llmSyncModels(false);            // 有 Key 就顺手把完整模型列表拉回来当候选
}
const openSettings = () => showPage("settings");
pageInit.settings = () => { loadSettings().catch(() => {}); };
async function saveSettings() {
  const pid = document.getElementById("set-llm-provider").value;
  const model = llmPickedModel();
  const base = llmPickedBase();
  if (!model) {
    alertMsg("还差一点", "模型名不能空着，填一个服务商文档里的模型 id（也可以点「同步模型列表」从候选里选）。");
    return;
  }
  if (pid === "custom" && !base) {
    alertMsg("还差一点", "选了「自定义」就要手动填接口地址（到 /v1 结尾）。");
    return;
  }
  await api("/api/settings/chrome_path", { path: document.getElementById("set-chrome").value });
  await api("/api/settings/screenshots_dir", { path: document.getElementById("set-shots").value });
  await api("/api/settings/llm", {
    enabled: document.getElementById("set-llm-on").checked,
    base_url: base,
    model,
    api_key: document.getElementById("set-llm-key").value,
  });
  const tip = document.getElementById("set-saved");
  tip.textContent = "✔ 已保存 " + new Date().toTimeString().slice(0, 8);
  setTimeout(() => { tip.textContent = ""; }, 4000);
  pollState();
}
async function testLlm() {
  const el = document.getElementById("set-llm-result");
  el.textContent = "测试中…";
  el.style.color = "var(--muted)";
  try {
    const d = await api("/api/settings/llm_test", {
      base_url: llmPickedBase(),
      model: llmPickedModel(),
      api_key: document.getElementById("set-llm-key").value,
    });
    el.textContent = d.msg;
    el.style.color = d.ok ? "var(--ok)" : "var(--accent)";
  } catch (e) { el.textContent = "测试失败"; }
}
(function bindLlmControls() {
  const p = document.getElementById("set-llm-provider");
  if (!p) return;
  p.addEventListener("change", llmProviderChanged);
  document.getElementById("set-llm-key").addEventListener("input", llmUpdateWarn);
  document.getElementById("set-llm-key").addEventListener("blur", () => llmSyncModels(false));
  document.getElementById("set-llm-on").addEventListener("change", llmUpdateWarn);
  llmFillProviders("deepseek");
  llmApplyProvider("deepseek", "");
})();

async function loadWecom() {
  const d = await api("/api/wecom");
  const c = d.cfg || {};
  const $ = (id) => document.getElementById(id);
  $("wecom-api-base").value = c.api_base || "";
  wecomCbAuto = defaultCallbackUrl(c.api_base);
  $("wecom-callback-url").value = c.callback_url || wecomCbAuto;
  $("wecom-agentid").value = c.agentid || "";
  $("wecom-corpid").value = c.corpid || "";
  // Secret / AESKey 不回显：留空 = 沿用已保存的，只在占位符里提示
  $("wecom-secret").value = "";
  $("wecom-secret").placeholder = c.secret_set ? "已保存（留空 = 不修改）" : "应用详情 → 查看 Secret";
  $("wecom-token").value = c.token || "";
  $("wecom-aeskey").value = "";
  $("wecom-aeskey").placeholder = c.aeskey_set ? "已保存（留空 = 不修改）" : "企微后台接收消息页的 EncodingAESKey";
  $("wecom-port").value = c.port || 8443;
  $("wecom-users").value = (c.allow_users || []).join(",");
  $("wecom-enabled").checked = !!c.enabled;
  $("wecom-state").textContent =
    !c.enabled ? "当前状态：已停用"
      : (d.running ? "当前状态：回调服务运行中" : "当前状态：仅推送模式");
  const badge = document.getElementById("wecom-badge");
  const anySaved = !!String(c.corpid || "").trim() || !!c.secret_set;
  badge.textContent = c.enabled ? (d.running ? "运行中" : "仅推送")
    : (anySaved ? "已停用" : "未配置");
  badge.classList.toggle("on", !!c.enabled);
  $("wecom-saved").textContent = "";
  $("wecom-relay-state").textContent = "";
}

  /* 中转地址去掉 /qyapi 就是回调根，拼出建议给企微后台填的回调 URL */
function defaultCallbackUrl(apiBase) {
  const b = String(apiBase || "").trim().replace(/\/+$/, "");
  if (!b) return "";
  const root = b.replace(/\/cgi-bin$/, "").replace(/\/qyapi$/, "");
  return root + "/wecom/callback";
}

let wecomCbAuto = "";        // 上次自动填进去的回调地址（用户手填过就不再覆盖）
function wecomSyncCallback() {
  const base = document.getElementById("wecom-api-base").value.trim();
  const el = document.getElementById("wecom-callback-url");
  const cur = el.value.trim();
  if (cur && cur !== wecomCbAuto) return;
  const v = defaultCallbackUrl(base);
  el.value = v;
  wecomCbAuto = v;
}

async function wecomTestRelay() {
  const el = document.getElementById("wecom-relay-state");
  const base = document.getElementById("wecom-api-base").value.trim();
  if (!base) { el.textContent = "先填消息推送代理"; return; }
  el.textContent = "正在测中转…";
  try {
    const d = await api("/api/wecom/test_relay", { api_base: base });
    el.textContent = (d.ok ? "✔ " : "✘ ") + (d.msg || "");
  } catch (e) {
    el.textContent = "✘ " + String(e.message || e);
  }
}

async function wecomCopyCallback() {
  const el = document.getElementById("wecom-callback-url");
  const v = (el.value || "").trim();
  if (!v) { alertMsg("提示", "先填消息推送代理，或直接手填回调地址"); return; }
  try {
    await navigator.clipboard.writeText(v);
    const tip = document.getElementById("wecom-relay-state");
    tip.textContent = "✔ 回调地址已复制";
  } catch (e) {
    el.select();
    document.execCommand("copy");
  }
}

const openWecom = () => showPage("wecom");

/* 退出登录 */
document.getElementById("logout-link")?.addEventListener("click", async () => {
  try { await fetch("/api/logout", { method: "POST" }); } catch (_) {}
  location.href = "/login";
});
pageInit.wecom = () => { loadWecom().catch(() => {}); };
async function wecomSave() {
  try {
    const d = await api("/api/wecom", {
      agentid: document.getElementById("wecom-agentid").value,
      api_base: document.getElementById("wecom-api-base").value,
      callback_url: document.getElementById("wecom-callback-url").value,
      corpid: document.getElementById("wecom-corpid").value,
      secret: document.getElementById("wecom-secret").value,
      token: document.getElementById("wecom-token").value,
      aeskey: document.getElementById("wecom-aeskey").value,
      port: document.getElementById("wecom-port").value,
      allow_users: document.getElementById("wecom-users").value,
      enabled: document.getElementById("wecom-enabled").checked,
    });
    document.getElementById("wecom-state").textContent = "当前状态：" + d.state;
    await loadWecom().catch(() => {});          // 刷新徽标/状态，注意它会清空提示，所以放前面
    const tip = document.getElementById("wecom-saved");
    tip.textContent = "✔ 已保存并应用 " + d.state;
    setTimeout(() => { tip.textContent = ""; }, 5000);
  } catch (e) {
    alertMsg("保存失败", String(e.message || e));
  }
}
async function wecomTest() {
  try {
    const d = await api("/api/wecom/test_push", {
      corpid: document.getElementById("wecom-corpid").value,
      secret: document.getElementById("wecom-secret").value,
      agentid: document.getElementById("wecom-agentid").value,
    });
    alertMsg("测试推送", "已发到应用会话（接收人：" + (d.to || "默认") + "），去企微看看");
  } catch (e) {}
}

/* ================= 文件浏览 / 选择位置（资源管理器样式） ================= */
const SVG_FOLDER = '<svg class="fs-ic" viewBox="0 0 24 24"><path fill="currentColor" d="M10 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2h-8l-2-2z"/></svg>';
const SVG_FILE = '<svg class="fs-ic" viewBox="0 0 24 24"><path fill="currentColor" d="M14 2H6c-1.1 0-2 .9-2 2v16c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V8l-6-6zm-1 7V3.5L18.5 9H13z"/></svg>';
const SVG_DISK = '<svg class="fs-ic" viewBox="0 0 24 24"><path fill="currentColor" d="M6 2h12c1.1 0 2 .9 2 2v16c0 1.1-.9 2-2 2H6c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2zm6 3.5A6.5 6.5 0 0 0 5.5 12H9a3 3 0 0 1 6 0h3.5A6.5 6.5 0 0 0 12 5.5zM5.5 14A6.5 6.5 0 0 0 12 20.5 6.5 6.5 0 0 0 18.5 14H15a3 3 0 0 1-6 0H5.5z"/></svg>';
let fsPickTarget = null;   // 点「选用此目录」时要写回的输入框 id
let fsCurPath = "";
let fsRootsCache = null;

async function fsGetRoots() {
  if (!fsRootsCache) {
    try { fsRootsCache = (await api("/api/fs/roots")).roots || []; }
    catch (e) { fsRootsCache = []; }
  }
  return fsRootsCache;
}

async function fsBrowse(path, targetId) {
  if (targetId !== undefined) {
    fsPickTarget = targetId || null;
    fsRootsCache = null;           // 重新打开选择器时刷新挂载状态
  }
  document.getElementById("fs-pick").style.display = fsPickTarget ? "" : "none";
  document.getElementById("fs-title").textContent = fsPickTarget ? "选择位置" : "文件";
  showModal("fs-modal");
  let d;
  try {
    d = await api("/api/fs/list?path=" + encodeURIComponent(path || ""));
  } catch (e) {
    // 路径不存在 / 未挂载 → 落到第一个可访问的挂载根目录，别让弹窗空着
    try {
      const roots = (await fsGetRoots()).filter(x => x.exists);
      const pick = roots.find(x => !x.is_data) || roots[0];
      if (pick && pick.path !== path) {
        d = await api("/api/fs/list?path=" + encodeURIComponent(pick.path));
      }
    } catch (e2) {}
    if (!d) {
      document.getElementById("fs-crumb").innerHTML = "";
      document.getElementById("fs-list").innerHTML =
        `<div class="muted" style="padding:8px">${esc(e.message || e)}</div>`;
      return;
    }
  }
  fsCurPath = d.path;
  const roots = await fsGetRoots();
  renderFsCrumb(d, roots);
  renderFsList(d, roots);
}

/* 面包屑：位置快捷芯片 + 逐级路径，点任何一段都能跳 */
function renderFsCrumb(d, roots) {
  const crumb = document.getElementById("fs-crumb");
  crumb.innerHTML = "";
  const p = d.path.replace(/\\/g, "/").replace(/\/+$/, "");
  // 找到所属根目录
  let rootUsed = null, rootPath = "";
  for (const rt of roots) {
    const rp = rt.path.replace(/\\/g, "/").replace(/\/+$/, "");
    if (p === rp || p.startsWith(rp + "/")) { rootUsed = rt; rootPath = rp; break; }
  }
  const crumbEl = (label, path, icon) => {
    const s = document.createElement("span");
    s.className = "fs-crumb-seg";
    if (icon) s.innerHTML = icon;
    s.appendChild(document.createTextNode(label));
    s.addEventListener("click", () => fsBrowse(path));
    crumb.appendChild(s);
  };
  // 左侧：所有可访问的根目录（数据目录 + 各挂载盘/NAS）
  roots.forEach(rt => {
    const rp = rt.path.replace(/\\/g, "/").replace(/\/+$/, "");
    const name = rt.is_data ? "数据目录" : rp.split("/").pop() || rp;
    const cls = rt.is_data ? "is-data" : (rt.exists ? "" : "off");
    const icon = rt.is_data ? SVG_FOLDER : SVG_DISK;
    const s = document.createElement("span");
    s.className = "fs-root-chip " + cls;
    s.innerHTML = icon;
    s.appendChild(document.createTextNode(" " + name + (rt.exists ? "" : "（未挂载）")));
    s.addEventListener("click", () => fsBrowse(rp));
    crumb.appendChild(s);
  });
  const sep = document.createElement("span");
  sep.className = "fs-crumb-sep";
  sep.textContent = "›";
  crumb.appendChild(sep);
  // 逐级路径
  if (rootUsed) {
    let acc = rootPath;
    crumbEl(rootUsed.is_data ? "数据目录" : (rootPath.split("/").pop() || rootPath), acc);
    const rest = p.slice(rootPath.length).replace(/^\//, "");
    if (rest) {
      rest.split("/").forEach(seg => {
        acc += "/" + seg;
        crumbEl(seg, acc);
      });
    }
  } else {
    crumbEl(p, p);
  }
}

function renderFsList(d, roots) {
  const list = document.getElementById("fs-list");
  list.innerHTML = "";
  if (d.parent) {
    const up = document.createElement("div");
    up.className = "fs-item";
    up.innerHTML = '<span class="fs-ic-wrap">↩</span> ..（上一级）';
    up.addEventListener("click", () => fsBrowse(d.parent));
    list.appendChild(up);
  }
  const dirs = d.items.filter(x => x.dir), files = d.items.filter(x => !x.dir);
  const row = (iconCls, iconSvg, name, extra, onClick, cls) => {
    const el = document.createElement("div");
    el.className = "fs-item " + (cls || "");
    el.innerHTML = `<span class="fs-ic-wrap ${iconCls}">${iconSvg}</span>`
                 + `<span class="fs-name">${esc(name)}</span>`
                 + (extra ? `<span class="size">${extra}</span>` : "");
    el.addEventListener("click", onClick);
    list.appendChild(el);
  };
  dirs.forEach(it => row("ic-dir", SVG_FOLDER, it.name, "",
    () => fsBrowse(d.path.replace(/\\/g, "/") + "/" + it.name)));
  files.forEach(it => row("ic-file", SVG_FILE, it.name, fmtSize(it.size),
    () => window.open("/api/fs/download?path=" + encodeURIComponent(d.path + "/" + it.name))));
  if (!dirs.length && !files.length && !d.parent) {
    list.innerHTML = '<div class="muted" style="padding:10px">（空目录）</div>';
  }
  // 选择模式下提示当前选中的位置
  const hint = document.getElementById("fs-hint");
  if (hint) hint.textContent = fsPickTarget ? "当前选中：" + fsCurPath : "";
}
async function fsPickCurrent() {
  const target = fsPickTarget;
  if (!target || !fsCurPath) return;
  document.getElementById(target).value = fsCurPath;
  hideModal("fs-modal");
  fsPickTarget = null;
  if (target === "mat-out") await matSaveOut();
  else if (target === "pub-out") await pubSaveOut();
  else if (target === "ap-mat") await apSaveSettings();
}
/* 检查目录是否在容器里存在且可写（NAS/第二块盘没挂载时会明确提示） */
async function fsCheck(path, label) {
  if (!path || !String(path).trim()) return true;
  try {
    const d = await api("/api/fs/check?path=" + encodeURIComponent(String(path).trim()));
    if (d.ok) return true;
    alertMsg("目录不可用", `${label}：${path}\n\n${d.msg}`);
    return false;
  } catch (e) {
    return true;  // 检查接口本身出错时不阻塞保存
  }
}
function fmtSize(n) {
  if (n > 1048576) return (n / 1048576).toFixed(1) + " MB";
  if (n > 1024) return (n / 1024).toFixed(1) + " KB";
  return n + " B";
}

/* ================= 浏览器画面 ================= */
function vncUrl() {
  if (G.env && G.env.novnc_url) {
    return G.env.novnc_url.startsWith("http") ? G.env.novnc_url
      : location.origin + G.env.novnc_url;
  }
  return `http://${location.hostname}:6080/vnc.html?autoconnect=1&resize=scale`;
}
async function loadVnc() {
  const tip = document.getElementById("vnc-tip");
  const frame = document.getElementById("vnc-frame");
  tip.style.display = "flex";
  tip.textContent = "正在检查虚拟屏幕…";
  let h = null;
  try { h = await api("/api/vnc/health"); } catch (e) { /* 检查失败就照常直连 */ }
  if (h && h.ok === false) {
    // 直接怼 noVNC 只会看到「无法连接到服务器」，这里把具体哪一环挂了说清楚
    tip.innerHTML = '<div style="max-width:640px;line-height:1.9;color:#ffb4b4;font-size:13px;white-space:pre-wrap">'
      + esc(h.hint) + "</div>";
    frame.removeAttribute("src");
    return;
  }
  tip.style.display = "none";
  frame.src = vncUrl();
}
function loadVncFullscreen() {
  window.open(vncUrl(), "_blank");
}

/* ================= 杂项 ================= */
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function HUB_log(channel, msg) { appendLog(channel, { ts: new Date().toTimeString().slice(0, 8), msg, level: "info" }); }

/* ================= 启动 ================= */
connectWs();
initPubDefaults();
pollState();
setInterval(pollState, 3000);
setInterval(() => {
  // 当前页面的数据快照轮询（运行中的任务状态/行状态）
  const active = document.querySelector(".page.active").id.replace("page-", "");
  if (active === "material") refreshMaterial().catch(() => {});
  else if (active === "publish") api("/api/publish").then(d => {
    PUB = d; renderPubAccounts(); renderPubTasks(); pubButtons();
  }).catch(() => {});
  else if (active === "appeal") refreshAppeal().catch(() => {});
}, 4000);
