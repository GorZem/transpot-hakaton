// Эмулятор: живая карта участка и панель объекта.
const $ = s => document.querySelector(s);
const api = async (url, opts = {}) => {
  const r = await fetch(url, {headers: {"Content-Type": "application/json"}, ...opts});
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(body.detail?.message || body.detail || r.statusText), {body});
  return body;
};

const STATE_RU = {red: "красный", red_yellow: "красный + жёлтый", green: "зелёный", green_blink: "зелёный мигает",
  yellow: "жёлтый", flash_yellow: "жёлтый мигающий", off: "выключен"};
const MODE_RU = {local: "локальная программа", remote: "управляет система", flash: "жёлтый мигающий"};
const KIND_RU = {crossing: "переход вне перекрёстка", cross: "крестовой перекрёсток", tee: "Т-образный перекрёсток"};
const FAULT_RU = [[null, "Исправна"], ["black", "Чёрный кадр"], ["freeze", "Зависание"], ["offline", "Нет потока"], ["noise", "Помехи"], ["fog", "Туман"], ["covered", "Закрыт объектив"]];
const LAMP = {red: "#ff5a4f", red_yellow: "#ff9a3c", green: "#3ccf7d", green_blink: "#3ccf7d", yellow: "#f2b705",
  flash_yellow: "#f2b705", off: "#444"};

let MAP = null, OBJECTS = [], selected = null;
let prev = null, cur = null, curAt = 0;
const view = {cx: 0, cy: 0, scale: 0.6};
const canvas = $("#map"), ctx = canvas.getContext("2d");
let staticLayer = null, staticKey = "";

// ------------------------------------------------------------------ геометрия
function offset(pts, d) {
  const out = [];
  for (let i = 0; i < pts.length; i++) {
    const a = pts[Math.max(0, i - 1)], b = pts[Math.min(pts.length - 1, i + 1)];
    let dx = b[0] - a[0], dy = b[1] - a[1];
    const L = Math.hypot(dx, dy) || 1; dx /= L; dy /= L;
    out.push([pts[i][0] + dy * d, pts[i][1] - dx * d]);
  }
  return out;
}
const W2S = (x, y) => [(x - view.cx) * view.scale + canvas.width / 2, canvas.height / 2 - (y - view.cy) * view.scale];
const S2W = (sx, sy) => [(sx - canvas.width / 2) / view.scale + view.cx, (canvas.height / 2 - sy) / view.scale + view.cy];

// ------------------------------------------------------------------ отрисовка
function drawStatic() {
  const key = [canvas.width, canvas.height, view.cx.toFixed(2), view.cy.toFixed(2), view.scale.toFixed(4)].join();
  if (key === staticKey && staticLayer) return;
  staticKey = key;
  staticLayer = staticLayer || document.createElement("canvas");
  staticLayer.width = canvas.width; staticLayer.height = canvas.height;
  const c = staticLayer.getContext("2d");
  c.fillStyle = "#1a2124"; c.fillRect(0, 0, canvas.width, canvas.height);
  const P = (pts, close = true) => { c.beginPath(); pts.forEach((p, i) => { const [x, y] = W2S(p[0], p[1]); i ? c.lineTo(x, y) : c.moveTo(x, y); }); if (close) c.closePath(); };
  c.fillStyle = "#2b3337";
  for (const b of MAP.buildings) { P(b.pts); c.fill(); }
  c.fillStyle = "#57606a";
  for (const r of MAP.roads) { P(offset(r.pts, r.right + 4).concat(offset(r.pts, -r.left - 4).reverse())); c.fill(); }
  for (const j of MAP.junctions) { const [x, y] = W2S(j.x, j.y); c.beginPath(); c.arc(x, y, (j.r + 4) * view.scale, 0, 7); c.fill(); }
  c.fillStyle = "#383f45";
  for (const r of MAP.roads) { P(offset(r.pts, r.right).concat(offset(r.pts, -r.left).reverse())); c.fill(); }
  for (const j of MAP.junctions) { const [x, y] = W2S(j.x, j.y); c.beginPath(); c.arc(x, y, j.r * view.scale, 0, 7); c.fill(); }
  c.strokeStyle = "rgba(230,230,230,.55)"; c.lineWidth = Math.max(1, 0.15 * view.scale);
  for (const r of MAP.roads) if (r.left && r.right) { P(r.pts, false); c.stroke(); }
  c.lineWidth = Math.max(2, 3.6 * view.scale);
  c.setLineDash([Math.max(1, 0.5 * view.scale), Math.max(1, 0.5 * view.scale)]);
  c.strokeStyle = "rgba(240,240,235,.8)";
  for (const w of MAP.crosswalks) { P([w.a, w.b], false); c.stroke(); }
  c.setLineDash([]);
  if (view.scale > 0.9) {
    c.fillStyle = "rgba(160,170,178,.8)"; c.font = "12px Segoe UI, sans-serif";
    const seen = new Set();
    for (const r of MAP.roads) {
      if (seen.has(r.name) || r.pts.length < 2) continue; seen.add(r.name);
      const m = r.pts[Math.floor(r.pts.length / 2)], [x, y] = W2S(m[0], m[1]);
      c.fillText(r.name, x + 8, y - 8);
    }
  }
}

