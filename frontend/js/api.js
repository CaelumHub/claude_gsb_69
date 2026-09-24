/* LightChain frontend API client + shared helpers.
 * The API base defaults to the origin that served the page (the node), but can
 * be overridden on the network-settings page (persisted in localStorage) so a
 * single UI can talk to any node in the simulated P2P network.
 */

const API = (function () {
  function base() {
    const stored = localStorage.getItem("lc_api_base");
    if (stored) return stored.replace(/\/$/, "");
    return window.location.origin;
  }

  async function request(method, path, body) {
    const url = base() + path;
    const opts = { method, headers: { "Content-Type": "application/json" } };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const resp = await fetch(url, opts);
    let data = null;
    try { data = await resp.json(); } catch (e) { /* non-JSON */ }
    if (!resp.ok) {
      const msg = (data && (data.error || data.message)) || resp.statusText;
      const err = new Error(msg);
      err.status = resp.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  return {
    base,
    get: (p) => request("GET", p),
    post: (p, b) => request("POST", p, b || {}),
    del: (p) => request("DELETE", p),
  };
})();

/* ---------- formatting helpers ---------- */
const fmt = {
  addr: (a, n) => {
    if (!a) return "—";
    n = n || 8;
    if (a.length <= n * 2 + 2) return a;
    return a.slice(0, n + 2) + "…" + a.slice(-n);
  },
  hash: (h, n) => {
    if (!h) return "—";
    n = n || 10;
    if (h.length <= n * 2) return h;
    return h.slice(0, n) + "…" + h.slice(-n);
  },
  amount: (v) => {
    v = Number(v || 0);
    return v.toLocaleString(undefined, { maximumFractionDigits: 6 });
  },
  time: (ts) => {
    if (!ts) return "—";
    return new Date(ts * 1000).toLocaleString();
  },
  ago: (ts) => {
    if (!ts) return "—";
    const s = Math.floor(Date.now() / 1000 - ts);
    if (s < 5) return "刚刚";
    if (s < 60) return s + " 秒前";
    if (s < 3600) return Math.floor(s / 60) + " 分钟前";
    if (s < 86400) return Math.floor(s / 3600) + " 小时前";
    return Math.floor(s / 86400) + " 天前";
  },
  dur: (s) => {
    if (s == null) return "—";
    if (s < 1) return Math.round(s * 1000) + " ms";
    return s.toFixed(2) + " s";
  },
  hex: (s) => "0x" + s,
  shortType: (t) => ({ transfer: "转账", deploy: "部署", call: "调用",
    coinbase: "出块" }[t] || t),
};

/* ---------- toast notifications ---------- */
function toast(msg, type = "info", ms = 3200) {
  let box = document.getElementById("toasts");
  if (!box) {
    box = document.createElement("div");
    box.id = "toasts";
    document.body.appendChild(box);
  }
  const el = document.createElement("div");
  el.className = "toast " + type;
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .3s"; }, ms - 300);
  setTimeout(() => el.remove(), ms);
}

/* ---------- tiny DOM helpers ---------- */
const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const k in attrs) {
      if (k === "class") node.className = attrs[k];
      else if (k === "text") node.textContent = attrs[k];
      else if (k === "html") node.innerHTML = attrs[k];
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), attrs[k]);
      else node.setAttribute(k, attrs[k]);
    }
  }
  (children || []).forEach((c) => {
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  });
  return node;
}

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
