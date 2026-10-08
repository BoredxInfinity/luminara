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
    li.append(shade, art(s, "logo"));
    li.addEventListener("click", () => { select(s.id); launch(); });
    // The remote's touchpad cursor focuses tiles just like the D-pad does.
    li.addEventListener("mouseenter", () => { focusArea = "tiles"; paintUpdate(); select(s.id); });
    return li;
  }));
  paint(true);
}

let glowFlip = false, glowColor = "";
function paint(quiet = false) {
  for (const li of $("tiles").children) {
    li.setAttribute("aria-selected", String(focusArea === "tiles" && li.dataset.id === selected));
  }
  const s = services.find((x) => x.id === selected);
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
  if (!quiet) { hero.classList.remove("swap"); void hero.offsetWidth; hero.classList.add("swap"); }
}

function select(id, announce = true) {
  if (!services.some((s) => s.id === id) || (id === selected && !announce)) return;
  const changed = id !== selected;
  selected = id;
  paint(!changed);
  if (announce && ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ t: "select", id }));
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
  $("clock").textContent = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
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
    if (msg.selected !== selected) select(msg.selected, false);
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
