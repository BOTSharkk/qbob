// 统计页: 从 app.js 动态 import(图表代码只在看的时候才拉)
import { BASE, api, esc, $, LIST_LOADING_HTML, viewSignal, relTime }
  from './app.js';

// ==================== 统计 ====================

const RANGES = [7, 14, 30];
let statsDays = 7;
let statsAppid = "";
// 默认按日均排: 只按总量的话老群永远霸榜, 看不出谁在变热
let peerSort = "daily_avg";
let statsCache = null;

export async function renderStats(main) {
  const sig = viewSignal();
  const bots = await api(`${BASE}/api/bots`, { signal: sig });
  if (sig?.aborted) return;
  main.innerHTML = `
    <div class="page-head">
      <h2>统计</h2>
      <div class="toolbar">
        <select id="st-bot">
          <option value="">全部 bot</option>
          ${bots.map((b) => `<option value="${esc(b.appid)}"${b.appid === statsAppid ? " selected" : ""}>${esc(b.name || b.appid)}</option>`).join("")}
        </select>
        <select id="st-days">
          ${RANGES.map((d) => `<option value="${d}"${d === statsDays ? " selected" : ""}>近 ${d} 天</option>`).join("")}
        </select>
      </div>
    </div>
    <div id="st-body">${LIST_LOADING_HTML}</div>`;
  $("#st-bot").addEventListener("change", (e) => {
    statsAppid = e.target.value; loadStats();
  });
  $("#st-days").addEventListener("change", (e) => {
    statsDays = Number(e.target.value); loadStats();
  });
  await loadStats();
}

async function loadStats() {
  const box = $("#st-body");
  if (!box) return;
  box.innerHTML = LIST_LOADING_HTML;
  try {
    const params = new URLSearchParams({ days: String(statsDays) });
    if (statsAppid) params.set("appid", statsAppid);
    const data = await api(`${BASE}/api/stats?${params}`, { signal: viewSignal() });
    if (!$("#st-body")) return;
    statsCache = data;
    box.innerHTML = statsHTML(data);
    bindChartHover(box);
    bindDonut(box);
    box.addEventListener("click", (e) => {
      const tab = e.target.closest("[data-sort]");
      if (!tab || !statsCache) return;
      peerSort = tab.dataset.sort;
      box.innerHTML = statsHTML(statsCache);   // 换个排法不必再请求一次
      bindChartHover(box);
      bindDonut(box);
    });
  } catch (e) {
    if (e?.aborted) return;
    box.innerHTML = `<div class="inf-msg err">加载失败: ${esc(e.message)}</div>`;
  }
}

function statsHTML(d) {
  const daily = d.daily || [];
  const totalIn = daily.reduce((n, r) => n + (r.incoming || 0), 0);
  const totalOut = daily.reduce((n, r) => n + (r.outgoing || 0), 0);
  const kinds = Object.fromEntries(
    (d.by_kind || []).map((r) => [`${r.chat_type}_${r.direction}`, r.n]));
  // 配额: 各 bot 的运行态汇总(只有在跑的才有)
  const quota = Object.entries(d.quota || {});
  const blocked = quota.flatMap(([appid, q]) =>
    (q.blocked || []).map((b) => ({ appid, ...b })));
  const proactive = quota.reduce((n, [, q]) => n + (q.proactive_24h_total || 0), 0);

  return `
    <div class="stat-cards">
      ${statCard("收到消息", totalIn, `近 ${d.days} 天`)}
      ${statCard("发出消息", totalOut, `近 ${d.days} 天`)}
      ${statCard("主动消息", proactive, "近 24 小时 · 吃配额的那部分")}
      ${statCard("群聊 / 私聊", `${(kinds.group_in || 0) + (kinds.group_out || 0)} / ${(kinds.private_in || 0) + (kinds.private_out || 0)}`, "按会话类型")}
    </div>

    ${blocked.length ? `<div class="page-hint warn-hint">
      ${blocked.length} 个会话正处于<b>配额静默</b>: ${blocked.map((b) =>
        `<code>${esc(b.peer_openid.slice(0, 8))}…</code> 至 ${esc(b.boundary)}`).join("、")}
      —— 期间不下发给插件, 到点自动恢复</div>` : ""}

    <section class="card">
      <h3 class="card-title">每日消息量</h3>
      ${dailyChart(daily)}
    </section>

    <div class="card-row">
      <section class="card">
        <h3 class="card-title">会话类型</h3>
        ${donut([
          { label: "群聊 收", value: kinds.group_in || 0 },
          { label: "群聊 发", value: kinds.group_out || 0 },
          { label: "私聊 收", value: kinds.private_in || 0 },
          { label: "私聊 发", value: kinds.private_out || 0 },
        ])}
      </section>
      <section class="card">
        <h3 class="card-title">活跃时段 <span class="muted">北京时间</span></h3>
        ${hourChart(d.hourly || [])}
      </section>
    </div>

    <section class="card">
      <h3 class="card-title">各 bot 分布</h3>
      ${barList((d.by_bot || []).map((b) => ({
        label: b.name, value: b.n,
        sub: `发出 ${b.outgoing || 0}`,
      })))}
    </section>

    <section class="card">
      <h3 class="card-title">最活跃会话
        <span class="sort-tabs">${[["daily_avg", "按日均"], ["n", "按总量"],
          ["trend", "按增幅"]].map(([k, label]) =>
          `<button type="button" class="sort-tab${peerSort === k ? " on" : ""}"
             data-sort="${k}">${label}</button>`).join("")}</span>
      </h3>
      <div class="table-wrap" style="box-shadow:none"><table>
        <thead><tr><th>会话</th><th>Bot</th><th>消息</th><th>日均</th>
          <th>环比</th><th>发出</th><th>最近</th></tr></thead>
        <tbody>${sortPeers(d.top_peers || []).slice(0, 15).map(peerRow).join("")
          || '<tr><td colspan="7" class="muted">暂无数据</td></tr>'}</tbody>
      </table></div>
      <p class="muted" style="margin:8px 0 0;font-size:12px">
        日均 = 该会话在本周期内**有消息的天数**的平均, 新会话不因进来得晚而吃亏;
        环比 = 与上一个等长周期相比</p>
    </section>`;
}

