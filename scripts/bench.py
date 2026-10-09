"""Measure the TV box on the Pi: CPU, memory and frame rate, through Chromium's DevTools port.

Run on the Pi with the project's Python:
  .venv/bin/python scripts/bench.py cpu 5            CPU (% of one core) per process kind, for 5 s
  .venv/bin/python scripts/bench.py frames 5 ['JS']  frames drawn and dropped over 5 s (Chromium's
                                                     own trace), plus CPU; JS runs in the page as it starts
  .venv/bin/python scripts/bench.py mem              memory (PSS) per process kind, MB
  .venv/bin/python scripts/bench.py eval 'JS'        run JS in the TV page and print the result
  .venv/bin/python scripts/bench.py evalfile FILE    the same, from a file
  .venv/bin/python scripts/bench.py media 20         which video decoder the next player uses
  .venv/bin/python scripts/bench.py shot out.jpg     a screenshot of the TV

For example, the screensaver: `curl -XPOST localhost:8080/api/saver/start`, wait a few
seconds, then `frames 6`. 60 drawn frames a second with no dropped ones is smooth.
"""
import asyncio
import json
import os
import re
import sys
import time

import aiohttp

CDP = "http://127.0.0.1:9222"


def kind(cmd: str) -> str | None:
    if "chromium" in cmd:
        m = re.search(r"--type=(\S+)", cmd)
        if not m:
            return "browser"
        t = m.group(1)
        if t == "utility":
            s = re.search(r"--utility-sub-type=(\S+)", cmd)
            return "util:" + (s.group(1).split(".")[0] if s else "?")
        if t == "renderer" and "--top-chrome-webui" in cmd:
            return "renderer(webui)"
        if t == "renderer" and "--extension-process" in cmd:
            return "renderer(ext)"
        return t
    if "cage" in cmd:
        return "cage"
    if "server.main" in cmd:
        return "controller"
    return None


def procs():
    out = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace")
            k = kind(cmd)
            if not k:
                continue
            st = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
            out[int(pid)] = (k, int(st[11]) + int(st[12]))
        except (FileNotFoundError, ProcessLookupError, IndexError):
            pass
    return out


def system_busy():
    f = open("/proc/stat").readline().split()[1:]
    v = list(map(int, f))
    idle = v[3] + v[4]
    return sum(v) - idle, sum(v)


async def cpu_sample(seconds):
    hz = os.sysconf("SC_CLK_TCK")
    a, (b0, t0) = procs(), system_busy()
    s0 = time.time()
    await asyncio.sleep(seconds)
    b, (b1, t1) = procs(), system_busy()
    el = time.time() - s0
    agg = {}
    for pid, (k, ticks) in b.items():
        prev = a.get(pid, (k, ticks if pid not in a else 0))[1] if pid in a else ticks
        agg[k] = agg.get(k, 0) + (ticks - prev) / hz / el * 100
    total = sum(agg.values())
    rows = sorted(agg.items(), key=lambda x: -x[1])
    return {"by_kind": {k: round(v, 1) for k, v in rows if v >= 0.05}, "ours_total": round(total, 1),
            "system_busy_pct_of_4_cores": round((b1 - b0) / max(1, t1 - t0) * 100, 1)}


async def page_ws(http):
    async with http.get(CDP + "/json/list") as r:
        tabs = await r.json()
    tab = next(t for t in tabs if t["type"] == "page")
    return tab["webSocketDebuggerUrl"], tab["url"]


class Conn:
    def __init__(self, ws):
        self.ws, self.n, self.events = ws, 0, []

    async def call(self, method, params=None):
        self.n += 1
        my = self.n
        await self.ws.send_json({"id": my, "method": method, "params": params or {}})
        while True:
            msg = await self.ws.receive_json(timeout=60)
            if msg.get("id") == my:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})
            self.events.append(msg)


async def evaluate(js):
    async with aiohttp.ClientSession() as http:
        url, _ = await page_ws(http)
        async with http.ws_connect(url, max_msg_size=0) as ws:
            c = Conn(ws)
            r = await c.call("Runtime.evaluate", {"expression": js, "awaitPromise": True, "returnByValue": True})
            return r.get("result", {}).get("value", r)


