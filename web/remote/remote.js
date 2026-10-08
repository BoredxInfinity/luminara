// Phone/laptop remote. Buttons use the HTTP API; touchpad motion goes over the
// WebSocket, batched to one message per animation frame.
"use strict";

const $ = (id) => document.getElementById(id);
const BRAND = "#7c5cff";
let services = [];
let state = {};
let ws = null;
let paired = true;
let launching = null;

// ---- HTTP ------------------------------------------------------------------

async function api(path, body) {
  const opts = body === undefined
    ? { method: "POST" }
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  try {
    const res = await fetch(path, opts);
    if (res.status === 401) return needPairing();
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      toast(err.error || (res.status === 503 ? "The TV browser isn't ready yet" : `Error ${res.status}`));
    }
    return res;
  } catch {
    toast("Can't reach the TV box");
  }
}

function buzz(ms = 8) { navigator.vibrate?.(ms); }

let toastTimer = 0;
function toast(text) {
  const el = $("toast");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 2500);
}

const sendKey = (key) => api("/api/key", { key });

// Press-and-hold repeat for D-pad arrows, seek and volume.
function repeater(fire, { delay = 420, every = 120 } = {}) {
  let t1 = 0, t2 = 0;
  return {
    start() { this.stop(); t1 = setTimeout(() => { t2 = setInterval(fire, every); }, delay); },
    stop() { clearTimeout(t1); clearInterval(t2); },
  };
}

function holdButton(el, fire) {
  const rep = repeater(fire);
  el.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    el.setPointerCapture(e.pointerId);
    el.classList.add("held");
    buzz();
    fire();
    rep.start();
  });
  const end = () => { el.classList.remove("held"); rep.stop(); };
  el.addEventListener("pointerup", end);
  el.addEventListener("pointercancel", end);
  el.addEventListener("click", (e) => { if (e.detail === 0) fire(); });  // keyboard activation
}

// ---- pairing ---------------------------------------------------------------

function needPairing() {
  paired = false;
  $("pair").hidden = false;
  $("pin-input").focus();
}

async function pair(pin) {
  const res = await fetch("/api/pair", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pin }),
  }).catch(() => null);
  if (res && res.ok) {
    paired = true;
    $("pair").hidden = true;
    $("pair-error").textContent = "";
    buzz(20);
    start();
    return true;
  }
  const err = res ? await res.json().catch(() => ({})) : { error: "Can't reach the TV box" };
  $("pair-error").textContent = err.error || "Pairing failed";
  return false;
}

$("pair-form").addEventListener("submit", (e) => {
  e.preventDefault();
  pair($("pin-input").value);
});
$("pin-input").addEventListener("input", (e) => {
  if (/^\d{4}$/.test(e.target.value)) pair(e.target.value);  // no need to tap Pair
});

// ---- artwork -----------------------------------------------------------------

// Paint a service's app icon onto an element, or its glyph if the icon isn't available.
// icon.bg may be a colour or a gradient; the icon image is layered on top of it.
function paintIcon(el, s) {
  const bg = s.icon?.bg || s.color;
  const gradient = bg.includes("gradient(");
  el.style.cssText = "";
  el.style.backgroundColor = gradient ? "" : bg;
  el.style.backgroundImage = gradient ? bg : "";
  el.textContent = s.glyph;
  if (!s.icon) return;
  const url = `/logos/${encodeURIComponent(s.id)}/icon?v=${s.icon.v}`;
  const img = new Image();
  img.onload = () => {
    el.textContent = "";
    el.style.backgroundImage = `url("${url}")${gradient ? `, ${bg}` : ""}`;
    el.style.backgroundSize = `${s.icon.size || "cover"}${gradient ? ", cover" : ""}`;
    el.style.backgroundPosition = `${s.icon.position || "center"}${gradient ? ", center" : ""}`;
  };
  img.src = url;
}

// ---- services & state ------------------------------------------------------