/** 增幅: 上期为 0 时算新增(排最前), 避免 0 除 */
function growth(p) {
  if (!p.prev) return p.n > 0 ? Infinity : 0;
  return (p.n - p.prev) / p.prev;
}

function sortPeers(rows) {
  const copy = [...rows];
  if (peerSort === "trend") {
    // 增幅榜里剔掉样本太小的: 3 条变 6 条也是 +100%, 但没有意义
    return copy.filter((p) => p.n >= 20)
      .sort((a, b) => growth(b) - growth(a));
  }
  return copy.sort((a, b) => (b[peerSort] || 0) - (a[peerSort] || 0));
}

function statCard(label, value, hint) {
  return `<div class="stat-card">
    <div class="stat-label">${esc(label)}</div>
    <div class="stat-value">${esc(String(value))}</div>
    <div class="stat-hint">${esc(hint)}</div>
  </div>`;
}

function peerRow(p) {
  const g = growth(p);
  const trend = !p.prev
    ? '<span class="trend new">新</span>'
    : `<span class="trend ${g > 0.05 ? "up" : g < -0.05 ? "down" : ""}">${
        g > 0 ? "+" : ""}${(g * 100).toFixed(0)}%</span>`;
  return `<tr>
    <td><span class="grp-name" title="${esc(p.name || "")}">${esc(p.name || "-")}</span>
      <span class="muted">${p.chat_type === "group" ? "群" : "私"}</span></td>
    <td>${esc(p.bot_name || "")}</td>
    <td class="mono">${p.n}</td>
    <td class="mono">${p.daily_avg ?? "-"}</td>
    <td>${trend}</td>
    <td class="mono">${p.outgoing || 0}</td>
    <td class="nowrap">${esc(relTime(p.last_ts))}</td>
  </tr>`;
}

