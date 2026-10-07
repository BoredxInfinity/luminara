// Injected into every page by the TV box (top frame only).
// - Draws the remote's cursor: touchpad input arrives as synthetic mouse events,
//   which don't move a visible OS cursor.
// - Shows on-screen messages the controller sends: volume level, toasts.
// Everything lives in a shadow root so the host page's CSS can't touch it, and
// the DOM is built without innerHTML: YouTube and others enforce Trusted Types.
(() => {
  if (window.top !== window || window.__tvbox) return;

  const CSS = `
    :host { all: initial; position: fixed; inset: 0; z-index: 2147483647; pointer-events: none; }
    .layer { position: fixed; inset: 0; pointer-events: none; font: 600 22px/1.2 system-ui, sans-serif; color: #fff; }
    .dot { position: absolute; left: 0; top: 0; width: 26px; height: 26px; margin: -13px 0 0 -13px; border-radius: 50%;
           background: rgba(255,255,255,.92); border: 3px solid rgba(0,0,0,.55); box-shadow: 0 0 10px rgba(0,0,0,.6);
           opacity: 0; transition: opacity .25s; will-change: transform; }
    .pill { position: absolute; display: flex; align-items: center; gap: 16px; padding: 16px 26px; border-radius: 999px;
            background: rgba(14,16,22,.85); box-shadow: 0 10px 40px rgba(0,0,0,.45);
            opacity: 0; transform: translateY(-14px) scale(.96); transition: opacity .2s, transform .25s cubic-bezier(.2,.9,.3,1.2); }
    .pill.show { opacity: 1; transform: none; }
    .vol { top: 48px; right: 56px; min-width: 360px; }
    .toast { top: 48px; left: 50%; translate: -50% 0; }
    .bar { flex: 1; height: 10px; border-radius: 6px; background: rgba(255,255,255,.18); overflow: hidden; }
    .fill { height: 100%; width: 0; border-radius: 6px; background: #fff; transition: width .18s ease-out; }
    .muted .fill { background: #ff5d5d; }
    .num { min-width: 64px; text-align: right; font-variant-numeric: tabular-nums; }
    svg { width: 30px; height: 30px; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; flex: none; }
  `;
  const ICONS = {
    speaker: ["M4 9h4l5-4v14l-5-4H4z", "M16.5 8.5a5 5 0 0 1 0 7M19 6a8.5 8.5 0 0 1 0 12"],
    muted: ["M4 9h4l5-4v14l-5-4H4z", "M17 9l5 6M22 9l-5 6"],
    phone: ["M9.5 2.5h5A2.5 2.5 0 0 1 17 5v14a2.5 2.5 0 0 1-2.5 2.5h-5A2.5 2.5 0 0 1 7 19V5a2.5 2.5 0 0 1 2.5-2.5z", "M11 18.5h2"],
    info: ["M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18z", "M12 11v5M12 8h.01"],
  };
  const SVG = "http://www.w3.org/2000/svg";

  function el(tag, cls, parent) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (parent) parent.appendChild(node);
    return node;
  }
  function icon(svg, name) {
    svg.replaceChildren(...(ICONS[name] || ICONS.info).map((d) => {
      const path = document.createElementNS(SVG, "path");
      path.setAttribute("d", d);
      return path;
    }));
  }
  function svgIn(parent) {
    const svg = document.createElementNS(SVG, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    parent.appendChild(svg);
    return svg;
  }

  let host = null, ui = null;
  function ensure() {
    if (ui) return mount(), ui;
    host = document.createElement("tvbox-overlay");
    const root = host.attachShadow({ mode: "closed" });
    el("style", "", root).textContent = CSS;
    const layer = el("div", "layer", root);
    const dot = el("div", "dot", layer);
    const vol = el("div", "pill vol", layer);
    const volIcon = svgIn(vol);
    const fill = el("div", "fill", el("div", "bar", vol));
    const num = el("span", "num", vol);
    const toast = el("div", "pill toast", layer);
    const toastIcon = svgIn(toast);
    const toastText = el("span", "text", toast);
    ui = { dot, vol, volIcon, fill, num, toast, toastIcon, toastText };
    mount();
    return ui;
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

  addEventListener("mousemove", (e) => {
    const { dot } = ensure();
    dot.style.transform = `translate(${e.clientX}px,${e.clientY}px)`;
    dot.style.opacity = "1";
    clearTimeout(timers.dot);
    timers.dot = setTimeout(() => (dot.style.opacity = "0"), 3000);
  }, { capture: true, passive: true });

  window.__tvbox = {
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
        flash(u.toast, "toast", 2600);
      }
    },
  };
})();
