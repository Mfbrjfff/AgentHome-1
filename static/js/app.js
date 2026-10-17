/* ============================================================
   Liquid Glass dashboard SPA — vanilla JS, WebSocket-driven.
   Event types from the backend eventbus map to distinct glow FX.
   ============================================================ */
"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const api = (path, opts = {}) => fetch(path, { headers: { "Content-Type": "application/json" }, ...opts }).then(r => r.json());

const state = { speakReplies: true, ttsLang: "lv-LV", netHist: [], devices: [], tracked: {} };

/* ---------------- spring helper: bounce any element on click ---------------- */
function spring(el) {
  el.animate(
    [{ transform: "scale(1)" }, { transform: "scale(.9)" }, { transform: "scale(1.06)" }, { transform: "scale(1)" }],
    { duration: 420, easing: "cubic-bezier(.34,1.56,.64,1)" });
}
document.addEventListener("pointerdown", (e) => {
  const b = e.target.closest(".spec-btn, .toggle, .jobcard, .radar-node");
  if (b) spring(b);
});

/* ---------------- tabs ---------------- */
$$("nav.tabs button").forEach((btn) =>
  btn.onclick = () => {
    $$("nav.tabs button").forEach((x) => x.classList.remove("active"));
    btn.classList.add("active");
    $$(".tabpane").forEach((p) => p.classList.remove("active"));
    $("#tab-" + btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "analytics") loadAnalytics();
    if (btn.dataset.tab === "auto") loadJobs();
    if (btn.dataset.tab === "net") refreshNetTab();
  });

/* ---------------- toast ---------------- */
let toastT;
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove("show"), 3200);
}

/* ---------------- liquid glow engine ---------------- */
const GLOW_MAP = {
  hue_on: "amber", hue_off: "rose", vacuum_clean: "blue", vacuum_dock: "cyan",
  withings_sync: "green", spotify_play: "purple", spotify_pause: "rose",
  device_arrived: "cyan", device_left: "rose", unknown_device: "red",
  routine_start: "purple", routine_done: "green", skill_installed: "green",
};
let glowTimer;
function setGlow(body) {
  document.body.dataset.glow = body;
  clearTimeout(glowTimer);
  glowTimer = setTimeout(() => { document.body.dataset.glow = ""; }, 6000);
}
function ripple(color) {
  const d = document.createElement("div");
  d.className = "ripple";
  const size = Math.max(innerWidth, innerHeight) * 0.7;
  Object.assign(d.style, { width: size + "px", height: size + "px",
    left: `calc(50% - ${size / 2}px)`, top: `calc(45% - ${size / 2}px)`, borderColor: color });
  document.body.appendChild(d);
  setTimeout(() => d.remove(), 1700);
}

/* ---------------- browser TTS (window.speechSynthesis) ---------------- */
function speak(text, lang) {
  if (!state.speakReplies || !window.speechSynthesis) return;
  try {
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    u.lang = lang || state.ttsLang;
    const v = speechSynthesis.getVoices().find(v => v.lang.startsWith(u.lang.slice(0, 2)));
    if (v) u.voice = v;
    speechSynthesis.speak(u);
  } catch (e) { /* ignore */ }
}