function lerpAgents(a, b, t) {
  if (!a) return b;
  const map = new Map(a.map(v => [v[0], v]));
  return b.map(v => {
    const o = map.get(v[0]);
    if (!o) return v;
    const r = v.slice();
    r[1] = o[1] + (v[1] - o[1]) * t; r[2] = o[2] + (v[2] - o[2]) * t;
    return r;
  });
}

function draw() {
  requestAnimationFrame(draw);
  if (!MAP) return;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth * dpr, h = canvas.clientHeight * dpr;
  if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  drawStatic();
  ctx.drawImage(staticLayer, 0, 0);
  if (!cur) return;
  const t = Math.min(1, (performance.now() - curAt) / 200);
  const cars = lerpAgents(prev?.cars, cur.cars, t), peds = lerpAgents(prev?.peds, cur.peds, t);
  const s = view.scale;

  // зоны обзора камер выбранного объекта
  for (const cam of MAP.cameras) {
    const sel = cam.object_id === selected;
    if (!sel && s < 1.2) continue;
    const [x, y] = W2S(cam.x, cam.y), R = 45 * s;
    const az = (90 - cam.azimuth) * Math.PI / 180, half = cam.hfov / 2 * Math.PI / 180;
    ctx.beginPath(); ctx.moveTo(x, y);
    ctx.arc(x, y, R, -az - half, -az + half); ctx.closePath();
    ctx.fillStyle = sel ? "rgba(90,162,255,.22)" : "rgba(90,162,255,.08)"; ctx.fill();
    ctx.fillStyle = "#5aa2ff"; ctx.beginPath(); ctx.arc(x, y, Math.max(3, 0.8 * s), 0, 7); ctx.fill();
  }
  // машины
  const col = ["#d9dde2", "#f2b705", "#c9a36a", "#ff5a4f"];
  for (const c of cars) {
    const [x, y] = W2S(c[1], c[2]);
    const L = [4.5, 12, 7, 5.6][c[4]] * s, W = [1.8, 2.5, 2.4, 2.1][c[4]] * s;
    ctx.save(); ctx.translate(x, y); ctx.rotate(-c[3]);
    ctx.fillStyle = col[c[4]]; ctx.fillRect(-L / 2, -W / 2, L, W);
    ctx.fillStyle = "rgba(0,0,0,.35)"; ctx.fillRect(L * 0.05, -W / 2, L * 0.2, W);
    ctx.restore();
  }
  // пешеходы
  for (const p of peds) {
    const [x, y] = W2S(p[1], p[2]);
    ctx.fillStyle = p[4] ? "#ff5a4f" : p[3] === 1 ? "#f2b705" : p[3] === 2 ? "#ffffff" : "#9aa6ad";
    ctx.beginPath(); ctx.arc(x, y, Math.max(2, 0.45 * s), 0, 7); ctx.fill();
  }
  // светофорные объекты
  for (const sig of MAP.signals) {
    const st = cur.signals[sig.node_id]; if (!st) continue;
    const [x, y] = W2S(sig.x, sig.y);
    const g = st.groups, vehA = g.veh || g.veh_A, vehB = g.veh_B;
    const r = sig.object_id ? Math.max(7, 2.2 * s) : Math.max(4, 1.3 * s);
    ctx.lineWidth = sig.object_id ? 3 : 2;
    if (sig.object_id === selected) { ctx.strokeStyle = "#f2b705"; ctx.beginPath(); ctx.arc(x, y, r + 7, 0, 7); ctx.stroke(); }
    ctx.fillStyle = LAMP[vehA] || "#444"; ctx.beginPath(); ctx.arc(x, y, r, Math.PI / 2, Math.PI * 1.5); ctx.fill();
    ctx.fillStyle = LAMP[vehB || (g.ped === "green" || g.ped === "green_blink" ? "green" : "red")] || "#444";
    ctx.beginPath(); ctx.arc(x, y, r, -Math.PI / 2, Math.PI / 2); ctx.fill();
    ctx.strokeStyle = st.mode === "remote" ? "#5aa2ff" : st.mode === "flash" ? "#f2b705" : "rgba(0,0,0,.6)";
    ctx.beginPath(); ctx.arc(x, y, r, 0, 7); ctx.stroke();
    if (sig.object_id && s > 0.45) {
      const o = OBJECTS.find(o => o.id === sig.object_id);
      ctx.fillStyle = "#e3e8ea"; ctx.font = "600 12px Segoe UI, sans-serif";
      ctx.fillText(o ? o.title : sig.object_id, x + r + 6, y + 4);
    }
  }
}

