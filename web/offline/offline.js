// What's connected, so the screen points at the likely culprit. The controller takes the TV
// off this page as soon as the internet is back.
"use strict";

const LABELS = {
  wifi: { up: "Connected to a network", down: "Not connected" },
  ethernet: { up: "Cable connected", down: "Cable not connected" },
};

function paint(kind, link) {
  const row = document.getElementById(kind);
  row.hidden = !link.present;
  row.classList.toggle("up", link.connected);
  document.getElementById(`${kind}-state`).textContent = LABELS[kind][link.connected ? "up" : "down"];
}

async function refresh() {
  try {
    const status = await (await fetch("/api/network")).json();
    paint("wifi", status.wifi);
    paint("ethernet", status.ethernet);
  } catch { /* the controller is restarting; the next refresh fixes it */ }
}

refresh();
setInterval(refresh, 5000);