/* ---------------- WebSocket ---------------- */
let ws;
function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => { $("#statusdot").classList.remove("off"); };
  ws.onclose = () => { $("#statusdot").classList.add("off"); setTimeout(connectWS, 2500); };
  ws.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.type === "hello") { (ev.data.events || []).forEach(renderEvent); return; }
    if (ev.type === "heartbeat") { renderSys(ev.data); return; }
    renderEvent(ev);
  };
}
function renderEvent(ev) {
  termLine(ev);
  const glow = GLOW_MAP[ev.type];
  if (glow) { setGlow(glow); }
  switch (ev.type) {
    case "hue_on": case "hue_off": ripple("#ffcf8a55"); refreshTiles(); break;
    case "vacuum_clean": ripple("#4aa8ff66"); refreshTiles(); break;
    case "withings_sync": ripple("#30d15866"); refreshTiles(); break;
    case "spotify_play": case "spotify_pause": ripple("#bf5af266"); refreshTiles(); break;
    case "device_arrived": toast(`🏠 ${ev.message}`); refreshNetTab(); break;
    case "device_left": toast(`👋 ${ev.message}`); refreshNetTab(); break;
    case "unknown_device": toast(`⚠️ ${ev.message}`); setGlow("red"); break;
    case "network_health": updateLatency(ev.data); break;
    case "schedule": toast(`📅 ${ev.message}`); loadJobs(); break;
    case "skill_installed": toast(`🧩 ${ev.message}`); break;
    case "alert": toast(ev.message); break;
    case "tts": speak(ev.data.text, ev.data.lang); break;
    case "routine_start": $("#routine-status").textContent = "darbojas…"; break;
    case "routine_done": $("#routine-status").textContent = ev.message; refreshTiles(); break;
    case "tool_result": refreshTiles(); break;
  }
}

/* ---------------- thought terminal ---------------- */
$("#fab").onclick = () => $("#terminal").classList.toggle("open");
$("#close-term").onclick = () => $("#terminal").classList.remove("open");
function termClass(ev) {
  if (ev.type === "tool_call") return "tool";
  if (ev.type === "agent_thought") return "think";
  if (ev.type === "tool_result" && ev.message.includes("ERROR")) return "err";
  if (ev.type === "tool_result") return "ok";
  if (ev.type === "system" || ev.type === "schedule") return "sys";
  return "";
}
function termLine(ev) {
  const log = $("#termlog");
  const ln = document.createElement("div");
  ln.className = "ln " + termClass(ev);
  ln.textContent = `[${ev.time}] ${ev.source}·${ev.type}: ${ev.message}`;
  log.appendChild(ln);
  while (log.children.length > 400) log.firstChild.remove();
  log.scrollTop = log.scrollHeight;
}

/* ---------------- dashboard tiles ---------------- */
async function refreshTiles() {
  try {
    const d = await api("/api/dashboard");
    // Hue
    const lights = d.hue?.lights || [];
    $("#tile-hue .sub").innerHTML = lights.map(l =>
      `<span class="pill ${l.on ? "on" : "off"}">${l.name}</span>`).join(" ") || "—";
    // Spotify
    const sp = d.spotify || {};
    $("#tile-spotify .sub").innerHTML = sp.playing
      ? `<div class="big" style="font-size:18px">▶ ${sp.track || ""}</div><div class="sub">${sp.artist || ""} · ${sp.volume}% @ ${sp.device || ""}</div>`
      : '<span class="pill off"> pauzē </span>';
    // Vacuum
    const v = d.vacuum || {};
    $("#tile-vacuum .sub").innerHTML =
      `<div class="big">${v.battery ?? "—"}%</div><div class="sub">${v.state || "—"} · tvertne ${v.dustbin_pct ?? "?"}%</div>`;
    // Withings
    const w = d.withings?.latest;
    $("#tile-withings .sub").innerHTML = w
      ? `<div class="big">${w.weight} kg</div><div class="sub">ĶMI ${w.bmi} · tauki ${w.fat}% · ${w.hr} bpm</div>`
      : "nav datu — sinhronizē ar aģentu";
    // Weather
    const wx = d.weather?.current || {};
    $("#tile-weather .sub").innerHTML =
      `<div class="big">${wx.temp_c ?? "—"}°C</div><div class="sub">${wx.condition_lv || wx.condition || ""} · ${wx.humidity_pct ?? ""}% mitrums</div>`;
    // Network tile
    const devs = d.network?.devices || [];
    state.devices = devs; state.tracked = d.network?.tracked || {};
    renderNetworkTile(devs);
    // Notes
    const notes = (d.notes?.notes || []).slice(0, 5);
    $("#tile-notes .sub").innerHTML = notes.map(n =>
      `<div>• <b>${esc(n.title)}</b>${n.remind_at ? ` ⏰ ${new Date(n.remind_at * 1000).toLocaleTimeString("lv", { hour: "2-digit", minute: "2-digit" })}` : ""}</div>`).join("") || "—";
    renderSys(d.system);
  } catch (e) { console.warn(e); }
}
function renderNetworkTile(devs) {
  const home = Object.values(state.tracked).filter(t => t.home).length;
  $("#tile-network .sub").innerHTML =
    `<div class="big">${devs.length}</div><div class="sub">ierīces tīklā · klāt: ${home ? "🟢 mājās" : "🔴 prom"}</div>` +
    Object.entries(state.tracked).map(([k, t]) =>
      `<div><span class="presence-dot ${t.home ? "presence-home" : "presence-away"}"></span>${k}</div>`).join("");
}
function esc(s) { const d = document.createElement("div"); d.textContent = s || ""; return d.innerHTML; }