// ------------------------------------------------------------------ управление картой
let drag = null;
canvas.addEventListener("mousedown", e => { drag = {x: e.clientX, y: e.clientY, cx: view.cx, cy: view.cy, moved: false}; canvas.classList.add("drag"); });
window.addEventListener("mouseup", e => {
  canvas.classList.remove("drag");
  if (drag && !drag.moved) pick(e);
  drag = null;
});
window.addEventListener("mousemove", e => {
  if (!drag) return;
  const dpr = window.devicePixelRatio || 1;
  const dx = (e.clientX - drag.x) * dpr, dy = (e.clientY - drag.y) * dpr;
  if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
  view.cx = drag.cx - dx / view.scale; view.cy = drag.cy + dy / view.scale;
});
canvas.addEventListener("wheel", e => {
  e.preventDefault();
  const dpr = window.devicePixelRatio || 1, r = canvas.getBoundingClientRect();
  const sx = (e.clientX - r.left) * dpr, sy = (e.clientY - r.top) * dpr;
  const [wx, wy] = S2W(sx, sy);
  view.scale = Math.min(12, Math.max(0.2, view.scale * (e.deltaY < 0 ? 1.15 : 1 / 1.15)));
  const [nx, ny] = S2W(sx, sy);
  view.cx += wx - nx; view.cy += wy - ny;
}, {passive: false});

function pick(e) {
  const dpr = window.devicePixelRatio || 1, r = canvas.getBoundingClientRect();
  const [wx, wy] = S2W((e.clientX - r.left) * dpr, (e.clientY - r.top) * dpr);
  let best = null, bd = 1e9;
  for (const o of OBJECTS) { const d = Math.hypot(o.x - wx, o.y - wy); if (d < bd) { bd = d; best = o; } }
  if (best && bd * view.scale < 40 * dpr) select(best.id, false);
}

function fit() {
  const [x0, y0, x1, y1] = MAP.bounds;
  const dpr = window.devicePixelRatio || 1;
  view.cx = (x0 + x1) / 2; view.cy = (y0 + y1) / 2;
  view.scale = Math.min(canvas.clientWidth * dpr / (x1 - x0), canvas.clientHeight * dpr / (y1 - y0)) * 0.95;
}