async def frames(seconds, js=None):
    """Count compositor frames over `seconds` using the browser-wide trace."""
    async with aiohttp.ClientSession() as http:
        async with http.get(CDP + "/json/version") as r:
            browser_ws = (await r.json())["webSocketDebuggerUrl"]
        async with http.ws_connect(browser_ws, max_msg_size=0) as ws:
            c = Conn(ws)
            cats = "disabled-by-default-devtools.timeline.frame,devtools.timeline"
            await c.call("Tracing.start", {"traceConfig": {"includedCategories": cats.split(","),
                                                            "excludedCategories": ["*"]},
                                           "transferMode": "ReportEvents"})
            cpu_task = asyncio.create_task(cpu_sample(seconds))
            if js:
                asyncio.create_task(evaluate(js))
            await asyncio.sleep(seconds)
            cpu = await cpu_task
            await ws.send_json({"id": 999999, "method": "Tracing.end"})
            events = []
            while True:
                msg = await ws.receive_json(timeout=60)
                if msg.get("method") == "Tracing.dataCollected":
                    events += msg["params"]["value"]
                elif msg.get("method") == "Tracing.tracingComplete":
                    break
    counts = {}
    draws = []
    for e in events:
        n = e.get("name")
        if n in ("DrawFrame", "DroppedFrame", "BeginFrame", "Commit", "NeedsBeginFrameChanged", "BeginMainThreadFrame",
                 "ActivateLayerTree", "Screenshot", "PipelineReporter"):
            counts[n] = counts.get(n, 0) + 1
            if n == "DrawFrame":
                draws.append(e["ts"])
    draws.sort()
    gaps = [(b - a) / 1000 for a, b in zip(draws, draws[1:])]
    gaps.sort()
    stats = {}
    if gaps:
        stats = {"draw_fps": round(len(draws) / seconds, 1),
                 "gap_ms_p50": round(gaps[len(gaps) // 2], 1),
                 "gap_ms_p95": round(gaps[int(len(gaps) * 0.95)], 1),
                 "gap_ms_max": round(gaps[-1], 1),
                 "gaps_over_25ms": sum(g > 25 for g in gaps)}
    return {"per_sec": {k: round(v / seconds, 1) for k, v in counts.items()}, **stats, "cpu": cpu}


def mem():
    agg = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace")
            k = kind(cmd)
            if not k:
                continue
            for line in open(f"/proc/{pid}/smaps_rollup"):
                if line.startswith("Pss:"):
                    agg[k] = agg.get(k, 0) + int(line.split()[1]) / 1024
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            pass
    meminfo = dict(l.split(":") for l in open("/proc/meminfo"))
    avail = int(meminfo["MemAvailable"].split()[0]) / 1024
    return {"pss_mb": {k: round(v) for k, v in sorted(agg.items(), key=lambda x: -x[1])},
            "total_pss_mb": round(sum(agg.values())), "mem_available_mb": round(avail)}


async def main():
    cmd = sys.argv[1]
    if cmd == "cpu":
        print(json.dumps(await cpu_sample(float(sys.argv[2])), indent=1))
    elif cmd == "frames":
        print(json.dumps(await frames(float(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else None), indent=1))
    elif cmd == "eval":
        print(json.dumps(await evaluate(sys.argv[2]), indent=1))
    elif cmd == "evalfile":
        print(json.dumps(await evaluate(open(sys.argv[2]).read() + "\n;1"), indent=1))
    elif cmd == "shot":
        async with aiohttp.ClientSession() as http:
            url, _ = await page_ws(http)
            async with http.ws_connect(url, max_msg_size=0) as ws:
                r = await Conn(ws).call("Page.captureScreenshot", {"format": "jpeg", "quality": 80})
        import base64
        open(sys.argv[2], "wb").write(base64.b64decode(r["data"]))
    elif cmd == "media":
        async with aiohttp.ClientSession() as http:
            url, _ = await page_ws(http)
            async with http.ws_connect(url, max_msg_size=0) as ws:
                c = Conn(ws)
                await c.call("Media.enable")
                end = time.time() + float(sys.argv[2])
                props = {}
                while time.time() < end:
                    try:
                        msg = await ws.receive_json(timeout=max(0.1, end - time.time()))
                    except asyncio.TimeoutError:
                        break
                    if msg.get("method") == "Media.playerPropertiesChanged":
                        for p in msg["params"]["properties"]:
                            props[p["name"]] = p["value"]
                    if msg.get("method") == "Media.playerMessagesLogged":
                        for m in msg["params"]["messages"]:
                            if m["level"] in ("error", "warning"):
                                print("MSG", m["level"], m["message"][:150])
                c.events.clear()
                for k in ("kVideoDecoderName", "kIsPlatformVideoDecoder", "kAudioDecoderName", "kVideoTracks", "kFrameUrl", "kResolution", "kIsVideoDecryptingDemuxerStream"):
                    if k in props: print(k, "=", str(props[k])[:200])
    elif cmd == "mem":
        print(json.dumps(mem(), indent=1))


asyncio.run(main())
