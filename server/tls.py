"""HTTPS for the remote, so a laptop can share its screen (browsers only allow screen
capture on secure pages).

On first start the box uses mkcert to create its own certificate authority, issues a
certificate for its names (aravbansal.local, its IP, localhost), and then deletes the
authority's private key. Devices trust the authority's public certificate (/ca.crt) once;
with the key gone, that trust can only ever vouch for this box, never for other sites.

A new certificate (and authority) is made only when the old one is nearly expired or the
box's hostname changed; devices then need the new /ca.crt.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import ssl
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("tvbox.tls")

RENEW_AFTER_DAYS = 760  # mkcert certificates last 825 days


@dataclass(frozen=True)
class Certificate:
    cert: Path
    key: Path
    ca: Path         # the authority's public certificate, for devices to trust
    names: tuple[str, ...]

    def context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(self.cert, self.key)
        return ctx


def box_names(ip: str) -> list[str]:
    host = socket.gethostname().split(".")[0].lower()
    names = [f"{host}.local", host, "localhost", "127.0.0.1"]
    if ip and ip != "127.0.0.1":
        names.append(ip)
    return names


def ensure_certificate(data_dir: Path, ip: str) -> Certificate | None:
    """The box's certificate, made with mkcert if needed. None if mkcert isn't installed."""
    tls = data_dir / "tls"
    cert, key, ca, meta_file = tls / "box.crt", tls / "box.key", tls / "ca" / "rootCA.pem", tls / "meta.json"
    try:
        meta = json.loads(meta_file.read_text())
    except (FileNotFoundError, ValueError):
        meta = {}
    host = socket.gethostname().split(".")[0].lower()
    fresh = time.time() - meta.get("issued_at", 0) < RENEW_AFTER_DAYS * 86400
    if cert.exists() and key.exists() and ca.exists() and fresh and meta.get("host") == host:
        # Keep it even if the IP changed since: re-issuing would make every device trust a
        # new authority, and the .local name still matches.
        return Certificate(cert, key, ca, tuple(meta.get("names", [])))

    mkcert = shutil.which("mkcert")
    if not mkcert:
        log.info("mkcert isn't installed; the remote is HTTP only (no screen sharing)")
        return None
    names = box_names(ip)
    shutil.rmtree(tls, ignore_errors=True)  # a new authority: the old key is long gone
    (tls / "ca").mkdir(parents=True)
    tls.chmod(0o700)
    proc = subprocess.run(
        [mkcert, "-cert-file", str(cert), "-key-file", str(key), *names],
        env={**os.environ, "CAROOT": str(tls / "ca")},
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0 or not cert.exists():
        log.warning("mkcert failed: %s", (proc.stderr or proc.stdout).strip()[-300:])
        return None
    # The authority has done its one job; without its key nothing else can be signed.
    (tls / "ca" / "rootCA-key.pem").unlink(missing_ok=True)
    key.chmod(0o600)
    meta_file.write_text(json.dumps({"issued_at": time.time(), "host": host, "names": names}))
    log.info("made an HTTPS certificate for %s", ", ".join(names))
    return Certificate(cert, key, ca, tuple(names))