let renderedHidden = "";
function renderServices() {
  const hidden = new Set(state.settings?.hidden_apps || []);
  renderedHidden = JSON.stringify([...hidden]);
  const visible = services.filter((s) => !hidden.has(s.id));
  $("apps").style.setProperty("--cols", String(Math.min(Math.max(visible.length, 1), 5)));
  $("apps").replaceChildren(...visible.map((s) => {
    const b = document.createElement("button");
    b.className = "app";
    b.style.setProperty("--c", s.color);
    b.dataset.id = s.id;
    const icon = document.createElement("span");
    icon.className = "app-icon";
    paintIcon(icon, s);
    const name = document.createElement("span");
    name.textContent = s.name;
    b.append(icon, name);
    b.addEventListener("click", () => {
      buzz(12);
      launching = s.id;
      paintState();
      api(`/api/launch/${encodeURIComponent(s.id)}`).finally(() => setTimeout(() => { launching = null; paintState(); }, 4000));
    });
    return b;
  }));
  paintState();
}

let lastIconFor = null;
function paintState() {
  const online = ws && ws.readyState === WebSocket.OPEN;
  $("conn").className = `conn ${online && state.cdp ? "on" : online ? "half" : ""}`;
  const svc = services.find((s) => s.id === state.service_id);
  const views = { launcher: "Home screen", service: "Now on TV", web: "Web page", offline: "TV browser starting" };
  $("now-view").querySelector("span").textContent = online ? (views[state.view] || "Connecting…") : "Reconnecting…";
  $("now-title").textContent =
    state.view === "launcher" ? "Luminara" : svc ? (state.title && state.title !== svc.name ? `${svc.name} · ${state.title}` : svc.name) : (state.title || "Luminara");

  // Theme the whole remote in the colour of what's on screen.
  document.documentElement.style.setProperty("--accent", svc ? svc.color : BRAND);
  document.querySelector('meta[name="theme-color"]').content = "#0b0d13";

  const iconKey = svc ? svc.id : "home";
  if (iconKey !== lastIconFor) {
    const icon = $("now-icon");
    if (svc) { icon.classList.remove("mark"); paintIcon(icon, svc); }
    else { icon.className = "now-icon mark"; icon.style.cssText = ""; icon.textContent = ""; }
    lastIconFor = iconKey;
  }

  for (const b of $("apps").children) {
    const id = b.dataset.id;
    b.classList.toggle("active", state.view === "service" && id === state.service_id);
    b.classList.toggle("selected", state.view === "launcher" && id === state.selected);
    b.classList.toggle("launching", launching === id && state.service_id !== id);
  }

  paintUpdateBanner();

  const meter = $("vol-meter");
  const known = state.volume != null;
  meter.classList.toggle("muted", Boolean(state.muted));
  meter.querySelector(".meter-fill").style.width = known ? `${state.muted ? 100 : state.volume}%` : "0";
  meter.querySelector("b").textContent = !known ? "–" : state.muted ? "Muted" : `${state.volume}%`;
}

function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.t === "state") {
      if (launching && msg.service_id === launching) launching = null;
      state = msg;
      if (JSON.stringify(msg.settings?.hidden_apps || []) !== renderedHidden) renderServices();
      paintState();
      window.onTvState?.(msg);  // settings.js keeps its panel in sync
    }
  };
  ws.onopen = paintState;
  ws.onclose = () => {
    paintState();
    if (!paired) return;
    // A WebSocket can't tell us why it closed; ask over HTTP whether we were un-paired.
    fetch("/api/state").then((r) => {
      if (r.status === 401) needPairing();
      else setTimeout(connect, 1500);
    }, () => setTimeout(connect, 3000));
  };
}

// ---- update banner ---------------------------------------------------------------

