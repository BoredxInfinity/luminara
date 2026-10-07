// TV launcher. Arrow keys and Enter come from the remote (as real key events via
// CDP) or from a USB keyboard; both take the same path below.
"use strict";

const tilesEl = document.getElementById("tiles");
const statusEl = document.getElementById("status");
let services = [];
let selected = 0;
let ws = null;

function render() {
  tilesEl.replaceChildren(...services.map((s, i) => {
    const li = document.createElement("li");
    li.className = "tile";
    li.setAttribute("role", "option");
    li.style.setProperty("--tile", s.color);
    li.dataset.index = i;
    const icon = document.createElement("span");
    icon.className = "icon";
    icon.textContent = s.icon || s.name[0];
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = s.name;
    li.append(icon, name);
    li.addEventListener("click", () => { select(i); launch(); });
    return li;
  }));
  paint();
}

function paint() {
  for (const li of tilesEl.children) {
    li.setAttribute("aria-selected", String(Number(li.dataset.index) === selected));
  }
}

function select(i, announce = true) {
  if (!services.length) return;
  selected = Math.max(0, Math.min(services.length - 1, i));
  paint();
  if (announce && ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ t: "select", index: selected }));
  }
}

function launch() {
  const s = services[selected];
  if (s) fetch(`/api/launch/${encodeURIComponent(s.id)}`, { method: "POST" });
}

function columns() {
  return getComputedStyle(tilesEl).gridTemplateColumns.split(" ").length || 1;
}

addEventListener("keydown", (e) => {
  const cols = columns();
  const moves = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -cols, ArrowDown: cols };
  if (e.key in moves) {
    const next = selected + moves[e.key];
    if (next >= 0 && next < services.length) select(next);
    e.preventDefault();
  } else if (e.key === "Enter" || e.key === " ") {
    launch();
    e.preventDefault();
  }
});

// ---- clock -----------------------------------------------------------------
const clockEl = document.getElementById("clock");
function tick() {
  clockEl.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  setTimeout(tick, 60000 - (Date.now() % 60000) + 50);  // wake once a minute
}
tick();

// ---- pairing info (refreshed: the Pi may get its IP after we load) ------------
async function loadInfo() {
  try {
    const info = await (await fetch("/api/info")).json();
    document.getElementById("url").textContent =
      `${info.remote_url.replace(/^http:\/\//, "")}  ·  ${info.hostname}.local:${info.port}`;
    const pin = document.getElementById("pin");
    pin.hidden = !info.pin;
    if (info.pin) pin.querySelector("strong").textContent = info.pin;
    document.getElementById("qr").src = `/qr.svg?u=${encodeURIComponent(info.remote_url)}`;
  } catch { /* server restarting; next refresh will fix it */ }
}

// ---- live state ------------------------------------------------------------
function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => { statusEl.hidden = true; };
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.t === "state" && msg.selected !== selected) select(msg.selected, false);
  };
  ws.onclose = () => { statusEl.hidden = false; setTimeout(connect, 2000); };
}

(async function init() {
  try {
    services = await (await fetch("/api/services")).json();
  } catch {
    setTimeout(init, 2000);
    return;
  }
  render();
  loadInfo();
  setInterval(loadInfo, 60000);
  connect();
})();
