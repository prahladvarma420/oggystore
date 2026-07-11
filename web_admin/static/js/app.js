/* ===========================================================================
   OGGY PREMIUM STORE - Web Admin frontend
   Vanilla JS SPA. Every button is wired to the Flask API in app.py.
=========================================================================== */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, c => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const stripTg = (s) => String(s == null ? "" : s).replace(/<tg-emoji emoji-id="\d+">([\s\S]*?)<\/tg-emoji>/g, "$1");
const escName = (s) => esc(stripTg(s));
const money = (n) => "$" + Number(n || 0).toFixed(2);
const fmtDate = (d) => d ? String(d).replace("T", " ").slice(0, 16) : "";

let currentView = "dashboard";
let searchTimer = null;
let cache = {};

/* ---------------------------------------------------------------- API call */
const CSRF = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
async function api(path, opts = {}) {
  const method = (opts.method || "GET").toUpperCase();
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (method !== "GET") headers["X-CSRF-Token"] = CSRF;
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) { location.href = "/login"; return {}; }
  if (res.headers.get("content-type", "").includes("text/csv")) return res;
  let data;
  try { data = await res.json(); } catch (e) { data = { ok: false, error: "Bad response" }; }
  return data;
}
const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body || {}) });

/* ------------------------------------------------------------------ toast */
function toast(msg, type = "ok") {
  const el = document.createElement("div");
  el.className = "toast " + type;
  el.textContent = msg;
  $("#toastWrap").appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; el.style.transform = "translateX(120%)"; el.style.transition = ".4s"; }, 2600);
  setTimeout(() => el.remove(), 3100);
}

/* ------------------------------------------------------------------ modal */
function openModal(title, html) {
  $("#modalTitle").textContent = title;
  $("#modalBody").innerHTML = html;
  $("#modalBackdrop").classList.add("show");
}
function closeModal() { $("#modalBackdrop").classList.remove("show"); }
$("#modalClose").onclick = closeModal;
$("#modalBackdrop").onclick = (e) => { if (e.target === $("#modalBackdrop")) closeModal(); };

const val = (id) => { const e = $("#" + id); return e ? e.value.trim() : ""; };
const checked = (id) => { const e = $("#" + id); return e ? e.checked : false; };

/* ----------------------------------------------------------- view routing */
const VIEW_TITLES = {
  dashboard: "Dashboard", products: "Products & Stock", orders: "Orders",
  users: "Users", coupons: "Coupons", broadcasts: "Broadcasts",
  templates: "Message Templates", tickets: "Support Tickets",
  withdrawals: "Withdrawals", settings: "Settings", admins: "Admins",
  logs: "Activity Logs", backups: "Backups",
  deposits: "Deposits & Payments", restock: "Restock Requests",
  profit: "Profit & Cost", security: "Security",
};
const SEARCH_VIEWS = { products: "Search products...", orders: "Search orders / user id...", users: "Search users..." };

function setView(view) {
  currentView = view;
  $$(".nav-item").forEach(b => b.classList.toggle("active", b.dataset.view === view));
  $("#viewTitle").textContent = VIEW_TITLES[view] || view;
  const sw = $("#searchWrap");
  if (SEARCH_VIEWS[view]) { sw.style.display = ""; $("#searchInput").placeholder = SEARCH_VIEWS[view]; $("#searchInput").value = ""; }
  else sw.style.display = "none";
  render();
}

$$(".nav-item").forEach(b => b.onclick = () => setView(b.dataset.view));
$("#refreshBtn").onclick = () => render();
$("#searchInput").oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(render, 320); };

function loading() { $("#content").innerHTML = '<div class="loader"><div class="spinner"></div></div>'; }
function empty(msg, icon = "\u2728") { return `<div class="empty"><div class="big">${icon}</div>${esc(msg)}</div>`; }

async function render() {
  const v = currentView;
  loading();
  try {
    if (v === "dashboard") await viewDashboard();
    else if (v === "products") await viewProducts();
    else if (v === "orders") await viewOrders();
    else if (v === "users") await viewUsers();
    else if (v === "coupons") await viewCoupons();
    else if (v === "broadcasts") await viewBroadcasts();
    else if (v === "templates") await viewTemplates();
    else if (v === "tickets") await viewTickets();
    else if (v === "withdrawals") await viewWithdrawals();
    else if (v === "settings") await viewSettings();
    else if (v === "admins") await viewAdmins();
    else if (v === "logs") await viewLogs();
    else if (v === "backups") await viewBackups();
    else if (v === "deposits") await viewDeposits();
    else if (v === "restock") await viewRestock();
    else if (v === "profit") await viewProfit();
    else if (v === "security") await viewSecurity();
  } catch (e) {
    $("#content").innerHTML = empty("Failed to load: " + e.message, "\u26a0\ufe0f");
  }
}

