#!/usr/bin/env python3
"""ASL3 Deviation Meter - level / deviation meter and live listen stream for an
AllStarLink 3 node.

Audio:   app_rpt outstream, mirrored to UDP 127.0.0.1:47444 by m444-tap.
         With duplex = 0 this carries local RX audio while COS is up and the
         node's TX audio (network + telemetry) otherwise.
Keying:  Asterisk AMI (RptStatus XStat / SawStat) at ~7 Hz tells us which side
         is talking (RX = local RF, TX = network / telemetry) and which linked
         node is keyed. Credentials come from Allmon3's allmon3.ini, so nothing
         new is configured in Asterisk.

HTTP (default :8088):
  /                    meter page          /calibration   calibration page
  /api/status          JSON snapshot (backward compatible with the old meter)
  /api/stream          Server-Sent Events, ~25 updates/s
  /api/audio           raw PCM s16le 8 kHz mono, live (Listen button)
  /api/calibrate       POST, LAN only

Python 3 standard library only.
"""
import array
import configparser
import ipaddress
import json
import math
import os
import queue
import re
import socket
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

VERSION = "2.0.0"
CONF_PATH = os.environ.get("M444_CONF", "/etc/m444-meter.json")
WWW = os.environ.get("M444_WWW", os.path.join(os.path.dirname(os.path.abspath(__file__)), "www"))

DEFAULTS = {
    "node": "",
    "title": "ASL3 Deviation Meter",   # page heading / browser tab
    "http_host": "0.0.0.0",
    "http_port": 8088,
    "udp_port": 47444,
    "listen_enabled": True,          # serve /api/audio
    "push_hz": 25,                   # SSE update rate
    "ami_poll_ms": 150,
    "activity_dbfs": -42.0,          # "audio present" threshold (averages, fallback keying)
    "ami": {},                       # optional override: host, port, user, pass
    "allmon3_ini": "/etc/allmon3/allmon3.ini",
    "cal": {"rx_fs_khz": None, "tx_fs_khz": None},   # kHz deviation at 0 dBFS peak
    "public_url": "",                # optional public address shown by the Bookmark button
}

FRAME = 160                          # 20 ms @ 8 kHz
RATE = 8000
FLOOR_DB = -96.0


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def load_conf():
    c = json.loads(json.dumps(DEFAULTS))
    try:
        with open(CONF_PATH) as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(c.get(k), dict):
                c[k].update(v)
            else:
                c[k] = v
    except FileNotFoundError:
        pass
    except Exception as e:
        log("config error:", e)
    return c


CONF = load_conf()
CONF_LOCK = threading.Lock()


def save_conf():
    with CONF_LOCK:
        tmp = CONF_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(CONF, f, indent=2)
        os.replace(tmp, CONF_PATH)


def db(lin):
    return 20 * math.log10(lin) if lin > 1e-5 else FLOOR_DB


# ---------------------------------------------------------------------------
# Keying state from AMI
# ---------------------------------------------------------------------------
class Keys:
    def __init__(self):
        self.ami_ok = False
        self.rx = False          # RPT_RXKEYED: local receiver COS
        self.tx = False          # RPT_TXKEYED: node is transmitting
        self.talkers = []        # linked nodes currently sending audio to us
        self.links = 0
        self.t = 0.0


KEYS = Keys()


def ami_creds():
    a = CONF.get("ami") or {}
    if a.get("user"):
        return a.get("host", "127.0.0.1"), int(a.get("port", 5038)), a["user"], a.get("pass", "")
    node = str(CONF["node"])
    p = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        p.read(CONF["allmon3_ini"])
        sec = node if p.has_section(node) else next((s for s in p.sections() if p.has_option(s, "user")), None)
        if sec:
            return (p.get(sec, "host", fallback="127.0.0.1"), p.getint(sec, "port", fallback=5038),
                    p.get(sec, "user"), p.get(sec, "pass", fallback=""))
    except Exception as e:
        log("allmon3.ini:", e)
    # last resort: first manager.conf user
    p = configparser.ConfigParser(interpolation=None, strict=False, inline_comment_prefixes=(";",))
    try:
        p.read("/etc/asterisk/manager.conf")
        for s in p.sections():
            if s != "general" and p.has_option(s, "secret"):
                return "127.0.0.1", 5038, s, p.get(s, "secret")
    except Exception:
        pass
    return None


