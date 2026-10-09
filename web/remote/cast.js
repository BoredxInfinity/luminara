// Screen sharing from this computer to the TV. The picture goes straight from this
// browser to the TV's Chromium over WebRTC; /ws/cast (server/cast.py) only passes the
// connection details along and switches the TV to its receiver page.
// Browsers only allow screen capture on secure pages, so on plain HTTP this panel walks
// through trusting the box's certificate (/ca.crt) and opening the HTTPS remote instead.
"use strict";

(() => {
  const sheet = $("cast");
  const canShare = window.isSecureContext && !!navigator.mediaDevices?.getDisplayMedia;
  // Phones and tablets can't share their screen from a web page; computers can.
  const computer = canShare || matchMedia("(pointer: fine)").matches;
  $("cast-btn").hidden = !computer;

  // Text stays sharp at 1080p with fewer frames; video gets 720p at 30 fps. Both keep
  // the Pi's decoding work bounded.
  const MODES = {
    text: { label: "Text & slides", width: 1920, height: 1080, fps: 15, hint: "detail",
            bitrate: 5_000_000, degradation: "maintain-resolution" },
    video: { label: "Video", width: 1280, height: 720, fps: 30, hint: "motion",
             bitrate: 6_000_000, degradation: "maintain-framerate" },
  };
  let mode = "text";
  try { mode = MODES[localStorage.getItem("tvbox-cast-mode")] ? localStorage.getItem("tvbox-cast-mode") : "text"; } catch { /* storage unavailable */ }

  let stream = null, pc = null, sock = null, sharing = false;

  // ---- panel -------------------------------------------------------------------

  $("cast-btn").addEventListener("click", () => { buzz(); paintPanel(); sheet.showModal(); });
  $("cast-close").addEventListener("click", () => sheet.close());
  sheet.addEventListener("click", (e) => { if (e.target === sheet) sheet.close(); });

  function status(text, kind = "") {
    const el = $("cast-status");
    el.textContent = text;
    el.className = `cast-status ${kind}`;
  }

  function paintMode() {
    $("cast-mode").replaceChildren(...Object.entries(MODES).map(([key, m]) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = m.label;
      b.setAttribute("aria-pressed", String(key === mode));
      b.addEventListener("click", () => {
        buzz();
        mode = key;
        try { localStorage.setItem("tvbox-cast-mode", key); } catch { /* storage unavailable */ }
        paintMode();
        if (sharing) applyMode();
      });
      return b;
    }));
  }

  async function paintPanel() {
    $("cast-ready").hidden = !canShare;
    $("cast-unsupported").hidden = canShare || window.isSecureContext === false;
    $("cast-setup").hidden = $("cast-nocert").hidden = true;
    paintMode();
    if (canShare) return;
    if (window.isSecureContext && !navigator.mediaDevices?.getDisplayMedia) return;  // unsupported browser
    // Plain HTTP: show the one-time setup, if the box has a certificate to offer.
    $("cast-unsupported").hidden = true;
    const info = await fetch("/api/info").then((r) => (r.ok ? r.json() : {}), () => ({}));
    if (!info.secure_url) { $("cast-nocert").hidden = false; return; }
    $("cast-setup").hidden = false;
    $("cast-howto").textContent = howToTrust();
    const secure = $("cast-secure");
    secure.href = info.secure_url;
    secure.textContent = `Open ${new URL(info.secure_url).host}`;
    $("cast-secure-alt").textContent = "Pair it with the PIN on the TV, like any new remote." + (info.secure_ip_url
      ? ` If that address doesn't open, use ${info.secure_ip_url.replace(/\/remote$/, "")} instead.` : "");
  }

  function howToTrust() {
    const ua = navigator.userAgent;
    if (/Firefox\//.test(ua)) {
      return "Firefox keeps its own list: Settings → Privacy & Security → Certificates → View Certificates → " +
             "Authorities → Import, choose the downloaded file and tick “Trust this CA to identify websites”.";
    }
    if (/Mac OS X/.test(ua)) {
      return "Double-click the downloaded file to add it to Keychain Access. Then double-click the new " +
             "“mkcert …” certificate in the login keychain, open Trust, set “When using this certificate” " +
             "to Always Trust and close it (it asks for your password). Restart the browser afterwards.";
    }
    if (/Windows/.test(ua)) {
      return "Open the downloaded file → Install Certificate → Current User → “Place all certificates in the " +
             "following store” → Browse → Trusted Root Certification Authorities → Next → Finish → Yes. " +
             "Restart the browser afterwards.";
    }
    return "Import the downloaded file as a trusted certificate authority in your system or browser " +
           "certificate settings, then restart the browser.";
  }

  function paintSharing() {
    $("cast-start").hidden = sharing;
    $("cast-stop").hidden = !sharing;
    $("cast-btn").classList.toggle("live", sharing);
  }

  // ---- sharing -----------------------------------------------------------------

  const send = (msg) => { if (sock && sock.readyState === WebSocket.OPEN) sock.send(JSON.stringify(msg)); };

  $("cast-start").addEventListener("click", start);
  $("cast-stop").addEventListener("click", () => stop("Stopped."));

  async function start() {
    buzz(12);
    const m = MODES[mode];
    try {
      stream = await navigator.mediaDevices.getDisplayMedia({
        video: { width: { max: m.width }, height: { max: m.height }, frameRate: { ideal: m.fps, max: m.fps } },
        audio: true,                  // a tab's sound (Chrome/Edge ask per tab), or the system's on Windows
        selfBrowserSurface: "exclude",  // sharing this remote page would just show itself
        surfaceSwitching: "include",
        systemAudio: "include",
      });
    } catch (err) {
      status(err.name === "NotAllowedError" ? "Sharing was cancelled." : `Couldn't share: ${err.message}`,
             err.name === "NotAllowedError" ? "" : "bad");
      return;
    }
    const [track] = stream.getVideoTracks();
    track.contentHint = m.hint;
    track.addEventListener("ended", () => stop("Stopped."));  // the browser's own "Stop sharing" bar
    sharing = true;
    paintSharing();
    status("Switching the TV over…");
    open();
  }

  function open() {
    const s = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/cast`);
    // One message at a time: an ICE candidate must wait for the answer it belongs to.
    let queue = Promise.resolve();
    s.onmessage = (e) => { queue = queue.then(() => handle(JSON.parse(e.data))); };
    const handle = async (msg) => {
      try {
        if (msg.t === "ready") await offer();                   // the TV's receiver page is up
        else if (msg.t === "answer") await pc.setRemoteDescription(msg.sdp);
        else if (msg.t === "ice" && pc) await pc.addIceCandidate(msg.candidate);
        else if (msg.t === "ended") {
          stop({ tv: "Stopped on the TV.", replaced: "Another computer started sharing.",
                 "tv-unavailable": "The TV isn't ready; try again in a moment." }[msg.reason] || "Stopped.", true);
        }
      } catch (err) {
        status(`Couldn't connect to the TV: ${err.message}`, "bad");
      }
    };
    s.onclose = () => {
      if (sock !== s || !sharing) return;
      // Lost the box (Wi-Fi, a restart, or this computer was un-paired).
      fetch("/api/state").then((r) => (r.status === 401 ? needPairing() : null), () => {});
      stop("Lost the connection to the TV box.", true);
    };
    sock = s;
  }

  function preferH264(transceiver) {
    const caps = RTCRtpSender.getCapabilities && RTCRtpSender.getCapabilities("video");
    if (!caps || !transceiver.setCodecPreferences) return;
    const rank = (c) => (/h264/i.test(c.mimeType) ? 0 : /rtx|red|ulpfec/i.test(c.mimeType) ? 2 : 1);
    try { transceiver.setCodecPreferences([...caps.codecs].sort((a, b) => rank(a) - rank(b))); } catch { /* keep defaults */ }
  }

  async function offer() {
    if (pc) pc.close();  // the TV's page reloaded: start the connection over
    pc = new RTCPeerConnection({ iceServers: [] });  // same Wi-Fi: no relay servers needed
    for (const track of stream.getTracks()) {
      const t = pc.addTransceiver(track, { direction: "sendonly", streams: [stream] });
      if (track.kind === "video") preferH264(t);
    }
    pc.onicecandidate = (e) => { if (e.candidate) send({ t: "ice", candidate: e.candidate }); };
    pc.onconnectionstatechange = () => {
      const st = pc && pc.connectionState;
      if (st === "connected") status(`Sharing on the TV · ${MODES[mode].label.toLowerCase()}`, "live");
      else if (st === "connecting") status("Connecting to the TV…");
      else if (st === "failed") status("Couldn't reach the TV over the network. Is this computer on the same Wi-Fi?", "bad");
    };
    await pc.setLocalDescription(await pc.createOffer());
    send({ t: "offer", sdp: pc.localDescription });
    applyMode();
  }

  // Frame rate, sharpness and bitrate, changeable while sharing without reconnecting.
  async function applyMode() {
    const m = MODES[mode];
    const track = stream && stream.getVideoTracks()[0];
    if (!track) return;
    track.contentHint = m.hint;
    track.applyConstraints({ width: { max: m.width }, height: { max: m.height }, frameRate: { ideal: m.fps, max: m.fps } })
      .catch(() => {});
    const sender = pc && pc.getSenders().find((s) => s.track === track);
    if (!sender) return;
    const params = sender.getParameters();
    if (!params.encodings || !params.encodings.length) params.encodings = [{}];
    params.encodings[0].maxBitrate = m.bitrate;
    params.encodings[0].maxFramerate = m.fps;
    params.degradationPreference = m.degradation;
    try { await sender.setParameters(params); } catch { /* not every browser takes every setting */ }
    if (pc.connectionState === "connected") status(`Sharing on the TV · ${m.label.toLowerCase()}`, "live");
  }

  function stop(message, fromBox = false) {
    if (!sharing) return;
    sharing = false;
    if (!fromBox) send({ t: "stop" });  // the TV goes back to what it was showing
    if (stream) for (const t of stream.getTracks()) t.stop();
    if (pc) pc.close();
    const s = sock;
    stream = pc = sock = null;
    if (s) s.close();
    paintSharing();
    status(message, message.startsWith("Couldn't") || message.startsWith("Lost") ? "bad" : "");
  }

  addEventListener("pagehide", () => stop("Stopped."));
  paintSharing();
})();