/* ---------------- system rings ---------------- */
function renderSys(s) {
  if (!s) return;
  ring("#ring-cpu", s.cpu_pct, `${Math.round(s.cpu_pct)}%`);
  ring("#ring-ram", s.ram_pct, `${Math.round(s.ram_pct)}%`);
  ring("#ring-temp", ((s.temp_c || 40) / 85) * 100, s.temp_c ? `${s.temp_c}°` : "—");
  $("#sys-ip").textContent = s.lan_ip || "";
  $("#sys-sub").textContent = `diskā ${s.disk_free_gb} GB brīvs (${s.disk_used_pct}% liots) · uptime ${Math.floor(s.uptime_s / 60)} min`;
  $("#sys-demo").textContent = `DEMO_MODE=${s.demo_mode} · modelis bonsai-2:4b`;
}
function ring(sel, pct, label) {
  const el = $(sel); el.style.setProperty("--p", Math.min(100, Math.max(0, pct)));
  el.querySelector("span").textContent = label;
}

/* ---------------- chat ---------------- */
$("#chatform").onsubmit = async (e) => {
  e.preventDefault();
  const text = $("#chatinput").value.trim();
  if (!text) return;
  $("#chatinput").value = "";
  addMsg("user", text);
  const bubble = addMsg("bot", "…domāju");
  const thinking = setInterval(() => { bubble.textContent += " ·"; }, 900);
  try {
    const r = await api("/api/chat", { method: "POST", body: JSON.stringify({ text, speak: false }) });
    clearInterval(thinking);
    bubble.textContent = r.reply || "(tukša atbilde)";
    const tools = (r.tools || []).map(t => `${t.tool}${t.ok ? " ✓" : " ✗"}`).join(" · ");
    if (tools) {
      const div = document.createElement("div");
      div.className = "tools"; div.textContent = `rīki: ${tools} (${r.ms}ms)`;
      bubble.parentElement.appendChild(div);
    }
    if (state.speakReplies) speak(r.reply);
  } catch (err) {
    clearInterval(thinking); bubble.textContent = "kļūda: " + err;
  }
};
function addMsg(role, text) {
  const m = document.createElement("div");
  m.className = "msg " + (role === "user" ? "user" : "bot");
  m.textContent = text;
  $("#chatlog").appendChild(m);
  $("#chatlog").scrollTop = 1e9;
  return m;
}
$("#tts-toggle").onclick = () => {
  state.speakReplies = !state.speakReplies;
  $("#tts-toggle").classList.toggle("on", state.speakReplies);
};
$("#tts-lang").onchange = async (e) => {
  state.ttsLang = e.target.value;
  await api("/api/settings/tts", { method: "POST", body: JSON.stringify({ language: state.ttsLang }) });
};

