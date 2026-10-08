// D-pad navigation for websites built for a mouse (Netflix, Prime Video, JioHotstar).
// Injected after overlay.js; runs only on sites listed in window.__tvboxConfig.dpad
// ([{hosts, cards}], from services.json); `cards` selects title cards that the site
// doesn't mark as clickable.
//
// Arrow keys move a focus ring to the nearest clickable thing in that direction and
// Enter clicks it. At the end of a carousel row we press the site's own (often
// invisible until hovered) "next titles" button. While a video fills the screen the
// keys go to the player untouched, so its seek/volume shortcuts keep working.
(() => {
  if (window.top !== window || window.__tvboxDpad) return;
  const host = location.hostname.toLowerCase();
  const site = ((window.__tvboxConfig || {}).dpad || []).find((s) => s.hosts.some((d) => host === d || host.endsWith("." + d)));
  if (!site) return;
  // For poking at from DevTools: what's focusable, and what has the ring.
  window.__tvboxDpad = { candidates: () => candidates().map((c) => c.el), current: () => current };

  const CLICKABLE = 'a[href],button,input:not([type=hidden]),select,textarea,[role=button],[role=link],[role=tab],' +
    '[role=menuitem],[role=option],[role=checkbox],[role=switch],[tabindex]:not([tabindex="-1"])';
  const CARD = "a[href],button,[role=button],[role=link],[role=tab],[role=option],[role=menuitem]";
  const DIALOG ='[role=dialog],[role=alertdialog],[aria-modal=true],dialog[open]';
  const PAGER = { right: /next|more titles|forward|scroll right|^right$/i, left: /previous|prev\b|scroll left|^left$/i };
  const PAGER_CLASS = { right: /swiper-button-next|handleNext|arrow-?right|next-?button/i, left: /swiper-button-prev|handlePrev|arrow-?left|prev-?button/i };
  const SELECTOR = site.cards ? `${CLICKABLE},${site.cards}` : CLICKABLE;
  const isPager = (el, dir) => PAGER[dir].test(el.getAttribute("aria-label") || "") || PAGER_CLASS[dir].test(String(el.className));
  const DIRS = { ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right" };

  let current = null;     // the element with the ring
  let lastRect = null;    // where it was, to recover when the page re-renders it
  let mouseMoved = false; // the touchpad was used since the last D-pad press
  let mouseX = -1, mouseY = -1;
  let busy = false;       // waiting for a row to page / the page to scroll
  let opener = null;      // what Enter was pressed on, to return to when a dialog closes

  // ---- the ring ------------------------------------------------------------------

  let ring = null;
  function ensureRing() {
    if (ring) return ring;
    const hostEl = document.createElement("tvbox-dpad");
    const root = hostEl.attachShadow({ mode: "closed" });
    const style = document.createElement("style");
    style.textContent = `
      :host { all: initial; position: fixed; inset: 0; z-index: 2147483646; pointer-events: none; }
      .ring { position: fixed; left: 0; top: 0; box-sizing: border-box; border: 4px solid #fff; border-radius: 10px;
              box-shadow: 0 0 0 3px rgba(0,0,0,.55), 0 0 26px rgba(255,255,255,.5); opacity: 0;
              transition: transform .14s ease-out, width .14s ease-out, height .14s ease-out, opacity .2s; }
      .ring.on { opacity: 1; }`;
    ring = document.createElement("div");
    ring.className = "ring";
    root.append(style, ring);
    (document.body || document.documentElement).appendChild(hostEl);
    return ring;
  }

  let followUntil = 0;
  function paint() {
    const r = ensureRing();
    if (!current || !current.isConnected || playerMode()) { r.classList.remove("on"); return; }
    const b = current.getBoundingClientRect();
    if (!b.width || !b.height) { r.classList.remove("on"); return; }
    const pad = 4;
    r.style.transform = `translate(${b.left - pad}px,${b.top - pad}px)`;
    r.style.width = `${b.width + pad * 2}px`;
    r.style.height = `${b.height + pad * 2}px`;
    r.classList.add("on");
    lastRect = b;
  }
  // Rows slide and cards grow after a move: track the element for a moment, then stop.
  function follow(ms = 700) {
    const start = !followUntil;
    followUntil = Math.max(followUntil, performance.now() + ms);
    if (!start) return;
    const loop = () => {
      paint();
      if (performance.now() < followUntil) requestAnimationFrame(loop);
      else followUntil = 0;
    };
    requestAnimationFrame(loop);
  }
  addEventListener("scroll", () => current && follow(200), { capture: true, passive: true });
  addEventListener("resize", () => current && follow(200), { passive: true });

  function hide() {
    current = null;
    if (ring) ring.classList.remove("on");
  }

  // Using the touchpad hands control back to the cursor.
  addEventListener("mousemove", (e) => {
    const first = mouseX < 0;
    if (Math.abs(e.clientX - mouseX) + Math.abs(e.clientY - mouseY) < 3) return;  // synthetic, from layout changes
    mouseX = e.clientX;
    mouseY = e.clientY;
    if (first) return;  // where the pointer rests, reported after a layout change
    mouseMoved = true;
    if (current) hide();
  }, { capture: true, passive: true });

  // ---- what can be focused -------------------------------------------------------

  function playerMode() {
    const fs = document.fullscreenElement;
    if (fs && (fs.tagName === "VIDEO" || fs.querySelector("video"))) return true;
    const big = innerWidth * innerHeight * 0.8;
    for (const v of document.querySelectorAll("video")) {
      if (v.ended || !v.videoWidth || v.muted) continue;  // muted = an autoplaying trailer behind the menus
      const r = v.getBoundingClientRect();
      if (r.width * r.height >= big) return true;
    }
    return false;
  }

  function scope() {
    // An open dialog (title details, menus) takes over; the page behind it doesn't count.
    const open = [...document.querySelectorAll(DIALOG)].filter((d) => {
      const r = d.getBoundingClientRect();
      return r.width > 50 && r.height > 50 && d.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true });
    });
    return open.length ? open[open.length - 1] : document;
  }

  function candidates() {
    const vw = innerWidth, vh = innerHeight, maxArea = vw * vh * 0.35;
    const list = [];
    for (const el of scope().querySelectorAll(SELECTOR)) {
      if (el.disabled || el.getAttribute("aria-hidden") === "true") continue;
      if (isPager(el, "left") || isPager(el, "right")) continue;  // pressed for you at the end of a row
      const r = el.getBoundingClientRect();
      if (r.width < 8 || r.height < 8 || r.width * r.height > maxArea) continue;
      // Mostly on screen sideways: carousels hide their overflow off the edges.
      const shown = Math.min(r.right, vw) - Math.max(r.left, 0);
      if (shown < r.width * 0.7) continue;
      if (!el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) continue;
      // On screen: must actually be what you'd click there (not under a menu or clipped).
      if (r.bottom > 0 && r.top < vh) {
        const x = Math.min(Math.max(r.left + r.width / 2, 0), vw - 1);
        const y = Math.min(Math.max(r.top + r.height / 2, 0), vh - 1);
        const hit = document.elementFromPoint(x, y);
        if (hit && hit !== el && !el.contains(hit) && !hit.contains(el)) continue;
      }
      list.push({ el, r });
    }
    // Nested clickables: a link or button wrapping others is a card whose extras only
    // appear on hover, so the card wins. A bare [tabindex] box is just a container.
    const inSet = new Set(list.map((c) => c.el));
    const drop = new Set();
    for (const { el } of list) {
      for (let p = el.parentElement; p; p = p.parentElement) {
        if (!inSet.has(p)) continue;
        drop.add(p.matches(CARD) || (site.cards && p.matches(site.cards)) ? el : p);
        break;
      }
    }
    return list.filter((c) => !drop.has(c.el));
  }

  // ---- choosing the next element -------------------------------------------------

  const centre = (r) => ({ x: r.left + r.width / 2, y: r.top + r.height / 2 });
  const gap = (a1, a2, b1, b2) => Math.max(0, b1 - a2, a1 - b2);  // 0 if the spans overlap

  function best(from, dir, list, inRow = true) {
    const c = centre(from);
    const ahead = [];
    for (const cand of list) {
      const r = cand.r, k = centre(r);
      let is, along, across;
      if (dir === "right") { is = k.x > c.x + 4 && r.left > from.left + 4; along = r.left - from.right; across = gap(from.top, from.bottom, r.top, r.bottom); }
      if (dir === "left") { is = k.x < c.x - 4 && r.right < from.right - 4; along = from.left - r.right; across = gap(from.top, from.bottom, r.top, r.bottom); }
      if (dir === "down") { is = k.y > c.y + 4 && r.top > from.top + 4; along = r.top - from.bottom; across = gap(from.left, from.right, r.left, r.right); }
      if (dir === "up") { is = k.y < c.y - 4 && r.bottom < from.bottom - 4; along = from.top - r.bottom; across = gap(from.left, from.right, r.left, r.right); }
      if (!is) continue;
      const offAxis = dir === "left" || dir === "right" ? Math.abs(k.y - c.y) : Math.abs(k.x - c.x);
      ahead.push({ cand, along: Math.max(0, along), across, offAxis });
    }
    let pool = ahead;
    if (dir === "left" || dir === "right") {
      // Stay in the row (see move() for the exception).
      if (inRow) pool = ahead.filter((a) => a.across === 0);
    } else if (ahead.length) {
      // Up/down stay in the column (content vs a sidebar) when they can...
      const above = ahead.filter((a) => a.across === 0);
      if (above.length) pool = above;
      // ...and go to the next row of titles, skipping the small heading links that sit
      // just above each row. Small things in a row of their own (menus, buttons) count.
      const big = pool.filter((a) => a.cand.r.height >= 60);
      const nearBig = big.length ? Math.min(...big.map((a) => a.along)) : Infinity;
      const leavingRow = from.height >= 60 && dir === "up";
      const filtered = pool.filter((a) => a.cand.r.height >= 60 || (a.along < nearBig - 120 && !(leavingRow && a.along < 60)));
      if (filtered.length) pool = filtered;
      const rowAlong = Math.min(...pool.map((a) => a.along));
      pool = pool.filter((a) => a.along <= rowAlong + 30);
    }
    let pick = null, pickScore = Infinity;
    for (const a of pool) {
      const score = a.along + a.across * 3 + a.offAxis * 0.3;
      if (score < pickScore) { pick = a.cand; pickScore = score; }
    }
    return pick;
  }

  // First press: start under the cursor if it was just used, else top-left of the content.
  function startingPoint(list) {
    if (mouseMoved && mouseX >= 0) {
      const under = list.find(({ r }) => mouseX >= r.left && mouseX <= r.right && mouseY >= r.top && mouseY <= r.bottom);
      if (under) return under;
    }
    const onScreen = list.filter(({ r }) => r.top >= 0 && r.bottom <= innerHeight);
    const pool = onScreen.length ? onScreen : list;
    const target = { x: innerWidth * 0.15, y: innerHeight * 0.45 };
    let pick = null, d = Infinity;
    for (const cand of pool) {
      const k = centre(cand.r);
      const dist = Math.hypot(k.x - target.x, (k.y - target.y) * 1.5);
      if (dist < d) { pick = cand; d = dist; }
    }
    return pick;
  }

  function nearest(rect, list) {
    const c = centre(rect);
    let pick = null, d = Infinity;
    for (const cand of list) {
      const k = centre(cand.r);
      const dist = Math.hypot(k.x - c.x, k.y - c.y);
      if (dist < d) { pick = cand; d = dist; }
    }
    return d < innerHeight / 3 ? pick : null;
  }

  function scrollParent(el) {
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      const oy = getComputedStyle(p).overflowY;
      if ((oy === "auto" || oy === "scroll") && p.scrollHeight > p.clientHeight + 4) return p;
    }
    return null;
  }

  // Keep the focused row comfortably on screen (rows below the fold are already in the DOM).
  function reveal(el) {
    const r = el.getBoundingClientRect();
    const margin = Math.min(160, innerHeight * 0.15);
    if (r.top >= margin && r.bottom <= innerHeight - margin) return;
    const by = r.top + r.height / 2 - innerHeight / 2;
    const box = scrollParent(el);
    if (box) box.scrollBy({ top: by, behavior: "instant" });
    else scrollBy({ top: by, behavior: "instant" });
  }

  function focusOn(el) {
    current = el;
    mouseMoved = false;
    reveal(el);
    // Lets the site show its own focus styles; inputs only get focus on Enter.
    if (!/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) {
      try { el.focus({ preventScroll: true }); } catch { /* not focusable */ }
    }
    paint();
    follow();
  }

  const enabled = (b) => !b.disabled && b.getAttribute("aria-disabled") !== "true" && !/disabled/i.test(String(b.className));

  // The site's own row arrows, nearest to the current element, and the row they move.
  function pagerFor(el, dir) {
    const h = el.getBoundingClientRect().height;
    for (let p = el.parentElement, depth = 0; p && depth < 10; p = p.parentElement, depth++) {
      if (p.getBoundingClientRect().height > Math.max(h * 2.2, h + 150)) return null;  // left the row
      for (const b of p.querySelectorAll("button,[role=button],[aria-label]")) {
        if (b !== el && !b.contains(el) && isPager(b, dir)) return enabled(b) ? { pager: b, row: p } : null;
      }
    }
    return null;
  }

  function sameRow(list, row) {
    return list.filter(({ r }) => gap(row.top, row.bottom, r.top, r.bottom) === 0 && Math.abs(centre(r).y - centre(row).y) < row.height * 0.6);
  }

  const label = (el) => (el && (el.getAttribute("aria-label") || el.textContent || "").trim()) || "";

  // Wait for a row's slide animation to finish (its cards stop moving).
  function settle(row, fn) {
    busy = true;
    const where = () => [...row.querySelectorAll(SELECTOR)].slice(0, 30).map((e) => Math.round(e.getBoundingClientRect().left)).join();
    const first = where();
    let last = first, still = 0, moved = false;
    const started = performance.now();
    const tick = () => {
      const now = where(), age = performance.now() - started;
      moved = moved || now !== first;
      still = now === last ? still + 1 : 0;
      last = now;
      // Some sites wait a moment before sliding: give it time to start, then to stop.
      if ((moved && still >= 3) || (!moved && age > 1200) || age > 2500) {
        busy = false;
        fn();
      } else setTimeout(tick, 80);
    };
    setTimeout(tick, 80);
  }

  function later(ms, fn) {
    busy = true;
    setTimeout(() => { busy = false; fn(); }, ms);
  }

  function move(dir) {
    const list = candidates();
    if (!list.length) return;
    if (!current || !current.isConnected || !list.some((c) => c.el === current)) {
      // First press, a closed dialog, or a re-render: pick up where we were.
      const back = opener && list.find((c) => c.el === opener);
      const start = back || (!mouseMoved && lastRect && nearest(lastRect, list)) || startingPoint(list);
      opener = null;
      if (start) focusOn(start.el);
      return;
    }
    if (!current.closest(DIALOG)) opener = null;  // only needed while a dialog is open
    const from = current.getBoundingClientRect();
    const horizontal = dir === "left" || dir === "right";
    const found = horizontal ? pagerFor(current, dir) : null;
    const next = best(from, dir, list);
    // Leaving the row (e.g. left into a sidebar) waits until the row can't page any further.
    if (next && !(found && !found.row.contains(next.el))) return focusOn(next.el);

    if (horizontal) {
      if (!found) {
        // Alone in its row (a sidebar, a lone button): step across to the nearest thing.
        const back = dir === "left" ? "right" : "left";
        const across = !best(from, back, list) && best(from, dir, list, false);
        if (across) focusOn(across.el);
        return;
      }
      activate(found.pager);
      // Once the row stops sliding, land on the first new title (the far end for left).
      settle(found.row, () => {
        const row = sameRow(candidates().filter((c) => found.row.contains(c.el)), from);
        if (!row.length) return paint();
        row.sort((a, b) => a.r.left - b.r.left);
        if (dir === "left") row.reverse();
        // Netflix keeps the title you were on at the edge of the new page; step past it.
        const was = label(current);
        const fresh = row.find((c) => label(c.el) !== was) || row[0];
        focusOn(fresh.el);
      });
    } else {
      // Nothing more on screen: scroll so lazy-loaded rows appear, then try again.
      const box = scrollParent(current);
      const before = box ? box.scrollTop : scrollY;
      const by = (dir === "down" ? 1 : -1) * innerHeight * 0.6;
      if (box) box.scrollBy({ top: by, behavior: "instant" });
      else scrollBy({ top: by, behavior: "instant" });
      if ((box ? box.scrollTop : scrollY) === before) return;
      later(350, () => {
        const again = best(current.isConnected ? current.getBoundingClientRect() : from, dir, candidates());
        if (again) focusOn(again.el);
        else paint();
      });
    }
  }

  // ---- clicking ------------------------------------------------------------------

  function activate(el) {
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) || el.isContentEditable) {
      el.focus();  // ready for typing from the phone
      el.click();
      return;
    }
    // Click what a real tap at the element's centre would hit: some sites listen on an
    // inner element. Hidden row arrows (no size) just get clicked directly.
    const r = el.getBoundingClientRect();
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    const hit = r.width && r.height ? document.elementFromPoint(x, y) : null;
    const target = hit && el.contains(hit) ? hit : el;
    const at = { bubbles: true, cancelable: true, composed: true, view: window, clientX: x, clientY: y, button: 0 };
    target.dispatchEvent(new PointerEvent("pointerdown", { ...at, pointerType: "mouse", isPrimary: true }));
    target.dispatchEvent(new MouseEvent("mousedown", at));
    target.dispatchEvent(new PointerEvent("pointerup", { ...at, pointerType: "mouse", isPrimary: true }));
    target.dispatchEvent(new MouseEvent("mouseup", at));
    target.dispatchEvent(new MouseEvent("click", { ...at, detail: 1 }));
  }

  // ---- keys ----------------------------------------------------------------------

  function typing(el) {
    return el && (el.isContentEditable || el.tagName === "TEXTAREA" ||
      (el.tagName === "INPUT" && !/^(button|submit|reset|checkbox|radio|range|color|file|image)$/i.test(el.type)));
  }

  addEventListener("keydown", (e) => {
    if (e.altKey || e.ctrlKey || e.metaKey || e.defaultPrevented) return;
    const dir = DIRS[e.key];
    const enter = e.key === "Enter";
    if (!dir && !enter) return;
    if (playerMode()) { if (current) hide(); return; }
    // In a text box, left/right/Enter edit and submit; up/down leave it.
    if (typing(document.activeElement) && (enter || dir === "left" || dir === "right")) return;
    if (enter) {
      if (!current || !current.isConnected || !ring.classList.contains("on")) return;  // nothing chosen: the page has it
      e.preventDefault();
      e.stopImmediatePropagation();
      opener = current;
      activate(current);
      follow(1200);  // dialogs open, rows re-render
      return;
    }
    e.preventDefault();
    e.stopImmediatePropagation();
    if (busy) return;
    if (typing(document.activeElement)) document.activeElement.blur();
    move(dir);
  }, true);

  // A dialog closed (back to the title that opened it), or a single-page app swapped
  // its content and the ring lost its element.
  setInterval(() => {
    if (!current || current.isConnected) return;
    if (opener && opener.isConnected) {
      current = opener;
      opener = null;
      paint();
      return;
    }
    const r = lastRect;
    hide();
    lastRect = r;
  }, 500);
})();
