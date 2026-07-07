/* OGGY Mini App storefront */
const tg = window.Telegram ? window.Telegram.WebApp : null;
if (tg) { try { tg.ready(); tg.expand(); tg.setHeaderColor && tg.setHeaderColor("#0a0612"); tg.setBackgroundColor && tg.setBackgroundColor("#0a0612"); } catch (e) {} }

const $ = s => document.querySelector(s);
let STATE = null;

function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), 2200);
}
function haptic(type) { try { tg && tg.HapticFeedback && tg.HapticFeedback.impactOccurred(type || "light"); } catch (e) {} }
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function money(n) { return "$" + (Number(n) || 0).toFixed(2); }

async function api(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Telegram-Init-Data": (tg && tg.initData) || "" },
    body: JSON.stringify(body || {}),
  });
  return r.json();
}

function openInBot(param) {
  const u = STATE && STATE.bot_username;
  if (u) {
    const link = "https://t.me/" + u + (param ? "?start=" + encodeURIComponent(param) : "");
    if (tg && tg.openTelegramLink) { tg.openTelegramLink(link); setTimeout(() => tg.close && tg.close(), 300); }
    else window.open(link, "_blank");
  } else if (tg && tg.sendData) {
    tg.sendData(JSON.stringify({ action: param || "home" })); tg.close && tg.close();
  } else {
    toast("Open this from inside Telegram");
  }
}