def ami_loop():
    backoff = 1
    while True:
        creds = ami_creds()
        if not creds:
            KEYS.ami_ok = False
            log("AMI: no credentials found (allmon3.ini / manager.conf); using audio-level keying")
            time.sleep(30)
            continue
        host, port, user, pw = creds
        try:
            s = socket.create_connection((host, port), timeout=5)
            f = s.makefile("rb")
            f.readline()                                 # banner

            def send(**kv):
                s.sendall(("".join(f"{k}: {v}\r\n" for k, v in kv.items()) + "\r\n").encode())

            def block():
                lines = []
                while True:
                    ln = f.readline()
                    if not ln:
                        raise ConnectionError("AMI closed")
                    ln = ln.decode("utf-8", "replace").rstrip("\r\n")
                    if not ln:
                        if lines:
                            return lines
                        continue
                    lines.append(ln)

            def reply(aid):
                while True:                              # skip stray events
                    b = block()
                    if any(x == f"ActionID: {aid}" for x in b):
                        return b

            send(Action="Login", Username=user, Secret=pw, Events="off", ActionID="l")
            b = reply("l")
            if not any(x.startswith("Response: Success") for x in b):
                raise PermissionError("AMI login refused: " + " | ".join(b))
            log(f"AMI connected to {host}:{port} as {user}")
            backoff = 1
            node = str(CONF["node"])
            n = 0
            while True:
                t0 = time.monotonic()
                n += 1
                send(Action="RptStatus", Command="XStat", Node=node, ActionID=f"x{n}")
                xs = reply(f"x{n}")
                send(Action="RptStatus", Command="SawStat", Node=node, ActionID=f"s{n}")
                ss = reply(f"s{n}")
                if any(x.startswith("Response: Error") for x in xs):
                    raise RuntimeError("XStat: " + " | ".join(xs))
                var = {}
                for x in xs:
                    if x.startswith("Var: ") and "=" in x:
                        k, v = x[5:].split("=", 1)
                        var[k.strip()] = v.strip()
                talkers, links = [], 0
                for x in ss:
                    if x.startswith("Conn: "):
                        p = x[6:].split()
                        links += 1
                        if len(p) >= 2 and p[1] == "1":
                            talkers.append(p[0])
                KEYS.rx = var.get("RPT_RXKEYED") == "1"
                KEYS.tx = var.get("RPT_TXKEYED") == "1"
                KEYS.talkers, KEYS.links = talkers, links
                KEYS.t = time.time()
                KEYS.ami_ok = True
                dt = CONF["ami_poll_ms"] / 1000 - (time.monotonic() - t0)
                if dt > 0:
                    time.sleep(dt)
        except Exception as e:
            if KEYS.ami_ok or backoff == 1:
                log("AMI:", e)
            KEYS.ami_ok = False
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
        finally:
            try:
                s.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Metering