/* =====================================================  DASHBOARD  ======== */
let chartRev = null;
async function viewDashboard() {
  const d = await api("/api/dashboard");
  if (!d.ok) return $("#content").innerHTML = empty("No data");
  const s = d.stats, t = d.totals;
  const card = (cls, ic, label, value) =>
    `<div class="stat-card glass ${cls}"><div class="ic">${ic}</div><div class="label">${label}</div><div class="value">${value}</div></div>`;
  const top = d.top.length ? d.top.map(r =>
    `<div class="kv"><span>${escName(r.product_name || "-")}</span><b>${r.sold} sold \u00b7 ${money(r.revenue)}</b></div>`).join("") : empty("No sales yet");
  const recent = d.recent.length ? `<div class="tbl-wrap"><table><thead><tr><th>#</th><th>Product</th><th>User</th><th>Price</th><th>When</th></tr></thead><tbody>${
    d.recent.map(o => `<tr><td>#${o.id}</td><td>${escName(o.product_name)}</td><td>${esc(o.first_name || o.user_id)}</td><td>${money(o.price)}</td><td class="muted">${fmtDate(o.created)}</td></tr>`).join("")
  }</tbody></table></div>` : empty("No recent orders");

  $("#content").innerHTML = `
    <div class="stat-grid">
      ${card("c2", "\ud83d\udcb0", "Revenue Today", money(s.today_revenue))}
      ${card("c1", "\ud83d\uded2", "Orders Today", s.today_orders)}
      ${card("c3", "\ud83d\udc64", "New Users Today", s.new_users)}
      ${card("c4", "\ud83c\udfab", "Open Tickets", s.open_tickets)}
      ${card("c5", "\u26a0\ufe0f", "Low Stock", s.low_stock)}
    </div>
    <div class="stat-grid">
      ${card("c2", "\ud83d\udcc8", "Total Revenue", money(s.total_revenue))}
      ${card("c1", "\ud83d\udce6", "Total Orders", s.total_orders)}
      ${card("c3", "\ud83d\udc65", "Total Users", t.users)}
      ${card("c5", "\ud83d\udce6", "Active Products", t.active_products + "/" + t.products)}
      ${card("c4", "\ud83d\udcb8", "Pending Withdrawals", t.pending_withdrawals)}
    </div>
    <div class="row-2">
      <div class="panel glass"><h3>\ud83d\udcc8 Revenue \u2014 last 7 days</h3><div class="chart-box"><canvas id="revChart"></canvas></div></div>
      <div class="panel glass"><h3>\ud83c\udfc6 Top Sellers</h3>${top}</div>
    </div>
    <div class="panel glass"><h3>\ud83d\udd52 Recent Orders</h3>${recent}</div>
    <div class="row-2">
      <div class="panel glass"><h3>\ud83d\udcb5 Sales vs Deposits \u2014 14 days</h3><div class="chart-box"><canvas id="sdChart"></canvas></div></div>
      <div class="panel glass"><h3>\ud83c\udfc5 Top Products</h3><div class="chart-box"><canvas id="topChart"></canvas></div></div>
    </div>
    <div class="row-2">
      <div class="panel glass"><h3>\ud83d\udc65 User Growth \u2014 14 days</h3><div class="chart-box"><canvas id="ugChart"></canvas></div></div>
      <div class="panel glass"><h3>\u23f0 Busiest Hours (orders)</h3><div class="chart-box"><canvas id="hrChart"></canvas></div></div>
    </div>
    <div class="panel glass" id="lowStockPanel"><h3>\u26a0\ufe0f Low Stock</h3><div class="muted">Loading\u2026</div></div>`;

  if (window.Chart) {
    const ctx = $("#revChart");
    if (chartRev) chartRev.destroy();
    const grad = ctx.getContext("2d").createLinearGradient(0, 0, 0, 240);
    grad.addColorStop(0, "rgba(0,240,255,.55)"); grad.addColorStop(1, "rgba(155,92,255,0)");
    chartRev = new Chart(ctx, {
      type: "line",
      data: { labels: d.series.map(x => x.date.slice(5)), datasets: [{
        label: "Revenue", data: d.series.map(x => x.revenue), fill: true,
        backgroundColor: grad, borderColor: "#00f0ff", tension: .4, pointBackgroundColor: "#ff3df0", borderWidth: 3 }] },
      options: { plugins: { legend: { display: false } }, scales: {
        x: { ticks: { color: "#9a90c8" }, grid: { color: "rgba(140,90,255,.08)" } },
        y: { ticks: { color: "#9a90c8" }, grid: { color: "rgba(140,90,255,.08)" } } },
        maintainAspectRatio: false } });
  }
  loadDashCharts();
}

const AX = { ticks: { color: "#9a90c8" }, grid: { color: "rgba(140,90,255,.08)" } };
let dashCharts = {};
async function loadDashCharts() {
  if (!window.Chart) return;
  const a = await api("/api/analytics");
  if (!a.ok) return;
  Object.values(dashCharts).forEach(c => { try { c.destroy(); } catch (e) {} });
  dashCharts = {};
  let el = $("#sdChart");
  if (el) dashCharts.sd = new Chart(el, { type: "line",
    data: { labels: a.sales_deposits.map(x => x.date.slice(5)), datasets: [
      { label: "Sales", data: a.sales_deposits.map(x => x.sales), borderColor: "#00f0ff", backgroundColor: "rgba(0,240,255,.15)", fill: true, tension: .4, borderWidth: 2 },
      { label: "Deposits", data: a.sales_deposits.map(x => x.deposits), borderColor: "#ffb800", backgroundColor: "rgba(255,184,0,.12)", fill: true, tension: .4, borderWidth: 2 } ] },
    options: { plugins: { legend: { labels: { color: "#cfc6f5" } } }, scales: { x: AX, y: AX }, maintainAspectRatio: false } });
  el = $("#topChart");
  if (el) dashCharts.top = new Chart(el, { type: "bar",
    data: { labels: a.top_products.map(x => (x.product_name || "-").slice(0, 14)), datasets: [
      { label: "Sold", data: a.top_products.map(x => x.sold), backgroundColor: "#ff3df0", borderRadius: 6 } ] },
    options: { plugins: { legend: { display: false } }, scales: { x: AX, y: AX }, maintainAspectRatio: false } });
  el = $("#ugChart");
  if (el) dashCharts.ug = new Chart(el, { type: "line",
    data: { labels: a.user_growth.map(x => x.date.slice(5)), datasets: [
      { label: "Total users", data: a.user_growth.map(x => x.total), borderColor: "#9b5cff", backgroundColor: "rgba(155,92,255,.18)", fill: true, tension: .4, borderWidth: 2 },
      { label: "New", data: a.user_growth.map(x => x.new), borderColor: "#39ff14", backgroundColor: "rgba(57,255,20,.1)", fill: false, tension: .4, borderWidth: 2 } ] },
    options: { plugins: { legend: { labels: { color: "#cfc6f5" } } }, scales: { x: AX, y: AX }, maintainAspectRatio: false } });
  el = $("#hrChart");
  if (el) dashCharts.hr = new Chart(el, { type: "bar",
    data: { labels: a.busy_hours.map((_, i) => i + "h"), datasets: [
      { label: "Orders", data: a.busy_hours, backgroundColor: "#00f0ff", borderRadius: 4 } ] },
    options: { plugins: { legend: { display: false } }, scales: { x: AX, y: AX }, maintainAspectRatio: false } });
  const lp = $("#lowStockPanel");
  if (lp) lp.innerHTML = `<h3>\u26a0\ufe0f Low Stock (\u2264 ${a.low_threshold})</h3>` + (a.low_stock.length
    ? `<div class="tbl-wrap"><table><thead><tr><th>Product</th><th>Available</th></tr></thead><tbody>${a.low_stock.map(p => `<tr><td>${escName(p.name)}</td><td><span class="badge ${p.available === 0 ? "off" : "warn"}">${p.available}</span></td></tr>`).join("")}</tbody></table></div>`
    : empty("All products well stocked \ud83c\udf89"));
}

/* =====================================================  PRODUCTS  ========= */
async function viewProducts() {
  const search = $("#searchInput").value.trim();
  const d = await api("/api/products?search=" + encodeURIComponent(search));
  cache.products = d.products || [];
  const rows = cache.products.map(p => {
    const flash = p.flash_price ? `<span class="badge warn">\u26a1${money(p.flash_price)}</span>` : "";
    return `<tr>
      <td>#${p.id}</td>
      <td><b>${escName(p.name)}</b> ${p.featured ? '<span class="badge purple">\u2b50</span>' : ""} ${flash}</td>
      <td>${p.flash_price ? `<s class="muted">${money(p.price)}</s> <b style="color:#ff5db1">${money(p.flash_price)}</b>` : money(p.price)}</td>
      <td>${p.available_stock} <span class="muted">avail</span> \u00b7 ${p.sold_count} sold</td>
      <td>${p.active ? '<span class="badge on">Active</span>' : '<span class="badge off">Hidden</span>'}</td>
      <td><div class="row-actions">
        <button class="btn btn-sm btn-glow" onclick="editProduct(${p.id})"><span>Edit</span></button>
        <button class="btn btn-sm btn-ghost" onclick="manageStock(${p.id})">Stock</button>
        <button class="btn btn-sm btn-ghost" onclick="manageTiers(${p.id})">Offers</button>
        <button class="btn btn-sm btn-orange" onclick="flashSale(${p.id})"><span>Flash</span></button>
        <button class="btn btn-sm btn-ghost" onclick="toggleProduct(${p.id},'active')">${p.active ? "Hide" : "Show"}</button>
        <button class="btn btn-sm btn-ghost" onclick="toggleProduct(${p.id},'featured')">${p.featured ? "Unfeature" : "Feature"}</button>
        <button class="btn btn-sm btn-red" onclick="delProduct(${p.id},'${escName(p.name)}')"><span>Del</span></button>
      </div></td></tr>`;
  }).join("");
  $("#content").innerHTML = `
    <div class="panel glass">
      <div class="toolbar"><div class="grow"></div>
        <button class="btn btn-ghost" onclick="exportCsv('products')">\u2b07 Export CSV</button>
        <button class="btn btn-glow" onclick="addProduct()"><span>\u2795 Add Product</span></button>
      </div>
      ${cache.products.length ? `<div class="tbl-wrap"><table><thead><tr><th>ID</th><th>Name</th><th>Price</th><th>Stock</th><th>Status</th><th>Actions</th></tr></thead><tbody>${rows}</tbody></table></div>` : empty("No products yet \u2014 add one!", "\ud83d\udce6")}
    </div>`;
}

function productForm(p = {}) {
  return `
  <div class="form-grid">
    <label class="full"><span>Name</span><input id="f_name" value="${esc(p.name || "")}"></label>
    <label><span>Price (USDT)</span><input id="f_price" type="number" step="0.01" value="${p.price ?? ""}"></label>
    <label><span>Cost / buy price (USDT)</span><input id="f_cost" type="number" step="0.01" value="${p.cost ?? ""}"></label>
    <label><span>Max per order</span><input id="f_max" type="number" value="${p.max_per_order ?? 1}"></label>
    <label class="full"><span>Description</span><textarea id="f_desc">${esc(p.description || "")}</textarea></label>
    <label><span>Validity</span><input id="f_validity" value="${esc(p.validity || "")}"></label>
    <label><span>Warranty</span><input id="f_warranty" value="${esc(p.warranty || "")}"></label>
    <label class="full"><span>Rules</span><textarea id="f_rules">${esc(p.rules || "")}</textarea></label>
    <label class="full"><span>Redeem link / instructions</span><input id="f_redeem" value="${esc(p.redeem_link || "")}"></label>
    ${p.id ? "" : `<label class="full"><span>Initial stock codes (one per line, optional)</span><textarea id="f_codes" placeholder="CODE-1\nCODE-2"></textarea></label>`}
  </div>
  <div class="toolbar" style="margin-top:18px"><div class="grow"></div>
    <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
    <button class="btn btn-glow" id="saveProductBtn"><span>${p.id ? "Save Changes" : "Create Product"}</span></button>
  </div>`;
}
function collectProduct() {
  return { name: val("f_name"), price: parseFloat(val("f_price") || 0),
    cost: parseFloat(val("f_cost") || 0),
    max_per_order: parseInt(val("f_max") || 1), description: val("f_desc"),
    validity: val("f_validity"), warranty: val("f_warranty"), rules: val("f_rules"),
    redeem_link: val("f_redeem") };
}
function addProduct() {
  openModal("\u2795 Add Product", productForm());
  $("#saveProductBtn").onclick = async () => {
    const body = collectProduct();
    body.stock_codes = val("f_codes");
    if (!body.name) return toast("Name required", "err");
    const r = await post("/api/products/create", body);
    if (r.ok) { toast("Product created"); closeModal(); render(); } else toast(r.error || "Failed", "err");
  };
}
function editProduct(id) {
  const p = cache.products.find(x => x.id === id);
  openModal("\u270f\ufe0f Edit " + stripTg(p.name), productForm(p));
  $("#saveProductBtn").onclick = async () => {
    const r = await post(`/api/products/${id}/update`, collectProduct());
    if (r.ok) { toast("Saved"); closeModal(); render(); } else toast(r.error || "Failed", "err");
  };
}
async function toggleProduct(id, field) {
  const r = await post(`/api/products/${id}/toggle`, { field });
  if (r.ok) { toast(field + " updated"); render(); } else toast("Failed", "err");
}
async function delProduct(id, name) {
  if (!confirm("Delete product '" + name + "'? This removes its stock & offers.")) return;
  const r = await post(`/api/products/${id}/delete`, {});
  if (r.ok) { toast("Deleted"); render(); } else toast("Failed", "err");
}
function flashSale(id) {
  const p = cache.products.find(x => x.id === id);
  openModal("\u26a1 Flash Sale \u2014 " + stripTg(p.name), `
    <div class="form-grid">
      <label><span>Current price (USDT)</span><input value="${p.price ?? ""}" disabled></label>
      <label><span>Flash price (USDT)</span><input id="fl_price" type="number" step="0.01" value="${p.flash_price ?? ""}"></label>
      <label><span>Duration (hours)</span><input id="fl_hours" type="number" value="24"></label>
    </div>
    <label class="chk"><input type="checkbox" id="fl_bc" checked> <span>\ud83d\udce2 Broadcast this flash sale to all users</span></label>
    <div class="toolbar" style="margin-top:18px"><div class="grow"></div>
      ${p.flash_price ? '<button class="btn btn-red" id="flClear"><span>Clear Flash</span></button>' : ""}
      <button class="btn btn-glow" id="flSave"><span>\ud83d\ude80 Start Flash Sale</span></button>
    </div>`);
  $("#flSave").onclick = async () => {
    const price = parseFloat(val("fl_price") || 0);
    if (!price || price <= 0) { toast("Enter a flash price greater than 0", "err"); return; }
    $("#flSave").disabled = true;
    const r = await post(`/api/products/${id}/flash`, { flash_price: price, hours: parseFloat(val("fl_hours") || 24), broadcast: checked("fl_bc") });
    if (r.ok) {
      let m = "Flash sale set" + (r.discount ? ` (-${r.discount}% OFF)` : "");
      if (r.broadcast) m += ` \u00b7 notified ${r.sent}` + (r.failed ? `, ${r.failed} failed` : "");
      toast(m); closeModal(); render();
    } else { const b = $("#flSave"); if (b) b.disabled = false; toast(r.error || "Failed", "err"); }
  };
  if ($("#flClear")) $("#flClear").onclick = async () => {
    const r = await post(`/api/products/${id}/flash`, { clear: true });
    if (r.ok) { toast("Flash cleared"); closeModal(); render(); }
  };
}
async function manageStock(id) {
  const p = cache.products.find(x => x.id === id);
  const d = await api(`/api/products/${id}/stock`);
  const items = (d.items || []).map(i =>
    `<tr><td>#${i.id}</td><td class="mono" style="word-break:break-all;white-space:normal;max-width:340px">${esc(i.code)}</td><td>${i.sold ? '<span class="badge off">Sold</span>' : '<span class="badge on">Available</span>'}</td>
     <td class="muted">${i.sold_to || ""}</td>
     <td>${i.sold ? "" : `<button class="btn btn-sm btn-red" onclick="delStock(${i.id},${id},this)"><span>Del</span></button>`}</td></tr>`).join("");
  openModal("\ud83e\uddfe Stock \u2014 " + stripTg(p.name), `
    <label class="field" style="margin-bottom:14px"><span>Add stock codes (one per line)</span>
      <textarea id="st_codes" placeholder="NEW-CODE-1\nNEW-CODE-2"></textarea></label>
    <div class="toolbar"><div class="grow"></div><button class="btn btn-glow" id="stAdd"><span>\u2795 Add Stock</span></button></div>
    <div class="tbl-wrap" style="margin-top:10px"><table><thead><tr><th>ID</th><th>Code</th><th>Status</th><th>Sold to</th><th></th></tr></thead><tbody>${items || '<tr><td colspan=5 class="muted">No codes</td></tr>'}</tbody></table></div>`);
  $("#stAdd").onclick = async () => {
    const codes = val("st_codes");
    if (!codes) return toast("Enter codes", "err");
    const r = await post(`/api/products/${id}/stock/add`, { codes });
    if (r.ok) { toast("Added " + r.added + " codes"); manageStock(id); render(); } else toast("Failed", "err");
  };
}
async function delStock(stockId, pid, btn) {
  const r = await post(`/api/stock/${stockId}/delete`, {});
  if (r.ok) {
    toast("Code removed");
    if (btn && btn.closest) { const tr = btn.closest("tr"); if (tr) tr.remove(); }
    render();
  } else { toast("Failed", "err"); }
}
async function manageTiers(id) {
  const p = cache.products.find(x => x.id === id);
  const tiers = (p.tiers || []).map(t =>
    `<tr><td>${t.quantity}</td><td>${money(t.price)}</td><td><button class="btn btn-sm btn-red" onclick="delTier(${id},${t.quantity})"><span>Del</span></button></td></tr>`).join("");
  openModal("\ud83c\udff7\ufe0f Price Offers \u2014 " + stripTg(p.name), `
    <p class="muted" style="margin-bottom:12px">Bulk price tiers shown as buy buttons in the bot (e.g. 5 for $20).</p>
    <div class="form-grid">
      <label><span>Quantity</span><input id="ti_qty" type="number"></label>
      <label><span>Total price</span><input id="ti_price" type="number" step="0.01"></label>
    </div>
    <div class="toolbar" style="margin-top:14px"><div class="grow"></div><button class="btn btn-glow" id="tiAdd"><span>Save Tier</span></button></div>
    <p class="muted" style="margin:14px 0 6px">Or add <b>multiple offers</b> at once — one per line as <b>QUANTITY | TOTAL PRICE</b>:</p>
    <textarea id="ti_bulk" rows="4" style="width:100%" placeholder="1 | 1&#10;5 | 4&#10;10 | 7"></textarea>
    <div class="toolbar" style="margin-top:10px"><div class="grow"></div><button class="btn btn-glow" id="tiBulk"><span>📢 Save & Publish All</span></button></div>
    <div class="tbl-wrap"><table><thead><tr><th>Qty</th><th>Price</th><th></th></tr></thead><tbody>${tiers || '<tr><td colspan=3 class="muted">No tiers</td></tr>'}</tbody></table></div>`);
  $("#tiAdd").onclick = async () => {
    const r = await post(`/api/products/${id}/tiers/set`, { quantity: parseInt(val("ti_qty")), price: parseFloat(val("ti_price")) });
    if (r.ok) { toast("Tier saved"); await reloadProducts(); manageTiers(id); } else toast("Failed", "err");
  };
  $("#tiBulk").onclick = async () => {
    const lines = val("ti_bulk").split("\n").map(s => s.trim()).filter(Boolean);
    if (!lines.length) { toast("Enter at least one offer line", "err"); return; }
    const items = [];
    for (const line of lines) {
      const parts = line.split("|");
      const quantity = parseInt((parts[0] || "").trim());
      const price = parseFloat((parts[1] || "").trim());
      if (parts.length !== 2 || !quantity || !price || quantity <= 0 || price <= 0) {
        toast("Invalid line: " + line + " \u2014 use QUANTITY | PRICE", "err"); return;
      }
      items.push({ quantity, price });
    }
    const r = await post(`/api/products/${id}/tiers/bulk_set`, { tiers: items });
    if (r.ok) { toast(`Saved ${r.saved} offers \u2022 published to ${r.sent} users`); await reloadProducts(); manageTiers(id); } else toast("Failed", "err");
  };
}
async function delTier(id, qty) {
  const r = await post(`/api/products/${id}/tiers/delete`, { quantity: qty });
  if (r.ok) { toast("Removed"); await reloadProducts(); manageTiers(id); }
}
async function reloadProducts() { const d = await api("/api/products"); cache.products = d.products || []; }

/* =====================================================  ORDERS  =========== */
async function viewOrders() {
  const search = $("#searchInput").value.trim();
  const d = await api("/api/orders?search=" + encodeURIComponent(search));
  const rows = (d.orders || []).map(o => `<tr>
    <td>#${o.id}</td><td>${escName(o.product_name)}</td>
    <td>${esc(o.first_name || "")} <span class="muted">${o.user_id}</span></td>
    <td>${money(o.price)}</td><td class="muted">${fmtDate(o.created)}</td>
    <td><div class="row-actions">
      <button class="btn btn-sm btn-ghost" onclick="resendOrder(${o.id})">Resend</button>
      <button class="btn btn-sm btn-red" onclick="refundOrder(${o.id},${o.price})"><span>Refund</span></button>
    </div></td></tr>`).join("");
  $("#content").innerHTML = `<div class="panel glass">
    <div class="toolbar"><div class="grow"></div><button class="btn btn-ghost" onclick="exportCsv('orders')">\u2b07 Export CSV</button></div>
    ${(d.orders || []).length ? `<div class="tbl-wrap"><table><thead><tr><th>ID</th><th>Product</th><th>User</th><th>Price</th><th>When</th><th>Actions</th></tr></thead><tbody>${rows}</tbody></table></div>` : empty("No orders", "\ud83d\uded2")}</div>`;
}
async function refundOrder(id, price) {
  if (!confirm(`Refund order #${id} (${money(price)}) back to the user's wallet and delete the order?`)) return;
  const r = await post(`/api/orders/${id}/refund`, { notify: true });
  if (r.ok) { toast("Refunded"); render(); } else toast("Failed", "err");
}
async function resendOrder(id) {
  const r = await post(`/api/orders/${id}/resend`, {});
  if (r.ok) toast("Order resent to user"); else toast("Send failed: " + (r.detail || ""), "err");
}