// ------------------------------------------------------------------ панель объекта
function objList() {
  const box = $("#objlist");
  if (box.children.length === OBJECTS.length) {
    for (const b of box.children) {
      const o = OBJECTS.find(o => o.id === b.dataset.id);
      b.classList.toggle("on", o.id === selected);
      b.querySelector("i").className = o.signal.mode;
    }
    return;
  }
  box.innerHTML = OBJECTS.map(o =>
    `<button class="chip ${o.id === selected ? "on" : ""}" data-id="${o.id}"><i class="${o.signal.mode}"></i>${o.title}</button>`).join("");
  $("#objlist").querySelectorAll(".chip").forEach(b => b.onclick = () => select(b.dataset.id, true));
}

function select(id, center) {
  if (selected === id) return;
  selected = id;
  const o = OBJECTS.find(o => o.id === id);
  if (center) { view.cx = o.x; view.cy = o.y; view.scale = Math.max(view.scale, 3); }
  objList();
  renderObj(o);
}

function vehButtons(g) {
  return g.kind === "veh" ? ["green", "green_blink", "yellow", "red", "red_yellow"] : ["green", "green_blink", "red"];
}

// Кадры камер в интерфейсе запрашиваются по одному (/cam/{id}.jpg): каждый запрос сразу закрывается.
// Бесконечный MJPEG держит соединение, а браузер открывает к серверу не больше 6 соединений,
// поэтому после нескольких переключений объектов новые потоки переставали грузиться.
function playCamera(img, cid) {
  const token = {};
  img._play = token;
  const next = () => {
    if (!img.isConnected || img._play !== token) return;
    img.src = `/cam/${cid}.jpg?t=${Date.now()}`;
  };
  img.onload = () => { img.parentElement.classList.remove("nosig"); setTimeout(next, 50); };
  img.onerror = () => { img.parentElement.classList.add("nosig"); setTimeout(next, 1000); };
  next();
}

function stopCameras(root) {
  root.querySelectorAll("img").forEach(img => { img._play = null; img.removeAttribute("src"); });
}