/* ---------------- automation manager (drag-sort) ---------------- */
async function loadJobs() {
  const { jobs } = await api("/api/jobs");
  jobs.sort((a, b) => (a.spec.ui_order ?? 99) - (b.spec.ui_order ?? 99));
  const list = $("#joblist"); list.innerHTML = "";
  for (const j of jobs) {
    const card = document.createElement("div");
    card.className = "glass jobcard"; card.draggable = true; card.dataset.id = j.job_id;
    card.innerHTML = `
      <span class="grip">⠿</span>
      <div class="meta"><b>${esc(j.name)}</b>
        <small>${j.kind} · ${JSON.stringify(j.spec).slice(0, 60)} → ${esc(j.payload.kind || "")}:${esc(j.payload.name || j.payload.tool || j.payload.text || "")}</small><br>
        <small>${j.next_run_iso ? "nākamreiz: " + new Date(j.next_run_iso).toLocaleString("lv") : "izslēgts"}</small></div>
      <span class="pill ${j.created_by === "agent" ? "badge-agent" : "badge-user"}">${j.created_by}</span>
      <span class="toggle ${j.enabled ? "on" : ""}"></span>
      <button class="spec-btn del" style="padding:6px 10px">✕</button>`;
    card.querySelector(".toggle").onclick = async () => {
      await api(`/api/jobs/${j.job_id}/toggle`, { method: "POST", body: JSON.stringify({ enabled: !j.enabled }) });
      loadJobs();
    };
    card.querySelector(".del").onclick = async () => { await api(`/api/jobs/${j.job_id}`, { method: "DELETE" }); loadJobs(); };
    list.appendChild(card);
  }
  wireDrag(list);
}
function wireDrag(list) {
  let dragEl = null;
  list.querySelectorAll(".jobcard").forEach(c => {
    c.ondragstart = () => { dragEl = c; c.classList.add("dragging"); };
    c.ondragend = async () => {
      c.classList.remove("dragging");
      const order = [...list.querySelectorAll(".jobcard")].map(x => x.dataset.id);
      await api("/api/jobs/reorder", { method: "POST", body: JSON.stringify({ order }) });
    };
    c.ondragover = (e) => {
      e.preventDefault();
      const box = c.getBoundingClientRect();
      const after = e.clientY > box.top + box.height / 2;
      if (dragEl !== c) list.insertBefore(dragEl, after ? c.nextSibling : c);
    };
  });
}
$("#addjob").onclick = () => {
  const name = prompt("Uzdevuma nosaukums:", "Sienas pulkstenis");
  if (!name) return;
  const kind = prompt("veids: interval | cron | date", "interval");
  let spec = "{}";
  if (kind === "interval") spec = JSON.stringify({ seconds: parseInt(prompt("sekundes:", "3600") || "3600") });
  if (kind === "cron") spec = JSON.stringify({ hour: prompt("stunda (0-23 vai mon-fri fields):", "8"), minute: prompt("minūte:", "0") });
  if (kind === "date") spec = JSON.stringify({ run_at: prompt("ISO laiks:", new Date(Date.now() + 3600e3).toISOString()) });
  const toolName = prompt("rīks (tts_speak / vacuum_start / hue_scene …):", "tts_speak");
  const args = prompt("args JSON:", '{"text":"pārbaude"}');
  api("/api/jobs", { method: "POST", body: JSON.stringify({
    name, kind, spec: JSON.parse(spec),
    payload: { kind: "tool", tool: toolName, args: JSON.parse(args || "{}") } }) })
    .then(loadJobs);
};

