#!/usr/bin/python3 -I
"""Wi-Fi settings for the TV box, run as root (installed as /usr/local/sbin/tvbox-wifi).

  tvbox-wifi set        join a network: {"ssid": ..., "password": ...} as JSON on stdin,
                        the result as JSON on stdout. The new network is tried first; if it
                        doesn't work the old one stays, so a typo can't strand the box.
  tvbox-wifi restore    at boot: if the box has no Wi-Fi network saved, bring it back from
                        the copy on the boot partition.

The box's power can be cut at any moment (it's often powered from the TV), which has wiped
saved networks before. So the profile is a NetworkManager keyfile written and flushed to disk
before it takes effect, and a second copy lives on the boot partition, in the file Raspberry Pi
Imager writes there (network-config). WPA2 networks are stored as the key derived from the
password, not the password itself.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

CONNECTIONS = Path("/etc/NetworkManager/system-connections")
BOOT_COPIES = (Path("/boot/firmware/network-config"), Path("/boot/network-config"))
PROFILE_NAME = "Luminara Wi-Fi"
JOIN_SECONDS = 60


def problem_with(ssid, password):
    """Why these can't be a Wi-Fi network's details, or None."""
    if not isinstance(ssid, str) or not isinstance(password, str):
        return "Enter the network name and password."
    if not 1 <= len(ssid.encode()) <= 32:
        return "The network name must be 1 to 32 characters."
    if "\n" in ssid or "\0" in ssid:
        return "That isn't a valid network name."
    if password and not is_key(password) and not (8 <= len(password) <= 63 and password.isascii() and password.isprintable()):
        return "A Wi-Fi password is 8 to 63 characters, with no emoji or accented letters."
    return None


def is_key(password):
    return bool(re.fullmatch(r"[0-9a-fA-F]{64}", password))


def derived_key(ssid, password):
    """The 64-character key WPA2 uses, worked out from the password. Works just like it."""
    return password if is_key(password) else hashlib.pbkdf2_hmac("sha1", password.encode(), ssid.encode(), 4096, 32).hex()


def _keyfile_escape(value):
    return value.replace("\\", "\\\\").replace(" ", "\\s").replace("\t", "\\t")


def keyfile(profile_id, ssid, password, sae=False, hidden=False, autoconnect=True):
    """A NetworkManager profile. The name is written as a list of byte values, the keyfile
    format that's safe for any character."""
    lines = ["[connection]", f"id={PROFILE_NAME}", f"uuid={profile_id}", "type=wifi",
             f"autoconnect={'true' if autoconnect else 'false'}", "",
             "[wifi]", "mode=infrastructure", "ssid=" + "".join(f"{b};" for b in ssid.encode())]
    if hidden:
        lines.append("hidden=true")
    lines.append("")
    if password:
        psk = password if sae else derived_key(ssid, password)
        lines += ["[wifi-security]", f"key-mgmt={'sae' if sae else 'wpa-psk'}", f"psk={_keyfile_escape(psk)}", ""]
    lines += ["[ipv4]", "method=auto", "", "[ipv6]", "method=auto", ""]
    return "\n".join(lines)


def parse_scan(output):
    """`nmcli -t -f SSID,SECURITY dev wifi list` -> {ssid: security}."""
    found = {}
    for line in output.splitlines():
        fields = [f.replace("\\:", ":").replace("\\\\", "\\") for f in re.split(r"(?<!\\):", line)]
        if len(fields) == 2 and fields[0]:
            found[fields[0]] = fields[1]
    return found


def sae_only(security):
    return "SAE" in security and "WPA2" not in security and "WPA1" not in security


def write_durably(path, text, mode=0o600):
    """Written and flushed to disk under a temporary name, then swapped in: a power cut leaves
    the old file or the new one, never an empty one."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    folder = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(folder)
    finally:
        os.close(folder)


def boot_copy():
    return next((p for p in BOOT_COPIES if p.parent.is_dir()), None)


def read_boot_copy(path):
    """(ssid, password) from a network-config file, or None."""
    import yaml
    try:
        doc = yaml.safe_load(path.read_text())
        for wifi in (doc["network"]["wifis"] or {}).values():
            for ssid, ap in (wifi.get("access-points") or {}).items():
                return str(ssid), str((ap or {}).get("password") or "")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, yaml.YAMLError):
        pass
    return None


def updated_boot_copy(existing, ssid, password):
    """network-config's text with this as its one Wi-Fi network, keeping everything else."""
    import yaml
    try:
        doc = yaml.safe_load(existing)
    except yaml.YAMLError:
        doc = None
    if not isinstance(doc, dict) or not isinstance(doc.get("network"), dict):
        doc = {"network": {"version": 2}}
    net = doc["network"]
    wifis = net.get("wifis") if isinstance(net.get("wifis"), dict) else {}
    net["wifis"] = wifis
    wlan = wifis.get("wlan0") if isinstance(wifis.get("wlan0"), dict) else {"dhcp4": True, "optional": True}
    wifis["wlan0"] = wlan
    wlan["access-points"] = {ssid: {"password": derived_key(ssid, password)} if password else {}}
    return yaml.safe_dump(doc, sort_keys=False)


