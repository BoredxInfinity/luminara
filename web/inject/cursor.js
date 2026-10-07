// Injected into every page by the TV box. Mouse input from the remote arrives as
// synthetic events that don't move a visible OS cursor, so draw one ourselves.
(() => {
  if (window.top !== window || window.__tvboxCursor) return;
  window.__tvboxCursor = true;

  let dot = null;
  let hideTimer = 0;

  function ensureDot() {
    if (dot) return dot;
    dot = document.createElement("div");
    dot.setAttribute("aria-hidden", "true");
    dot.style.cssText =
      "position:fixed;left:0;top:0;width:22px;height:22px;margin:-11px 0 0 -11px;" +
      "border-radius:50%;background:rgba(255,255,255,.9);border:3px solid rgba(0,0,0,.6);" +
      "box-shadow:0 0 8px rgba(0,0,0,.6);pointer-events:none;z-index:2147483647;" +
      "transition:opacity .25s;opacity:0;will-change:transform";
    mount();
    return dot;
  }

  // A fullscreen element sits in the top layer; the dot must live inside it to stay visible.
  function mount() {
    if (!dot) return;
    const host = document.fullscreenElement || document.body || document.documentElement;
    if (host && dot.parentNode !== host) host.appendChild(dot);
  }

  addEventListener("mousemove", (e) => {
    const d = ensureDot();
    d.style.transform = `translate(${e.clientX}px,${e.clientY}px)`;
    d.style.opacity = "1";
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => (d.style.opacity = "0"), 3000);
  }, { capture: true, passive: true });

  document.addEventListener("fullscreenchange", mount);
})();