function paintUpdateBanner() {
  const u = state.update || {};
  const show = Boolean((u.available && u.latest !== state.settings?.update_dismissed) || u.installing);
  $("update-card").hidden = !show;
  if (!show) return;
  const n = (u.commits || []).length;
  $("update-sub").textContent = u.installing ? "Installing… the TV will restart in a moment"
    : u.error ? u.error : `${n} change${n === 1 ? "" : "s"} · ${(u.commits || [])[0]?.subject || ""}`;
  $("update-install").disabled = $("update-later").disabled = Boolean(u.installing);
}
$("update-install").addEventListener("click", async () => {
  buzz(15);
  const res = await api("/api/update/install");
  if (res && res.ok) toast("Installing the update…");
});
$("update-later").addEventListener("click", () => { buzz(); api("/api/update/dismiss"); });

// ---- buttons -----------------------------------------------------------------

document.addEventListener("click", (e) => {
  const keyBtn = e.target.closest("[data-key]");
  if (keyBtn) { buzz(); sendKey(keyBtn.dataset.key); }
});
for (const el of document.querySelectorAll("[data-hold-key]")) holdButton(el, () => sendKey(el.dataset.holdKey));
for (const el of document.querySelectorAll("[data-hold-vol]")) holdButton(el, () => api("/api/volume", { action: el.dataset.holdVol }));
$("vol-meter").addEventListener("click", () => { buzz(); api("/api/volume", { action: "mute" }); });
$("home-btn").addEventListener("click", () => { buzz(12); api("/api/home"); });
$("saver-btn").addEventListener("click", () => { buzz(12); api("/api/saver/start"); });

// Tabs
const tabs = [...document.querySelectorAll("[role=tab]")];
function showTab(name) {
  tabs.forEach((t, i) => {
    const on = t.dataset.tab === name;
    t.setAttribute("aria-selected", String(on));
    $(`tab-${t.dataset.tab}`).hidden = !on;
    if (on) document.querySelector(".tab-ink").style.transform = `translateX(${i * 100}%)`;
  });
  try { localStorage.setItem("tvbox-tab", name); } catch { /* storage unavailable */ }
}
for (const t of tabs) t.addEventListener("click", () => { buzz(); showTab(t.dataset.tab); });

// Text
$("text-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("text-input");
  const res = await api("/api/text", { text: input.value, enter: $("text-enter").checked });
  if (res && res.ok) { input.value = ""; toast("Sent to the TV"); }
});

// Power
$("power-btn").addEventListener("click", () => $("power-dialog").showModal());
$("power-dialog").addEventListener("close", () => {
  const action = $("power-dialog").returnValue;
  if (action) api("/api/power", { action }).then((r) => r && r.ok && toast(action === "reboot" ? "Restarting…" : "Shutting down…"));
});

// Laptop: forward keys while no text field is focused.
const KEYMAP = { ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right",
                 Enter: "enter", Escape: "escape", Backspace: "back", " ": "playpause" };
addEventListener("keydown", (e) => {
  if (e.target.closest("input, dialog") || e.metaKey || e.ctrlKey || e.altKey) return;
  const key = KEYMAP[e.key];
  if (key) { e.preventDefault(); sendKey(key); }
});

// ---- D-pad: tap a zone, swipe anywhere on it, or hold an arrow ------------------

const dpad = $("dpad");
const SWIPE_PX = 26;
let touch = null;  // { x, y, zone, swiped }
const arrowRepeat = repeater(() => touch && sendKey(touch.zone), { delay: 450, every: 110 });

function zoneAt(e) {
  const r = dpad.getBoundingClientRect();
  const dx = e.clientX - (r.left + r.width / 2), dy = e.clientY - (r.top + r.height / 2);
  if (Math.hypot(dx, dy) < r.width * 0.19) return "enter";
  return Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? "right" : "left") : (dy > 0 ? "down" : "up");
}
function light(zone) {
  for (const z of dpad.querySelectorAll(".zone")) z.classList.toggle("hit", z.dataset.zone === zone);
}

