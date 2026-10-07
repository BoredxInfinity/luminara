// Phone/laptop remote. Buttons use the HTTP API; trackpad motion goes over the
// WebSocket, batched to one message per animation frame.
"use strict";

const $ = (id) => document.getElementById(id);
let services = [];
let state = {};
let ws = null;
let paired = true;

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
      toast(err.error || (res.status === 503 ? "TV browser isn't ready" : `Error ${res.status}`));
    }
    return res;
  } catch {
    toast("Can't reach the TV box");
  }
}

function buzz() { navigator.vibrate?.(8); }

let toastTimer = 0;
function toast(text) {
  const el = $("toast");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 2500);
}

const sendKey = (key) => { buzz(); return api("/api/key", { key }); };

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

// ---- services & state ------------------------------------------------------

function renderServices() {
  $("services").replaceChildren(...services.map((s, i) => {
    const b = document.createElement("button");
    b.className = "service";
    b.style.setProperty("--tile", s.color);
    b.dataset.index = i;
    b.innerHTML = `<span class="icon"></span><span class="name"></span>`;
    b.querySelector(".icon").textContent = s.icon || s.name[0];
    b.querySelector(".name").textContent = s.name;
    b.addEventListener("click", () => { buzz(); api(`/api/launch/${encodeURIComponent(s.id)}`); });
    return b;
  }));
  paintState();
}

function paintState() {
  $("conn").classList.toggle("on", Boolean(ws && ws.readyState === WebSocket.OPEN && state.cdp));
  const svc = services.find((s) => s.id === state.service_id);
  const views = { launcher: "Home screen", service: svc ? svc.name : "Streaming", web: "Web page", offline: "TV browser offline" };
  $("now-view").textContent = views[state.view] || "Connecting…";
  $("now-title").textContent = state.view === "launcher" ? "" : (state.title || "");
  for (const b of $("services").children) {
    const s = services[Number(b.dataset.index)];
    b.classList.toggle("active", state.view === "service" && s.id === state.service_id);
    b.classList.toggle("selected", state.view === "launcher" && Number(b.dataset.index) === state.selected);
  }
  const vol = $("vol-level");
  vol.classList.toggle("muted", Boolean(state.muted));
  vol.textContent = state.volume == null ? "Vol –" : state.muted ? "Muted" : `Vol ${state.volume}%`;
}

function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.t === "state") { state = msg; paintState(); }
  };
  ws.onopen = paintState;
  ws.onclose = () => {
    paintState();
    if (paired) setTimeout(connect, 1500);
  };
}

// ---- buttons -----------------------------------------------------------------

document.addEventListener("click", (e) => {
  const keyBtn = e.target.closest("[data-key]");
  if (keyBtn) return sendKey(keyBtn.dataset.key);
  const volBtn = e.target.closest("[data-vol]");
  if (volBtn) { buzz(); api("/api/volume", { action: volBtn.dataset.vol }); }
});

$("home-btn").addEventListener("click", () => { buzz(); api("/api/home"); });

// Tabs
const tabs = document.querySelectorAll("[role=tab]");
function showTab(name) {
  for (const t of tabs) {
    const on = t.dataset.tab === name;
    t.setAttribute("aria-selected", String(on));
    $(`tab-${t.dataset.tab}`).hidden = !on;
  }
  try { localStorage.setItem("tvbox-tab", name); } catch { /* storage unavailable */ }
}
for (const t of tabs) t.addEventListener("click", () => showTab(t.dataset.tab));

// Text
$("text-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("text-input");
  const res = await api("/api/text", { text: input.value, enter: $("text-enter").checked });
  if (res && res.ok) { input.value = ""; toast("Sent"); }
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

// ---- trackpad --------------------------------------------------------------

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
    pendScroll += dy * 3 / pointers.size;          // two fingers: scroll
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
  connect();
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
