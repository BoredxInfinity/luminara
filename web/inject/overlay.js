// Injected into every page by the TV box, before the page's own scripts.
// window.__tvboxConfig (set just above this script) holds the user's settings.
//
// - Codec steering: hide VP9/AV1 support so sites pick H.264, which the Pi decodes in
//   hardware. VP9/AV1 fall back to slow software decoding and make Netflix/Prime stutter.
// - Screensaver after idle time (never over a playing video): a pulsing colour dot grid,
//   or a turntable spinning the album art when music is playing: on your Spotify account
//   anywhere (the box checks while the screensaver is up, server/spotify.py), or in the page.
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
  // hides the ones sites draw themselves (OverlayScrollbars, used by Spotify's web player).
  try {
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(".os-scrollbar { display: none !important; }");
    document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
  } catch { /* very old engine: leave them */ }

  // Some players (e.g. Spotify's web player) play through an <audio> that's never put in the page, where
  // querySelectorAll can't see it. We run before the page's scripts, so remember every
  // element that starts playing.
  // Held weakly: a player the page throws away can still be freed.
  const started = new Set(), seen = new WeakSet();
  const realPlay = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function (...args) {
    if (!seen.has(this)) { seen.add(this); started.add(new WeakRef(this)); }
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
    .saver canvas { position: absolute; inset: 0; width: 100%; height: 100%; transition: opacity .7s ease; }
    .clock { position: absolute; left: 50%; top: 50%; translate: -50% -50%; text-align: center; color: rgba(255,255,255,.92);
             text-shadow: 0 0 40px rgba(0,0,0,.9), 0 0 12px rgba(0,0,0,.8); }
    .clock b { display: block; font-size: 12vh; font-weight: 200; letter-spacing: -.02em; font-variant-numeric: tabular-nums; }
    .clock span { font-size: 2.6vh; font-weight: 500; color: rgba(255,255,255,.7); }
    .saver:not(.music) .clock { display: none; }  /* the dots are the clock */
    .saver.plain:not(.music) .clock { display: block; }  /* unless there's no WebGL to draw them */
    .music .clock { left: auto; top: 6vh; right: 6vw; translate: none; text-align: right; }
    .music .clock b { font-size: 6vh; }

    /* Turntable (music playing) */
    /* Clock <-> turntable. The turntable is always laid out (so the particles know where
       the record sits) but hidden; .music fades it in, .closing sends it away. */
    .tt { position: absolute; inset: 0; display: flex; align-items: center; gap: 7vw; padding: 0 8vw;
          opacity: 0; visibility: hidden; transition: opacity .8s ease, visibility 0s linear .8s; }
    .music .tt { opacity: 1; visibility: visible; transition: opacity .8s ease; }
    .music canvas { opacity: 0; }
    .saver:not(.music) .record { animation-play-state: paused; }
    .music .deck { animation: deck-in .9s cubic-bezier(.2,.8,.3,1) both; }
    @keyframes deck-in { from { opacity: 0; transform: scale(.9); } }
    .music .meta > * { animation: rise .7s cubic-bezier(.2,.8,.3,1) both; }
    .music .meta > :nth-child(1) { animation-delay: .45s; }
    .music .meta > :nth-child(2) { animation-delay: .55s; }
    .music .meta > :nth-child(3) { animation-delay: .65s; }
    .music .meta > :nth-child(4) { animation-delay: .75s; }
    @keyframes rise { from { opacity: 0; transform: translateY(3vh); } }
    .music .clock { animation: rise .7s .5s cubic-bezier(.2,.8,.3,1) both; }
    .closing .deck { animation: deck-out .6s cubic-bezier(.5,0,.75,0) both; }
    @keyframes deck-out { to { opacity: 0; transform: scale(.88); } }
    .closing .meta > * { animation: sink .45s cubic-bezier(.5,0,.75,0) both; }
    .closing .meta > :nth-child(2) { animation-delay: .05s; }
    .closing .meta > :nth-child(3) { animation-delay: .1s; }
    .closing .meta > :nth-child(4) { animation-delay: .15s; }
    @keyframes sink { to { opacity: 0; transform: translateY(-2vh); } }
    /* The deck glows in the album's average colour on a plain black screen. The glow reaches
       at most spread + blur = 5.3vw past the deck, which sits 8vw from the screen's edge
       (and far more from the top and bottom), so it never touches the edges. */
    .deck { position: relative; isolation: isolate; flex: none; width: 62vh; height: 52vh; border-radius: 3vh;
            background: linear-gradient(160deg, #3a2a1f, #1d1510 60%, #120d0a);
            box-shadow: 0 4vh 9vh rgba(0,0,0,.65), inset 0 .3vh 0 rgba(255,255,255,.08); }
    /* Two glows crossfade when the colour changes: fading opacity is the GPU's job, while
       changing a shadow's colour would repaint the whole deck on every frame. */
    .halo { position: absolute; inset: 0; border-radius: inherit; z-index: -1; opacity: 0; transition: opacity 1.2s ease;
            box-shadow: 0 0 min(7vh, 4.5vw) min(1.2vh, .8vw) var(--glow, hsla(0, 0%, 55%, .35)); }
    .halo.on { opacity: 1; }
    .platter { position: absolute; left: 3.5vh; top: 3.5vh; width: 45vh; height: 45vh; border-radius: 50%;
               background: radial-gradient(circle, #2b2b2b 0 69%, #8d8d8d 70% 71%, #444 72%);
               box-shadow: 0 1.2vh 3vh rgba(0,0,0,.6); perspective: 140vh; }
    /* The disc lifts and flips when the song changes; the record inside it spins. */
    .disc { position: absolute; inset: 0; border-radius: 50%; will-change: transform; }
    .lift { position: absolute; inset: 1.2vh; border-radius: 50%; box-shadow: 0 5vh 7vh rgba(0,0,0,.7); opacity: 0; }
    .swapping .disc { z-index: 2; }  /* above the spindle while it's off the platter */
    .swapping .record { animation-play-state: paused; }
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
    .paused .arm, .swapping .arm { transform: rotate(4deg); }
    .swapping .arm { transition-duration: .6s; }  /* off the record quickly; back on gently */
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
    .eq i { width: .5vh; height: 100%; background: #1ed760; border-radius: .3vh; transform-origin: 50% 100%;
            animation: eq 1s ease-in-out infinite; }
    .eq i:nth-child(2) { animation-delay: -.4s; } .eq i:nth-child(3) { animation-delay: -.7s; }
    .paused .eq i { animation-play-state: paused; }
    @keyframes eq { 0%, 100% { transform: scaleY(.3); } 50% { transform: none; } }  /* transform: no relayout per frame */
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
    const deck = u.deck = el("div", "deck", u.tt);
    u.halos = [el("div", "halo", deck), el("div", "halo", deck)];
    const platter = el("div", "platter", deck);
    u.lift = el("div", "lift", platter);  // the record's shadow while it's lifted off
    u.disc = el("div", "disc", platter);
    const record = el("div", "record", u.disc);
    u.label = el("div", "label", record);
    el("div", "shine", u.disc);
    el("div", "spindle", platter);
    el("div", "arm", deck);
    const meta = u.meta = el("div", "meta", u.tt);
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
    const first = mouseX < 0;
    if (Math.abs(e.clientX - mouseX) + Math.abs(e.clientY - mouseY) < 3) return;
    mouseX = e.clientX;
    mouseY = e.clientY;
    // The first report after a page loads is just where the pointer rests, sent when the
    // layout changes under it (e.g. the turntable appearing), not someone moving it.
    if (first) return;
    onInput(e);
    const { dot } = ensure();
    dot.style.transform = `translate(${e.clientX}px,${e.clientY}px)`;
    dot.style.opacity = "1";
    clearTimeout(timers.dot);
    timers.dot = setTimeout(() => (dot.style.opacity = "0"), 3000);
  }, { capture: true, passive: true });

  function videoPlaying() {
    const minArea = innerWidth * innerHeight * 0.15;
    for (const v of document.querySelectorAll("video")) {
      // Muted autoplaying trailers (site menus) aren't being watched; the screensaver pauses them.
      if (v.paused || v.ended || v.muted || v.readyState < 3 || !v.videoWidth) continue;
      const r = v.getBoundingClientRect();
      if (r.width * r.height >= minArea) return true;
    }
    return false;
  }

  // What's playing on your Spotify account, sent by the box while the screensaver is up
  // (server/spotify.py). It wins over anything the page itself reports.
  let remoteMusic = null;

  function nowPlaying() {
    if (remoteMusic) return remoteMusic;
    const ms = navigator.mediaSession;
    const md = ms && ms.metadata;
    if (!md || !md.title) return null;
    const live = [];
    for (const ref of started) {
      const m = ref.deref();
      if (m && (m.isConnected || m.currentSrc)) live.push(m);
      else started.delete(ref);  // discarded players
    }
    // What you can hear decides, not the site's own playbackState: a muted trailer under a
    // menu (which the screensaver pauses) is no reason to put a record on.
    const media = [...new Set([...document.querySelectorAll("audio, video"), ...live])].filter((m) => !m.muted);
    const playing = media.some((m) => !m.paused && !m.ended);
    const paused = media.some((m) => m.paused && !m.ended && m.currentTime > 0);
    if (!playing && !paused) return null;
    const art = [...(md.artwork || [])].sort((a, b) => parseInt(b.sizes) - parseInt(a.sizes))[0];
    return { title: md.title, artist: md.artist, album: md.album, art: art && art.src, playing,
             source: playing ? "Now playing" : "Paused" };
  }

  let beat = 0;
  setInterval(() => {
    // While it's up, keep telling the box (every 15 s), so it picks Spotify checks back up
    // if it ever lost track, e.g. after its connection to Chromium blipped.
    if (saverOn && ++beat % 3 === 0) signal({ saver: true });
    if (saverOn || !cfg.saverMinutes || document.visibilityState !== "visible") return;
    if (Date.now() - lastInput < cfg.saverMinutes * 60000) return;
    // Never over a video someone is watching. (A playing video also counts as "now playing"
    // music to the page, so that can't be what decides; music without a picture still
    // brings the screensaver up, as the turntable.)
    if (!videoPlaying()) startSaver();
  }, 5000);

  let rafId = 0, startedAt = 0, metaTimer = 0, lastFrame = 0;

  // Muted autoplaying trailers (Netflix, Prime, JioHotstar menus) keep decoding video
  // under the screensaver for nobody: pause them while it's up.
  let pausedUnder = [];
  function pauseTrailers() {
    pausedUnder = [...document.querySelectorAll("video")].filter((v) => v.muted && !v.paused);
    for (const v of pausedUnder) v.pause();
  }
  function resumeTrailers() {
    for (const v of pausedUnder) if (v.isConnected && v.paused) v.play().catch(() => {});
    pausedUnder = [];
  }

  // Tell the box, so it checks Spotify only while the screensaver is up.
  function signal(msg) {
    try { if (typeof window.__tvboxSignal === "function") window.__tvboxSignal(JSON.stringify(msg)); } catch { /* not under the box */ }
  }
  signal({ saver: false });  // a page that just loaded has no screensaver up

  function startSaver() {
    const u = ensure();
    saverOn = true;
    trackShown = "";                 // the first song goes straight on, no swap
    mode = "clock";
    session++;
    u.saver.classList.remove("music", "closing", "swapping");
    signal({ saver: true });
    pauseTrailers();
    startedAt = performance.now();
    u.saver.classList.remove("leaving");
    u.clock.style.display = cfg.saverClock ? "" : "none";
    dots = [];  // dots drift in from all over the screen
    shown = "";
    refreshSaver();
    metaTimer = setInterval(refreshSaver, 2000);  // track changes, clock
    rafId = requestAnimationFrame(draw);
    requestAnimationFrame(() => u.saver.classList.add("on"));
  }

  function stopSaver() {
    const u = ensure();
    saverOn = false;
    session++;
    remoteMusic = null;
    signal({ saver: false });
    resumeTrailers();
    clearInterval(metaTimer);
    u.saver.classList.add("leaving");
    u.saver.classList.remove("on");
    setTimeout(() => {
      if (saverOn) return;
      cancelAnimationFrame(rafId);
      rafId = 0;
      gpuRelease();
    }, 800);
  }

  let artShown = "";
  function refreshSaver() {
    const u = ui;
    const now = new Date();
    const t12 = clock12(now);
    u.clockTime.textContent = `${t12.time} ${t12.suffix}`;
    u.clockDate.textContent = now.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" });
    const music = nowPlaying();
    // Mid-transition: leave it alone; the next refresh (every 2 s) catches up.
    if (mode === "to-music" || mode === "to-clock") return;
    if (!music) {
      if (mode === "music") toClock();
      return;
    }
    if (mode === "clock") return toMusic(music);
    u.saver.classList.toggle("paused", !music.playing);
    u.source.textContent = music.source;
    // A new song while the record is on the turntable: swap records.
    if (swapping) pendingTrack = music;
    else if (trackKey(music) !== trackShown) swapRecord(music);
  }


  // ---- clock <-> turntable ---------------------------------------------------------
  let mode = "clock";  // "clock" | "to-music" | "music" | "to-clock"
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));

  function recordCentre() {
    const r = ui.disc.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2, r: r.width / 2 };
  }

  // Music started: the clock's particles stream into where the record will be, the
  // turntable fades in around them as they fade out, the record drops onto the platter,
  // the titles rise in, and the tonearm swings on.
  // Each screensaver session has a number; a transition from a session that has since
  // closed (or restarted) stops instead of changing the new one.
  let session = 0;
  const still = (n) => n === session && saverOn;

  async function toMusic(m) {
    const u = ui, n = session;
    mode = "to-music";
    try {
      showTrack(m);
      u.source.textContent = m.source;
      u.saver.classList.toggle("paused", !m.playing);
      const fromClock = dots.length > 0;  // the screensaver just started: nothing to gather
      if (fromClock) {
        gatherInto(recordCentre());
        await wait(800);
        if (!still(n)) return;
      }
      u.saver.classList.add("swapping");        // tonearm waits off the record
      u.saver.classList.add("music");
      u.disc.animate([{ transform: "translateY(-6vh) scale(1.12)", opacity: 0 },
                      { transform: "translateY(-6vh) scale(1.12)", opacity: 1, offset: 0.35 },
                      { transform: "none", opacity: 1 }],
                     { duration: 900, easing: "cubic-bezier(.5,0,.55,1.3)" });
      await wait(1100);
      if (!still(n)) return;
      u.saver.classList.remove("swapping");     // tonearm swings onto the record
      await wait(600);                          // particles are faded out by now
    } finally {
      if (n === session) {                      // whatever happened, land in a real state
        mode = u.saver.classList.contains("music") ? "music" : "clock";
        u.saver.classList.remove("swapping");
      }
    }
  }

  // Music gone: the tonearm lifts, the record winds down, the titles and turntable sink
  // away, and the particles burst out of the record into the clock.
  async function toClock() {
    const u = ui, n = session;
    mode = "to-clock";
    try {
      u.saver.classList.add("swapping");
      await wait(550);
      if (!still(n)) return;
      u.saver.classList.add("closing");          // titles sink, the deck shrinks away
      await wait(600);
      if (!still(n)) return;
      const c = recordCentre();
      u.saver.classList.remove("music");         // the (now empty) turntable fades; the clock fades in
      burstFrom(c);
      if (saverOn && !rafId) rafId = requestAnimationFrame(draw);
      await wait(900);                           // keep it gone and the arm up until faded
    } finally {
      if (n === session) {
        u.saver.classList.remove("music", "closing", "swapping");
        mode = "clock";
      }
    }
  }

  let trackShown = "", swapping = false, pendingTrack = null;
  const trackKey = (m) => `${m.title}|${m.artist}|${m.art}`;

  function showTrack(m) {
    const u = ui;
    trackShown = trackKey(m);
    u.title.textContent = m.title;
    u.artist.textContent = m.artist || "";
    u.album.textContent = m.album || "";
    if (m.art !== artShown) {
      artShown = m.art || "";
      const css = artShown ? `url("${artShown.replace(/"/g, "%22")}")` : "";
      u.label.style.backgroundImage = css;
      const key = trackShown;
      averageColour(artShown).then((rgb) => {
        if (trackShown === key) glowTo(glowColour(rgb));  // still this song?
      });
    }
  }

  // The album art's average colour, from a 24x24 copy. Spotify art arrives as a data: URL,
  // which a canvas may read; a picture from another site can't be, so it gets the default.
  function averageColour(src) {
    return new Promise((resolve) => {
      if (!src) return resolve(null);
      const img = new Image();
      img.onload = () => {
        try {
          const c = document.createElement("canvas");
          c.width = c.height = 24;
          const g = c.getContext("2d", { willReadFrequently: true });
          g.drawImage(img, 0, 0, 24, 24);
          const d = g.getImageData(0, 0, 24, 24).data;
          let r = 0, gr = 0, b = 0, n = 0;
          for (let i = 0; i < d.length; i += 4) {
            if (d[i + 3] < 128) continue;  // see-through pixels aren't part of the cover
            r += d[i]; gr += d[i + 1]; b += d[i + 2]; n++;
          }
          resolve(n ? [r / n, gr / n, b / n] : null);
        } catch { resolve(null); }
      };
      img.onerror = () => resolve(null);
      img.src = src;
    });
  }

  let halo = 0;
  function glowTo(colour) {
    const [next, prev] = [ui.halos[halo ^ 1], ui.halos[halo]];
    if (prev.classList.contains("on") && prev.style.getPropertyValue("--glow") === colour) return;
    next.style.setProperty("--glow", colour);
    next.classList.add("on");
    prev.classList.remove("on");
    halo ^= 1;
  }

  // Keep the average's hue and saturation, but bring its lightness into a range that glows
  // on black: a dark cover's true average would be an invisible near-black.
  function glowColour(rgb) {
    if (!rgb) return "hsla(0, 0%, 55%, .35)";
    const [r, g, b] = rgb.map((v) => v / 255);
    const max = Math.max(r, g, b), min = Math.min(r, g, b), l = (max + min) / 2, d = max - min;
    let h = 0, s = 0;
    if (d) {
      s = d / (1 - Math.abs(2 * l - 1));
      h = max === r ? ((g - b) / d) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
      h = (h * 60 + 360) % 360;
    }
    const light = Math.min(62, Math.max(42, l * 100));
    return `hsla(${Math.round(h)}, ${Math.round(Math.min(100, s * 100 * 1.15))}%, ${Math.round(light)}%, .6)`;
  }

  // The song changed: the tonearm lifts, the record rises off the platter, flips over to
  // show the new album, drops back down, and the tonearm swings back onto it. The label
  // and titles change while the record is edge-on, so the flip reveals them.
  const LIFT = "translateY(-6vh) scale(1.1)";
  async function swapRecord(m) {
    const u = ui;
    swapping = true;
    trackShown = trackKey(m);
    const step = (frames, duration, easing) =>
      u.disc.animate(frames, { duration, easing, fill: "forwards" }).finished;
    try {
      u.saver.classList.add("swapping");                     // tonearm off, record stops
      await new Promise((r) => setTimeout(r, 650));
      u.meta.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 450, fill: "forwards" });
      u.lift.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 550, fill: "forwards" });
      await step([{ transform: "none" }, { transform: LIFT }], 550, "cubic-bezier(.3,.7,.4,1)");
      await step([{ transform: `${LIFT} rotateY(0deg)` }, { transform: `${LIFT} rotateY(90deg)` }], 320, "ease-in");
      showTrack(m);                                            // edge-on: the other side is the new song
      await step([{ transform: `${LIFT} rotateY(-90deg)` }, { transform: `${LIFT} rotateY(0deg)` }], 320, "ease-out");
      u.meta.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 500, fill: "forwards" });
      u.lift.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 520, fill: "forwards" });
      await step([{ transform: LIFT }, { transform: "none" }], 520, "cubic-bezier(.5,0,.55,1.3)");  // a little bounce
    } catch { /* animation cancelled (screensaver closed): just show the new song */
      showTrack(m);
    } finally {
      for (const a of [...u.disc.getAnimations(), ...u.meta.getAnimations(), ...u.lift.getAnimations()]) a.cancel();
      u.saver.classList.remove("swapping");                  // tonearm swings back onto the record
      swapping = false;
    }
    const next = pendingTrack;
    pendingTrack = null;
    if (next && saverOn && trackKey(next) !== trackShown) swapRecord(next);
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
  const SUB = 2;              // particles per cell side: each lit cell is a SUB x SUB cluster
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

  let dots = [];       // {x, y, s (current), fx, fy, fs (from), tx, ty, ts (to), t0, dur, phase, key}
  let shown = "", shownW = 0, shownH = 0;
  // What's on screen: the time, plus the date when it's turned on in Settings.
  function stamp(d) {
    const t = clock12(d);
    return { t, date: cfg.saverClock ? dateText(d) : "", key: `${t.time}${t.suffix}|${cfg.saverClock ? dateText(d) : ""}` };
  }
  const ease = (p) => (p < 0.5 ? 4 * p * p * p : 1 - (-2 * p + 2) ** 3 / 2);

  // Where each particle is right now (the GPU works this out for drawing; the page only
  // needs it when it sends particles somewhere new).
  function settle(now) {
    for (const d of dots) {
      if (d.t0 === undefined) continue;
      const k = ease(Math.min(1, Math.max(0, (now - d.t0) / d.dur)));
      d.x = d.fx + (d.tx - d.fx) * k;
      d.y = d.fy + (d.ty - d.fy) * k;
      d.s = d.fs + (d.ts - d.fs) * k;
    }
  }

  // Into the turntable: every particle streams into a disc where the record will sit.
  function gatherInto(c) {
    const now = performance.now();
    settle(now);
    for (const d of dots) {
      const a = Math.random() * 6.2832, rr = c.r * 0.92 * Math.sqrt(Math.random());  // even over the disc
      Object.assign(d, { fx: d.x, fy: d.y, fs: d.s, tx: c.x + Math.cos(a) * rr, ty: c.y + Math.sin(a) * rr,
                         ts: d.s * 0.8, key: "", t0: now + Math.random() * 250, dur: 900 });
    }
    dirty = true;
  }

  // Out of the turntable: the particles start packed at the record's centre and the next
  // frame sends each one gliding to its spot in the clock.
  let burstOrigin = null;
  function burstFrom(c) {
    burstOrigin = c;  // used if there are no particles yet (the screensaver began with music)
    for (const d of dots) {
      const a = Math.random() * 6.2832, rr = c.r * 0.35 * Math.random();
      Object.assign(d, { x: c.x + Math.cos(a) * rr, y: c.y + Math.sin(a) * rr, key: "", t0: undefined });
    }
    shown = "";
  }

  function retarget(now, w, h, scatter) {
    const { t, date, key } = stamp(new Date());
    const { pts, size } = layout(t, date, w, h);
    const origin = burstOrigin;  // coming out of the record rather than drifting in
    burstOrigin = null;
    shown = key; shownW = w; shownH = h;
    settle(now);
    if (scatter || dots.length !== dotCount()) {  // fresh start: drift in from all over the screen
      dots = Array.from({ length: dotCount() }, () => {
        const a = Math.random() * 6.2832, rr = origin ? origin.r * 0.35 * Math.random() : 0;
        const x = origin ? origin.x + Math.cos(a) * rr : Math.random() * w;
        const y = origin ? origin.y + Math.sin(a) * rr : Math.random() * h;
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
    const drift = scatter && !origin;
    for (const [d, p] of plan) {
      const key = keyOf(p);
      if (key === d.key && !scatter) continue;
      Object.assign(d, { fx: d.x, fy: d.y, fs: d.s, tx: p.x, ty: p.y, ts: p.s, key,
                         t0: now + Math.random() * (drift ? 900 : 350), dur: drift ? 2600 : 1500 });
    }
    dirty = true;
  }

  // Drawn with WebGL. Each particle's glide (from, to, start, length) and its colour are
  // worked out on the GPU every frame, so the page only uploads new targets when the time
  // changes and a frame costs the Pi's CPU next to nothing. 60 fps while particles glide;
  // the slow shimmer in between moves a fraction of a pixel per frame, so it's drawn at 30.
  const VERTEX = `
    attribute vec3 aFrom, aTo, aTime;  // from x, y, size; to x, y, size; start, length (s), phase
    uniform vec2 uView;                // screen size in CSS pixels
    uniform float uNow, uHue, uScale;  // seconds; hue drift; device pixels per CSS pixel
    uniform vec3 uField;               // the colour field's centre (x, y) and wave offset
    uniform vec2 uShimmer;
    varying vec3 vColour;
    varying float vR, vSize;
    vec3 hsl(float h, float s, float l) {
      vec3 k = mod(vec3(0.0, 8.0, 4.0) + h / 30.0, 12.0);
      return l - s * min(l, 1.0 - l) * clamp(min(k - 3.0, 9.0 - k), -1.0, 1.0);
    }
    void main() {
      float p = clamp((uNow - aTime.x) / aTime.y, 0.0, 1.0);
      float k = p < 0.5 ? 4.0 * p * p * p : 1.0 - pow(-2.0 * p + 2.0, 3.0) / 2.0;
      vec3 d = mix(aFrom, aTo, k);
      float v = (sin(distance(d.xy, uField.xy) / 90.0 - uField.z) + 1.0) / 2.0;
      float hue = mod(uHue + d.x / uView.x * 140.0 + d.y / uView.y * 50.0 + v * 50.0, 360.0);
      vColour = hsl(hue, 0.9, 0.52 + v * 0.03);
      // A faint shimmer around each particle's spot, and a gentle pulse.
      vec2 at = d.xy + vec2(sin(uShimmer.x + aTime.z), cos(uShimmer.y + aTime.z)) * d.z * 0.12;
      vR = d.z * (0.3 + 0.08 * v) * uScale;
      vSize = ceil(vR * 2.0 + 2.0);
      gl_PointSize = vSize;
      gl_Position = vec4(at / uView * vec2(2.0, -2.0) + vec2(-1.0, 1.0), 0.0, 1.0);
    }`;
  const FRAGMENT = `
    precision mediump float;
    varying vec3 vColour;
    varying float vR, vSize;
    void main() {
      float a = clamp(vR + 0.5 - length(gl_PointCoord - 0.5) * vSize, 0.0, 1.0);  // smooth edge
      gl_FragColor = vec4(vColour * a, a);
    }`;

  let gpu = null, dirty = true, moveUntil = 0, timeBase = 0, checkAt = 0;

  function gpuSetup(canvas) {
    const gl = canvas.getContext("webgl", { alpha: false, antialias: false, depth: false, stencil: false,
                                             preserveDrawingBuffer: false, powerPreference: "low-power" });
    if (!gl) return null;
    const shader = (type, src) => {
      const sh = gl.createShader(type);
      gl.shaderSource(sh, src);
      gl.compileShader(sh);
      return sh;
    };
    const prog = gl.createProgram();
    gl.attachShader(prog, shader(gl.VERTEX_SHADER, VERTEX));
    gl.attachShader(prog, shader(gl.FRAGMENT_SHADER, FRAGMENT));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) return null;
    gl.useProgram(prog);
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    ["aFrom", "aTo", "aTime"].forEach((name, i) => {
      const loc = gl.getAttribLocation(prog, name);
      gl.enableVertexAttribArray(loc);
      gl.vertexAttribPointer(loc, 3, gl.FLOAT, false, 36, i * 12);
    });
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.clearColor(0, 0, 0, 1);
    const u = {};
    for (const name of ["uView", "uNow", "uHue", "uScale", "uField", "uShimmer"]) u[name] = gl.getUniformLocation(prog, name);
    // The GPU can drop the context (e.g. its process restarted): start again on a new canvas.
    canvas.addEventListener("webglcontextlost", (e) => {
      e.preventDefault();
      if (gpu && gpu.gl === gl) gpuRelease();
    }, { once: true });
    return { gl, u, data: new Float32Array(0) };
  }

  // Hand the GPU memory back (the canvas's picture and the particles) when the screensaver
  // closes; a fresh canvas is made the next time it opens.
  function gpuRelease() {
    if (!ui) return;
    if (gpu) {
      const lose = gpu.gl.getExtension("WEBGL_lose_context");
      if (lose && !gpu.gl.isContextLost()) lose.loseContext();
      gpu = null;
    }
    const fresh = el("canvas", "");
    ui.canvas.replaceWith(fresh);
    ui.canvas = fresh;
    dirty = true;
  }

  // The particles' glides, in seconds from timeBase (kept recent so 32-bit floats stay exact).
  function upload(now) {
    const { gl } = gpu;
    timeBase = now;
    if (gpu.data.length !== dots.length * 9) gpu.data = new Float32Array(dots.length * 9);
    const f = gpu.data;
    moveUntil = 0;
    dots.forEach((d, i) => {
      const o = i * 9, moving = d.t0 !== undefined;
      f[o] = moving ? d.fx : d.x; f[o + 1] = moving ? d.fy : d.y; f[o + 2] = moving ? d.fs : d.s;
      f[o + 3] = moving ? d.tx : d.x; f[o + 4] = moving ? d.ty : d.y; f[o + 5] = moving ? d.ts : d.s;
      f[o + 6] = moving ? (d.t0 - now) / 1000 : -1; f[o + 7] = moving ? d.dur / 1000 : 1; f[o + 8] = d.phase;
      if (moving) moveUntil = Math.max(moveUntil, d.t0 + d.dur);
    });
    gl.bufferData(gl.ARRAY_BUFFER, f, gl.STATIC_DRAW);
    dirty = false;
  }

  function draw(t) {
    // Stop the loop when hidden or while the turntable (pure CSS) is showing.
    if ((!saverOn && !ui.saver.classList.contains("leaving")) || mode === "music") {
      rafId = 0;
      return;
    }
    rafId = requestAnimationFrame(draw);
    const now = performance.now();
    if (now >= moveUntil && !dirty && t - lastFrame < 30) return;  // 30 fps while nothing glides
    lastFrame = t;
    const w = innerWidth, h = innerHeight;
    // A new layout when the screen size or the minute changes (not while gathering into a record).
    if (!dots.length || !shown || w !== shownW || h !== shownH ||
        (now >= checkAt && mode !== "to-music" && stamp(new Date()).key !== shown)) {
      retarget(now, w, h, !dots.length);
    }
    if (now >= checkAt) checkAt = now + 1000;  // a new minute is looked for once a second
    if (!gpu && !(gpu = gpuSetup(ui.canvas))) {
      ui.saver.classList.add("plain");  // no WebGL: the plain text clock instead
      rafId = 0;
      return;
    }
    const { gl, u } = gpu, c = ui.canvas, scale = devicePixelRatio || 1;
    if (c.width !== Math.round(w * scale) || c.height !== Math.round(h * scale)) {
      c.width = Math.round(w * scale);
      c.height = Math.round(h * scale);
      gl.viewport(0, 0, c.width, c.height);
    }
    if (dirty) upload(now);
    // Everything that drifts with time is worked out here in double precision, then
    // wrapped, so the GPU's 32-bit floats stay exact however long the screensaver runs.
    const s = (now - startedAt) / 1000, TAU = 6.283185307179586;
    gl.uniform2f(u.uView, w, h);
    gl.uniform1f(u.uScale, scale);
    gl.uniform1f(u.uNow, (now - timeBase) / 1000);
    gl.uniform1f(u.uHue, (s * 14) % 360);
    gl.uniform3f(u.uField, w * (0.5 + 0.35 * Math.cos(s * 0.11)), h * (0.5 + 0.35 * Math.sin(s * 0.13)), (s * 1.3) % TAU);
    gl.uniform2f(u.uShimmer, (s * 1.6) % TAU, (s * 1.3) % TAU);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.drawArrays(gl.POINTS, 0, dots.length);
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
    music(m) {  // from the box: {title, artist, album, art, playing, source}, or null
      remoteMusic = m && m.title ? m : null;
      if (saverOn) refreshSaver();
    },
    saver(on) {  // for testing from the console / CDP
      if (on && !saverOn) startSaver();
      if (!on && saverOn) stopSaver();
    },
  };
})();