/* =====================================================  USERS  ============ */
async function viewUsers() {
  const search = $("#searchInput").value.trim();
  const d = await api("/api/users?search=" + encodeURIComponent(search));
  const rows = (d.users || []).map(u => `<tr>
    <td class="mono">${u.user_id}</td><td>${esc(u.first_name || "")}</td>
    <td>${u.username && u.username !== "No username" ? "@" + esc(u.username) : '<span class="muted">-</span>'}</td>
    <td>${money(u.balance)}</td>
    <td>${u.blocked ? '<span class="badge off">Blocked</span>' : '<span class="badge on">Active</span>'}</td>
    <td><div class="row-actions">
      <button class="btn btn-sm btn-glow" onclick="userDetail(${u.user_id})"><span>View</span></button>
      <button class="btn btn-sm btn-green" onclick="adjustBalance(${u.user_id},'${esc(u.first_name)}')"><span>Balance</span></button>
      <button class="btn btn-sm ${u.blocked ? "btn-green" : "btn-red"}" onclick="blockUser(${u.user_id},${u.blocked ? 0 : 1})"><span>${u.blocked ? "Unblock" : "Block"}</span></button>
    </div></td></tr>`).join("");
  $("#content").innerHTML = `<div class="panel glass">
    <div class="toolbar"><div class="grow"></div><button class="btn btn-ghost" onclick="exportCsv('users')">\u2b07 Export CSV</button></div>
    ${(d.users || []).length ? `<div class="tbl-wrap"><table><thead><tr><th>User ID</th><th>Name</th><th>Username</th><th>Balance</th><th>Status</th><th>Actions</th></tr></thead><tbody>${rows}</tbody></table></div>` : empty("No users", "\ud83d\udc65")}</div>`;
}
function adjustBalance(uid, name) {
  openModal("\ud83d\udcb0 Balance \u2014 " + name, `
    <label class="field"><span>Amount (use negative to remove)</span><input id="bal_amt" type="number" step="0.01" placeholder="e.g. 10 or -5"></label>
    <label class="field" style="flex-direction:row;align-items:center;gap:8px;margin-top:10px"><input type="checkbox" id="bal_notify" style="width:auto" checked> <span>Notify user on Telegram</span></label>
    <div class="toolbar" style="margin-top:16px"><div class="grow"></div><button class="btn btn-glow" id="balSave"><span>Apply</span></button></div>`);
  $("#balSave").onclick = async () => {
    const r = await post(`/api/users/${uid}/balance`, { amount: parseFloat(val("bal_amt") || 0), notify: checked("bal_notify") });
    if (r.ok) { toast("New balance " + money(r.balance)); closeModal(); render(); } else toast("Failed", "err");
  };
}
async function blockUser(uid, block) {
  const r = await post(`/api/users/${uid}/block`, { block: !!block });
  if (r.ok) { toast(block ? "User blocked" : "User unblocked"); render(); }
}
async function userDetail(uid) {
  const d = await api(`/api/users/${uid}`);
  const u = d.user || {};
  const orders = (d.orders || []).map(o => `<div class="kv"><span>#${o.id} ${escName(o.product_name)}</span><b>${money(o.price)}</b></div>`).join("") || '<p class="muted">No orders</p>';
  const deps = (d.deposits || []).map(o => `<div class="kv"><span>${fmtDate(o.created)} \u00b7 ${esc(o.status)}</span><b>${money(o.amount)}</b></div>`).join("") || '<p class="muted">No deposits</p>';
  openModal("\ud83d\udc64 " + (u.first_name || uid), `
    <div class="kv"><span>User ID</span><b class="mono">${u.user_id}</b></div>
    <div class="kv"><span>Username</span><b>${u.username ? "@" + esc(u.username) : "-"}</b></div>
    <div class="kv"><span>Balance</span><b>${money(u.balance)}</b></div>
    <div class="kv"><span>Joined</span><b>${fmtDate(u.joined)}</b></div>
    <div class="kv"><span>Referral earnings</span><b>${money(u.referral_earnings)}</b></div>
    <h3 style="margin:16px 0 6px">Orders</h3>${orders}
    <h3 style="margin:16px 0 6px">Deposits</h3>${deps}`);
}

