// Settings panel. Uses api(), $(), buzz(), toast(), services and state from remote.js.
// Every change is saved to the box immediately; there is no Save button.
"use strict";

const sheet = $("settings");
let prefs = {};
let choices = { saver_minutes: [0, 1, 2, 5, 10, 15, 30], boot_volume: [0, 25, 50, 75, 100] };
let spotify = {};

async function saveSetting(changes) {
  Object.assign(prefs, changes);
  paintSettings();
  const res = await fetch("/api/settings", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(changes),
  }).catch(() => null);
  if (!res || !res.ok) return toast("Couldn't save that setting");
  const data = await res.json();
  prefs = data.values;
  if (data.restart_display) $("set-restart-note").hidden = false;
  paintSettings();
}

function segmented(container, values, current, label, onPick) {
  container.replaceChildren(...values.map((v) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = label(v);
    b.setAttribute("aria-pressed", String(v === current));
    b.addEventListener("click", () => { buzz(); onPick(v); });
    return b;
  }));
}

function paintSettings() {
  if (!sheet.open) return;
  const hidden = new Set(prefs.hidden_apps || []);

  // Apps: one switch per service, with its icon.
  $("set-apps").replaceChildren(...services.map((s) => {
    const row = document.createElement("label");
    row.className = "switch-row app-row";
    const icon = document.createElement("span");
    icon.className = "app-icon mini";
    paintIcon(icon, s);
    const name = document.createElement("span");
    name.textContent = s.name;
    const sw = document.createElement("input");
    sw.type = "checkbox";
    sw.className = "switch";
    sw.checked = !hidden.has(s.id);
    sw.addEventListener("change", () => {
      buzz();
      const next = new Set(prefs.hidden_apps || []);
      sw.checked ? next.delete(s.id) : next.add(s.id);
      if (next.size === services.length) { sw.checked = true; return toast("Keep at least one app"); }
      saveSetting({ hidden_apps: [...next] });
    });
    row.append(icon, name, sw);
    return row;
  }));

  segmented($("set-saver"), choices.saver_minutes, prefs.saver_minutes,
    (v) => (v === 0 ? "Off" : `${v} min`), (v) => saveSetting({ saver_minutes: v }));
  segmented($("set-volume"), choices.boot_volume, prefs.boot_volume,
    (v) => (v === 0 ? "As left" : `${v}%`), (v) => saveSetting({ boot_volume: v }));
  $("set-saver-clock").checked = prefs.saver_clock;
  $("set-h264").checked = prefs.prefer_h264;
  $("set-lite").checked = prefs.lite_browser;
  $("set-autoupdate").checked = prefs.auto_update_check;
  paintUpdateSection();
}

function paintUpdateSection() {
  const u = state.update || {};
  const parts = [];
  if (u.current) parts.push(`Running version ${u.current}`);
  if (u.installing) parts.push("Installing an update…");
  else if (u.checking) parts.push("Checking…");
  else if (u.error) parts.push(u.error);
  else if (u.available) parts.push(`Update available: ${(u.commits || []).length} change(s)`);
  else if (u.checked_at) parts.push(`Up to date · checked ${new Date(u.checked_at * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}`);
  $("set-update-status").textContent = parts.join(" · ");
  $("set-install").hidden = !u.available || u.installing;
  $("set-check").disabled = Boolean(u.checking || u.installing);
}

async function loadAbout() {
  const res = await fetch("/api/system").catch(() => null);
  if (!res || !res.ok) return;
  const s = await res.json();
  $("set-pin").textContent = s.pin || "off";
  const fmtUptime = (sec) => {
    if (sec == null) return "–";
    const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
    return d ? `${d} d ${h} h` : h ? `${h} h ${m} min` : `${m} min`;
  };
  const rows = [
    ["Name", `${s.hostname}.local`],
    ["Address", `${s.ip}:${s.port}`],
    ["Version", s.version ? `${s.version} · ${s.version_subject}` : "–"],
    ["Memory", s.mem_total_mb ? `${s.mem_total_mb - s.mem_available_mb} of ${s.mem_total_mb} MB in use` : "–"],
    ["Temperature", s.cpu_temp_c != null ? `${s.cpu_temp_c} °C` : "–"],
    ["Up for", fmtUptime(s.uptime_s)],
  ];
  $("set-about").replaceChildren(...rows.flatMap(([k, v]) => {
    const dt = document.createElement("dt"); dt.textContent = k;
    const dd = document.createElement("dd"); dd.textContent = v;
    return [dt, dd];
  }));
}

// ---- Spotify: shown on the screensaver's turntable (server/spotify.py) ----------------

function paintSpotify() {
  $("sp-connected").hidden = !spotify.connected;
  $("sp-setup").hidden = !!spotify.connected;
  $("sp-user").textContent = spotify.user || "your account";
  $("sp-redirect").textContent = spotify.redirect_uri || "";
  if (spotify.client_id && !$("sp-client").value) $("sp-client").value = spotify.client_id;
}

async function loadSpotify() {
  const res = await fetch("/api/spotify").catch(() => null);
  if (res && res.ok) { spotify = await res.json(); paintSpotify(); }
}

function spotifyError(text) { $("sp-error").textContent = text || ""; }

