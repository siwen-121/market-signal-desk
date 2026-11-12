"use strict";
const byId = id => document.getElementById(id);
const sourceNames = {demo: "示例行情", csv: "本地 CSV", alpha: "Alpha Vantage · 日线"};
const notes = {demo: "演示使用合成行情。推进一帧可观察过期拦截、有效信号和重复数据去重。",
  csv: "导入本地 CSV。程序检查时间戳并记录信号或拦截。",
  alpha: "日线监控。页面标注数据日期，刷新可能受 API 额度和网络影响。"};
const eventNames = {alert: "信号", blocked: "拦截", notice: "公告"};
let state;

function element(tag, value, className) {
  const node = document.createElement(tag);
  node.textContent = value;
  if (className) node.className = className;
  return node;
}

function message(text, error = false) {
  for (const id of ["feedback", "setup-feedback"]) {
    byId(id).textContent = text;
    byId(id).classList.toggle("error", error);
  }
}

const dateFormat = new Intl.DateTimeFormat("zh-CN", {month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", hourCycle: "h23"});
const localDate = value => dateFormat.format(new Date(value));
const percent = value => (value >= 0 ? "+" : "") + value.toFixed(2) + "%";

function chart(samples) {
  const canvas = byId("plot");
  const ctx = canvas.getContext("2d");
  const width = canvas.clientWidth;
  if (!width) return;
  const height = 220, scale = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * scale);
  canvas.height = Math.round(height * scale);
  ctx.scale(scale, scale);
  ctx.font = "12px system-ui";
  const last = samples.filter(row => row.state === "fresh").at(-1);
  const series = samples.filter(row => row.state === "fresh" && row.symbol === last?.symbol);
  byId("last-price").textContent = last ? last.value.toFixed(2) : "—";
  byId("plot-caption").textContent = last ? last.symbol + " · 最近有效输入" : "等待有效数据";
  if (series.length === 0) {
    ctx.fillStyle = "#a8b5c5"; ctx.fillText("保存数据源后刷新，或导入一份 CSV。", 15, 35);
    return;
  }
  const values = series.map(row => row.value);
  const minimum = Math.min(...values), maximum = Math.max(...values);
  const span = Math.max(maximum - minimum, maximum * 0.003);
  const top = maximum + span * 0.14, bottom = minimum - span * 0.14;
  const left = 13, right = width - 64, upper = 16, lower = height - 25;
  const y = value => lower - (value - bottom) / (top - bottom) * (lower - upper);
  const x = index => series.length === 1 ? (left + right) / 2 : left + index / (series.length - 1) * (right - left);
  for (let row = 0; row <= 4; row++) {
    const level = bottom + (top - bottom) * row / 4;
    ctx.strokeStyle = "#2e3b4c"; ctx.beginPath(); ctx.moveTo(left, y(level)); ctx.lineTo(right, y(level)); ctx.stroke();
    ctx.fillStyle = "#a8b5c5"; ctx.fillText(level.toFixed(2), right + 6, y(level) + 4);
  }
  ctx.strokeStyle = "#87dec0"; ctx.lineWidth = 2; ctx.beginPath();
  values.forEach((value, index) => index ? ctx.lineTo(x(index), y(value)) : ctx.moveTo(x(index), y(value)));
  ctx.stroke();
  ctx.fillStyle = "#87dec0"; ctx.beginPath(); ctx.arc(x(values.length - 1), y(values.at(-1)), 3.5, 0, Math.PI * 2); ctx.fill();
}

function render() {
  const {config, samples, events, counts, status} = state;
  byId("mode").textContent = sourceNames[config.source];
  byId("mode-note").textContent = notes[config.source];
  byId("run").disabled = false;
  byId("run").textContent = config.source === "demo" ? "推进一帧" : "刷新监控";
  for (const [id, kind] of [["alerts", "alert"], ["blocked", "blocked"], ["notices", "notice"]]) {
    byId(id).textContent = counts[kind] ?? 0;
  }
  byId("push-state").textContent = config.push ? "已开启" : config.has_bark_key ? "已配置" : "未配置";
  const body = byId("readings"); body.replaceChildren();
  for (const row of samples.slice(-7).reverse()) {
    const tr = document.createElement("tr");
    for (const value of [row.symbol, config.source === "alpha" ? row.ts.slice(0, 10) : localDate(row.ts), row.value.toFixed(2), percent(row.change)]) {
      tr.append(element("td", value));
    }
    tr.append(element("td", row.state === "fresh" ? "有效" : "已拦截", row.state === "fresh" ? "ok" : "bad"));
    body.append(tr);
  }
  if (body.children.length === 0) {
    const tr = document.createElement("tr"), td = element("td", "暂无输入数据");
    td.colSpan = 5; tr.append(td); body.append(tr);
  }
  const list = byId("events"); list.replaceChildren();
  for (const event of events) {
    const li = document.createElement("li");
    li.append(element("span", eventNames[event.kind], "tag " + event.kind));
    if (event.link.startsWith("https://www.federalreserve.gov/")) {
      const link = element("a", event.title);
      link.href = event.link; link.target = "_blank"; link.rel = "noopener noreferrer";
      const heading = document.createElement("strong"); heading.append(link); li.append(heading);
    } else {
      li.append(element("strong", event.title));
    }
    const delivery = event.delivery === "sent" ? " · Bark 已接收" : event.delivery === "failed" ? " · Bark 失败" : "";
    li.append(element("small", localDate(event.ts) + " · " + event.detail + delivery));
    list.append(li);
  }
  if (!list.children.length) list.append(element("li", "暂无事件。示例模式下可推进一帧。"));
  const statuses = [status[config.source], config.fed && status.fed].filter(Boolean);
  byId("source-status").textContent = statuses.map(item => localDate(item.at) + " · " + item.message).join(" / ");
  chart(samples);
}

function fields() {
  const source = byId("source").value;
  byId("alpha-fields").hidden = source !== "alpha";
  byId("csv-fields").hidden = source !== "csv";
}

function loadForm() {
  const cfg = state.config;
  for (const id of ["source", "symbol", "threshold"]) byId(id).value = cfg[id];
  for (const id of ["fed", "scheduled", "push"]) byId(id).checked = cfg[id];
  byId("alpha-key").placeholder = cfg.has_alpha_key ? "已保存，留空保留" : "填写 API Key";
  byId("bark-key").placeholder = cfg.has_bark_key ? "已保存，留空保留" : "填写设备 Key";
  fields();
}

function formData() {
  return {source: byId("source").value, symbol: byId("symbol").value, threshold: Number(byId("threshold").value),
    alpha_key: byId("alpha-key").value.trim(), bark_key: byId("bark-key").value.trim(),
    fed: byId("fed").checked, scheduled: byId("scheduled").checked, push: byId("push").checked};
}

async function getState(updateForm = false) {
  const response = await fetch("/api/state", {cache: "no-store"});
  if (!response.ok) throw new Error("面板连接失败，请从桌面入口重启。");
  state = await response.json();
  render();
  if (updateForm) loadForm();
}

async function post(path, data, button, success, reset = false) {
  if (!state) return;
  button.disabled = true;
  message("正在处理…");
  try {
    const response = await fetch("/api/" + path, {method: "POST",
      headers: {"Content-Type": "application/json", "X-Desk-Token": state.csrf}, body: JSON.stringify(data)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error);
    if (reset) { byId("alpha-key").value = ""; byId("bark-key").value = ""; }
    if (path === "stop") {
      clearInterval(refreshTimer);
      byId("mode").textContent = "本机监控已停止";
      byId("run").disabled = true;
      state = null;
    } else {
      await getState(reset);
    }
    const errors = result.errors || [];
    message(errors.length ? errors.join(" / ") : typeof success === "function" ? success(result) : success, !!errors.length);
  } catch (error) { message(error.message, true); }
  finally { button.disabled = false; }
}

byId("source").addEventListener("change", fields);
byId("settings").addEventListener("submit", event => {
  event.preventDefault();
  post("save", formData(), event.submitter, "设置已保存。", true);
});
byId("run").addEventListener("click", event => post("run", {}, event.currentTarget, "本次监控已完成。"));
byId("test").addEventListener("click", event => post("bark-test", {}, event.currentTarget, "Bark 已确认接收测试请求。请查看 iPhone。"));
byId("clear").addEventListener("click", event => {
  const conf = state?.config;
  if (conf) post("save", {...conf, clear_bark_key: true, push: false}, event.currentTarget, "Bark Key 已清除。", true);
});
byId("desktop").addEventListener("click", event => post("desktop", {}, event.currentTarget, "桌面入口已创建。"));
byId("stop").addEventListener("click", event => post("stop", {}, event.currentTarget, "已停止。可从桌面入口重新打开。"));
byId("import").addEventListener("click", async event => {
  const button = event.currentTarget, file = byId("csv").files[0];
  if (!file || file.size > 500000) { message("请选择不超过 500 KB 的 CSV 文件。", true); return; }
  try { await post("csv", {text: await file.text()}, button, result => "已检查 " + result.rows + " 行。"); }
  catch { message("文件无法读取。", true); }
});
window.addEventListener("resize", () => state && chart(state.samples));
getState(true).catch(error => message(error.message, true));
const refreshTimer = setInterval(() => getState().catch(() => {}), 30000);
