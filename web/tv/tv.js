// TV launcher. Arrow keys and Enter come from the remote (as real key events via
// CDP) or from a USB keyboard; both take the same path below. The screensaver and
// on-screen messages live in web/inject/overlay.js, which runs on every page.
"use strict";

const $ = (id) => document.getElementById(id);

let allServices = [];
let services = [];          // visible ones (Settings can hide apps)
let state = { recent: {}, remotes: 0, settings: {}, update: {} };
let selected = "";          // focused service id
let focusArea = "tiles";    // "tiles" | "update"
let updateBtn = 0;          // 0 = Install, 1 = Later
let ws = null;

// ---- artwork (logo files are cached by the server; fall back to the glyph) ----

function art(service, kind) {
  const style = service[kind];
  if (!style) return glyph(service);
  const img = new Image();
  img.src = `/logos/${encodeURIComponent(service.id)}/${kind}?v=${style.v}`;
  img.alt = service.name;
  if (style.filter === "white") img.className = "white";
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
  const hidden = new Set(state.settings?.hidden_apps || []);
  services = allServices.filter((s) => !hidden.has(s.id));
  if (!services.some((s) => s.id === selected)) {
    // The focused app was hidden: move focus and tell the remotes.
    selected = services[0]?.id || "";
    if (selected && ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ t: "select", id: selected }));
  }
  $("tiles").style.setProperty("--cols", String(Math.min(Math.max(services.length, 1), 5)));
  $("tiles").replaceChildren(...services.map((s) => {
    const li = document.createElement("li");
    li.className = "tile";
    li.setAttribute("role", "option");
    li.setAttribute("aria-label", s.name);
    li.style.setProperty("--tile", s.tile);
    li.style.setProperty("--c", s.color);
    li.dataset.id = s.id;
    const shade = document.createElement("span");
    shade.className = "shade";
    const sweep = document.createElement("span");
    sweep.className = "sweep";
    li.append(shade, art(s, "logo"), sweep);
    li.addEventListener("click", () => { select(s.id); launch(); });
    // The remote's touchpad cursor focuses tiles just like the D-pad does.
    li.addEventListener("mouseenter", () => { focusArea = "tiles"; paintUpdate(); select(s.id); });
    return li;
  }));
  paint(true);
}

// ---- ambient glow ----------------------------------------------------------------
// The focused service's colour glowing from two corners, under a vignette. Drawn on a
// small canvas the GPU stretches over the screen: the glows are so soft they look the
// same, and changing colour redraws a few thousand pixels instead of crossfading two
// full-screen layers, which was more than the Pi's GPU could do without dropping frames.
const ambient = $("ambient").getContext("2d", { alpha: false });
const GLOW_MS = 1100;
let glow = { from: null, to: null, start: 0, raf: 0, drawn: 0 };