function boxError(res, data) {
  if (!res) return "Couldn't reach the TV box";
  return data.error || `The TV box ran into a problem (error ${res.status}). Try again.`;
}

// Phones on plain HTTP have no clipboard API: fall back to a hidden text box.
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch { /* fall back */ }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.cssText = "position:fixed;opacity:0";
  document.body.append(ta);
  ta.select();
  const ok = document.execCommand("copy");
  ta.remove();
  return ok;
}

$("sp-copy").addEventListener("click", async () => {
  buzz();
  toast((await copyText(spotify.redirect_uri)) ? "Copied" : "Press and hold the address to copy it");
});

$("sp-login").addEventListener("click", async () => {
  buzz();
  spotifyError("");
  const clientId = $("sp-client").value.trim();
  if (!clientId) return spotifyError("Paste the Client ID from your Spotify app first.");
  // Open the tab now, while this still counts as a tap; phones block pop-ups opened later.
  const tab = window.open("", "_blank");
  const res = await fetch("/api/spotify/login", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ client_id: clientId }),
  }).catch(() => null);
  const data = res ? await res.json().catch(() => ({})) : {};
  if (!res || !res.ok) { if (tab) tab.close(); return spotifyError(boxError(res, data)); }
  if (tab) tab.location.href = data.url;
  else location.href = data.url;  // pop-up blocked: go in this tab, then come back
});

$("sp-finish").addEventListener("click", async () => {
  buzz();
  spotifyError("");
  const url = $("sp-paste").value.trim();
  if (!url) return spotifyError("Paste the address of the page Spotify opened after you logged in.");
  $("sp-finish").disabled = true;
  const res = await fetch("/api/spotify/finish", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url }),
  }).catch(() => null);
  $("sp-finish").disabled = false;
  const data = res ? await res.json().catch(() => ({})) : {};
  if (!res || !res.ok) {
    await loadSpotify();  // it may have connected even so
    if (spotify.connected) return toast("Spotify connected");
    return spotifyError(boxError(res, data));
  }
  spotify = data;
  $("sp-paste").value = "";
  paintSpotify();
  toast(data.user ? `Spotify connected as ${data.user}` : "Spotify connected");
});

$("sp-disconnect").addEventListener("click", async () => {
  if (!confirm("Disconnect Spotify? The screensaver will stop showing what's playing.")) return;
  const res = await api("/api/spotify/disconnect");
  if (res && res.ok) { spotify = await res.json(); paintSpotify(); }
});

let aboutTimer = 0;
async function openSettings() {
  buzz();
  const res = await fetch("/api/settings").catch(() => null);
  if (!res || !res.ok) return toast("Couldn't load settings");
  const data = await res.json();
  prefs = data.values;
  choices = data.choices || choices;
  $("set-restart-note").hidden = true;
  sheet.showModal();
  paintSettings();
  loadSpotify();
  loadAbout();
  aboutTimer = setInterval(loadAbout, 5000);  // live memory / temperature
}

$("settings-btn").addEventListener("click", openSettings);
$("settings-close").addEventListener("click", () => sheet.close());
sheet.addEventListener("close", () => clearInterval(aboutTimer));

$("set-saver-clock").addEventListener("change", (e) => saveSetting({ saver_clock: e.target.checked }));
$("set-h264").addEventListener("change", (e) => saveSetting({ prefer_h264: e.target.checked }));
$("set-lite").addEventListener("change", (e) => saveSetting({ lite_browser: e.target.checked }));
$("set-autoupdate").addEventListener("change", (e) => saveSetting({ auto_update_check: e.target.checked }));

$("set-saver-preview").addEventListener("click", async () => {
  buzz();
  const res = await api("/api/saver/start");
  if (res && res.ok) toast("Press any button to wake the TV");
});

async function restartDisplay() {
  buzz(15);
  const res = await api("/api/display/restart");
  if (res && res.ok) { toast("Restarting the TV display…"); $("set-restart-note").hidden = true; }
}
$("set-restart-now").addEventListener("click", restartDisplay);
$("set-restart-display").addEventListener("click", () => {
  if (confirm("Restart the TV display? Whatever is playing will stop.")) restartDisplay();
});

$("set-check").addEventListener("click", async () => {
  buzz();
  $("set-update-status").textContent = "Checking…";
  const res = await api("/api/update/check");
  if (res && res.ok) {
    state.update = (await res.json()).update;
    paintUpdateSection();
    paintUpdateBanner();
  }
});
$("set-install").addEventListener("click", async () => {
  buzz(15);
  const res = await api("/api/update/install");
  if (res && res.ok) { toast("Installing the update…"); sheet.close(); }
});

$("set-forget").addEventListener("click", async () => {
  if (!confirm("Forget every paired phone, including this one? The TV will show a new PIN.")) return;
  const res = await api("/api/pin/reset");
  if (res && res.ok) { sheet.close(); needPairing(); }
});

// Keep the panel in sync with changes made from another phone or by the box itself.
window.onTvState = (msg) => {
  if (!sheet.open) return;
  if (JSON.stringify(msg.settings) !== JSON.stringify(prefs)) { prefs = { ...msg.settings }; paintSettings(); }
  else paintUpdateSection();
};