# ---------------------------------------------------------------------------
class Meter:
    def __init__(self):
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.leftover = b""
        self.carry = array.array("h")
        self.seq = 0
        self.frames = deque(maxlen=250)          # (seq, t, rms_db, peak_lin, dir) ~5 s
        self.wave = deque(maxlen=512)
        self.last_audio = 0.0
        self.peak_hold = (FLOOR_DB, 0.0)         # 3 s peak hold, dBFS
        self.dev_hold = (None, 0.0)              # 2 s deviation hold
        self.cur = None                          # current transmission
        self.dir = None
        self.idle_since = 0.0
        self.fallback_until = 0.0
        self.history = deque(maxlen=10)
        self.listeners = set()
        self.listen_lock = threading.Lock()

    # which side is talking right now
    def direction(self, level_db, now):
        if KEYS.ami_ok and now - KEYS.t < 2:
            if KEYS.rx:
                return "rx"
            if KEYS.tx:
                return "tx"
            return None
        # no AMI: audio threshold with a 1.5 s hang
        if level_db > CONF["activity_dbfs"]:
            self.fallback_until = now + 1.5
        return "audio" if now < self.fallback_until else None

    def fs_khz(self, d):
        cal = CONF["cal"]
        if d == "rx":
            return cal.get("rx_fs_khz")
        if d in ("tx", "audio"):
            return cal.get("tx_fs_khz") if d == "tx" else (cal.get("rx_fs_khz") or cal.get("tx_fs_khz"))
        return None

    def feed(self, data):
        # broadcast raw audio to listeners first (lowest latency)
        if self.listeners:
            with self.listen_lock:
                for q in list(self.listeners):
                    try:
                        q.put_nowait(data)
                    except queue.Full:
                        try:
                            q.get_nowait()
                            q.put_nowait(data)
                        except Exception:
                            pass
        data = self.leftover + data
        if len(data) & 1:
            self.leftover, data = data[-1:], data[:-1]
        else:
            self.leftover = b""
        a = array.array("h")
        a.frombytes(data)
        if sys.byteorder == "big":
            a.byteswap()
        self.carry.extend(a)
        while len(self.carry) >= FRAME:
            fr = self.carry[:FRAME]
            del self.carry[:FRAME]
            self.frame(fr)

    def frame(self, fr):
        now = time.time()
        ss = 0
        pk = 0
        for v in fr:
            ss += v * v
            if v > pk:
                pk = v
            elif -v > pk:
                pk = -v
        rms_db = db(math.sqrt(ss / len(fr)) / 32768)
        peak_lin = min(pk / 32768, 1.0)
        with self.cond:
            self.last_audio = now
            self.wave.extend(fr)
            d = self.direction(rms_db, now)
            self.seq += 1
            self.frames.append((self.seq, now, rms_db, peak_lin, d))
            pdb = db(peak_lin)
            if pdb >= self.peak_hold[0] or now - self.peak_hold[1] > 3:
                self.peak_hold = (pdb, now)
            self.track(d, rms_db, peak_lin, now)
            self.cond.notify_all()

    def window_dev(self, d, now, ms=60):
        fs = self.fs_khz(d)
        if not fs or not d:
            return None
        pk = max((f[3] for f in self.frames if now - f[1] <= ms / 1000), default=0)
        return pk * fs

    def track(self, d, rms_db, peak_lin, now):
        # transmission bookkeeping; a 300 ms gap is needed to end one
        if d:
            self.idle_since = 0
            if self.cur and self.cur["dir"] != d and d != "audio":
                self.close(now)
            if not self.cur:
                self.cur = {"dir": d, "start": now, "n": 0, "sum": 0.0, "max": None, "pk": FLOOR_DB,
                            "talkers": set()}
            c = self.cur
            if d == "tx":
                c["talkers"].update(KEYS.talkers)
            c["pk"] = max(c["pk"], db(peak_lin))
            dev = self.window_dev(d, now)
            if dev is not None:
                if c["max"] is None or dev > c["max"]:
                    c["max"] = dev
                if rms_db > CONF["activity_dbfs"]:
                    c["n"] += 1
                    c["sum"] += dev
                if self.dev_hold[0] is None or dev >= self.dev_hold[0] or now - self.dev_hold[1] > 2:
                    self.dev_hold = (dev, now)
        elif self.cur:
            if not self.idle_since:
                self.idle_since = now
            elif now - self.idle_since > 0.3:
                self.close(self.idle_since)
        self.dir = d

    def close(self, end):
        c, self.cur = self.cur, None
        dur = end - c["start"]
        if dur < 0.4:
            return
        self.history.appendleft({
            "ended": round(end, 2),
            "started": round(c["start"], 2),
            "duration_s": round(dur, 2),
            "direction": c["dir"],
            "talkers": sorted(c["talkers"]),
            "average_deviation_khz": round(c["sum"] / c["n"], 2) if c["n"] else None,
            "max_deviation_khz": round(c["max"], 2) if c["max"] is not None else None,
            "peak_dbfs": round(c["pk"], 1),
        })

    # ---- snapshots ----
    def snapshot(self, since_seq=None, wave_pts=200):
        now = time.time()
        with self.lock:
            online = now - self.last_audio < 2
            if self.frames:
                seq, _, rms_db, peak_lin, d = self.frames[-1]
            else:
                seq, rms_db, peak_lin, d = 0, FLOOR_DB, 0.0, None
            if not online:
                rms_db, peak_lin, d = FLOOR_DB, 0.0, self.direction(FLOOR_DB, now) if KEYS.ami_ok else None
            # every frame since the client's last update, so no transient is missed
            if since_seq is not None:
                fresh = [f for f in self.frames if f[0] > since_seq]
            else:
                fresh = list(self.frames)[-2:]
            lv = [round(f[2], 1) for f in fresh][-12:]
            burst_peak = max((f[3] for f in fresh), default=peak_lin)
            dev = self.window_dev(d, now) if online else None
            dh, dht = self.dev_hold
            if now - dht > 2:
                dh = None
            ph, pht = self.peak_hold
            if now - pht > 3:
                ph = rms_db
            w = list(self.wave)[-wave_pts:]                 # last 25 ms, full rate
            cur = None
            if self.cur:
                c = self.cur
                cur = {"dir": c["dir"], "start": round(c["start"], 2),
                       "max_deviation_khz": round(c["max"], 2) if c["max"] is not None else None,
                       "talkers": sorted(c["talkers"] | (set(KEYS.talkers) if c["dir"] == "tx" else set()))}
            cal = CONF["cal"]
            return seq, {
                "v": VERSION,
                "node": str(CONF["node"]),
                "title": CONF.get("title") or "ASL3 Deviation Meter",
                "public_url": CONF.get("public_url") or "",
                "t": round(now, 3),
                "online": online,
                "ami": KEYS.ami_ok,
                "direction": d,
                "rx_keyed": KEYS.rx if KEYS.ami_ok else None,
                "tx_keyed": KEYS.tx if KEYS.ami_ok else None,
                "talkers": KEYS.talkers if KEYS.ami_ok else [],
                "links": KEYS.links,
                "dbfs": round(rms_db, 1),
                "lv": lv,
                "burst_peak_dbfs": round(db(burst_peak), 1),
                "peak_dbfs": round(ph, 1),
                "deviation_khz": round(dev, 2) if dev is not None else None,
                "max_deviation_khz": round(dh, 2) if dh is not None else None,
                "calibrated": bool(cal.get("rx_fs_khz") or cal.get("tx_fs_khz")),
                "cal": {"rx": cal.get("rx_fs_khz") is not None, "tx": cal.get("tx_fs_khz") is not None},
                "cal_values": {"rx_fs_khz": cal.get("rx_fs_khz"), "tx_fs_khz": cal.get("tx_fs_khz")},
                "tx_active": d is not None,
                "current": cur,
                "tx_history": list(self.history),
                "channel": f"app_rpt outstream · node {CONF['node']}",
                "listen": bool(CONF["listen_enabled"]),
                "waveform": [round(v / 32768, 3) for v in w],
            }

    def calibrate(self, d, khz):
        """Set full-scale kHz so that recent audio on side d reads `khz`."""
        now = time.time()
        with self.lock:
            pk = sorted(f[3] for f in self.frames if now - f[1] <= 2.0 and f[2] > CONF["activity_dbfs"]
                        and (f[4] == d or f[4] == "audio"))
        if len(pk) < 25:
            raise ValueError("Need about 2 seconds of steady tone on that side first")
        ref = pk[len(pk) // 2]                       # median 20 ms peak of a steady tone
        if ref < 0.003:
            raise ValueError("Signal too low to calibrate")
        return khz / ref


METER = Meter()


def udp_loop():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 18)
    s.bind(("127.0.0.1", int(CONF["udp_port"])))
    log(f"audio tap on udp 127.0.0.1:{CONF['udp_port']}")
    while True:
        data, _ = s.recvfrom(65536)
        try:
            METER.feed(data)
        except Exception as e:
            log("feed:", e)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/calibration": ("calibration.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/calibration.js": ("calibration.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}