function rgb(hex) {
  const h = hex.replace("#", "");
  const full = h.length === 3 ? [...h].map((c) => c + c).join("") : h;
  const n = parseInt(full, 16);
  return Number.isNaN(n) ? [124, 92, 255] : [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

// A little more saturated, as CSS's saturate(1.2) would make it.
function saturate([r, g, b], s = 1.2) {
  const m = [[0.213 + 0.787 * s, 0.715 - 0.715 * s, 0.072 - 0.072 * s],
             [0.213 - 0.213 * s, 0.715 + 0.285 * s, 0.072 - 0.072 * s],
             [0.213 - 0.213 * s, 0.715 - 0.715 * s, 0.072 + 0.928 * s]];
  return m.map(([a, b2, c]) => Math.min(255, Math.max(0, Math.round(a * r + b2 * g + c * b))));
}

function drawAmbient([r, g, b]) {
  const ctx = ambient, w = ctx.canvas.width, h = ctx.canvas.height;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = "#07080c";
  ctx.fillRect(0, 0, w, h);
  // An elliptical glow (radii as fractions of the width and height): a circle stretched
  // sideways by the transform, fading out at 70% of its size.
  const blob = (cx, cy, rx, ry) => {
    const RX = rx * w, RY = ry * h;
    ctx.setTransform(RX / RY, 0, 0, 1, cx * w, cy * h);
    const grad = ctx.createRadialGradient(0, 0, 0, 0, 0, RY * 0.7);
    grad.addColorStop(0, `rgba(${r},${g},${b},.32)`);
    grad.addColorStop(1, `rgba(${r},${g},${b},0)`);
    ctx.fillStyle = grad;
    ctx.fillRect(-cx * w * RY / RX, -cy * h, w * RY / RX, h);
  };
  blob(0.22, 0.22, 0.63, 0.77);
  blob(0.92, 0.85, 0.56, 0.70);
  // Vignette.
  ctx.setTransform(1.2 * w / (0.9 * h), 0, 0, 1, 0.5 * w, 0.4 * h);
  const vig = ctx.createRadialGradient(0, 0, 0, 0, 0, 0.9 * h);
  vig.addColorStop(0.4, "rgba(0,0,0,0)");
  vig.addColorStop(1, "rgba(0,0,0,.65)");
  ctx.fillStyle = vig;
  ctx.fillRect(-0.5 * w * 0.9 * h / (1.2 * w), -0.4 * h, w * 0.9 * h / (1.2 * w), h);
}

function glowTo(hex) {
  const to = saturate(rgb(hex));
  if (!glow.to) {  // first paint: no fade
    glow.to = to;
    drawAmbient(to);
    return;
  }
  const now = performance.now();
  glow.from = glowAt(now);
  glow.to = to;
  glow.start = now;
  if (!glow.raf) glow.raf = requestAnimationFrame(glowFrame);
}

const easeOut = (p) => 1 - (1 - p) ** 3;
function glowAt(now) {
  if (!glow.from) return glow.to;
  const k = easeOut(Math.min(1, (now - glow.start) / GLOW_MS));
  return glow.from.map((v, i) => Math.round(v + (glow.to[i] - v) * k));
}

// A slow colour fade looks the same redrawn 30 times a second as 60, for half the work.
function glowFrame(now) {
  const done = now - glow.start >= GLOW_MS;
  if (done || now - glow.drawn >= 30) {
    drawAmbient(glowAt(now));
    glow.drawn = now;
  }
  if (!done) glow.raf = requestAnimationFrame(glowFrame);
  else { glow.raf = 0; glow.from = null; }
}

let glowColor = "";
// Runs on every D-pad press, so it only touches what changed: no page-wide style changes
// and nothing that makes the browser lay the page out there and then.
function setText(el, text) {
  if (el.textContent !== text) el.textContent = text;
}

function paint(quiet = false) {
  for (const li of $("tiles").children) {
    const on = String(focusArea === "tiles" && li.dataset.id === selected);
    if (li.getAttribute("aria-selected") !== on) li.setAttribute("aria-selected", on);
  }
  const s = services.find((x) => x.id === selected);
  if (!s) return;

  // Fade the ambient glow to the new colour.
  if (s.color !== glowColor) {
    glowTo(s.color);
    $("hero-eyebrow").style.color = s.color;
    glowColor = s.color;
  }

  const opened = state.recent?.[s.id];
  const newest = Math.max(0, ...Object.values(state.recent || {}));
  setText($("hero-eyebrow"), opened && opened === newest ? "Jump back in" : "Watch on");
  setText($("hero-title"), s.name);
  setText($("hero-sub"), [s.tagline, opened ? `Opened ${ago(opened)}` : ""].filter(Boolean).join("  ·  "));
  if (!quiet) {
    document.querySelector(".hero").animate(
      [{ opacity: 0, transform: "translateY(1vh)" }, { opacity: 1, transform: "none" }],
      { duration: 450, easing: "ease-out" });
  }
}

let selectedHere = 0;  // when a press here last moved the focus
function select(id, announce = true) {
  if (!services.some((s) => s.id === id) || (id === selected && !announce)) return;
  const changed = id !== selected;
  selected = id;
  paint(!changed);
  if (announce) {
    selectedHere = performance.now();
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ t: "select", id }));
  }
}

function move(step) {
  const i = services.findIndex((s) => s.id === selected);
  const next = services[i + step];
  if (next) select(next.id);
}

function launch() {
  if (selected) fetch(`/api/launch/${encodeURIComponent(selected)}`, { method: "POST" }).catch(() => {});
}