/* =====================================================  COUPONS  ========== */
async function viewCoupons() {
  const d = await api("/api/coupons");
  const rows = (d.coupons || []).map(c => `<tr>
    <td class="mono"><b>${esc(c.code)}</b></td>
    <td>${c.discount_type === "fixed" ? money(c.value) : c.value + "%"}</td>
    <td>${c.used_count}/${c.max_uses || "\u221e"}</td>
    <td>${c.expires_at ? fmtDate(c.expires_at) : '<span class="muted">never</span>'}</td>
    <td>${c.active ? '<span class="badge on">Active</span>' : '<span class="badge off">Off</span>'}</td>
    <td><div class="row-actions">
      <button class="btn btn-sm btn-ghost" onclick="toggleCoupon('${esc(c.code)}')">${c.active ? "Disable" : "Enable"}</button>
      <button class="btn btn-sm btn-red" onclick="delCoupon('${esc(c.code)}')"><span>Del</span></button>
    </div></td></tr>`).join("");
  $("#content").innerHTML = `<div class="panel glass">
    <div class="toolbar"><div class="grow"></div><button class="btn btn-glow" onclick="addCoupon()"><span>\u2795 New Coupon</span></button></div>
    ${(d.coupons || []).length ? `<div class="tbl-wrap"><table><thead><tr><th>Code</th><th>Discount</th><th>Used</th><th>Expires</th><th>Status</th><th>Actions</th></tr></thead><tbody>${rows}</tbody></table></div>` : empty("No coupons", "\ud83c\udf9f\ufe0f")}</div>`;
}
function addCoupon() {
  openModal("\ud83c\udf9f\ufe0f New Coupon", `
    <div class="form-grid">
      <label><span>Code</span><input id="c_code" placeholder="SAVE10"></label>
      <label><span>Type</span><select id="c_type"><option value="percent">Percent %</option><option value="fixed">Fixed $</option></select></label>
      <label><span>Value</span><input id="c_value" type="number" step="0.01"></label>
      <label><span>Max uses (0 = unlimited)</span><input id="c_max" type="number" value="0"></label>
      <label class="full"><span>Expires at (optional, YYYY-MM-DD HH:MM:SS)</span><input id="c_exp" placeholder=""></label>
    </div>
    <div class="toolbar" style="margin-top:16px"><div class="grow"></div><button class="btn btn-glow" id="cSave"><span>Create</span></button></div>`);
  $("#cSave").onclick = async () => {
    const r = await post("/api/coupons/create", { code: val("c_code"), discount_type: val("c_type"), value: parseFloat(val("c_value") || 0), max_uses: parseInt(val("c_max") || 0), expires_at: val("c_exp") });
    if (r.ok) { toast("Coupon created"); closeModal(); render(); } else toast(r.error || "Failed", "err");
  };
}
async function toggleCoupon(code) { const r = await post(`/api/coupons/${encodeURIComponent(code)}/toggle`, {}); if (r.ok) { toast("Updated"); render(); } }
async function delCoupon(code) { if (!confirm("Delete coupon " + code + "?")) return; const r = await post(`/api/coupons/${encodeURIComponent(code)}/delete`, {}); if (r.ok) { toast("Deleted"); render(); } }