def is_lan(handler):
    h = handler.headers
    if h.get("X-Forwarded-For") or h.get("CF-Connecting-IP") or h.get("X-Real-IP") or h.get("Forwarded"):
        return False
    try:
        ip = ipaddress.ip_address(handler.client_address[0])
        return ip.is_private or ip.is_loopback
    except ValueError:
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "M444Meter/" + VERSION
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        b = json.dumps(obj, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/status":
            _, snap = METER.snapshot()
            snap["lan"] = is_lan(self)
            return self.send_json(snap)
        if path == "/api/stream":
            return self.stream()
        if path == "/api/audio":
            return self.audio()
        if path in STATIC:
            name, ctype = STATIC[path]
            try:
                with open(os.path.join(WWW, name), "rb") as f:
                    b = f.read()
            except FileNotFoundError:
                return self.send_error(404)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        self.send_error(404)

    def stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        period = 1.0 / max(5, min(50, int(CONF["push_hz"])))
        last_seq = None
        hist_sig = None
        lan = is_lan(self)
        try:
            self.wfile.write(b"retry: 1500\n\n")
            while True:
                t0 = time.monotonic()
                seq, snap = METER.snapshot(last_seq)
                last_seq = seq
                sig = (len(snap["tx_history"]), snap["tx_history"][0]["ended"] if snap["tx_history"] else 0)
                if sig == hist_sig:
                    snap.pop("tx_history")                   # only send history when it changes
                hist_sig = sig
                snap["lan"] = lan
                self.wfile.write(b"data: " + json.dumps(snap, separators=(",", ":")).encode() + b"\n\n")
                self.wfile.flush()
                dt = period - (time.monotonic() - t0)
                if dt > 0:
                    time.sleep(dt)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return

    def audio(self):
        if not CONF["listen_enabled"]:
            return self.send_error(403, "Listening is disabled")
        q = queue.Queue(maxsize=50)                         # ~1 s; older audio is dropped
        self.send_response(200)
        self.send_header("Content-Type", "audio/L16;rate=8000;channels=1")
        self.send_header("X-Audio-Format", "s16le;rate=8000;channels=1")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        with METER.listen_lock:
            METER.listeners.add(q)
        silence = bytes(FRAME * 2 * 5)
        try:
            while True:
                try:
                    d = q.get(timeout=0.5)
                except queue.Empty:
                    d = silence                             # keep the player's clock alive
                if sys.byteorder == "big":
                    a = array.array("h")
                    a.frombytes(d[: len(d) & ~1])
                    a.byteswap()
                    d = a.tobytes()
                self.wfile.write(d)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            with METER.listen_lock:
                METER.listeners.discard(q)

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/calibrate":
            return self.send_error(404)
        if not is_lan(self):
            return self.send_json({"error": "Calibration can only be changed from the local network"}, 403)
        try:
            n = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(min(n, 4096)) or b"{}")
            d = req.get("dir")
            if d not in ("rx", "tx"):
                raise ValueError("dir must be rx or tx")
            key = f"{d}_fs_khz"
            if req.get("reset"):
                CONF["cal"][key] = None
            elif "fs_khz" in req:
                v = float(req["fs_khz"])
                if not 0.5 <= v <= 100:
                    raise ValueError("full-scale value out of range (0.5-100 kHz)")
                CONF["cal"][key] = round(v, 4)
            else:
                khz = float(req.get("khz", 0))
                if not 0.1 <= khz <= 10:
                    raise ValueError("known deviation must be 0.1-10 kHz")
                CONF["cal"][key] = round(METER.calibrate(d, khz), 4)
            save_conf()
            return self.send_json({"ok": True, "cal": CONF["cal"]})
        except Exception as e:
            return self.send_json({"error": str(e)}, 400)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    log(f"{CONF.get('title') or 'ASL3 Deviation Meter'} {VERSION} for node {CONF['node']}")
    threading.Thread(target=udp_loop, daemon=True).start()
    threading.Thread(target=ami_loop, daemon=True).start()
    srv = Server((CONF["http_host"], int(CONF["http_port"])), Handler)
    log(f"http on {CONF['http_host']}:{CONF['http_port']}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
