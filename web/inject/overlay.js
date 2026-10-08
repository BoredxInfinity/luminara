// Injected into every page by the TV box, before the page's own scripts.
// window.__tvboxConfig (set just above this script) holds the user's settings.
//
// - Codec steering: hide VP9/AV1 support so sites pick H.264, which the Pi decodes in
//   hardware. VP9/AV1 fall back to slow software decoding and make Netflix/Prime stutter.
// - Screensaver after idle time (never over a playing video): a pulsing colour dot grid,
//   or a turntable spinning the album art when music is playing (e.g. Spotify).
// - The remote's cursor dot, and on-screen messages: volume, toasts, "updating".
// Everything visual lives in a shadow root so page CSS can't touch it, and is built
// without innerHTML because YouTube and others enforce Trusted Types.
(() => {
  const cfg = Object.assign({ preferH264: true, saverMinutes: 5, saverClock: true }, window.__tvboxConfig || {});

  // ---- codec steering (all frames: players sometimes live in iframes) ----------
  if (cfg.preferH264 && !window.__tvboxCodecs) {
    window.__tvboxCodecs = true;
    const slow = (type) => /vp0?9|av01|av1\b/i.test(String(type || ""));
    for (const MS of [window.MediaSource, window.ManagedMediaSource, window.WebKitMediaSource]) {
      if (MS && MS.isTypeSupported) {
        const real = MS.isTypeSupported.bind(MS);
        MS.isTypeSupported = (type) => !slow(type) && real(type);
      }
    }
    const canPlay = HTMLMediaElement.prototype.canPlayType;
    HTMLMediaElement.prototype.canPlayType = function (type) { return slow(type) ? "" : canPlay.call(this, type); };
    const mc = navigator.mediaCapabilities;
    if (mc && mc.decodingInfo) {
      const real = mc.decodingInfo.bind(mc);
      mc.decodingInfo = (config) => slow(config && config.video && config.video.contentType)
        ? Promise.resolve({ supported: false, smooth: false, powerEfficient: false })
        : real(config);
    }
  }

  if (window.top !== window || window.__tvbox) return;

  // No scrollbars on a TV. Chromium's own are off (--hide-scrollbars in kiosk.sh); this
  // hides the ones sites draw themselves (OverlayScrollbars, used by Spotify).
  try {
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(".os-scrollbar { display: none !important; }");
    document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
  } catch { /* very old engine: leave them */ }

  // Some players (Spotify) play through an <audio> that's never put in the page, where
  // querySelectorAll can't see it. We run before the page's scripts, so remember every
  // element that starts playing.
  const started = new Set();
  const realPlay = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function (...args) {
    started.add(this);
    return realPlay.apply(this, args);
  };

  const CSS = `
    :host { all: initial; position: fixed; inset: 0; z-index: 2147483647; pointer-events: none; }
    .layer { position: fixed; inset: 0; font: 600 22px/1.2 "Noto Sans Display","Noto Sans",system-ui,sans-serif; color: #fff; }
    .dot { position: absolute; left: 0; top: 0; width: 26px; height: 26px; margin: -13px 0 0 -13px; border-radius: 50%;
           background: rgba(255,255,255,.92); border: 3px solid rgba(0,0,0,.55); box-shadow: 0 0 10px rgba(0,0,0,.6);
           opacity: 0; transition: opacity .25s; will-change: transform; z-index: 5; }
    .pill { position: absolute; display: flex; align-items: center; gap: 16px; padding: 16px 26px; border-radius: 999px;
            background: rgba(14,16,22,.85); box-shadow: 0 10px 40px rgba(0,0,0,.45); z-index: 4;
            opacity: 0; transform: translateY(-14px) scale(.96); transition: opacity .2s, transform .25s cubic-bezier(.2,.9,.3,1.2); }
    .pill.show { opacity: 1; transform: none; }
    .vol { top: 48px; right: 56px; min-width: 360px; }
    .toast { top: 48px; left: 50%; translate: -50% 0; max-width: 70vw; }
    .bar { flex: 1; height: 10px; border-radius: 6px; background: rgba(255,255,255,.18); overflow: hidden; }
    .fill { height: 100%; width: 0; border-radius: 6px; background: #fff; transition: width .18s ease-out; }
    .muted .fill { background: #ff5d5d; }
    .num { min-width: 64px; text-align: right; font-variant-numeric: tabular-nums; }
    svg { width: 30px; height: 30px; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; flex: none; }

    /* "Updating" curtain */
    .updating { position: absolute; inset: 0; z-index: 6; display: grid; place-content: center; justify-items: center; gap: 28px;
                background: rgba(5,6,10,.92); opacity: 0; visibility: hidden; transition: opacity .6s, visibility 0s .6s; font-size: 34px; }
    .updating.show { opacity: 1; visibility: visible; transition: opacity .6s; }
    .updating:not(.show) .spinner { animation: none; }  /* don't burn the Pi's CPU on every page */
    .updating small { font-size: 20px; color: #9aa3b8; font-weight: 500; }
    .spinner { width: 64px; height: 64px; border-radius: 50%; border: 5px solid rgba(255,255,255,.15); border-top-color: #fff; animation: spin 1s linear infinite; }

    /* Screensaver: fades in slowly, out quickly */
    .saver { position: absolute; inset: 0; z-index: 3; background: #000; opacity: 0; visibility: hidden;
             transition: opacity 3.2s cubic-bezier(.4,0,.2,1), visibility 0s linear 3.2s; }
    .saver.on { opacity: 1; visibility: visible; transition: opacity 3.2s cubic-bezier(.4,0,.2,1), visibility 0s; }
    .saver.leaving { transition: opacity .7s ease-out, visibility 0s linear .7s; }
    .saver canvas { position: absolute; inset: 0; width: 100%; height: 100%; }
    .clock { position: absolute; left: 50%; top: 50%; translate: -50% -50%; text-align: center; color: rgba(255,255,255,.92);
             text-shadow: 0 0 40px rgba(0,0,0,.9), 0 0 12px rgba(0,0,0,.8); }
    .clock b { display: block; font-size: 12vh; font-weight: 200; letter-spacing: -.02em; font-variant-numeric: tabular-nums; }
    .clock span { font-size: 2.6vh; font-weight: 500; color: rgba(255,255,255,.7); }
    .saver:not(.music) .clock { display: none; }  /* the dots are the clock */
    .music .clock { left: auto; top: 6vh; right: 6vw; translate: none; text-align: right; }
    .music .clock b { font-size: 6vh; }

    /* Turntable (music playing) */
    .tt { position: absolute; inset: 0; display: none; align-items: center; gap: 7vw; padding: 0 8vw; }
    .music .tt { display: flex; }
    .music canvas { display: none; }
    .tt-bg { position: absolute; inset: -10%; background-size: cover; background-position: center;
             filter: blur(70px) saturate(1.4) brightness(.45); transform: scale(1.1); }
    .deck { position: relative; flex: none; width: 62vh; height: 52vh; border-radius: 3vh;
            background: linear-gradient(160deg, #3a2a1f, #1d1510 60%, #120d0a);
            box-shadow: 0 4vh 9vh rgba(0,0,0,.65), inset 0 .3vh 0 rgba(255,255,255,.08); }
    .platter { position: absolute; left: 3.5vh; top: 3.5vh; width: 45vh; height: 45vh; border-radius: 50%;
               background: radial-gradient(circle, #2b2b2b 0 69%, #8d8d8d 70% 71%, #444 72%);
               box-shadow: 0 1.2vh 3vh rgba(0,0,0,.6); }
    .record { position: absolute; inset: 1.2vh; border-radius: 50%;
              background: repeating-radial-gradient(circle, #0c0c0c 0 .25vh, #1a1a1a .3vh .5vh);
              animation: spin 1.8s linear infinite; }
    .paused .record, .saver:not(.on) .record, .saver:not(.on) .eq i { animation-play-state: paused; }
    .label { position: absolute; inset: 29%; border-radius: 50%; background: #c33 center / cover;
             box-shadow: 0 0 0 .5vh #0a0a0a; }
    .spindle { position: absolute; left: 50%; top: 50%; width: 1.4vh; height: 1.4vh; margin: -.7vh; border-radius: 50%;
               background: radial-gradient(circle at 35% 35%, #fff, #888 60%, #444); }
    .shine { position: absolute; inset: 1.2vh; border-radius: 50%; pointer-events: none;
             background: conic-gradient(from 20deg, transparent 0 8%, rgba(255,255,255,.10) 12%, transparent 18% 50%,
                         rgba(255,255,255,.08) 62%, transparent 68%); }
    .arm { position: absolute; right: 5.5vh; top: 4vh; width: 6vh; height: 6vh; border-radius: 50%;
           background: radial-gradient(circle at 40% 35%, #d9d9d9, #7b7b7b 55%, #3d3d3d);
           box-shadow: 0 .8vh 1.6vh rgba(0,0,0,.6); transform-origin: 50% 50%; transform: rotate(24deg);
           transition: transform 1.6s cubic-bezier(.3,.8,.3,1); }
    .paused .arm { transform: rotate(4deg); }
    .arm::before { content: ""; position: absolute; left: 2.4vh; top: 3vh; width: 1vh; height: 34vh; border-radius: .5vh;
                   background: linear-gradient(90deg, #8a8a8a, #e6e6e6 45%, #8a8a8a); transform-origin: 50% 0; transform: rotate(8deg); }
    .arm::after { content: ""; position: absolute; left: .2vh; top: 35.5vh; width: 3.2vh; height: 5vh; border-radius: .6vh;
                  background: linear-gradient(#cfcfcf, #6d6d6d); transform: rotate(8deg) translateX(-5.6vh); }
    .meta { position: relative; min-width: 0; }
    .meta .eyebrow { font-size: 2.2vh; letter-spacing: .2em; text-transform: uppercase; color: #1ed760; font-weight: 700; }
    .meta h1 { margin: 1.6vh 0 1vh; font-size: 7vh; line-height: 1.05; font-weight: 750; letter-spacing: -.02em;
               display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
    .meta p { font-size: 3.4vh; color: rgba(255,255,255,.82); font-weight: 500; }
    .meta p + p { font-size: 2.6vh; color: rgba(255,255,255,.55); margin-top: .8vh; }
    .eq { display: inline-flex; gap: .5vh; align-items: flex-end; height: 2.2vh; margin-right: 1.2vh; vertical-align: -.2vh; }
    .eq i { width: .5vh; background: #1ed760; border-radius: .3vh; animation: eq 1s ease-in-out infinite; }
    .eq i:nth-child(2) { animation-delay: -.4s; } .eq i:nth-child(3) { animation-delay: -.7s; }
    .paused .eq i { animation-play-state: paused; }
    @keyframes eq { 0%, 100% { height: 30%; } 50% { height: 100%; } }
    @keyframes spin { to { transform: rotate(360deg); } }
  `;
  const ICONS = {
    speaker: ["M4 9h4l5-4v14l-5-4H4z", "M16.5 8.5a5 5 0 0 1 0 7M19 6a8.5 8.5 0 0 1 0 12"],
    muted: ["M4 9h4l5-4v14l-5-4H4z", "M17 9l5 6M22 9l-5 6"],
    phone: ["M9.5 2.5h5A2.5 2.5 0 0 1 17 5v14a2.5 2.5 0 0 1-2.5 2.5h-5A2.5 2.5 0 0 1 7 19V5a2.5 2.5 0 0 1 2.5-2.5z", "M11 18.5h2"],
    update: ["M20 12a8 8 0 1 1-2.34-5.66", "M20 4v5h-5"],
    info: ["M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18z", "M12 11v5M12 8h.01"],
  };
  const SVG = "http://www.w3.org/2000/svg";

  function el(tag, cls, parent) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (parent) parent.appendChild(node);
    return node;
  }
  function svgIn(parent) {
    const svg = document.createElementNS(SVG, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    parent.appendChild(svg);
    return svg;
  }
  function icon(svg, name) {
    svg.replaceChildren(...(ICONS[name] || ICONS.info).map((d) => {
      const path = document.createElementNS(SVG, "path");
      path.setAttribute("d", d);
      return path;
    }));
  }

  let host = null, ui = null;
  function ensure() {
    if (ui) return mount(), ui;
    host = document.createElement("tvbox-overlay");
    const root = host.attachShadow({ mode: "closed" });
    el("style", "", root).textContent = CSS;
    const layer = el("div", "layer", root);
    const u = (ui = { layer });

    // screensaver
    u.saver = el("div", "saver", layer);
    u.canvas = el("canvas", "", u.saver);
    u.tt = el("div", "tt", u.saver);
    u.ttBg = el("div", "tt-bg", u.tt);
    const deck = el("div", "deck", u.tt);
    const platter = el("div", "platter", deck);
    const record = el("div", "record", platter);
    u.label = el("div", "label", record);
    el("div", "shine", platter);
    el("div", "spindle", platter);
    el("div", "arm", deck);
    const meta = el("div", "meta", u.tt);
    const eyebrow = el("div", "eyebrow", meta);
    const eq = el("span", "eq", eyebrow);
    el("i", "", eq); el("i", "", eq); el("i", "", eq);
    u.source = el("span", "", eyebrow);
    u.title = el("h1", "", meta);
    u.artist = el("p", "", meta);
    u.album = el("p", "", meta);
    u.clock = el("div", "clock", u.saver);
    u.clockTime = el("b", "", u.clock);
    u.clockDate = el("span", "", u.clock);

    // messages
    u.vol = el("div", "pill vol", layer);
    u.volIcon = svgIn(u.vol);
    u.fill = el("div", "fill", el("div", "bar", u.vol));
    u.num = el("span", "num", u.vol);
    u.toast = el("div", "pill toast", layer);
    u.toastIcon = svgIn(u.toast);
    u.toastText = el("span", "text", u.toast);
    u.updating = el("div", "updating", layer);
    el("div", "spinner", u.updating);
    el("div", "", u.updating).textContent = "Updating Luminara…";
    el("small", "", u.updating).textContent = "The TV will restart in a moment";
    u.dot = el("div", "dot", layer);
    mount();
    return u;
  }

  // A fullscreen element sits in the top layer; the overlay must live inside it to stay visible.
  function mount() {
    const parent = document.fullscreenElement || document.body || document.documentElement;
    if (host && parent && host.parentNode !== parent) parent.appendChild(host);
  }
  document.addEventListener("fullscreenchange", mount);

  const timers = {};
  function flash(node, key, ms) {
    node.classList.add("show");
    clearTimeout(timers[key]);
    timers[key] = setTimeout(() => node.classList.remove("show"), ms);
  }

  // ---- idle tracking & screensaver ----------------------------------------------

  let lastInput = Date.now();
  let saverOn = false;

  function onInput(e) {
    lastInput = Date.now();
    if (!saverOn) return;
    stopSaver();
    if (e.type === "keydown") {  // the waking key shouldn't also act on the page
      e.preventDefault();
      e.stopImmediatePropagation();
    }
  }
  for (const type of ["keydown", "mousedown", "wheel", "touchstart"]) addEventListener(type, onInput, true);

  // Chromium fires synthetic mousemoves when the layout changes under a still pointer
  // (e.g. the screensaver appearing); only real movement counts as input.
  let mouseX = -1, mouseY = -1;
  addEventListener("mousemove", (e) => {
    if (Math.abs(e.clientX - mouseX) + Math.abs(e.clientY - mouseY) < 3) return;
    mouseX = e.clientX;
    mouseY = e.clientY;
    onInput(e);
    const { dot } = ensure();
    dot.style.transform = `translate(${e.clientX}px,${e.clientY}px)`;
    dot.style.opacity = "1";
    clearTimeout(timers.dot);
    timers.dot = setTimeout(() => (dot.style.opacity = "0"), 3000);
  }, { capture: true, passive: true });

  const onSpotify = /(^|\.)spotify\.com$/.test(location.hostname);

  function videoPlaying() {
    if (onSpotify) return false;  // Spotify's looping "Canvas" clips aren't something you watch
    const minArea = innerWidth * innerHeight * 0.15;
    for (const v of document.querySelectorAll("video")) {
      if (v.paused || v.ended || v.readyState < 3 || !v.videoWidth) continue;
      const r = v.getBoundingClientRect();
      if (r.width * r.height >= minArea) return true;
    }
    return false;
  }

  function nowPlaying() {
    const ms = navigator.mediaSession;
    const md = ms && ms.metadata;
    if (!md || !md.title) return null;
    for (const m of started) if (!m.isConnected && !m.currentSrc) started.delete(m);  // discarded players
    const media = [...new Set([...document.querySelectorAll("audio, video"), ...started])];
    // Spotify leaves playbackState at "none", so the elements decide when it isn't set.
    const playing = ms.playbackState === "playing" || media.some((m) => !m.paused && !m.ended);
    const paused = ms.playbackState === "paused" || media.some((m) => m.paused && !m.ended && m.currentTime > 0);
    if (!playing && !paused) return null;
    const art = [...(md.artwork || [])].sort((a, b) => parseInt(b.sizes) - parseInt(a.sizes))[0];
    return { title: md.title, artist: md.artist, album: md.album, art: art && art.src, playing,
             source: onSpotify ? "Playing on Spotify" : "Now playing" };
  }

  setInterval(() => {
    if (saverOn || !cfg.saverMinutes || document.visibilityState !== "visible") return;
    if (Date.now() - lastInput < cfg.saverMinutes * 60000) return;
    const music = nowPlaying();
    if (music && music.playing) startSaver();
    else if (!videoPlaying()) startSaver();
  }, 5000);

  let rafId = 0, startedAt = 0, metaTimer = 0, lastFrame = 0;

  function startSaver() {
    const u = ensure();
    saverOn = true;
    startedAt = performance.now();
    u.saver.classList.remove("leaving");
    u.clock.style.display = cfg.saverClock ? "" : "none";
    dots = [];  // dots drift in from all over the screen
    refreshSaver();
    metaTimer = setInterval(refreshSaver, 2000);  // track changes, clock
    rafId = requestAnimationFrame(draw);
    requestAnimationFrame(() => u.saver.classList.add("on"));
  }

  function stopSaver() {
    const u = ensure();
    saverOn = false;
    clearInterval(metaTimer);
    u.saver.classList.add("leaving");
    u.saver.classList.remove("on");
    setTimeout(() => { if (!saverOn) { cancelAnimationFrame(rafId); rafId = 0; } }, 800);
  }

  let artShown = "";
  function refreshSaver() {
    const u = ui;
    const now = new Date();
    const t12 = clock12(now);
    u.clockTime.textContent = `${t12.time} ${t12.suffix}`;
    u.clockDate.textContent = now.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" });
    const music = nowPlaying();
    u.saver.classList.toggle("music", !!music);
    if (!music) {
      if (saverOn && !rafId) rafId = requestAnimationFrame(draw);  // music stopped: back to the dots
      return;
    }
    u.saver.classList.toggle("paused", !music.playing);
    u.source.textContent = music.source;
    u.title.textContent = music.title;
    u.artist.textContent = music.artist || "";
    u.album.textContent = music.album || "";
    if (music.art && music.art !== artShown) {
      artShown = music.art;
      const css = `url("${music.art.replace(/"/g, "%22")}")`;
      u.label.style.backgroundImage = css;
      u.ttBg.style.backgroundImage = css;
    }
  }

  // ---- particle clock -----------------------------------------------------------
  // The time is drawn on a 5x7 grid per character, each lit cell filled with a cluster
  // of tiny particles. The particle count is fixed: when the time changes every particle
  // glides to a spot in the new digits and spares tuck in behind others, so particles
  // never appear or vanish.
  const GLYPHS = {
    0: ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    1: ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    2: ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    3: ["11111", "00010", "00100", "00010", "00001", "10001", "01110"],
    4: ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    5: ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    6: ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    7: ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    8: ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    9: ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    ":": ["0", "0", "1", "0", "1", "0", "0"],
    A: ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    P: ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    M: ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    // The rest of the alphabet the date needs (day and month names, in capitals).
    B: ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    C: ["01110", "10001", "10000", "10000", "10000", "10001", "01110"],
    D: ["11100", "10010", "10001", "10001", "10001", "10010", "11100"],
    E: ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    F: ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    G: ["01110", "10001", "10000", "10111", "10001", "10001", "01111"],
    H: ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    I: ["01110", "00100", "00100", "00100", "00100", "00100", "01110"],
    J: ["00111", "00010", "00010", "00010", "00010", "10010", "01100"],
    L: ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    N: ["10001", "10001", "11001", "10101", "10011", "10001", "10001"],
    O: ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    R: ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    S: ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    T: ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    U: ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    V: ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
    W: ["10001", "10001", "10001", "10101", "10101", "10101", "01010"],
    Y: ["10001", "10001", "01010", "00100", "00100", "00100", "00100"],
    " ": ["000", "000", "000", "000", "000", "000", "000"],
  };
  const SUFFIX_SCALE = 0.42;  // AM/PM drawn smaller, like a superscript
  const SUB = 4;              // particles per cell side: each lit cell is a SUB x SUB cluster
  const DATE_CELL = 1.2;      // a date cell (one particle) is this many clock particles wide
  const DAYS = ["SUNDAY", "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY"];
  const MONTHS = ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST",
                  "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"];
  const dateText = (d) => `${DAYS[d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()]}`;

  function clock12(d) {
    const h = d.getHours() % 12 || 12;
    return { time: `${h}:${String(d.getMinutes()).padStart(2, "0")}`, suffix: d.getHours() < 12 ? "AM" : "PM" };
  }

  // Grid cells (in character units) lit for a string: [{c, r}] plus its width in cells.
  function cells(text) {
    const out = [];
    let x = 0;
    for (const ch of text) {
      const g = GLYPHS[ch] || GLYPHS[" "];
      g.forEach((row, r) => [...row].forEach((bit, c) => { if (bit === "1") out.push({ c: x + c, r }); }));
      x += g[0].length + 1;
    }
    return { cells: out, width: x - 1 };
  }

  // Enough particles for the busiest time of day and the busiest date, so the count
  // never changes. Worked out on first use, not on every page load.
  let CLOCK_MAX = 0, DATE_MAX = 0;
  function dotCount() {
    if (!CLOCK_MAX) {
      for (let m = 0; m < 24 * 60; m++) {
        const t = clock12(new Date(2000, 0, 1, Math.floor(m / 60), m % 60));
        CLOCK_MAX = Math.max(CLOCK_MAX, cells(t.time).cells.length + cells(t.suffix).cells.length);
      }
      CLOCK_MAX *= SUB * SUB;
      const most = (words) => Math.max(...words.map((x) => cells(String(x)).cells.length));
      DATE_MAX = most(DAYS) + most(Array.from({ length: 31 }, (_, i) => i + 1)) + most(MONTHS);
    }
    return CLOCK_MAX + (cfg.saverClock ? DATE_MAX : 0);
  }

  // Particle positions for the time, and the date under it (one particle per cell),
  // centred on screen together. `size` is a clock cell.
  function layout(t, date, w, h) {
    const main = cells(t.time), suf = cells(t.suffix), day = date ? cells(date) : null;
    const units = main.width + 2.2 + suf.width * SUFFIX_SCALE;
    const size = Math.min((w * 0.8) / units, (h * 0.42) / 7);
    const dc = day ? Math.min((w * 0.86) / day.width, (size / SUB) * DATE_CELL) : 0;
    const gap = day ? size * 1.4 : 0;
    const x0 = (w - units * size) / 2, y0 = (h - (7 * size + gap + 7 * dc)) / 2;
    const pts = [];
    const fill = (ox, oy, cell) => {
      const step = cell / SUB;
      for (let i = 0; i < SUB; i++) {
        for (let j = 0; j < SUB; j++) pts.push({ x: ox + (i + 0.5) * step, y: oy + (j + 0.5) * step, s: step });
      }
    };
    for (const { c, r } of main.cells) fill(x0 + c * size, y0 + r * size, size);
    const sx = x0 + (main.width + 2.2) * size, ss = size * SUFFIX_SCALE;
    for (const { c, r } of suf.cells) fill(sx + c * ss, y0 + r * ss, ss);
    if (day) {
      const dx = (w - day.width * dc) / 2, dy = y0 + 7 * size + gap;
      for (const { c, r } of day.cells) pts.push({ x: dx + (c + 0.5) * dc, y: dy + (r + 0.5) * dc, s: dc });
    }
    return { pts, size };
  }

  let dots = [];       // {x, y, s (current), fx, fy, fs (from), tx, ty, ts (to), t0, dur, key}
  let shown = "", shownW = 0, shownH = 0;
  // What's on screen: the time, plus the date when it's turned on in Settings.
  function stamp(d) {
    const t = clock12(d);
    return { t, date: cfg.saverClock ? dateText(d) : "", key: `${t.time}${t.suffix}|${cfg.saverClock ? dateText(d) : ""}` };
  }
  const ease = (p) => (p < 0.5 ? 4 * p * p * p : 1 - (-2 * p + 2) ** 3 / 2);
  const clamp01 = (v) => Math.min(1, Math.max(0, v));

  // ---- the whirlpool between minutes ---------------------------------------------
  // When the minute changes, every particle spirals into a spinning sphere in the middle
  // of the screen, then flies out into the new time and date.
  const GATHER = 1300, SPIN = 1100, RELEASE = 1700, LAG = 350;  // ms
  let vortex = null;  // {t0, cx, cy, R, ps}

  // Where a particle sits on the sphere `te` ms into the whirlpool. The sphere turns on a
  // tilted axis, faster at its equator than at its poles, so the bands twist like a
  // whirlpool; nearer particles are drawn bigger.
  function onSphere(d, te) {
    const v = vortex, sec = te / 1000;
    const lon = d.lon + sec * (2.4 + sec * 0.9) * (1.5 - 0.9 * Math.abs(Math.sin(d.lat)));
    const x3 = Math.cos(d.lat) * Math.cos(lon), y3 = Math.sin(d.lat), z3 = Math.cos(d.lat) * Math.sin(lon);
    const tilt = 0.45;
    const y = y3 * Math.cos(tilt) - z3 * Math.sin(tilt), z = y3 * Math.sin(tilt) + z3 * Math.cos(tilt);
    return { x: v.cx + v.R * x3, y: v.cy + v.R * y, s: v.ps * (0.55 + 0.45 * (z + 1)) };
  }

  function retarget(now, w, h, mode) {  // mode: "scatter" | "vortex" | "glide"
    const scatter = mode === "scatter";
    const { t, date, key } = stamp(new Date());
    const { pts, size } = layout(t, date, w, h);
    shown = key; shownW = w; shownH = h;
    if (scatter || dots.length !== dotCount()) {  // fresh start: drift in from all over the screen
      dots = Array.from({ length: dotCount() }, () => {
        const x = Math.random() * w, y = Math.random() * h;
        return { x, y, s: size / SUB, key: "", phase: Math.random() * 6.2832 };
      });
    }
    const keyOf = (p) => `${Math.round(p.x)},${Math.round(p.y)}`;
    const free = new Set(dots);
    const plan = new Map();
    // 1. Dots already sitting on a spot that's still lit stay put.
    const byKey = new Map();
    for (const d of dots) if (d.key && !byKey.has(d.key)) byKey.set(d.key, d);
    const open = [];
    for (const p of pts) {
      const d = byKey.get(keyOf(p));
      if (d && free.has(d)) { plan.set(d, p); free.delete(d); } else open.push(p);
    }
    // 2. Each remaining spot takes the nearest free dot.
    for (const p of open) {
      let best = null, bestD = Infinity;
      for (const d of free) {
        const dd = (d.x - p.x) ** 2 + (d.y - p.y) ** 2;
        if (dd < bestD) { bestD = dd; best = d; }
      }
      plan.set(best, p); free.delete(best);
    }
    // 3. Spare dots tuck in behind the nearest lit spot, shimmering in step with the dot
    //    that owns it so they stay hidden.
    const owner = new Map([...plan].map(([d, p]) => [p, d]));
    for (const d of free) {
      let best = pts[0], bestD = Infinity;
      for (const p of pts) {
        const dd = (d.x - p.x) ** 2 + (d.y - p.y) ** 2;
        if (dd < bestD) { bestD = dd; best = p; }
      }
      plan.set(d, best);
      d.phase = owner.get(best).phase;
    }
    if (mode === "vortex") {
      vortex = { t0: now, cx: w / 2, cy: h / 2, R: Math.min(w, h) * 0.2, ps: (size / SUB) * 0.9 };
      for (const [d, p] of plan) {
        Object.assign(d, { fx: d.x, fy: d.y, fs: d.s, tx: p.x, ty: p.y, ts: p.s, key: keyOf(p), t0: undefined,
                           lon: Math.random() * 6.2832, lat: Math.asin(Math.random() * 2 - 1),  // even over the sphere
                           lagIn: Math.random() * LAG, lagOut: Math.random() * LAG });
      }
      return;
    }
    vortex = null;
    for (const [d, p] of plan) {
      const key = keyOf(p);
      if (key === d.key && !scatter) continue;
      Object.assign(d, { fx: d.x, fy: d.y, fs: d.s, tx: p.x, ty: p.y, ts: p.s, key,
                         t0: now + Math.random() * (scatter ? 900 : 350), dur: scatter ? 2600 : 1500 });
    }
  }

  // Shared colour field: hues drift over time and ripple across the screen.
  function field(x, y, s, w, h) {
    const ax = w * (0.5 + 0.35 * Math.cos(s * 0.11)), ay = h * (0.5 + 0.35 * Math.sin(s * 0.13));
    const v = (Math.sin(Math.hypot(x - ax, y - ay) / 90 - s * 1.3) + 1) / 2;  // 0..1
    return { hue: (s * 14 + (x / w) * 140 + (y / h) * 50 + v * 50) % 360, v };
  }

  function draw(t) {
    // Stop the loop when hidden or while the turntable (pure CSS) is showing.
    if ((!saverOn && !ui.saver.classList.contains("leaving")) || ui.saver.classList.contains("music")) {
      rafId = 0;
      return;
    }
    rafId = requestAnimationFrame(draw);
    if (t - lastFrame < 33) return;  // ~30 fps is plenty
    lastFrame = t;
    const c = ui.canvas, ctx = c.getContext("2d");
    const w = innerWidth, h = innerHeight;
    if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
    const now = performance.now();
    if (!dots.length) retarget(now, w, h, "scatter");
    else if (w !== shownW || h !== shownH) retarget(now, w, h, "glide");
    else if (stamp(new Date()).key !== shown) retarget(now, w, h, "vortex");
    const te = vortex ? now - vortex.t0 : 0;
    if (vortex && te > GATHER + SPIN + RELEASE + LAG) {  // whirlpool over: everyone's home
      for (const d of dots) Object.assign(d, { x: d.tx, y: d.ty, s: d.ts });
      vortex = null;
    }
    ctx.clearRect(0, 0, w, h);
    const s = (t - startedAt) / 1000;
    // Hundreds of particles: group them by (rounded) colour and fill each group in one go.
    const batches = new Map();
    for (const d of dots) {
      if (vortex) {
        // In from where it was, round the sphere, then out to its new spot.
        const sp = onSphere(d, te);
        const gin = ease(clamp01((te - d.lagIn) / GATHER));
        const out = ease(clamp01((te - GATHER - SPIN - d.lagOut) / RELEASE));
        const x = d.fx + (sp.x - d.fx) * gin, y = d.fy + (sp.y - d.fy) * gin, sz = d.fs + (sp.s - d.fs) * gin;
        d.x = x + (d.tx - x) * out;
        d.y = y + (d.ty - y) * out;
        d.s = sz + (d.ts - sz) * out;
      } else if (d.t0 !== undefined) {
        const p = Math.min(1, Math.max(0, (now - d.t0) / d.dur));
        const k = ease(p);
        d.x = d.fx + (d.tx - d.fx) * k;
        d.y = d.fy + (d.ty - d.fy) * k;
        d.s = d.fs + (d.ts - d.fs) * k;
      }
      const { hue, v } = field(d.x, d.y, s, w, h);
      const key = `${Math.round(hue / 4) * 4},${Math.round(v * 4)}`;
      let path = batches.get(key);
      if (!path) batches.set(key, (path = new Path2D()));
      // A faint shimmer around each particle's spot, and a gentle pulse.
      const x = d.x + Math.sin(s * 1.6 + d.phase) * d.s * 0.12;
      const y = d.y + Math.cos(s * 1.3 + d.phase) * d.s * 0.12;
      const r = d.s * (0.3 + 0.08 * v);
      path.moveTo(x + r, y);
      path.arc(x, y, r, 0, 6.2832);
    }
    for (const [key, path] of batches) {
      const [hue, v] = key.split(",").map(Number);
      ctx.fillStyle = `hsl(${hue}, 90%, ${52 + v * 3}%)`;
      ctx.fill(path);
    }
  }

  // ---- API for the controller -------------------------------------------------

  window.__tvbox = {
    configure(next) {
      Object.assign(cfg, next);
      lastInput = Date.now();
    },
    osd(msg) {
      const u = ensure();
      if (msg.kind === "volume" && msg.volume != null) {
        u.vol.classList.toggle("muted", !!msg.muted);
        icon(u.volIcon, msg.muted ? "muted" : "speaker");
        u.fill.style.width = `${msg.volume}%`;
        u.num.textContent = msg.muted ? "Muted" : `${msg.volume}`;
        flash(u.vol, "vol", 1800);
      } else if (msg.kind === "toast") {
        icon(u.toastIcon, msg.icon);
        u.toastText.textContent = msg.text || "";
        flash(u.toast, "toast", 3200);
      } else if (msg.kind === "updating") {
        if (saverOn) stopSaver();
        u.updating.classList.add("show");
        clearTimeout(timers.updating);  // fallback if we never hear back
        timers.updating = setTimeout(() => u.updating.classList.remove("show"), 180000);
      } else if (msg.kind === "ready") {  // the controller (re)connected: any update is done
        u.updating.classList.remove("show");
      }
    },
    saver(on) {  // for testing from the console / CDP
      if (on && !saverOn) startSaver();
      if (!on && saverOn) stopSaver();
    },
  };
})();