/* =====================================================  BROADCASTS  ======= */
async function viewBroadcasts() {
  const d = await api("/api/broadcasts");
  const warn = (!d.bot_online)
    ? `<div class="alert error" style="margin-bottom:12px">\u26a0\ufe0f The bot token is not loaded in the web admin, so messages cannot be sent. Add BOT_TOKEN to your .env (the same token your bot uses) and restart the web admin.</div>`
    : (d.last_error ? `<div class="alert error" style="margin-bottom:12px">\u26a0\ufe0f Last broadcast had failures: ${esc(d.last_error)}</div>` : "");
  const rows = (d.broadcasts || []).map(b => `<tr>
    <td>#${b.id}</td><td>${esc((b.message || "").slice(0, 60))}${(b.message || "").length > 60 ? "\u2026" : ""}</td>
    <td class="muted">${fmtDate(b.run_at)}</td>
    <td><span class="badge ${b.status === "sent" ? "on" : "warn"}">${esc(b.status)}</span></td>
    <td>${b.sent}\u2713 ${b.failed}\u2717${b.status === "pending" ? ` <button class="btn btn-sm" onclick="sendBroadcastNow(${b.id})"><span>Send now</span></button>` : ""}</td></tr>`).join("");
  $("#content").innerHTML = `
    <div class="panel glass">
      <h3>\ud83d\udce2 New Broadcast</h3>
      ${warn}
      <p class="muted" style="margin-bottom:12px">Sent to all bot users. Leave time empty to send instantly from the web admin. HTML formatting supported.</p>
      <textarea id="bc_msg" class="inp" placeholder="Your message to all users..." style="min-height:110px"></textarea>
      <div class="toolbar" style="margin-top:12px">
        <label class="field" style="flex-direction:row;align-items:center;gap:8px"><span>Schedule (optional)</span><input id="bc_time" class="inp" placeholder="YYYY-MM-DD HH:MM:SS" style="width:210px"></label>
        <div class="grow"></div>
        <button class="btn btn-glow" id="bcSend"><span>\ud83d\ude80 Send Broadcast</span></button>
      </div>
    </div>
    <div class="panel glass"><h3>\ud83d\udd52 Recent Broadcasts</h3>${(d.broadcasts || []).length ? `<div class="tbl-wrap"><table><thead><tr><th>ID</th><th>Message</th><th>Run at</th><th>Status</th><th>Sent/Fail</th></tr></thead><tbody>${rows}</tbody></table></div>` : empty("No broadcasts yet", "\ud83d\udce2")}</div>`;
  $("#bcSend").onclick = async () => {
    const message = val("bc_msg");
    if (!message) return toast("Enter a message", "err");
    const r = await post("/api/broadcasts/create", { message, run_at: val("bc_time") });
    if (r.ok) { toast(r.status === "scheduled" ? "Broadcast scheduled" : "Broadcast sending to all users"); render(); } else toast(r.error || "Failed", "err");
  };
}

/* =====================================================  TEMPLATES  ======== */
async function viewTemplates() {
  const d = await api("/api/templates");
  $("#content").innerHTML = (d.templates || []).map(t => `
    <div class="panel glass">
      <h3>\u270f\ufe0f ${esc(t.title)} <span class="muted" style="font-size:12px">(${esc(t.key)})</span></h3>
      <textarea class="inp" id="tpl_${t.key}" style="min-height:120px">${esc(t.body)}</textarea>
      <div class="toolbar" style="margin-top:10px"><div class="grow"></div><button class="btn btn-glow" onclick="saveTemplate('${t.key}')"><span>Save</span></button></div>
    </div>`).join("") || empty("No templates");
}
async function saveTemplate(key) {
  const body = $("#tpl_" + key).value;
  const r = await post(`/api/templates/${key}/update`, { body });
  if (r.ok) toast("Template saved"); else toast("Failed", "err");
}

/* =====================================================  TICKETS  ========== */
let ticketFilter = "open";
async function viewTickets() {
  const d = await api("/api/tickets?status=" + ticketFilter);
  const rows = (d.tickets || []).map(t => `<tr>
    <td>#${t.id}</td><td>${esc(t.first_name || t.user_id)}</td>
    <td>${esc(t.subject || "-")}</td>
    <td class="muted">${esc((t.message || "").slice(0, 50))}</td>
    <td><span class="badge ${t.status === "open" ? "warn" : t.status === "answered" ? "info" : "off"}">${esc(t.status)}</span></td>
    <td><div class="row-actions">
      <button class="btn btn-sm btn-glow" onclick="replyTicket(${t.id})"><span>Reply</span></button>
      <button class="btn btn-sm btn-ghost" onclick="closeTicket(${t.id})">Close</button>
    </div></td></tr>`).join("");
  $("#content").innerHTML = `<div class="panel glass">
    <div class="pill-tabs">
      <div class="pill-tab ${ticketFilter === "open" ? "active" : ""}" onclick="setTicketFilter('open')">Open / Answered</div>
      <div class="pill-tab ${ticketFilter === "all" ? "active" : ""}" onclick="setTicketFilter('all')">All</div>
    </div>
    ${(d.tickets || []).length ? `<div class="tbl-wrap"><table><thead><tr><th>ID</th><th>User</th><th>Subject</th><th>Message</th><th>Status</th><th>Actions</th></tr></thead><tbody>${rows}</tbody></table></div>` : empty("No tickets", "\ud83c\udfab")}</div>`;
  cache.tickets = d.tickets || [];
}
function setTicketFilter(f) { ticketFilter = f; render(); }
function replyTicket(id) {
  const t = (cache.tickets || []).find(x => x.id === id) || {};
  openModal("\ud83c\udfab Reply to Ticket #" + id, `
    <div class="kv"><span>User</span><b>${esc(t.first_name || t.user_id)}</b></div>
    <div class="kv"><span>Subject</span><b>${esc(t.subject || "-")}</b></div>
    <p class="muted" style="margin:12px 0">${esc(t.message || "")}</p>
    <label class="field"><span>Your reply (sent to user on Telegram)</span><textarea id="tk_reply" style="min-height:100px"></textarea></label>
    <div class="toolbar" style="margin-top:14px"><div class="grow"></div><button class="btn btn-glow" id="tkSend"><span>Send Reply</span></button></div>`);
  $("#tkSend").onclick = async () => {
    const reply = val("tk_reply");
    if (!reply) return toast("Enter a reply", "err");
    const r = await post(`/api/tickets/${id}/reply`, { reply });
    if (r.ok) { toast("Reply sent"); closeModal(); render(); } else toast("Failed", "err");
  };
}
async function closeTicket(id) { const r = await post(`/api/tickets/${id}/close`, {}); if (r.ok) { toast("Ticket closed"); render(); } }

/* =====================================================  WITHDRAWALS  ====== */
async function viewWithdrawals() {
  const d = await api("/api/withdrawals");
  const rows = (d.withdrawals || []).map(w => `<tr>
    <td>#${w.id}</td><td>${esc(w.first_name || w.user_id)}</td>
    <td>${money(w.amount)}</td><td class="mono">${esc((w.wallet || "").slice(0, 18))}</td>
    <td><span class="badge ${w.status === "pending" ? "warn" : w.status === "approved" ? "on" : "off"}">${esc(w.status)}</span></td>
    <td>${w.status === "pending" ? `<div class="row-actions">
      <button class="btn btn-sm btn-green" onclick="withdrawAction(${w.id},'approved')"><span>Approve</span></button>
      <button class="btn btn-sm btn-red" onclick="withdrawAction(${w.id},'rejected')"><span>Reject</span></button></div>` : '<span class="muted">-</span>'}</td></tr>`).join("");
  $("#content").innerHTML = `<div class="panel glass">${(d.withdrawals || []).length ? `<div class="tbl-wrap"><table><thead><tr><th>ID</th><th>User</th><th>Amount</th><th>Wallet</th><th>Status</th><th>Actions</th></tr></thead><tbody>${rows}</tbody></table></div>` : empty("No withdrawal requests", "\ud83d\udcb8")}</div>`;
}
async function withdrawAction(id, status) {
  let note = "";
  if (status === "rejected") { note = prompt("Reason (optional):") || ""; }
  const r = await post(`/api/withdrawals/${id}/action`, { status, note });
  if (r.ok) { toast("Withdrawal " + status); render(); } else toast("Failed", "err");
}

