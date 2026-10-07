// TV launcher. Arrow keys and Enter come from the remote (as real key events via
// CDP) or from a USB keyboard; both take the same path below.
"use strict";

const $ = (id) => document.getElementById(id);
const SAVER_AFTER_MS = 3 * 60 * 1000;

let services = [];
let state = { recent: {}, remotes: 0 };
let selected = 0;
let ws = null;

// ---- artwork (logo files are cached by the server; fall back to the glyph) ----

function art(service, kind, cls = "") {
  const style = service[kind];
  if (!style) return glyph(service);
  const img = new Image();
  img.src = `/logos/${encodeURIComponent(service.id)}/${kind}?v=${style.v}`;
  img.alt = service.name;
  img.className = cls + (style.filter === "white" ? " white" : "");
  img.onerror = () => img.replaceWith(glyph(service));
  return img;
}

function glyph(service) {
  const span = document.createElement("span");
  span.className = "glyph";
  span.textContent = service.glyph;
  return span;
}

// ---- tiles & hero ------------------------------------------------------------

function render() {
  $("tiles").replaceChildren(...services.map((s, i) => {
    const li = document.createElement("li");
    li.className = "tile";
    li.setAttribute("role", "option");
    li.setAttribute("aria-label", s.name);
    li.style.setProperty("--tile", s.tile);
    li.style.setProperty("--c", s.color);
    li.dataset.index = i;
    const shade = document.createElement("span");
    shade.className = "shade";
    li.append(shade, art(s, "logo"));
    li.addEventListener("click", () => { select(i); launch(); });
    return li;
  }));
  paint(true);
}

let glowFlip = false, glowColor = "";
function paint(force = false) {
  for (const li of $("tiles").children) {
    li.setAttribute("aria-selected", String(Number(li.dataset.index) === selected));
  }
  const s = services[selected];
  if (!s) return;
  document.documentElement.style.setProperty("--accent", s.color);

  // Crossfade the ambient glow to the new colour.
  if (s.color !== glowColor) {
    const [on, off] = glowFlip ? [$("glow-a"), $("glow-b")] : [$("glow-b"), $("glow-a")];
    on.style.setProperty("--c", s.color);
    on.classList.add("on");
    off.classList.remove("on");
    glowFlip = !glowFlip;
    glowColor = s.color;
  }

  const hero = document.querySelector(".hero");
  const opened = state.recent?.[s.id];
  const newest = Math.max(0, ...Object.values(state.recent || {}));
  $("hero-eyebrow").textContent = opened && opened === newest ? "Jump back in" : "Watch on";
  $("hero-title").textContent = s.name;
  $("hero-sub").textContent = [s.tagline, opened ? `Opened ${ago(opened)}` : ""].filter(Boolean).join("  ·  ");
  if (!force) { hero.classList.remove("swap"); void hero.offsetWidth; hero.classList.add("swap"); }
}

function select(i, announce = true) {
  if (!services.length) return;
  const next = Math.max(0, Math.min(services.length - 1, i));
  if (next === selected && !announce) return;
  selected = next;
  paint();
  if (announce && ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ t: "select", index: selected }));
}

function launch() {
  const s = services[selected];
  if (s) fetch(`/api/launch/${encodeURIComponent(s.id)}`, { method: "POST" }).catch(() => {});
}

// The server announces a launch (from here or from a phone) just before it navigates.
let splashTimer = 0;
function showSplash(id) {
  const s = services.find((x) => x.id === id);
  if (!s) return;
  const splash = $("splash");
  splash.style.setProperty("--tile", s.tile);
  splash.querySelector(".splash-logo").replaceChildren(art(s, "logo"));
  splash.classList.add("on");
  clearTimeout(splashTimer);
  splashTimer = setTimeout(() => splash.classList.remove("on"), 12000);  // in case the site never loads
}