dpad.addEventListener("pointerdown", (e) => {
  dpad.setPointerCapture(e.pointerId);
  touch = { x: e.clientX, y: e.clientY, zone: zoneAt(e), swiped: false };
  light(touch.zone);
  if (touch.zone !== "enter") arrowRepeat.start();
});
dpad.addEventListener("pointermove", (e) => {
  if (!touch || touch.swiped) return;
  const dx = e.clientX - touch.x, dy = e.clientY - touch.y;
  if (Math.hypot(dx, dy) < SWIPE_PX) return;
  arrowRepeat.stop();
  touch.swiped = true;
  touch.zone = Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? "right" : "left") : (dy > 0 ? "down" : "up");
  light(touch.zone);
  buzz();
  sendKey(touch.zone);
});
function dpadEnd() {
  if (!touch) return;
  arrowRepeat.stop();
  if (!touch.swiped) { buzz(); sendKey(touch.zone); }
  touch = null;
  setTimeout(() => light(null), 120);
}
dpad.addEventListener("pointerup", dpadEnd);
dpad.addEventListener("pointercancel", () => { arrowRepeat.stop(); touch = null; light(null); });

// ---- touchpad --------------------------------------------------------------

const pad = $("trackpad");
const pointers = new Map();          // pointerId -> {x, y}
let pendX = 0, pendY = 0, pendScroll = 0, frameQueued = false;
let gesture = null;                  // {start, moved, fingers}
const TAP_MS = 250, TAP_SLOP = 8;

function wsSend(obj) { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj)); }

function flush() {
  frameQueued = false;
  if (pendX || pendY) { wsSend({ t: "move", dx: pendX, dy: pendY }); pendX = pendY = 0; }
  if (pendScroll) { wsSend({ t: "scroll", dy: pendScroll }); pendScroll = 0; }
}
function queue() { if (!frameQueued) { frameQueued = true; requestAnimationFrame(flush); } }

pad.addEventListener("pointerdown", (e) => {
  pad.setPointerCapture(e.pointerId);
  pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
  if (!gesture) gesture = { start: performance.now(), moved: 0, fingers: 0 };
  gesture.fingers = Math.max(gesture.fingers, pointers.size);
  pad.classList.add("active");
});

pad.addEventListener("pointermove", (e) => {
  const p = pointers.get(e.pointerId);
  if (!p) return;
  const dx = e.clientX - p.x, dy = e.clientY - p.y;
  p.x = e.clientX; p.y = e.clientY;
  gesture.moved += Math.abs(dx) + Math.abs(dy);
  if (pointers.size >= 2) {
    pendScroll -= dy * 3 / pointers.size;  // two fingers: scroll like a phone (drag up = page moves up)
  } else {
    const speed = Math.hypot(dx, dy);
    const gain = 1.6 + Math.min(speed / 6, 2.4);  // pointer acceleration
    pendX += dx * gain; pendY += dy * gain;
  }
  queue();
});

function endPointer(e) {
  if (!pointers.delete(e.pointerId) || pointers.size) return;
  pad.classList.remove("active");
  const g = gesture;
  gesture = null;
  if (g && g.fingers === 1 && g.moved < TAP_SLOP && performance.now() - g.start < TAP_MS) {
    buzz();
    wsSend({ t: "click" });
  }
}
pad.addEventListener("pointerup", endPointer);
pad.addEventListener("pointercancel", endPointer);

$("click-btn").addEventListener("click", () => { buzz(); wsSend({ t: "click" }); });

// ---- startup ---------------------------------------------------------------

async function start() {
  const res = await fetch("/api/services").catch(() => null);
  if (!res) { setTimeout(start, 2000); return; }
  if (res.status === 401) return needPairing();
  services = await res.json();
  renderServices();
  if (!ws || ws.readyState === WebSocket.CLOSED) connect();
}

(async function init() {
  try { showTab(localStorage.getItem("tvbox-tab") || "pad"); } catch { showTab("pad"); }
  const params = new URLSearchParams(location.search);
  const pin = params.get("pin");
  if (pin) {
    history.replaceState(null, "", location.pathname);   // keep the PIN out of history
    if (await pair(pin)) return;
  }
  start();
})();