/* =====================================================  SETTINGS  ========= */
const SETTING_LABELS = {
  referral_percent: "Referral commission %", loyalty_percent: "Loyalty points %",
  first_purchase_discount_percent: "First purchase discount %",
  min_withdrawal: "Minimum withdrawal $", low_stock_alert_threshold: "Low stock alert threshold",
  low_stock_alert_levels: "Low stock alert levels (csv)", buy_x_qty: "Buy X quantity",
  buy_x_free: "Get Y free", auto_welcome_coupon: "Auto welcome coupon code",
  inactive_days: "Inactive days threshold", inactive_coupon: "Inactive user coupon code",
};
async function viewSettings() {
  const d = await api("/api/settings");
  const s = d.settings || {};
  const fields = Object.keys(SETTING_LABELS).map(k =>
    `<label><span>${SETTING_LABELS[k]}</span><input id="set_${k}" class="inp" value="${esc(s[k] ?? "")}"></label>`).join("");
  $("#content").innerHTML = `<div class="panel glass">
    <h3>\u2699\ufe0f Store Settings</h3>
    <div class="form-grid">${fields}</div>
    <div class="toolbar" style="margin-top:18px"><div class="grow"></div><button class="btn btn-glow" id="setSave"><span>Save Settings</span></button></div>
  </div>
  <div class="panel glass" style="margin-top:16px">
    <h3>\ud83c\udfa8 Store Logo / Branding</h3>
    <p class="muted">Upload your OGGY PREMIUM STORE logo. It appears on the login page and the top-left of the dashboard. PNG / JPG / WEBP / SVG, up to 8 MB.</p>
    <div style="display:flex;align-items:center;gap:18px;flex-wrap:wrap">
      <div id="logoPreview" style="min-width:130px;min-height:80px;display:flex;align-items:center;justify-content:center;background:rgba(255,255,255,.05);border-radius:14px;padding:10px"></div>
      <div style="display:flex;flex-direction:column;gap:10px">
        <input type="file" id="logoFile" accept="image/png,image/jpeg,image/webp,image/gif,image/svg+xml" class="inp">
        <div class="toolbar"><button class="btn btn-glow" id="logoUpload"><span>\u2b06\ufe0f Upload logo</span></button><button class="btn btn-red" id="logoRemove"><span>Remove</span></button></div>
      </div>
    </div>
  </div>`;
  $("#setSave").onclick = async () => {
    const body = {};
    Object.keys(SETTING_LABELS).forEach(k => body[k] = val("set_" + k));
    const r = await post("/api/settings/update", body);
    if (r.ok) toast("Settings saved"); else toast("Failed", "err");
  };
  const lp = $("#logoPreview");
  const showLogo = (u) => { lp.innerHTML = u ? `<img src="${u}?v=${Date.now()}" style="max-width:170px;max-height:90px;border-radius:10px">` : '<span class="muted">No logo yet</span>'; };
  showLogo(d.logo || "");
  $("#logoUpload").onclick = async () => {
    const f = $("#logoFile").files[0];
    if (!f) return toast("Pick an image first", "err");
    const fd = new FormData(); fd.append("logo", f);
    const res = await fetch("/api/branding/logo", { method: "POST", headers: { "X-CSRF-Token": CSRF }, body: fd });
    const r = await res.json().catch(() => ({ ok: false, error: "Upload failed" }));
    if (r.ok) { toast("Logo updated \u2728"); setTimeout(() => location.reload(), 700); } else toast(r.error || "Failed", "err");
  };
  $("#logoRemove").onclick = async () => {
    if (!confirm("Remove the custom logo?")) return;
    const r = await post("/api/branding/logo/remove", {});
    if (r.ok) { toast("Logo removed"); setTimeout(() => location.reload(), 600); } else toast("Failed", "err");
  };
}

/* =====================================================  ADMINS  =========== */
async function viewAdmins() {
  const d = await api("/api/admins");
  const rows = (d.admins || []).map(a => `<tr>
    <td class="mono">${a.user_id}</td><td>${esc(a.first_name || "-")}</td>
    <td>${a.username ? "@" + esc(a.username) : "-"}</td>
    <td>${a.user_id === d.owner_id ? '<span class="badge purple">Owner</span>' : '<span class="badge info">Admin</span>'}</td>
    <td>${a.user_id === d.owner_id ? '<span class="muted">-</span>' : `<button class="btn btn-sm btn-red" onclick="removeAdmin(${a.user_id})"><span>Remove</span></button>`}</td></tr>`).join("");
  $("#content").innerHTML = `<div class="panel glass">
    <div class="toolbar">
      <input id="adm_id" class="inp" placeholder="Telegram user ID" style="max-width:240px">
      <button class="btn btn-glow" id="admAdd"><span>\u2795 Add Admin</span></button>
      <div class="grow"></div>
    </div>
    <div class="tbl-wrap"><table><thead><tr><th>User ID</th><th>Name</th><th>Username</th><th>Role</th><th></th></tr></thead><tbody>${rows}</tbody></table></div></div>`;
  $("#admAdd").onclick = async () => {
    const uid = parseInt(val("adm_id"));
    if (!uid) return toast("Enter a user ID", "err");
    const r = await post("/api/admins/add", { user_id: uid });
    if (r.ok) { toast("Admin added"); render(); } else toast("Failed", "err");
  };
}
async function removeAdmin(uid) { if (!confirm("Remove admin " + uid + "?")) return; const r = await post(`/api/admins/${uid}/remove`, {}); if (r.ok) { toast("Removed"); render(); } }

/* =====================================================  LOGS  ============= */
async function viewLogs() {
  const d = await api("/api/logs");
  const rows = (d.logs || []).map(l => `<tr>
    <td>#${l.id}</td><td class="mono">${l.admin_id}</td><td>${esc(l.action)}</td>
    <td class="muted">${esc(l.details || "")}</td><td class="muted">${fmtDate(l.created)}</td></tr>`).join("");
  $("#content").innerHTML = `<div class="panel glass">
    <div class="toolbar"><div class="grow"></div><button class="btn btn-red" onclick="clearLogs()"><span>Clear Logs</span></button></div>
    ${(d.logs || []).length ? `<div class="tbl-wrap"><table><thead><tr><th>ID</th><th>Admin</th><th>Action</th><th>Details</th><th>When</th></tr></thead><tbody>${rows}</tbody></table></div>` : empty("No logs", "\ud83e\uddfe")}</div>`;
}
async function clearLogs() { if (!confirm("Clear all admin logs?")) return; const r = await post("/api/logs/clear", {}); if (r.ok) { toast("Logs cleared"); render(); } }

/* =====================================================  BACKUPS  ========== */
async function viewBackups() {
  const d = await api("/api/backup/list");
  const rows = (d.backups || []).map(b => `<tr>
    <td class="mono">${esc(b.name)}</td><td>${(b.size / 1024).toFixed(1)} KB</td><td class="muted">${esc(b.modified)}</td>
    <td><a class="btn btn-sm btn-ghost" href="/api/backup/download/${encodeURIComponent(b.name)}">\u2b07 Download</a> <button class="btn btn-sm btn-red" onclick="deleteBackup('${b.name}')"><span>\ud83d\uddd1 Delete</span></button></td></tr>`).join("");
  $("#content").innerHTML = `<div class="panel glass">
    <div class="toolbar"><div class="grow"></div><button class="btn btn-glow" id="bkCreate"><span>\ud83d\udcbe Create Backup</span></button></div>
    ${(d.backups || []).length ? `<div class="tbl-wrap"><table><thead><tr><th>File</th><th>Size</th><th>Created</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>` : empty("No backups yet", "\ud83d\udcbe")}</div>`;
  $("#bkCreate").onclick = async () => {
    const r = await post("/api/backup/create", {});
    if (r.ok) { toast("Backup created"); render(); } else toast("Failed", "err");
  };
}
async function deleteBackup(name) {
  if (!confirm('Delete backup "' + name + '"? This cannot be undone.')) return;
  const r = await post("/api/backup/delete/" + encodeURIComponent(name), {});
  if (r.ok) { toast("Backup deleted"); render(); } else toast(r.error || "Failed", "err");
}

/* =====================================================  DEPOSITS  ========= */
let depFilter = "";
async function viewDeposits() {
  const d = await api("/api/deposits" + (depFilter ? "?status=" + depFilter : ""));
  if (!d.ok) return $("#content").innerHTML = empty("No data");
  const badge = s => `<span class="badge ${s === "paid" ? "on" : (s === "pending" ? "warn" : "off")}">${esc(s)}</span>`;
  const rows = (d.deposits || []).map(x => `<tr><td>#${x.id}</td><td>${esc(x.first_name || x.user_id)}</td><td>${money(x.amount)}</td><td>${x.unique_amount ? money(x.unique_amount) : "-"}</td><td>${badge(x.status)}</td><td class="muted">${fmtDate(x.paid_at || x.created)}</td></tr>`).join("");
  const chip = (v, l) => `<button class="btn btn-sm ${depFilter === v ? "btn-glow" : "btn-ghost"}" onclick="depositFilter('${v}')">${l}</button>`;
  $("#content").innerHTML = `
    <div class="stat-grid">
      <div class="stat-card glass c2"><div class="ic">\ud83d\udcb0</div><div class="label">Total Paid</div><div class="value">${money(d.total_paid)}</div></div>
      <div class="stat-card glass c4"><div class="ic">\u23f3</div><div class="label">Unpaid / Pending</div><div class="value">${d.pending}</div></div>
    </div>
    <div class="toolbar">${chip("", "All")}${chip("paid", "Paid")}${chip("pending", "Pending")}${chip("cancelled", "Cancelled")}<div class="grow"></div></div>
    ${rows ? `<div class="tbl-wrap"><table><thead><tr><th>#</th><th>User</th><th>Amount</th><th>Unique</th><th>Status</th><th>When</th></tr></thead><tbody>${rows}</tbody></table></div>` : empty("No deposits", "\ud83d\udcb3")}`;
}
function depositFilter(v) { depFilter = v; render(); }

/* =====================================================  RESTOCK  ========== */
async function viewRestock() {
  const d = await api("/api/restock");
  if (!d.ok) return $("#content").innerHTML = empty("No data");
  const rows = (d.restock || []).map(x => `<tr><td>${escName(x.name)}</td><td><span class="badge info">${x.requests} waiting</span></td><td><span class="badge ${x.available === 0 ? "off" : "on"}">${x.available} in stock</span></td></tr>`).join("");
  $("#content").innerHTML = `<div class="panel glass"><h3>\ud83d\udce5 Customers waiting for restock</h3><p class="muted">People who asked to be notified when a sold-out product is back. Add stock and the bot alerts them.</p>${rows ? `<div class="tbl-wrap"><table><thead><tr><th>Product</th><th>Requests</th><th>Stock</th></tr></thead><tbody>${rows}</tbody></table></div>` : empty("No pending restock requests \ud83c\udf89")}</div>`;
}