/** 每日收发折线. 手写 SVG, viewBox + preserveAspectRatio=none 随容器缩放 */
function dailyChart(daily) {
  if (!daily.length) return '<div class="inf-msg end">暂无数据</div>';
  const W = 600, H = 160, PAD = 4;
  const peak = Math.max(1, ...daily.map((r) => Math.max(r.incoming || 0, r.outgoing || 0)));
  const step = daily.length > 1 ? (W - PAD * 2) / (daily.length - 1) : 0;
  const at = (i, v) => [
    PAD + i * step,
    H - PAD - (v / peak) * (H - PAD * 2),
  ];
  const path = (key) => daily
    .map((r, i) => `${i ? "L" : "M"}${at(i, r[key] || 0).map((n) => n.toFixed(1)).join(",")}`)
    .join("");
  // 面积: 折线首尾拉到底边闭合
  const area = (key) => `${path(key)}L${(PAD + (daily.length - 1) * step).toFixed(1)},${H - PAD}L${PAD},${H - PAD}Z`;
  const labels = daily.length <= 10 ? daily
    : daily.filter((_, i) => i % Math.ceil(daily.length / 10) === 0);
  // 数据点坐标存进 data-*, 交互时直接读
  const dots = daily.map((r, i) => {
    const [x, yi] = at(i, r.incoming || 0);
    const [, yo] = at(i, r.outgoing || 0);
    return `<circle class="ch-dot in" cx="${x.toFixed(1)}" cy="${yi.toFixed(1)}" r="3"></circle>
      <circle class="ch-dot out" cx="${x.toFixed(1)}" cy="${yo.toFixed(1)}" r="3"></circle>`;
  }).join("");
  const meta = daily.map((r, i) => ({
    x: at(i, 0)[0], day: r.day, incoming: r.incoming || 0, outgoing: r.outgoing || 0,
  }));
  return `
    <div class="chart-legend">
      <span><i class="dot-in"></i>收到</span><span><i class="dot-out"></i>发出</span>
      <span class="muted">峰值 ${peak}</span>
    </div>
    <div class="chart-box" data-chart="${esc(JSON.stringify(meta))}"
         data-w="${W}" data-h="${H}">
      <svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"
           role="img" aria-label="每日消息量">
        <path class="ch-area in" d="${area("incoming")}"></path>
        <path class="ch-area out" d="${area("outgoing")}"></path>
        <path class="ch-line in" d="${path("incoming")}"></path>
        <path class="ch-line out" d="${path("outgoing")}"></path>
        <line class="ch-cursor" x1="0" y1="0" x2="0" y2="${H}"></line>
        ${dots}
      </svg>
      <div class="chart-tip"></div>
    </div>
    <div class="chart-x">${labels.map((r) =>
      `<span>${esc(String(r.day || "").slice(5))}</span>`).join("")}</div>`;
}

/** 折线的悬停/点按: 找最近的点, 移准星并弹气泡.
 *  按比例把指针换算到 viewBox 坐标(SVG 被横向拉伸, 不取 <circle> 实际位置)。 */
function bindChartHover(root) {
  const box = root.querySelector(".chart-box");
  if (!box || box.dataset.bound) return;
  box.dataset.bound = "1";
  let meta;
  try { meta = JSON.parse(box.dataset.chart || "[]"); } catch { return; }
  if (!meta.length) return;
  const vw = Number(box.dataset.w) || 600;
  const cursor = box.querySelector(".ch-cursor");
  const tip = box.querySelector(".chart-tip");

  const show = (clientX) => {
    const rect = box.getBoundingClientRect();
    if (!rect.width) return;
    const vx = ((clientX - rect.left) / rect.width) * vw;   // 屏幕 -> viewBox
    let best = meta[0];
    for (const m of meta) {
      if (Math.abs(m.x - vx) < Math.abs(best.x - vx)) best = m;
    }
    cursor.setAttribute("x1", best.x);
    cursor.setAttribute("x2", best.x);
    box.classList.add("hot");
    tip.innerHTML = `<b>${esc(String(best.day || ""))}</b>
      <span><i class="dot-in"></i>收到 ${best.incoming}</span>
      <span><i class="dot-out"></i>发出 ${best.outgoing}</span>`;
    // 贴着点走, 但不许顶出容器(首尾两天最容易被裁掉一半)
    const px = (best.x / vw) * rect.width;
    const half = tip.offsetWidth / 2;
    tip.style.left = `${Math.max(half, Math.min(px, rect.width - half))}px`;
  };

  box.addEventListener("mousemove", (e) => show(e.clientX));
  box.addEventListener("mouseleave", () => box.classList.remove("hot"));
  // 触屏: 没有 hover, 按下与滑动都跟着走
  box.addEventListener("touchstart", (e) => show(e.touches[0].clientX),
                       { passive: true });
  box.addEventListener("touchmove", (e) => show(e.touches[0].clientX),
                       { passive: true });
}

// 配色: 复用主题变量, 暗色模式自动跟着变
const SLICE_COLORS = ["var(--accent)", "var(--ok)", "var(--warn)", "var(--err)",
                      "var(--muted)"];

