/* Shared shell: sidebar navigation + top status bar.
 * Each page defines `window.PAGE = "wallet"` (the active nav key) before this
 * script runs so the correct item is highlighted.
 */

const NAV = [
  { key: "wallet", href: "index.html", ico: "🔑", label: "钱包管理", group: "核心" },
  { key: "txpool", href: "txpool.html", ico: "🗂", label: "交易池与打包", group: "核心" },
  { key: "explorer", href: "explorer.html", ico: "⛓", label: "区块浏览器", group: "核心" },
  { key: "deploy", href: "deploy.html", ico: "🚀", label: "合约部署", group: "合约" },
  { key: "interact", href: "interact.html", ico: "🧪", label: "合约交互", group: "合约" },
  { key: "templates", href: "templates.html", ico: "📚", label: "合约模板库", group: "合约" },
  { key: "nodes", href: "nodes.html", ico: "🖥", label: "节点监控", group: "网络" },
  { key: "network", href: "network.html", ico: "🌐", label: "网络设置", group: "网络" },
  { key: "stats", href: "stats.html", ico: "📊", label: "统计报表", group: "系统" },
  { key: "admin", href: "admin.html", ico: "🛠", label: "管理后台", group: "系统" },
];

const PAGE_TITLES = {
  wallet: ["钱包管理", "密钥生成、余额查询与消息签名"],
  txpool: ["交易池与打包", "未确认交易、出块打包与挖矿"],
  explorer: ["区块浏览器", "链式结构、区块与交易详情"],
  deploy: ["智能合约部署", "编写并部署受限 Python 合约"],
  interact: ["合约交互", "代码编辑器、函数调用与事件"],
  templates: ["合约模板库", "预置合约模板，一键部署"],
  nodes: ["节点状态监控", "多节点状态与同步情况"],
  network: ["网络设置", "节点、端口与共识参数配置"],
  stats: ["统计报表", "出块、交易与难度可视化"],
  admin: ["管理后台", "链校验、回滚、重置与日志"],
};

function renderSidebar() {
  const aside = $("#sidebar");
  if (!aside) return;
  const active = window.PAGE || "wallet";
  let html = `<div class="brand"><span class="logo">⛓</span>
      <div>LightChain<span class="sub">轻量级区块链</span></div></div>`;
  let lastGroup = "";
  NAV.forEach((item) => {
    if (item.group !== lastGroup) {
      html += `<div class="nav-section">${item.group}</div>`;
      lastGroup = item.group;
    }
    const cls = item.key === active ? "nav-item active" : "nav-item";
    html += `<a class="${cls}" href="${item.href}">
      <span class="ico">${item.ico}</span>${item.label}</a>`;
  });
  aside.innerHTML = html;
}

async function renderTopbar() {
  const bar = $("#topbar");
  if (!bar) return;
  const active = window.PAGE || "wallet";
  const [title, sub] = PAGE_TITLES[active] || [active, ""];
  let status = null;
  try {
    status = await API.get("/api/node/status");
  } catch (e) { /* node down */ }
  const online = !!status;
  const dot = online ? (status.mining ? "dot" : "dot amber") : "dot off";
  const h = online ? `#${status.height}` : "离线";
  const txs = online ? `${status.txpool_size} tx` : "—";
  bar.innerHTML = `
    <div class="title">${title}</div>
    <div class="spacer"></div>
    <div class="status-pill"><span class="${dot}"></span>
      ${online ? "节点在线" : "节点离线"}</div>
    <div class="status-pill" title="当前区块高度">⛏ ${h}</div>
    <div class="status-pill" title="待确认交易">🗂 ${txs}</div>
    <div class="status-pill" title="连接节点">🖥 ${online ? status.peers : 0} 节点</div>
    <div class="status-pill" title="API 基址">${esc(API.base().replace(/^https?:/, ""))}</div>`;
}

function pollStatus(intervalMs) {
  renderTopbar();
  setInterval(renderTopbar, intervalMs || 5000);
}

document.addEventListener("DOMContentLoaded", () => {
  renderSidebar();
  renderTopbar();
});