/* presence rule builder */
const RULES_KEY = "atlas_presence_rules";
function loadRulesUI() {
  const rules = JSON.parse(localStorage.getItem(RULES_KEY) || "[]");
  $("#presence-rules").innerHTML = rules.map((r, i) =>
    `<div class="glass" style="padding:10px 14px;display:flex;gap:10px;align-items:center">
       <span>Ja <b>${esc(r.device)}</b> ${r.event === "left" ? "atstāj" : "pieslēdzas"} Wi-Fi → ${esc(r.action)}</span>
       <button class="spec-btn" style="margin-left:auto;padding:4px 10px" onclick="delRule(${i})">✕</button></div>`).join("")
    || '<span class="sub">Nav personisku noteikumu — noklusējie: visprom → tīrīšana, kāds mājās → sveiciens.</span>';
}
window.delRule = (i) => { const r = JSON.parse(localStorage.getItem(RULES_KEY)); r.splice(i, 1); localStorage.setItem(RULES_KEY, JSON.stringify(r)); loadRulesUI(); };
$("#addrule").onclick = () => {
  const device = prompt("Ierīce (sk. radar):", "andris-phone"); if (!device) return;
  const event = confirm("Atstāj tīklu? (OK=atstāj, Cancel=ieradās)") ? "left" : "arrived";
  const action = prompt("Rīcība:", "hue_set all off + spotify_pause + vacuum standby");
  const rules = JSON.parse(localStorage.getItem(RULES_KEY) || "[]");
  rules.push({ device, event, action });
  localStorage.setItem(RULES_KEY, JSON.stringify(rules)); loadRulesUI();
  toast("Noteikums saglabāts lokāli; aģentam vari to pateikt čatā — tas ierakstīs schedulerī.");
};
loadRulesUI();

/* client-side reactive rules also fire over WS */
const origRender = renderEvent;
renderEvent = function (ev) {
  origRender(ev);
  if (ev.type === "device_left" || ev.type === "device_arrived") {
    const want = ev.type === "device_left" ? "left" : "arrived";
    const dev = ev.data?.device;
    JSON.parse(localStorage.getItem(RULES_KEY) || "[]")
      .filter(r => r.device === dev && r.event === want)
      .forEach(r => toast(`⚙️ likums: ${dev} ${want} → ${r.action}`));
  }
};

/* ---------------- network tab + radar ---------------- */
async function refreshNetTab() {
  const d = await api("/api/tools/network_devices/call", { method: "POST", body: "{}" });
  const data = d.data || {};
  state.devices = data.devices || []; state.tracked = data.tracked || {};
  $("#net-ssid").textContent = data.ssid || "—";
  $("#net-count").textContent = `${state.devices.length} ierīces · pēdējā skene: ${data.last_scan ? new Date(data.last_scan * 1000).toLocaleTimeString("lv") : "—"}`;
  $("#tracked-list").innerHTML = Object.entries(state.tracked).map(([k, t]) =>
    `<div><span class="presence-dot ${t.home ? "presence-home" : "presence-away"}"></span>
     <b>${k}</b> — ${t.home ? "MĀJĀS 🟢" : "PROM 🔴"} ${t.rssi != null ? `(${t.rssi} dBm)` : ""}</div>`).join("") || "—";
  renderNetworkTile(state.devices);
}
$("#rescan").onclick = async () => {
  toast("Skenēju tīklu…");
  await api("/api/tools/network_scan/call", { method: "POST", body: "{}" });
  refreshNetTab();
};
function updateLatency(data) {
  $("#lat-ext").textContent = (data.external_ms ?? "—") + " ms";
  $("#lat-gw").textContent = (data.gateway_ms ?? "—") + " ms";
  state.netHist.push(data.external_ms || 0);
  if (state.netHist.length > 60) state.netHist.shift();
  sparkline("#spark-lat", state.netHist);
}
function sparkline(sel, vals) {
  if (!vals.length) return;
  const max = Math.max(...vals, 1), svg = $(sel);
  const pts = vals.map((v, i) => `${(i / (vals.length - 1 || 1)) * 300},${34 - (v / max) * 30}`).join(" ");
  svg.innerHTML = `<path d="M${pts}"/>`;
}
async function refreshHistorySpark() {
  const a = await api("/api/analytics");
  state.netHist = (a.network || []).map(r => r.external_ms || 0).slice(-60);
  if (state.netHist.length) updateLatency({ external_ms: state.netHist.at(-1), gateway_ms: a.network.at(-1)?.gateway_ms });
}