/* =====================================================  PROFIT  =========== */
async function viewProfit() {
  const d = await api("/api/profit");
  if (!d.ok) return $("#content").innerHTML = empty("No data");
  const t = d.totals;
  const rows = (d.rows || []).map(r => `<tr><td>${escName(r.name)}</td><td>${r.units}</td><td>${money(r.cost)}</td><td>${money(r.revenue)}</td><td>${money(r.cost_total)}</td><td class="${r.profit >= 0 ? "pos" : "neg"}"><b>${money(r.profit)}</b></td><td><span class="badge ${r.margin >= 0 ? "on" : "off"}">${r.margin}%</span></td></tr>`).join("");
  $("#content").innerHTML = `
    <div class="stat-grid">
      <div class="stat-card glass c2"><div class="ic">\ud83d\udcb5</div><div class="label">Revenue</div><div class="value">${money(t.revenue)}</div></div>
      <div class="stat-card glass c4"><div class="ic">\ud83e\uddfe</div><div class="label">Cost</div><div class="value">${money(t.cost)}</div></div>
      <div class="stat-card glass c1"><div class="ic">\ud83d\udcc8</div><div class="label">Profit</div><div class="value">${money(t.profit)}</div></div>
      <div class="stat-card glass c3"><div class="ic">\ud83d\udcca</div><div class="label">Margin</div><div class="value">${t.margin}%</div></div>
    </div>
    <div class="panel glass"><h3>\ud83d\udcb0 Profit by product</h3><p class="muted">Set each product's cost / buy price in the product editor to track profit.</p>${rows ? `<div class="tbl-wrap"><table><thead><tr><th>Product</th><th>Units</th><th>Cost/unit</th><th>Revenue</th><th>Total cost</th><th>Profit</th><th>Margin</th></tr></thead><tbody>${rows}</tbody></table></div>` : empty("No sales yet")}</div>`;
}

/* =====================================================  SECURITY  ========= */
async function viewSecurity() {
  const d = await api("/api/security");
  if (!d.ok) return $("#content").innerHTML = empty("No data");
  const dot = (on) => `<span class="badge ${on ? "on" : "off"}">${on ? "ON" : "OFF"}</span>`;
  const logs = (d.logs || []).map(l => `<tr><td>${esc(l.username || "-")}</td><td><span class="badge ${l.status === "success" ? "on" : "off"}">${esc(l.status)}</span></td><td class="muted">${esc(l.ip || "")}</td><td class="muted" title="${esc(l.user_agent || "")}">${esc((l.user_agent || "").slice(0, 30))}</td><td class="muted">${fmtDate(l.created)}</td></tr>`).join("");
  const sess = await api("/api/sessions");
  let accountsPanel = "";
  if (d.role === "owner") {
    const wa = await api("/api/webadmins");
    const list = (wa.admins || []);
    const arows = list.map(a => `<tr>
      <td>${esc(a.username)}${a.username === wa.me ? ' <span class="badge on">you</span>' : ''}</td>
      <td><span class="badge ${a.role === 'owner' ? 'on' : ''}">${esc(a.role)}</span></td>
      <td>${a.active ? '<span class="badge on">active</span>' : '<span class="badge off">disabled</span>'}</td>
      <td>${a.twofa_enabled ? '<span class="badge on">enabled</span>' : '<span class="badge warn">pending</span>'}</td>
      <td class="muted">${a.last_login ? fmtDate(a.last_login) : '-'}</td>
      <td>
        <button class="btn btn-sm btn-ghost" onclick="resetWebAdminPw(${a.id})"><span>Password</span></button>
        <button class="btn btn-sm btn-ghost" onclick="resetWebAdmin2fa(${a.id})"><span>Reset 2FA</span></button>
        ${a.username === wa.me ? '' : `<button class="btn btn-sm" onclick="toggleWebAdmin(${a.id})"><span>${a.active ? 'Disable' : 'Enable'}</span></button> <button class="btn btn-sm btn-red" onclick="deleteWebAdmin(${a.id})"><span>Delete</span></button>`}
      </td></tr>`).join("");
    accountsPanel = `
    <div class="panel glass" style="margin-top:16px">
      <div class="toolbar"><h3 style="margin:0">\ud83d\udc65 Web Admin Accounts</h3><div class="grow"></div>
        <button class="btn btn-glow" onclick="createWebAdmin()"><span>+ Add admin</span></button></div>
      <p class="muted">Create a separate login for each teammate. Everyone must set up their own 2FA at first login. Only owners can manage accounts.</p>
      ${list.length ? `<div class="tbl-wrap"><table><thead><tr><th>Username</th><th>Role</th><th>Status</th><th>2FA</th><th>Last login</th><th></th></tr></thead><tbody>${arows}</tbody></table></div>` : empty("No admin accounts yet")}
    </div>`;
  }
  const sessList = (sess.sessions || []);
  const sessRows = sessList.map(s => `<tr><td>${s.current ? '<span class="badge on">This device</span> ' : ''}<span class="muted mono">${esc(s.ip || "?")}</span></td><td class="muted" title="${esc(s.user_agent || "")}">${esc((s.user_agent || "").slice(0, 42)) || "-"}</td><td class="muted">${fmtDate(s.last_seen)}</td><td>${s.current ? '<span class="muted">-</span>' : `<button class="btn btn-sm btn-red" onclick="revokeSession('${esc(s.sid)}')"><span>Revoke</span></button>`}</td></tr>`).join("");
  const warn = d.force_2fa && !d.twofa_enabled
    ? `<div class="alert error" style="margin-bottom:12px">\u26a0\ufe0f 2FA is mandatory but not set up yet. Please enable it now.</div>` : "";
  $("#content").innerHTML = `
    ${warn}
    <div class="row-2">
      <div class="panel glass">
        <h3>\ud83d\udd10 Two-Factor Authentication</h3>
        <div class="kv"><span>Status</span>${dot(d.twofa_enabled)}</div>
        <div class="kv"><span>Mandatory 2FA</span>${dot(d.force_2fa)}</div>
        <div class="kv"><span>Recovery codes left</span><b>${d.recovery_remaining || 0}</b></div>
        <div class="kv"><span>Login alerts to Telegram</span>${dot(d.bot_alerts)}</div>
        <div class="kv"><span>HTTPS secure cookies</span>${dot(d.https)}</div>
        <div class="kv"><span>Data encrypted at rest</span>${dot(d.encrypted_at_rest)}</div>
        <div class="kv"><span>Encrypted backups</span>${dot(d.encrypted_backups)}</div>
        <div class="kv"><span>Brute-force lockout</span>${dot(true)}</div>
        <div class="kv"><span>CSRF protection</span>${dot(true)}</div>
        <div class="kv"><span>CSP + security headers</span>${dot(true)}</div>
        <div class="kv"><span>Rate limit (req/min)</span><b>${d.rate_limit || 0}</b></div>
        <div class="kv"><span>IP allowlist</span>${dot(d.ip_allowlist)}</div>
        <div class="kv"><span>Idle auto-logout (min)</span><b>${d.idle_minutes || 0}</b></div>
        <div class="toolbar" style="margin-top:14px"><div class="grow"></div>
          ${d.twofa_enabled
            ? `<button class="btn btn-ghost btn-sm" onclick="twofaRecovery()"><span>New recovery codes</span></button>${d.force_2fa ? "" : `<button class="btn btn-red" onclick="twofaDisable()"><span>Disable 2FA</span></button>`}`
            : `<button class="btn btn-glow" onclick="twofaSetup()"><span>\ud83d\udee1\ufe0f Enable 2FA</span></button>`}
        </div>
      </div>
      <div class="panel glass"><h3>\ud83d\udcdc Recent logins</h3>${logs ? `<div class="tbl-wrap"><table><thead><tr><th>User</th><th>Result</th><th>IP</th><th>Device</th><th>When</th></tr></thead><tbody>${logs}</tbody></table></div>` : empty("No login history yet")}</div>
    </div>
    <div class="panel glass" style="margin-top:16px">
      <div class="toolbar"><h3 style="margin:0">\ud83d\udda5\ufe0f Active sessions</h3><div class="grow"></div>${sessList.filter(s => !s.current).length ? `<button class="btn btn-red" onclick="signoutAll()"><span>Sign out everywhere else</span></button>` : ""}</div>
      <p class="muted">Devices currently signed in to this panel. Revoke anything you don't recognise.</p>
      ${sessRows ? `<div class="tbl-wrap"><table><thead><tr><th>IP</th><th>Device</th><th>Last active</th><th></th></tr></thead><tbody>${sessRows}</tbody></table></div>` : empty("Only this device is signed in", "\ud83d\udda5\ufe0f")}
    </div>
    ${accountsPanel}`;
}
async function twofaSetup() {
  const r = await post("/api/security/2fa/setup", {});
  if (!r.ok) return toast(r.error || "Failed", "err");
  const qr = "https://api.qrserver.com/v1/create-qr-code/?size=200x200&data=" + encodeURIComponent(r.otpauth);
  openModal("\ud83d\udee1\ufe0f Set up 2FA", `
    <p class="muted">1) Open Google Authenticator or Authy. 2) Scan this QR (or enter the key). 3) Type the 6-digit code to confirm.</p>
    <div style="text-align:center;margin:10px 0"><img src="${qr}" alt="QR" style="border-radius:12px;background:#fff;padding:8px"></div>
    <label class="field"><span>Manual key</span><input value="${esc(r.secret)}" readonly onclick="this.select()"></label>
    <label class="field"><span>6-digit code</span><input id="tf_code" inputmode="numeric" placeholder="123456"></label>
    <div class="toolbar" style="margin-top:14px"><div class="grow"></div>
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-glow" id="tf_enable"><span>Confirm & Enable</span></button>
    </div>`);
  $("#tf_enable").onclick = async () => {
    const rr = await post("/api/security/2fa/enable", { code: val("tf_code") });
    if (rr.ok) { toast("2FA enabled \ud83d\udd10"); showRecoveryCodes(rr.recovery_codes || []); } else toast(rr.error || "Failed", "err");
  };
}
async function twofaDisable() {
  openModal("Disable 2FA", `<p class="muted">Enter a current 6-digit code to turn off 2FA.</p>
    <label class="field"><span>6-digit code</span><input id="tf_dcode" inputmode="numeric" placeholder="123456"></label>
    <div class="toolbar" style="margin-top:14px"><div class="grow"></div>
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-red" id="tf_disable"><span>Disable</span></button></div>`);
  $("#tf_disable").onclick = async () => {
    const rr = await post("/api/security/2fa/disable", { code: val("tf_dcode") });
    if (rr.ok) { toast("2FA disabled"); closeModal(); render(); } else toast(rr.error || "Failed", "err");
  };
}
function showRecoveryCodes(codes) {
  const list = (codes || []).map(c => `<code style="display:inline-block;margin:4px 8px;font-size:15px;letter-spacing:1px">${esc(c)}</code>`).join("");
  openModal("\ud83d\udd11 Save your recovery codes", `
    <p class="muted">Store these somewhere safe. Each code works once if you ever lose your authenticator. They will NOT be shown again.</p>
    <div style="text-align:center;margin:12px 0;background:rgba(255,255,255,.04);padding:12px;border-radius:12px">${list || "<span class='muted'>None</span>"}</div>
    <div class="toolbar" style="margin-top:14px"><div class="grow"></div>
      <button class="btn btn-ghost" onclick="copyRecovery('${(codes || []).join(" ")}')">Copy</button>
      <button class="btn btn-glow" onclick="closeModal(); render();"><span>Done</span></button></div>`);
}
function copyRecovery(text) { try { navigator.clipboard.writeText(text); toast("Copied"); } catch (e) { toast("Copy failed", "err"); } }
async function twofaRecovery() {
  openModal("New recovery codes", `<p class="muted">Enter a current 6-digit code to generate a fresh set (old codes stop working).</p>
    <label class="field"><span>6-digit code</span><input id="tf_rcode" inputmode="numeric" placeholder="123456"></label>
    <div class="toolbar" style="margin-top:14px"><div class="grow"></div>
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-glow" id="tf_regen"><span>Generate</span></button></div>`);
  $("#tf_regen").onclick = async () => {
    const rr = await post("/api/security/2fa/recovery", { code: val("tf_rcode") });
    if (rr.ok) { showRecoveryCodes(rr.recovery_codes || []); } else toast(rr.error || "Failed", "err");
  };
}