function renderObj(o) {
  stopCameras($("#obj"));
  const cams = o.cameras.map(c => `
    <div class="cam" data-cam="${c.id}">
      <div class="frame nosig"><span>нет сигнала</span><img alt="${c.name}"></div>
      <div class="cbar"><span class="cname">${c.name}</span>
        ${FAULT_RU.map(([f, t]) => `<button class="fbtn ${f === null ? "ok" : ""} ${c.fault === f ? "on" : ""}" data-f="${f}">${t}</button>`).join("")}
      </div>
      <div class="cbar ptz"><span class="cname">Поворот</span>
        <button class="fbtn" data-p="-15" title="влево на 15°">◀ 15°</button><button class="fbtn" data-p="15" title="вправо на 15°">15° ▶</button>
        <button class="fbtn" data-t="-5" title="выше на 5°">▲</button><button class="fbtn" data-t="5" title="ниже на 5°">▼</button>
        <button class="fbtn" data-home="1">Исходное</button><span class="ptzv mono"></span>
      </div>
    </div>`).join("");
  $("#obj").innerHTML = `
    <div class="sect">
      <h2>${o.title}</h2>
      <div class="sub">${KIND_RU[o.kind]} · ${o.streets.join(", ")} · <span class="mono">${o.id}</span></div>
    </div>
    <div class="sect"><h3>Камеры</h3><div class="cams">${cams}</div>
      <div class="urls">${o.cameras.map(c => `${location.origin}${c.stream_url}`).join("<br>")}</div></div>
    <div class="sect"><h3>Светофор</h3>
      <div class="row"><span class="badge" id="mode"></span>
        <button class="btn" id="release">Локальная программа</button><button class="btn" id="flash">Жёлтый мигающий</button></div>
      <div class="groups" id="groups"></div>
      <div id="sigmsg"></div>
    </div>
    <div class="sect"><h3>Сценарии</h3>
      <div class="row"><button class="btn" id="grp">+10 пешеходов</button><button class="btn" id="amb">Скорая через объект</button>
        <button class="btn" id="ov">3D-обзор объекта</button></div>
      <div class="cam" id="ovbox" hidden><div class="frame nosig"><span>нет сигнала</span><img alt="3D-обзор"></div></div>
    </div>
    <div class="sect"><h3>Фактическая обстановка</h3><div class="truth" id="truth"></div></div>
    <div class="sect"><h3>Журнал контроллера</h3><ul class="log" id="log"></ul></div>`;
  $("#obj").querySelectorAll(".cam[data-cam]").forEach(el => {
    const cid = el.dataset.cam;
    playCamera(el.querySelector("img"), cid);
    el.querySelectorAll(".fbtn[data-f]").forEach(b => b.onclick = async () => {
      const f = b.dataset.f === "null" ? null : b.dataset.f;
      await api(`/api/cameras/${cid}/fault`, {method: "POST", body: JSON.stringify({fault: f})});
      el.querySelectorAll(".fbtn[data-f]").forEach(x => x.classList.toggle("on", x === b));
    });
    el.querySelectorAll(".ptz .fbtn").forEach(b => b.onclick = async () => {
      const r = b.dataset.home ? await api(`/api/cameras/${cid}/ptz/home`, {method: "POST"})
        : await api(`/api/cameras/${cid}/ptz`, {method: "PUT", body: JSON.stringify({
            relative: true, pan_deg: +(b.dataset.p || 0), tilt_deg: +(b.dataset.t || 0)})});
      showPtz(el, r);
    });
  });
  $("#release").onclick = () => sig(`/api/objects/${o.id}/release`, "POST");
  $("#flash").onclick = () => sig(`/api/objects/${o.id}/flash`, "POST");
  $("#grp").onclick = () => api(`/api/objects/${o.id}/pedestrians`, {method: "POST", body: JSON.stringify({count: 10})})
    .then(r => note(`Группа из ${r.count} человек идёт к переходу`)).catch(e => note(e.message, true));
  $("#amb").onclick = () => api(`/api/objects/${o.id}/emergency`, {method: "POST"})
    .then(() => note("Скорая выехала к объекту")).catch(e => note(e.message, true));
  $("#ov").onclick = async () => {
    await api("/api/overview", {method: "POST", body: JSON.stringify({object_id: o.id})});
    const box = $("#ovbox"); box.hidden = !box.hidden;
    const img = box.querySelector("img");
    if (box.hidden) stopCameras(box); else playCamera(img, "overview");
  };
  updateObj(o);
}

function updateObj(o) {
  if (!$("#groups")) return;
  const m = $("#mode"); m.className = `badge ${o.signal.mode}`; m.textContent = MODE_RU[o.signal.mode];
  const gs = Object.entries(o.signal.groups);
  const html = gs.map(([name, g]) => `
    <div class="grp"><span class="lamp" style="background:${LAMP[g.state]}"></span>
      <span class="lbl">${g.label}<small>${name} · ${STATE_RU[g.state]}</small></span>
      <div class="acts">${vehButtons(g).map(s => `<button class="sbtn" data-g="${name}" data-s="${s}">${STATE_RU[s]}</button>`).join("")}</div>
    </div>`).join("");
  for (const c of o.cameras) {
    const el = document.querySelector(`.cam[data-cam="${c.id}"]`);
    if (el && c.ptz) showPtz(el, c.ptz);
  }
  const box = $("#groups");
  if (box.dataset.html !== html) {
    box.innerHTML = html; box.dataset.html = html;
    box.querySelectorAll(".sbtn").forEach(b => b.onclick = () =>
      sig(`/api/objects/${o.id}/signals`, "PUT", {groups: {[b.dataset.g]: b.dataset.s}}));
  }
}

function showPtz(el, p) {
  const v = el.querySelector(".ptzv"); if (!v) return;
  const sgn = x => (x > 0 ? "+" : "") + x.toFixed(0);
  v.textContent = `${sgn(p.pan_deg)}° / ${sgn(p.tilt_deg)}°` + (p.moving ? " · поворачивается" : "");
}