/* radar modal */
$("#openradar").onclick = () => { $("#radar-modal").classList.add("open"); drawRadar(); };
$("#close-radar").onclick = () => $("#radar-modal").classList.remove("open");
function drawRadar() {
  const r = $("#radar"); r.innerHTML = "";
  const cx = 50, cy = 50;
  r.insertAdjacentHTML("beforeend",
    `<div style="position:absolute;left:${cx}%;top:${cy}%;transform:translate(-50%,-50%);font-size:22px">📡</div>` +
    [30, 60, 90].map(s => `<div style="position:absolute;left:${cx}%;top:${cy}%;width:${s * 3}px;height:${s * 3}px;border:1px solid rgba(255,255,255,.1);border-radius:50%;transform:translate(-50%,-50%)"></div>`).join(""));
  state.devices.forEach((d, i) => {
    const angle = (i / state.devices.length) * Math.PI * 2;
    const dist = d.rssi != null ? Math.min(42, Math.max(8, (-d.rssi - 40))) : 40;
    const x = cx + Math.cos(angle) * dist, y = cy + Math.sin(angle) * dist;
    const node = document.createElement("div");
    node.className = "radar-node" + (d.online ? "" : " away");
    node.style.left = x + "%"; node.style.top = y + "%";
    node.innerHTML = `${iconFor(d.kind)} ${esc(d.alias || d.name || d.ip)}`;
    node.onclick = () => radarDetail(d);
    r.appendChild(node);
  });
}
function iconFor(k) { return { phone: "📱", laptop: "💻", tv: "📺", speaker: "🔊", iot: "🤖", server: "🖥", unknown: "❓" }[k] || "•"; }
function radarDetail(d) {
  const alias = prompt("Ailiass:", d.alias || d.name || "") || d.name;
  const room = prompt("Istabas piešķiršana:", d.room || "Living Room") || "";
  d.alias = alias; d.room = room;
  $("#radar-detail").innerHTML =
    `<b>${esc(alias)}</b> · ${esc(d.ip)} · ${esc(d.mac)} · ${esc(d.vendor || "")} · istaba: ${esc(room)} ·
     RSSI <b>${d.rssi ?? "?"} dBm</b> · ${d.band_ghz ?? "?"} GHz · ${d.rx_rate ?? "?"} Mbps
     <button class="spec-btn" style="padding:4px 10px;margin-left:8px" id="blockdev">🚫 Bloķēt</button>`;
  $("#blockdev").onclick = async () => {
    await api("/api/tools/network_block_device/call", { method: "POST", body: JSON.stringify({ mac: d.mac, reason: "manual UI block" }) });
    toast("Ierīce marķēta/bloķēta");
  };
  drawRadar();
}

/* ---------------- analytics ---------------- */
async function loadAnalytics() {
  const a = await api("/api/analytics");
  lineChart("#chart-weight", (a.health || []).map(p => p.weight), "#5fb2ff");
  const en = a.energy || {};
  $("#energy-box").textContent = en.watts_now
    ? `Šobrīd ~${en.watts_now} W · diennaktī ≈ ${en.kwh_estimate} kWh (${JSON.stringify(en.breakdown_w)})` : "—";
  lineChart("#chart-energy", (a.network || []).map(r => r.devices), "#30d158");
  refreshHistorySpark();
}
function lineChart(sel, values, color) {
  const svg = $(sel); const W = 600, H = 150, P = 10;
  values = (values || []).filter(v => v != null);
  if (values.length < 2) { svg.innerHTML = `<text x="10" y="30" fill="#888">nav datu</text>`; return; }
  const min = Math.min(...values), max = Math.max(...values), rng = max - min || 1;
  const pts = values.map((v, i) => [P + (i / (values.length - 1)) * (W - 2 * P), H - P - ((v - min) / rng) * (H - 2 * P)]);
  const path = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
  svg.innerHTML = `
    <defs><linearGradient id="grad" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="${color}" stop-opacity=".35"/><stop offset="100%" stop-color="${color}" stop-opacity="0"/></linearGradient></defs>
    <path class="area" d="${path} L ${W - P} ${H - P} L ${P} ${H - P} Z"/>
    <path class="line" d="${path}" stroke="${color}"/>`;
}