async function signoutAll() {
  if (!confirm("Sign out of ALL other devices/sessions now?")) return;
  const r = await post("/api/sessions/signout-all", {});
  if (r.ok) { toast("Signed out everywhere else"); render(); } else toast(r.error || "Failed", "err");
}
async function revokeSession(sid) {
  if (!confirm("Revoke this session?")) return;
  const r = await post("/api/sessions/revoke", { sid });
  if (r.ok) { toast("Session revoked"); render(); } else toast(r.error || "Failed", "err");
}

/* =====================================================  EXPORT  =========== */
function exportCsv(kind) { window.open("/api/export/" + kind, "_blank"); }

async function createWebAdmin() {
  openModal("Add web admin", `
    <label class="field"><span>Username</span><input id="wa_user" placeholder="teammate" autocomplete="off"></label>
    <label class="field"><span>Password</span><input id="wa_pass" type="text" placeholder="at least 6 characters" autocomplete="off"></label>
    <label class="field" style="flex-direction:row;align-items:center;gap:8px"><input type="checkbox" id="wa_owner"><span>Make this an owner (can manage other admins)</span></label>
    <p class="muted">They will be required to set up their own 2FA the first time they log in.</p>
    <div class="toolbar" style="margin-top:14px"><div class="grow"></div>
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-glow" id="wa_create"><span>Create</span></button></div>`);
  $("#wa_create").onclick = async () => {
    const r = await post("/api/webadmins/create", { username: val("wa_user"), password: val("wa_pass"), role: checked("wa_owner") ? "owner" : "admin" });
    if (r.ok) { toast("Admin created"); closeModal(); render(); } else toast(r.error || "Failed", "err");
  };
}
async function resetWebAdminPw(id) {
  openModal("Reset password", `
    <label class="field"><span>New password</span><input id="wa_np" type="text" placeholder="at least 6 characters" autocomplete="off"></label>
    <div class="toolbar" style="margin-top:14px"><div class="grow"></div>
      <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
      <button class="btn btn-glow" id="wa_setpw"><span>Save</span></button></div>`);
  $("#wa_setpw").onclick = async () => {
    const r = await post(`/api/webadmins/${id}/password`, { password: val("wa_np") });
    if (r.ok) { toast("Password updated"); closeModal(); } else toast(r.error || "Failed", "err");
  };
}
async function resetWebAdmin2fa(id) {
  if (!confirm("Reset 2FA for this admin? They will set it up again at next login.")) return;
  const r = await post(`/api/webadmins/${id}/twofa/reset`, {});
  if (r.ok) { toast("2FA reset"); render(); } else toast(r.error || "Failed", "err");
}
async function toggleWebAdmin(id) {
  const r = await post(`/api/webadmins/${id}/toggle`, {});
  if (r.ok) { toast("Updated"); render(); } else toast(r.error || "Failed", "err");
}
async function deleteWebAdmin(id) {
  if (!confirm("Delete this admin account? This cannot be undone.")) return;
  const r = await post(`/api/webadmins/${id}/delete`, {});
  if (r.ok) { toast("Deleted"); render(); } else toast(r.error || "Failed", "err");
}

async function sendBroadcastNow(id) {
  if (!confirm("Send this broadcast to all users now?")) return;
  const r = await post(`/api/broadcasts/${id}/send`, {});
  if (r.ok) { toast("Broadcast sending to all users"); setTimeout(render, 1200); } else toast(r.error || "Failed", "err");
}

/* expose handlers used in inline onclick */
Object.assign(window, {
  editProduct, addProduct, toggleProduct, delProduct, flashSale, manageStock,
  delStock, manageTiers, delTier, refundOrder, resendOrder, adjustBalance,
  blockUser, userDetail, addCoupon, toggleCoupon, delCoupon, saveTemplate,
  replyTicket, closeTicket, setTicketFilter, withdrawAction, removeAdmin,
  clearLogs, exportCsv, closeModal, deleteBackup,
  depositFilter, twofaSetup, twofaDisable, twofaRecovery, showRecoveryCodes, copyRecovery, signoutAll, revokeSession,
  createWebAdmin, resetWebAdminPw, resetWebAdmin2fa, toggleWebAdmin, deleteWebAdmin, sendBroadcastNow, toggleTheme, toggleSidebar,
});

/* ----------------------------------------------------------- theme switch */
const THEMES = ["neon", "cyber", "sunset", "matrix", "candy"];
function applyTheme(t) { document.documentElement.setAttribute("data-theme", t); try { localStorage.setItem("oggy_theme", t); } catch (e) {} }
function toggleTheme() {
  const cur = document.documentElement.getAttribute("data-theme") || "neon";
  const next = THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length];
  applyTheme(next); toast("Theme: " + next, "ok");
}
function toggleSidebar() { document.body.classList.toggle("nav-open"); }
try { applyTheme(localStorage.getItem("oggy_theme") || "neon"); } catch (e) { applyTheme("neon"); }
const _themeBtn = $("#themeBtn"); if (_themeBtn) _themeBtn.onclick = toggleTheme;
const _menuBtn = $("#menuBtn"); if (_menuBtn) _menuBtn.onclick = toggleSidebar;
$$(".nav-item").forEach(b => b.addEventListener("click", () => document.body.classList.remove("nav-open")));

/* boot */
setView("dashboard");