function showHome() {
  $("#panel").classList.add("hidden");
  $("#home").classList.remove("hidden");
}
function openPanel(title, html) {
  $("#panelTitle").textContent = title;
  $("#panelBody").innerHTML = html;
  $("#home").classList.add("hidden");
  $("#panel").classList.remove("hidden");
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function row(lbl, val) { return `<div class="row"><span class="lbl">${lbl}</span><span class="val">${val}</span></div>`; }

const views = {
  shop() {
    const ps = (STATE.products || []);
    const body = ps.length ? ps.map(p => `
      <div class="card2">
        <h3>${esc(p.name)}</h3>
        <div class="meta">${p.available > 0 ? `<span class="badge on">${p.available} in stock</span>` : '<span class="badge off">Sold out</span>'} &middot; <span class="price">${money(p.price)}</span></div>
        ${p.description ? `<div class="note">${esc(p.description)}</div>` : ""}
        ${p.available > 0 ? `<button class="btn" data-buy="${p.id}">🛒 Buy now</button>` : `<button class="btn" data-notify="${p.id}" style="background:linear-gradient(135deg,#555,#777);box-shadow:none">🔔 Notify me</button>`}
      </div>`).join("") : `<div class="empty">No products available right now 🛍️</div>`;
    openPanel("🛍️ Shop", body);
  },
  wallet() {
    const u = STATE.user, dep = STATE.deposit_address;
    openPanel("💳 Wallet", `
      ${row("Balance", `<span class="price">${money(u.balance)}</span>`)}
      ${row("Membership", esc(u.membership))}
      ${row("Loyalty points", Math.round(u.loyalty_points || 0))}
      <button class="btn green" data-act="deposit">➕ Add funds (USDT)</button>
      ${dep ? `<div class="note">Your USDT (BEP20) deposit address:</div><div class="mono">${esc(dep)}</div>` : ""}
      <div class="note">Tap “Add funds” to start a tracked deposit in the bot.</div>`);
  },
  freebies() {
    const c = STATE.coupon;
    openPanel("🎁 Freebies", c
      ? `<div class="card2"><h3>🎟️ Active coupon</h3><div class="mono">${esc(c)}</div><div class="note">It will be applied automatically at checkout.</div></div>`
      : `<div class="empty">No active coupons yet.<br>Invite friends from “Refer &amp; Earn” to unlock rewards 🎉</div>`);
  },
  orders() {
    const os = (STATE.orders || []);
    openPanel("📦 My Orders", os.length ? os.map(o => `
      <div class="card2">
        <h3>${esc(o.product_name)}</h3>
        <div class="meta">Qty ${o.quantity || 1} &middot; <span class="price">${money(o.price)}</span> &middot; ${esc(o.created)}</div>
      </div>`).join("") : `<div class="empty">You have no orders yet 🛍️</div>`);
  },
  profile() {
    const u = STATE.user, r = STATE.referral || {};
    openPanel("👤 Profile", `
      ${row("Name", esc(u.name))}
      ${row("Membership", esc(u.membership))}
      ${row("Balance", money(u.balance))}
      ${row("Loyalty points", Math.round(u.loyalty_points || 0))}
      ${row("Referrals", r.count || 0)}
      ${row("Referral earnings", money(r.earned))}
      ${row("Joined", esc(u.joined || ""))}`);
  },
  refer() {
    const r = STATE.referral || {};
    openPanel("🤝 Refer & Earn", `
      <div class="card2"><h3>Your invite link</h3>
        <div class="mono" id="reflink">${esc(r.link || "")}</div>
        <button class="btn" id="copyRef">📋 Copy link</button>
      </div>
      ${row("Friends joined", r.count || 0)}
      ${row("Total earned", money(r.earned))}
      ${row("Available to withdraw", money(r.pending))}
      <div class="note">Share your link. When friends buy, you earn a commission automatically.</div>`);
    const cp = $("#copyRef");
    if (cp) cp.onclick = () => { copyText(r.link || ""); };
  },
  support() {
    const ts = (STATE.tickets || []);
    openPanel("🆘 Support", `
      <div class="card2"><h3>Open a ticket</h3>
        <input class="field" id="tk_subj" placeholder="Subject (e.g. Order issue)" maxlength="80">
        <textarea class="field" id="tk_msg" placeholder="Describe your issue…" maxlength="1000"></textarea>
        <button class="btn" id="tk_send">📨 Send to support</button>
      </div>
      <h3 style="margin:14px 0 6px;font-size:14px">Your recent tickets</h3>
      ${ts.length ? ts.map(t => `<div class="card2"><div class="meta">#${t.id} &middot; ${esc(t.status)}</div><b>${esc(t.subject)}</b>${t.admin_reply ? `<div class="note">💬 ${esc(t.admin_reply)}</div>` : ""}</div>`).join("") : `<div class="empty">No tickets yet.</div>`}`);
    $("#tk_send").onclick = sendTicket;
  },
};

function copyText(t) {
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(t).then(() => toast("Copied!")).catch(() => toast("Copy failed"));
  else { const i = document.createElement("textarea"); i.value = t; document.body.appendChild(i); i.select(); try { document.execCommand("copy"); toast("Copied!"); } catch (e) { toast("Copy failed"); } i.remove(); }
}

async function sendTicket() {
  const subject = ($("#tk_subj").value || "").trim();
  const message = ($("#tk_msg").value || "").trim();
  if (!subject || !message) return toast("Fill subject and message");
  $("#tk_send").disabled = true;
  const r = await api("/shop/api/ticket", { subject, message });
  $("#tk_send").disabled = false;
  if (r && r.ok) { toast("Ticket sent ✅"); haptic("medium"); load().then(() => views.support()); }
  else toast((r && r.error) || "Failed to send");
}

function bind() {
  document.querySelectorAll("[data-go]").forEach(b => b.onclick = () => { haptic(); const v = b.getAttribute("data-go"); views[v] && views[v](); });
  document.querySelectorAll("[data-act]").forEach(b => b.onclick = () => {
    const a = b.getAttribute("data-act");
    if (a === "clear") { haptic(); if (tg && tg.close) tg.close(); else openInBot("clear"); }
  });
  $("#backBtn").onclick = () => { haptic(); showHome(); };
  $("#panelBody").addEventListener("click", e => {
    const buy = e.target.closest("[data-buy]");
    if (buy) { haptic("medium"); openInBot("buy_" + buy.getAttribute("data-buy")); return; }
    const nf = e.target.closest("[data-notify]");
    if (nf) { haptic(); openInBot("notify_" + nf.getAttribute("data-notify")); return; }
    const dep = e.target.closest('[data-act="deposit"]');
    if (dep) { haptic("medium"); openInBot("deposit"); return; }
  });
}

function paint() {
  const u = STATE.user || {};
  $("#uname").textContent = u.name || "there";
  $("#ubal").textContent = money(u.balance);
  $("#urank").textContent = u.membership || "Bronze";
}

async function load() {
  const r = await api("/shop/api/state", {});
  if (!r || !r.ok) { document.querySelector(".sub").textContent = (r && r.error) || "Could not verify your Telegram session."; return null; }
  STATE = r; paint(); return r;
}

(async function boot() {
  bind();
  await load();
})();