/** 环形图. 用 stroke-dasharray 画弧, 不必手算 arc path */
function donut(slices) {
  const rows = slices.filter((s) => s.value > 0);
  const total = rows.reduce((n, s) => n + s.value, 0);
  if (!total) return '<div class="inf-msg end">暂无数据</div>';
  const R = 60, C = 2 * Math.PI * R;
  let offset = 0;
  const arcs = rows.map((s, i) => {
    const len = (s.value / total) * C;
    const seg = `<circle class="donut-seg" data-slice="${i}" cx="80" cy="80" r="${R}"
      stroke="${SLICE_COLORS[i % SLICE_COLORS.length]}"
      stroke-dasharray="${len.toFixed(2)} ${(C - len).toFixed(2)}"
      stroke-dashoffset="${(-offset).toFixed(2)}"><title>${esc(s.label)} ${s.value}</title></circle>`;
    offset += len;
    return seg;
  }).join("");
  // 中心默认显示总数; 悬停某项时换成该项(下面 bindDonut 直接改这两个节点)
  return `
    <div class="donut-wrap" data-donut="${esc(JSON.stringify(
      rows.map((s) => ({ label: s.label, value: s.value,
                         pct: ((s.value / total) * 100).toFixed(1) }))))}">
      <svg class="donut" viewBox="0 0 160 160" role="img" aria-label="会话类型分布">
        <g transform="rotate(-90 80 80)">${arcs}</g>
        <text class="donut-total" x="80" y="76">${total}</text>
        <text class="donut-cap" x="80" y="94">条</text>
      </svg>
      <ul class="legend">${rows.map((s, i) => `
        <li data-slice="${i}"><i style="background:${SLICE_COLORS[i % SLICE_COLORS.length]}"></i>
          ${esc(s.label)}
          <b>${((s.value / total) * 100).toFixed(1)}%</b>
          <span class="muted mono">${s.value}</span></li>`).join("")}
      </ul>
    </div>`;
}

/** 环形图的悬停: 扇区与图例(都带 data-slice)互相高亮, 中心数字换成该项 */
function bindDonut(root) {
  const wrap = root.querySelector(".donut-wrap");
  if (!wrap || wrap.dataset.bound) return;
  wrap.dataset.bound = "1";
  let slices;
  try { slices = JSON.parse(wrap.dataset.donut || "[]"); } catch { return; }
  const totalEl = wrap.querySelector(".donut-total");
  const capEl = wrap.querySelector(".donut-cap");
  const rest = { total: totalEl.textContent, cap: capEl.textContent };

  const mark = (idx) => {
    wrap.classList.toggle("focus", idx !== null);
    for (const el of wrap.querySelectorAll("[data-slice]")) {
      el.classList.toggle("on", el.dataset.slice === String(idx));
    }
    const s = idx === null ? null : slices[idx];
    totalEl.textContent = s ? `${s.pct}%` : rest.total;
    capEl.textContent = s ? `${s.label} ${s.value}` : rest.cap;
  };

  wrap.addEventListener("mouseover", (e) => {
    const hit = e.target.closest("[data-slice]");
    if (hit) mark(Number(hit.dataset.slice));
  });
  wrap.addEventListener("mouseleave", () => mark(null));
  // 触屏: 点一下高亮, 再点同一项取消
  wrap.addEventListener("click", (e) => {
    const hit = e.target.closest("[data-slice]");
    if (!hit) return;
    const idx = Number(hit.dataset.slice);
    mark(hit.classList.contains("on") ? null : idx);
  });
}

/** 24 小时活跃度: 竖条. 缺的小时补 0, 否则横轴会错位 */
function hourChart(hourly) {
  const byHour = new Map(hourly.map((r) => [Number(r.hour), r.n || 0]));
  const values = Array.from({ length: 24 }, (_, h) => byHour.get(h) || 0);
  const peak = Math.max(1, ...values);
  if (!hourly.length) return '<div class="inf-msg end">暂无数据</div>';
  return `
    <div class="hours">${values.map((v, h) => `
      <span class="hour-col" title="${h}:00 — ${v} 条">
        <span class="hour-bar" style="height:${(v / peak * 100).toFixed(1)}%"></span>
      </span>`).join("")}
    </div>
    <div class="chart-x hours-x"><span>0</span><span>6</span><span>12</span>
      <span>18</span><span>23</span></div>`;
}

/** 横向条形: 比饼图好读, 也不用算角度 */
function barList(rows) {
  if (!rows.length) return '<div class="inf-msg end">暂无数据</div>';
  const peak = Math.max(1, ...rows.map((r) => r.value));
  return `<div class="bar-list">${rows.map((r) => `
    <div class="bar-row">
      <span class="bar-label" title="${esc(r.label)}">${esc(r.label)}</span>
      <span class="bar-track"><span class="bar-fill" style="width:${(r.value / peak * 100).toFixed(1)}%"></span></span>
      <span class="bar-value mono">${r.value}</span>
      <span class="bar-sub muted">${esc(r.sub || "")}</span>
    </div>`).join("")}</div>`;
}