/* ---------------- settings panel actions ---------------- */
$("#save-tts").onclick = async () => {
  const lang = $("#set-tts-lang").value;
  const r = await api("/api/settings/tts", { method: "POST", body: JSON.stringify({ language: lang }) });
  state.ttsLang = lang; $("#tts-lang").value = lang;
  $("#tts-engine-info").textContent = `dzinējs: ${r.engine} · valoda: ${r.language}`;
  toast("TTS iestatīts: " + r.language);
};
$("#test-tts").onclick = async () => {
  await api("/api/tools/tts_speak/call", { method: "POST", body: JSON.stringify({ text: "Sveiki! Es esmu Atlas — sistēma darbojas." }) });
};
$("#bt-scan").onclick = async () => {
  $("#bt-list").textContent = "skenēju…";
  const r = await api("/api/tools/bluetooth_scan/call", { method: "POST", body: JSON.stringify({ seconds: 4 }) });
  $("#bt-list").innerHTML = (r.data.devices || []).map(d =>
    `<div>${iconFor(d.paired ? "iot" : "phone")} <b>${esc(d.name)}</b> <small>${d.mac}</small>
     ${d.connected ? '<span class="pill on">pievienots</span>' :
      `<button class="spec-btn" style="padding:2px 8px;font-size:11px" onclick="btConnect('${d.mac}')">Pievienot</button>`}</div>`).join("");
};
window.btConnect = async (mac) => {
  await api("/api/tools/bluetooth_connect/call", { method: "POST", body: JSON.stringify({ mac }) });
  toast("BT pievienots: " + mac);
};
$("#skill-install").onclick = async () => {
  const url = $("#skill-url").value.trim(); if (!url) return;
  $("#skill-status").textContent = "instalēju…";
  const r = await api("/api/skills/install", { method: "POST", body: JSON.stringify({ url }) });
  $("#skill-status").textContent = r.ok ? `✓ instalēts; jauni rīki: ${(r.new_tools || []).join(", ") || "—"}` : `✗ ${r.error}`;
  if (r.ok) toast("Skill hot-loaded bez restarta 🎉");
};
$("#btn-update").onclick = async () => sysOp("system_update", { check_only: true }, "apt update…");
$("#btn-upgrade").onclick = async () => sysOp("system_update", { check_only: false }, "apt upgrade…");
$("#btn-reboot").onclick = async () => { if (confirm("Restartēt Raspberry Pi?")) sysOp("system_reboot", { confirm: true }, "restarts…"); };
async function sysOp(tool, args, label) {
  toast(label);
  const r = await api(`/api/tools/${tool}/call`, { method: "POST", body: JSON.stringify(args) });
  $("#sys-health").textContent = JSON.stringify(r.data).slice(0, 300);
}

/* ---------------- routines buttons ---------------- */
$$(".routine-btn").forEach(b => b.onclick = async () => {
  $("#routine-status").textContent = "palaidu…" + b.dataset.r;
  const r = await api(`/api/routines/${b.dataset.r}/run`, { method: "POST" });
  $("#routine-status").textContent = r.ok ? `✓ ${b.dataset.r} (${r.ms}ms)` : "✗ " + (r.error || "");
});

/* ---------------- boot ---------------- */
(async function init() {
  connectWS();
  await refreshTiles();
  await loadJobs();
  await refreshNetTab();
  const st = await api("/api/settings");
  state.ttsLang = st.tts_language; $("#tts-lang").value = st.tts_language;
  $("#set-tts-lang").value = st.tts_language;
  $("#tts-engine-info").textContent = `dzinējs: ${st.tts_engine}`;
  const h = await api("/api/health");
  $("#sys-health").textContent = `status: ${h.status} · model: ${h.model} · tools: ${h.tools} · scheduler: ${h.scheduler_running}`;
  setInterval(refreshTiles, 20000);
})();