def nmcli(*args, timeout=30):
    try:
        return subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", "timed out")


def wifi_device():
    for line in nmcli("-t", "-f", "DEVICE,TYPE", "device").stdout.splitlines():
        name, _, kind = line.partition(":")
        if kind == "wifi":
            return name
    return None


def scan():
    """{ssid: security} of the networks in range. NetworkManager refuses a new scan right
    after another, and then what it already saw has to do."""
    listing = ("-t", "-f", "SSID,SECURITY", "device", "wifi", "list", "--rescan")
    return parse_scan(nmcli(*listing, "yes").stdout) or parse_scan(nmcli(*listing, "no").stdout)


def wifi_profiles():
    """UUIDs of the saved Wi-Fi networks."""
    out = nmcli("-t", "-f", "UUID,TYPE", "connection", "show").stdout
    return [line.split(":")[0] for line in out.splitlines() if line.endswith(":802-11-wireless")]


def profile_path(profile_id):
    return CONNECTIONS / f"luminara-wifi-{profile_id}.nmconnection"


def install_profile(profile_id, text):
    write_durably(profile_path(profile_id), text)
    return nmcli("connection", "reload")


def remove_profile(profile_id):
    nmcli("connection", "delete", "uuid", profile_id)
    profile_path(profile_id).unlink(missing_ok=True)


def explain(ssid, seen, nm_error):
    if "secrets were required" in nm_error.lower():
        return f"Couldn't join {ssid}: the password was wrong."
    if not seen:
        return f"Couldn't find a network called {ssid}. Check the name, and that it's in range."
    return f"Couldn't join {ssid}."


def set_network(request):
    ssid, password = request.get("ssid"), request.get("password", "")
    problem = problem_with(ssid, password)
    if problem:
        return {"ok": False, "error": problem}
    device = wifi_device()
    if not device:
        return {"ok": False, "error": "This box has no Wi-Fi."}
    # What's on the air decides how to connect. A network that isn't seen may be a hidden one.
    security = scan().get(ssid)
    seen = security is not None
    if seen and security.strip() and not password:
        return {"ok": False, "error": f"{ssid} needs a password."}
    sae = seen and sae_only(security) and bool(password)
    if sae and is_key(password):
        return {"ok": False, "error": f"{ssid} is WPA3-only, which needs the actual password."}

    others = wifi_profiles()
    profile_id = str(uuid.uuid4())
    # Tried with automatic connecting off: if it fails, nothing keeps retrying it.
    install_profile(profile_id, keyfile(profile_id, ssid, password, sae, not seen, autoconnect=False))
    joined = nmcli("--wait", str(JOIN_SECONDS), "connection", "up", "uuid", profile_id, timeout=JOIN_SECONDS + 15)
    if joined.returncode != 0:
        remove_profile(profile_id)
        nmcli("--wait", "30", "device", "connect", device, timeout=45)  # back to the old network
        return {"ok": False, "error": explain(ssid, seen, joined.stderr)}

    # It works: make it the box's network for good, then let go of the old ones.
    install_profile(profile_id, keyfile(profile_id, ssid, password, sae, not seen))
    for old in others:
        remove_profile(old)
    # A WPA3-only network has no derived key, and the boot partition is readable by any user.
    path = boot_copy()
    if path and not sae:
        existing = path.read_text() if path.exists() else ""
        write_durably(path, updated_boot_copy(existing, ssid, password), 0o755)
    return {"ok": True, "ssid": ssid}


def restore():
    for _ in range(30):  # NetworkManager may still be starting
        if nmcli("-t", "-f", "RUNNING", "general").stdout.strip() == "running":
            break
        time.sleep(1)
    if wifi_profiles():
        return "a Wi-Fi network is saved; nothing to restore"
    path = boot_copy()
    saved = read_boot_copy(path) if path and path.exists() else None
    if not saved or problem_with(*saved):
        return "no Wi-Fi network saved, and no usable copy on the boot partition"
    profile_id = str(uuid.uuid4())
    install_profile(profile_id, keyfile(profile_id, saved[0], saved[1]))
    nmcli("--wait", "45", "connection", "up", "uuid", profile_id, timeout=60)  # or it keeps trying by itself
    return f"restored the Wi-Fi network {saved[0]} from the boot partition"


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "set":
        try:
            print(json.dumps(set_network(json.loads(sys.stdin.read()))))
        except (ValueError, AttributeError):
            print(json.dumps({"ok": False, "error": "Bad request."}))
    elif command == "restore":
        print(restore())
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