function ago(epochSeconds) {
  const mins = Math.round((Date.now() / 1000 - epochSeconds) / 60);
  if (mins < 2) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours} hour${hours > 1 ? "s" : ""} ago`;
  const days = Math.round(hours / 24);
  return days === 1 ? "yesterday" : `${days} days ago`;
}

// ---- input -------------------------------------------------------------------

addEventListener("keydown", (e) => {
  if (wake()) { e.preventDefault(); return; }  // first key only wakes the screensaver
  const moves = { ArrowLeft: -1, ArrowRight: 1 };
  if (e.key in moves) {
    select(selected + moves[e.key]);
    e.preventDefault();
  } else if (e.key === "Enter" || e.key === " ") {
    launch();
    e.preventDefault();
  }
});
addEventListener("mousemove", () => wake(), { passive: true });

// ---- clock, greeting, screensaver ---------------------------------------------

function greeting(h) {
  if (h < 5) return "Good night";
  if (h < 12) return "Good morning";
  if (h < 17) return "Good afternoon";
  if (h < 23) return "Good evening";
  return "Good night";
}

function tick() {
  const now = new Date();
  const time = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const date = now.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" });
  $("clock").textContent = time;
  $("date").textContent = date;
  $("greeting").textContent = greeting(now.getHours());
  document.querySelector(".saver-clock time").textContent = time;
  document.querySelector(".saver-clock p").textContent = date;
  if (services.length) paint(true);  // keep "Opened 5 min ago" fresh
  setTimeout(tick, 60000 - (Date.now() % 60000) + 50);  // wake once a minute
}

let idleTimer = 0, driftTimer = 0;
function armSaver() {
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => {
    $("saver").classList.add("on");
    drift();
    driftTimer = setInterval(drift, 30000);  // move the clock around: no burn-in
  }, SAVER_AFTER_MS);
}
function drift() {
  const el = document.querySelector(".saver-clock");
  const x = Math.random() * (innerWidth - el.offsetWidth);
  const y = Math.random() * (innerHeight - el.offsetHeight);
  el.style.transform = `translate(${x}px, ${y}px)`;
}
function wake() {
  const was = $("saver").classList.contains("on");
  $("saver").classList.remove("on");
  clearInterval(driftTimer);
  armSaver();
  return was;
}

// ---- pairing info (refreshed: the Pi may get its IP after we load) -------------

async function loadInfo() {
  try {
    const info = await (await fetch("/api/info")).json();
    $("url").textContent = info.remote_url.replace(/^http:\/\//, "");
    $("pin").hidden = !info.pin;
    if (info.pin) $("pin").querySelector("strong").textContent = info.pin;
    $("qr").src = `/qr.svg?u=${encodeURIComponent(info.remote_url)}`;
  } catch { /* server restarting; the next refresh fixes it */ }
}

function paintRemotes(n, previous) {
  const chip = $("remotes");
  chip.hidden = n === 0;
  chip.querySelector("span").textContent = n === 1 ? "1 remote" : `${n} remotes`;
  if (n > previous) { chip.classList.remove("pop"); void chip.offsetWidth; chip.classList.add("pop"); }
  $("pair").classList.toggle("compact", n > 0);
  $("pair-title").textContent = n > 0 ? "Add another phone" : "Use your phone as the remote";
}

// ---- live state ----------------------------------------------------------------

function connect() {
  ws = new WebSocket(`ws://${location.host}/ws?role=tv`);
  ws.onopen = () => { $("status").hidden = true; };
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.t === "launch") { showSplash(msg.id); return; }
    if (msg.t !== "state") return;
    const before = state.remotes || 0;
    state = msg;
    paintRemotes(msg.remotes || 0, before);
    if (msg.selected !== selected) select(msg.selected, false);
    else paint(true);
  };
  ws.onclose = () => { $("status").hidden = false; setTimeout(connect, 2000); };
}

(async function init() {
  try {
    services = await (await fetch("/api/services")).json();
  } catch {
    setTimeout(init, 2000);
    return;
  }
  render();
  tick();
  loadInfo();
  setInterval(loadInfo, 60000);
  armSaver();
  connect();
})();