// The server announces a launch (from here or from a phone) just before it navigates.
let splashTimer = 0;
function showSplash(id) {
  const s = allServices.find((x) => x.id === id);
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

// ---- update banner -------------------------------------------------------------

function updateVisible() {
  const u = state.update || {};
  return Boolean(u.available && u.latest !== state.settings?.update_dismissed);
}

function paintUpdate() {
  const u = state.update || {};
  const show = updateVisible() || u.installing;
  $("update").hidden = !show;
  if (!show && focusArea === "update") focusArea = "tiles";
  if (!show) return;
  const n = (u.commits || []).length;
  $("update-detail").textContent = u.installing ? "Installing… the TV will restart"
    : u.error ? u.error
    : `${n} change${n === 1 ? "" : "s"}: ${(u.commits || []).map((c) => c.subject).slice(0, 3).join(" · ")}`;
  $("update").classList.toggle("busy", Boolean(u.installing));
  $("update-install").classList.toggle("focus", focusArea === "update" && updateBtn === 0);
  $("update-later").classList.toggle("focus", focusArea === "update" && updateBtn === 1);
}

async function updateAction(act) {
  if (state.update?.installing) return;
  await fetch(act === "install" ? "/api/update/install" : "/api/update/dismiss", { method: "POST" }).catch(() => {});
  focusArea = "tiles";
  paint(true);
  paintUpdate();
}
$("update-install").addEventListener("click", () => updateAction("install"));
$("update-later").addEventListener("click", () => updateAction("later"));

// ---- input -------------------------------------------------------------------

addEventListener("keydown", (e) => {
  const k = e.key;
  if (focusArea === "update") {
    if (k === "ArrowLeft" || k === "ArrowRight") updateBtn = k === "ArrowLeft" ? 0 : 1;
    else if (k === "ArrowDown" || k === "Escape") focusArea = "tiles";
    else if (k === "Enter" || k === " ") updateAction(updateBtn === 0 ? "install" : "later");
    else return;
    e.preventDefault();
    paint(true);
    paintUpdate();
    return;
  }
  if (k === "ArrowLeft" || k === "ArrowRight") move(k === "ArrowLeft" ? -1 : 1);
  else if (k === "ArrowUp" && updateVisible()) { focusArea = "update"; updateBtn = 0; paint(true); paintUpdate(); }
  else if (k === "Enter" || k === " ") launch();
  else return;
  e.preventDefault();
});

// ---- clock & greeting ----------------------------------------------------------

function greeting(h) {
  if (h < 5) return "Good night";
  if (h < 12) return "Good morning";
  if (h < 17) return "Good afternoon";
  if (h < 23) return "Good evening";
  return "Good night";
}

function tick() {
  const now = new Date();
  const h = now.getHours() % 12 || 12;
  const ampm = document.createElement("small");
  ampm.textContent = now.getHours() < 12 ? "AM" : "PM";
  $("clock").replaceChildren(`${h}:${String(now.getMinutes()).padStart(2, "0")}`, ampm);
  $("date").textContent = now.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" });
  $("greeting").textContent = greeting(now.getHours());
  if (services.length) paint(true);  // keep "Opened 5 min ago" fresh
  setTimeout(tick, 60000 - (Date.now() % 60000) + 50);  // wake once a minute
}

// ---- pairing info (refreshed: the Pi may get its IP after we load) -------------

async function loadInfo() {
  try {
    const info = await (await fetch("/api/info")).json();
    $("url").textContent = info.remote_url.replace(/^http:\/\//, "");
    $("pin").hidden = !info.pin;
    if (info.pin) $("pin").querySelector("strong").textContent = info.pin;
    $("qr").src = `/qr.svg?u=${encodeURIComponent(info.remote_url)}&p=${info.pin || ""}`;
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
    if (msg.t === "launch") return showSplash(msg.id);
    if (msg.t === "repaired") return loadInfo();
    if (msg.t !== "state") return;
    const before = state;
    state = msg;
    paintRemotes(msg.remotes || 0, before.remotes || 0);
    if (JSON.stringify(msg.settings?.hidden_apps) !== JSON.stringify(before.settings?.hidden_apps)) render();
    // Pressing quickly, the server's echo of an earlier press can arrive after a later one:
    // only follow a selection from elsewhere (a phone) when nothing was pressed here just now.
    if (msg.selected !== selected && performance.now() - selectedHere > 1000) select(msg.selected, false);
    else paint(true);
    paintUpdate();
  };
  ws.onclose = () => { $("status").hidden = false; setTimeout(connect, 2000); };
}

(async function init() {
  try {
    allServices = await (await fetch("/api/services")).json();
  } catch {
    setTimeout(init, 2000);
    return;
  }
  render();
  tick();
  loadInfo();
  setInterval(loadInfo, 60000);
  connect();
})();
