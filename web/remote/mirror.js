// Mirror mode: the TV picture on the phone, and touches on it act on the TV.
//   tap = click · double-tap = double-click · drag = scroll the page under your finger
//   long-press, then drag = hold the mouse button (seek bars, sliders) · pinch = zoom the
//   picture here on the phone (two fingers move it around while zoomed)
// Frames arrive as JPEGs over /ws/mirror (server/mirror.py); the TV only streams while a
// phone is watching, so we disconnect whenever this page is hidden or the mode is off.
"use strict";

(() => {
  const view = $("mirror"), stage = $("mirror-stage"), canvas = $("mirror-canvas");
  const ctx = canvas.getContext("2d");
  let active = false;
  let sock = null, retry = 0;

  // ---- mode --------------------------------------------------------------------

  function setMode(on) {
    active = on;
    view.hidden = !on;
    document.body.classList.toggle("mirroring", on);
    try { localStorage.setItem("tvbox-mode", on ? "mirror" : "remote"); } catch { /* storage unavailable */ }
    if (on) { fit(); connect(); } else { disconnect(); closeKeyboard(); }
  }
  $("mirror-btn").addEventListener("click", () => { buzz(12); setMode(true); });
  $("mirror-exit").addEventListener("click", () => { buzz(12); setMode(false); });
  $("mirror-home").addEventListener("click", () => { buzz(12); api("/api/home"); });
  document.addEventListener("visibilitychange", () => (document.hidden ? disconnect() : connect()));

  // ---- connection ----------------------------------------------------------------

  function status(text) { $("mirror-status").textContent = text; }

  function connect() {
    if (!active || document.hidden || sock) return;
    clearTimeout(retry);
    if (!canvas.dataset.live) status("Connecting to the TV…");
    const s = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/mirror`);
    s.binaryType = "blob";
    s.onmessage = (e) => { if (typeof e.data !== "string") show(e.data); };
    s.onclose = () => {
      if (sock !== s) return;  // we closed it on purpose
      sock = null;
      if (!active || document.hidden) return;
      status("Reconnecting…");
      // A WebSocket can't say why it failed; ask over HTTP whether this phone was un-paired.
      fetch("/api/state").then((r) => {
        if (r.status === 401) { setMode(false); needPairing(); } else retry = setTimeout(connect, 1500);
      }, () => { retry = setTimeout(connect, 3000); });
    };
    sock = s;
  }

  function disconnect() {
    clearTimeout(retry);
    const s = sock;
    sock = null;
    if (s) s.close();
  }

  function send(msg) { if (sock && sock.readyState === WebSocket.OPEN) sock.send(JSON.stringify(msg)); }

  // ---- picture -------------------------------------------------------------------

  let pending = null, drawing = false;  // only the newest frame matters
  function show(blob) {
    pending = blob;
    if (drawing) return;
    drawing = true;
    (async () => {
      while (pending) {
        const next = pending;
        pending = null;
        try {
          const bmp = await createImageBitmap(next);
          if (canvas.width !== bmp.width || canvas.height !== bmp.height) {
            canvas.width = bmp.width;
            canvas.height = bmp.height;
            fit();
          }
          ctx.drawImage(bmp, 0, 0);
          bmp.close();
          canvas.dataset.live = "1";
          status("");
        } catch { /* a broken frame: wait for the next */ }
      }
      drawing = false;
    })();
  }

  // Fit the picture in the space left by the controls, keeping the TV's shape.
  function fit() {
    const w = stage.clientWidth, h = stage.clientHeight, ratio = canvas.width / canvas.height;
    if (!w || !h) return;
    const cw = Math.min(w, h * ratio);
    canvas.style.width = `${cw}px`;
    canvas.style.height = `${cw / ratio}px`;
    applyZoom();
  }
  addEventListener("resize", fit);

  // ---- zoom (on the phone only) ---------------------------------------------------

  const zoom = { s: 1, x: 0, y: 0 };
  function applyZoom() {
    // Keep the picture covering the stage: it can't be dragged off into the black bars.
    const maxX = Math.max(0, (canvas.offsetWidth * zoom.s - stage.clientWidth) / 2);
    const maxY = Math.max(0, (canvas.offsetHeight * zoom.s - stage.clientHeight) / 2);
    zoom.x = Math.min(maxX, Math.max(-maxX, zoom.x));
    zoom.y = Math.min(maxY, Math.max(-maxY, zoom.y));
    canvas.style.transform = zoom.s === 1 ? "" : `translate(${zoom.x}px, ${zoom.y}px) scale(${zoom.s})`;
  }

  // ---- touches -------------------------------------------------------------------

  const fingers = new Map();   // pointerId -> {x, y}
  let g = null;                // the gesture in progress
  let lastTap = { at: 0, x: -1, y: -1 };
  const SLOP = 10, HOLD_MS = 450, DOUBLE_MS = 320;

  // Position on the TV picture as fractions (0..1); the transform (zoom) is included.
  function onTv(cx, cy) {
    const r = canvas.getBoundingClientRect();
    return { x: (cx - r.left) / r.width, y: (cy - r.top) / r.height };
  }
  const inside = (p) => p.x >= 0 && p.x <= 1 && p.y >= 0 && p.y <= 1;
  const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
  const mid = (a, b) => ({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 });

  // Drags arrive at screen rate; send at most one pan/drag message per animation frame.
  const out = { dx: 0, dy: 0, at: null, drag: null };
  let queued = false;
  function flush() {
    queued = false;
    if (out.dx || out.dy) {
      send({ t: "pan", x: out.at.x, y: out.at.y, dx: out.dx, dy: out.dy });
      out.dx = out.dy = 0;
    }
    if (out.drag) { send({ t: "drag", ...out.drag }); out.drag = null; }
  }
  function queue() { if (!queued) { queued = true; requestAnimationFrame(flush); } }

  stage.addEventListener("pointerdown", (e) => {
    try { stage.setPointerCapture(e.pointerId); } catch { /* pointer already gone */ }
    fingers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (fingers.size === 2) return startPinch();
    if (fingers.size > 2) return;
    const at = onTv(e.clientX, e.clientY);
    g = { mode: inside(at) ? "pending" : "none", sx: e.clientX, sy: e.clientY, lx: e.clientX, ly: e.clientY, at };
    if (g.mode === "none") return;
    send({ t: "hover", ...at });  // players show their controls when the mouse moves
    g.timer = setTimeout(() => {
      if (g && g.mode === "pending") {
        g.mode = "drag";
        buzz(25);
        send({ t: "press", ...g.at });
      }
    }, HOLD_MS);
  });

  stage.addEventListener("pointermove", (e) => {
    const f = fingers.get(e.pointerId);
    if (!f) return;
    f.x = e.clientX;
    f.y = e.clientY;
    if (!g || g.mode === "none") return;
    if (g.mode === "pinch") return movePinch();
    if (g.mode === "pending" && Math.hypot(e.clientX - g.sx, e.clientY - g.sy) > SLOP) {
      clearTimeout(g.timer);
      g.mode = "pan";
    }
    if (g.mode === "pan") {
      // Scroll whatever is under where the finger landed, following the finger.
      const r = canvas.getBoundingClientRect();
      out.dx += (e.clientX - g.lx) / r.width;
      out.dy += (e.clientY - g.ly) / r.height;
      out.at = g.at;
      queue();
    } else if (g.mode === "drag") {
      out.drag = onTv(e.clientX, e.clientY);
      queue();
    }
    g.lx = e.clientX;
    g.ly = e.clientY;
  });

  function endFinger(e, cancelled) {
    if (!fingers.delete(e.pointerId)) return;
    if (g && g.mode === "pinch") {
      if (!fingers.size) { g = null; if (zoom.s < 1.08) { zoom.s = 1; applyZoom(); } }
      return;  // lifting one finger of a pinch doesn't start a new gesture
    }
    if (fingers.size) return;
    const done = g;
    g = null;
    if (!done || done.mode === "none") return;
    clearTimeout(done.timer);
    flush();
    if (done.mode === "drag") {
      send({ t: "release", ...onTv(e.clientX, e.clientY) });
    } else if (done.mode === "pending" && !cancelled) {
      const now = performance.now();
      const double = now - lastTap.at < DOUBLE_MS && Math.hypot(done.at.x - lastTap.x, done.at.y - lastTap.y) < 0.03;
      buzz();
      send({ t: "tap", ...done.at, n: double ? 2 : 1 });
      lastTap = double ? { at: 0, x: -1, y: -1 } : { at: now, ...done.at };
    }
  }
  stage.addEventListener("pointerup", (e) => endFinger(e, false));
  stage.addEventListener("pointercancel", (e) => endFinger(e, true));

  function startPinch() {
    if (g) {
      clearTimeout(g.timer);
      if (g.mode === "drag") send({ t: "release", ...g.at });
      flush();
    }
    const [a, b] = [...fingers.values()];
    const box = stage.getBoundingClientRect();
    g = { mode: "pinch", d0: dist(a, b) || 1, m0: mid(a, b), s0: zoom.s, x0: zoom.x, y0: zoom.y,
          cx: box.left + box.width / 2, cy: box.top + box.height / 2 };
  }

  function movePinch() {
    const [a, b] = [...fingers.values()];
    const s = Math.min(4, Math.max(1, g.s0 * dist(a, b) / g.d0));
    const m = mid(a, b);
    // Keep the spot that was under the fingers under them while zooming and moving.
    zoom.x = m.x - g.cx - (g.m0.x - g.cx - g.x0) * s / g.s0;
    zoom.y = m.y - g.cy - (g.m0.y - g.cy - g.y0) * s / g.s0;
    zoom.s = s;
    applyZoom();
  }

  // ---- typing straight onto the TV ------------------------------------------------
  // Whatever changes in the box is replayed on the TV (autocorrect included), so the
  // phone keyboard behaves like a keyboard plugged into the TV.

  const form = $("mirror-type"), input = $("mirror-input");
  let typed = "";
  function closeKeyboard() { form.hidden = true; input.blur(); requestAnimationFrame(fit); }
  $("mirror-kbd").addEventListener("click", () => {
    buzz();
    if (!form.hidden) return closeKeyboard();
    form.hidden = false;
    input.value = typed = "";
    input.focus();
    requestAnimationFrame(fit);
  });
  $("mirror-type-done").addEventListener("click", closeKeyboard);
  input.addEventListener("input", () => {
    const now = input.value;
    let same = 0;
    while (same < now.length && same < typed.length && now[same] === typed[same]) same++;
    for (let i = typed.length; i > same; i--) send({ t: "key", k: "backspace" });
    if (now.length > same) send({ t: "text", s: now.slice(same) });
    typed = now;
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Backspace" && !input.value) send({ t: "key", k: "backspace" });  // keep deleting on the TV
  });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    send({ t: "key", k: "enter" });
    input.value = typed = "";
  });

  try { if (localStorage.getItem("tvbox-mode") === "mirror") setMode(true); } catch { /* storage unavailable */ }
})();
