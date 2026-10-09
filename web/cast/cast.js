// The TV side of screen sharing (server/cast.py). A laptop's picture arrives over WebRTC
// straight from its browser; the server only passes the connection details along and
// switches the TV to this page and back.
"use strict";

const video = document.getElementById("screen");
const note = document.getElementById("note");
let ws = null, pc = null;

function show(title, sub = "") {
  document.getElementById("note-title").textContent = title;
  document.getElementById("note-sub").textContent = sub;
  note.classList.remove("gone");
}

function send(msg) { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg)); }

// H.264 first: the Pi decodes it far more cheaply than VP8/VP9/AV1.
function preferH264(transceiver) {
  const caps = RTCRtpReceiver.getCapabilities && RTCRtpReceiver.getCapabilities("video");
  if (!caps || !transceiver.setCodecPreferences) return;
  const rank = (c) => (/h264/i.test(c.mimeType) ? 0 : /rtx|red|ulpfec/i.test(c.mimeType) ? 2 : 1);
  try { transceiver.setCodecPreferences([...caps.codecs].sort((a, b) => rank(a) - rank(b))); } catch { /* keep defaults */ }
}

async function onOffer(sdp) {
  if (pc) pc.close();
  pc = new RTCPeerConnection({ iceServers: [] });  // same Wi-Fi: no relay servers needed
  pc.onicecandidate = (e) => { if (e.candidate) send({ t: "ice", candidate: e.candidate }); };
  pc.ontrack = (e) => {
    if (!video.srcObject) video.srcObject = new MediaStream();
    video.srcObject.addTrack(e.track);
    // Keep the picture as current as possible rather than smoothing over hiccups.
    if ("jitterBufferTarget" in e.receiver) e.receiver.jitterBufferTarget = 50;
    play();
  };
  pc.onconnectionstatechange = () => {
    if (pc.connectionState === "failed") show("Lost the laptop's screen", "Try sharing again from the remote.");
  };
  video.srcObject = null;
  await pc.setRemoteDescription(sdp);
  for (const t of pc.getTransceivers()) if (t.receiver.track.kind === "video") preferH264(t);
  await pc.setLocalDescription(await pc.createAnswer());
  send({ t: "answer", sdp: pc.localDescription });
}

function play() {
  video.muted = false;  // the kiosk allows sound without a click
  video.play().catch(() => { video.muted = true; video.play().catch(() => {}); });
}
video.addEventListener("playing", () => note.classList.add("gone"));

function connect() {
  ws = new WebSocket(`ws://${location.host}/ws/cast?role=tv`);
  // One message at a time: an ICE candidate must wait for the offer it belongs to.
  let queue = Promise.resolve();
  ws.onmessage = (e) => { queue = queue.then(() => handle(JSON.parse(e.data))); };
  const handle = async (msg) => {
    try {
      if (msg.t === "offer") await onOffer(msg.sdp);
      else if (msg.t === "ice" && pc) await pc.addIceCandidate(msg.candidate);
      else if (msg.t === "stop") { show("Screen sharing ended"); if (pc) pc.close(); pc = null; }
    } catch (err) {
      show("Couldn't show the laptop's screen", String(err.message || err));
    }
  };
  ws.onclose = () => setTimeout(connect, 1000);
}

connect();