async function sig(url, method, body) {
  try {
    const r = await api(url, {method, body: body ? JSON.stringify(body) : undefined});
    note(r.message && r.message !== "ok" ? r.message : "Команда выполнена", !!(r.message && r.message !== "ok"));
  } catch (e) { note(e.message, true); }
  refreshObjects();
}

function note(text, err = false) {
  const el = $("#sigmsg"); if (!el) return;
  el.innerHTML = `<div class="msg ${err ? "err" : ""}">${text}</div>`;
  clearTimeout(note.t); note.t = setTimeout(() => el.innerHTML = "", 6000);
}

async function refreshObjects() {
  OBJECTS = await api("/api/objects");
  objList();
  const o = OBJECTS.find(o => o.id === selected);
  if (o) updateObj(o);
}

async function refreshDetails() {
  if (!selected) return;
  try {
    const [t, ev] = await Promise.all([api(`/api/objects/${selected}/truth`), api(`/api/objects/${selected}/events?limit=12`)]);
    const rows = [];
    t.crosswalks.forEach((c, i) => rows.push([`Переход ${i + 1} (${c.group}): ждут по сторонам`, `${c.waiting_side_0} / ${c.waiting_side_1}`],
      [`Переход ${i + 1}: на переходе`, c.crossing]));
    t.approaches.forEach(a => rows.push([`${a.street}, подход ${a.bearing_deg}°: машин / в очереди`, `${a.vehicles_within_80m} / ${a.queue}${a.emergency ? " · скорая" : ""}`]));
    $("#truth").innerHTML = rows.map(([k, v]) => `<span>${k}</span><span>${v}</span>`).join("");
    $("#log").innerHTML = ev.length ? ev.map(e => `<li><time>${new Date(e.ts * 1000).toLocaleTimeString("ru")}</time><span class="${e.level}">${e.message}</span></li>`).join("")
      : `<li><time>—</time><span>Событий пока нет. Команды системы и срабатывания защиты появятся здесь.</span></li>`;
  } catch (e) { /* объект мог смениться */ }
}

// ------------------------------------------------------------------ сценарий и статистика
async function scenario() {
  const s = await api("/api/scenario");
  $("#traffic").value = s.traffic_scale; $("#trafficOut").textContent = `×${s.traffic_scale}`;
  $("#peds").value = s.pedestrian_scale; $("#pedsOut").textContent = `×${s.pedestrian_scale}`;
}
for (const [id, key] of [["traffic", "traffic_scale"], ["peds", "pedestrian_scale"]]) {
  $("#" + id).addEventListener("input", e => { $("#" + id + "Out").textContent = `×${e.target.value}`; });
  $("#" + id).addEventListener("change", e => api("/api/scenario", {method: "PUT", body: JSON.stringify({[key]: +e.target.value})}));
}

$("#clearCars").onclick = () => api("/api/traffic/clear", {method: "POST", body: JSON.stringify({cars: true, pedestrians: false})});
$("#clearPeds").onclick = () => api("/api/traffic/clear", {method: "POST", body: JSON.stringify({cars: false, pedestrians: true})});
$("#fill").onclick = () => api("/api/traffic/fill", {method: "POST", body: JSON.stringify({})});

function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/live`);
  ws.onmessage = ev => {
    prev = cur; cur = JSON.parse(ev.data); curAt = performance.now();
    const m = Math.floor(cur.t / 60), s = Math.floor(cur.t % 60);
    const remote = Object.values(cur.signals).filter(x => x.mode === "remote").length;
    $("#stats").textContent = `модельное время ${m}:${String(s).padStart(2, "0")} · машин ${cur.cars.length} · пешеходов ${cur.peds.length} · под управлением системы ${remote}`;
    $(".dot").classList.remove("off");
  };
  ws.onclose = () => { $(".dot").classList.add("off"); setTimeout(connect, 1500); };
}

(async function init() {
  MAP = await api("/api/map");
  OBJECTS = await api("/api/objects");
  fit();
  objList();
  select(OBJECTS[0].id, false);
  scenario();
  connect();
  draw();
  setInterval(refreshObjects, 1000);
  setInterval(refreshDetails, 1500);
})();
