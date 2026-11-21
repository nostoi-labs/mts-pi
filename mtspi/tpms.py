#!/usr/bin/env python3
"""Listens for Bluetooth LE tyre pressure sensors (valve cap type) next to the bridge. Experimental and switched
off unless asked for (TPMS=on in mtspi.txt): for now it only listens and writes down what it hears. Standard library only.

It never connects and never pairs: the sensors only broadcast, and the Pi's radio listens (passive scan) for a small
share of the time, set by TPMS_WINDOW_MS / TPMS_INTERVAL_MS, because the same radio is busy with the bike and the
iPhone. During a phone call it does not listen at all, and with TPMS_ON_S / TPMS_OFF_S it listens in turns (for
example 60 and 540: one minute every ten). BlueZ is not involved: the scan is asked to the radio directly, and nothing
is written to its storage.

What it writes:
  - one short line on standard output (so in the system journal) for every reading that changes, and at least one
    every TPMS_REPEAT_S seconds per sensor;
  - a file of JSON lines in the log folder (tpms-<date>.jsonl): every sensor broadcast with its raw bytes, plus a
    survey of everything else on the air (first sight of each address, and the addresses that stay around), so that a
    sensor with a format we do not know yet can still be found afterwards.

Tried on the bike on 21 Nov 2026 (two HRTPMS valve caps): both read, values in line with the maker's app. Each sensor
broadcasts every 94.4 s. Listening all the time at 10% caught about half of the broadcasts and could be heard, slightly,
in the music going to the bike over the same radio. Hence the windows (class Windows): after a first search the radio is
asked for only around each expected broadcast. TPMS_PERIOD_S=0 goes back to listening all the time. The windows have
been tried on a simulated ride and on the bench radio, not yet on the bike.
At home (many broadcasters around) the broadcasts heard grow in step with the share: one beacon was heard 67 times in
30 s at 10%, 166 at 25%, 333 at 50%, 438 at 100%; the listener itself uses about 1.5% of one processor core.

Known formats (bytes worked out by the tpms_ble project, github.com/bkbilly/tpms_ble, MIT):
  F  "HRTPMS": service 0xA827, 9 bytes of manufacturer data: battery, temperature, pressure (kPa absolute, 2 bytes)
  H  "TC.TPMS": service 0xA828, the readings start in the two bytes of the company id

Needs CAP_NET_RAW and CAP_NET_ADMIN (see mtspi-tpms.service), or root:  sudo python3 mtspi/tpms.py --seconds 60
"""
import argparse
import fcntl
import json
import os
import select
import signal
import socket
import struct
import sys
import time

HCIGETCONNLIST = 0x800448D4                             # list of the radio's links (the same one hcitool con reads)
OP_SET_PARAM, OP_SET_ENABLE = 0x200B, 0x200C            # LE Set Scan Parameters / Enable
OP_EXT_PARAM, OP_EXT_ENABLE = 0x2041, 0x2042            # the same, "extended" flavour (newer radios)
DISALLOWED = 0x0C


def decode_f(mfr):
    """HRTPMS: mfr = manufacturer data WITHOUT the two company id bytes."""
    if len(mfr) != 9:
        return None
    volt, temp, press = struct.unpack(">BBH", mfr[:4])
    return {"bar": round((press - 100) * 0.01, 2), "temp_c": temp - 50, "volt": round(volt * 0.01 + 1.22, 2)}


def decode_h(company, mfr):
    """TC.TPMS: status and battery travel in the company id, then temperature, pressure (0.1 psi absolute), address."""
    frame = bytes([company & 0xFF, company >> 8]) + mfr
    if len(frame) != 11:
        return None
    psi = int.from_bytes(frame[3:5], "big") / 10
    return {"bar": round(max(0.0, psi - 14.6) * 0.0689476, 2), "temp_c": frame[2], "volt": frame[1] / 10}


def parse_ad(data):
    """Advertising data -> (16-bit service ids, name, [(company, bytes)])."""
    uuids, name, mfr, i = [], "", [], 0
    while i + 1 < len(data):
        n = data[i]
        if n == 0 or i + 1 + n > len(data):
            break
        t, body = data[i + 1], data[i + 2:i + 1 + n]
        if t in (0x02, 0x03):
            uuids += [int.from_bytes(body[j:j + 2], "little") for j in range(0, len(body) - 1, 2)]
        elif t in (0x08, 0x09):
            name = body.decode("utf-8", "replace")
        elif t == 0xFF and len(body) >= 2:
            mfr.append((int.from_bytes(body[:2], "little"), bytes(body[2:])))
        i += 1 + n
    return uuids, name, mfr


def reading(data):
    """Returns (format letter, values, status) for a sensor broadcast, (None, None, None) for anything else.
    Format '?' = it calls itself a tyre sensor but the bytes are not a format we know."""
    if b"\x27\xa8" not in data and b"\x28\xa8" not in data and b"TPMS" not in data.upper():
        return None, None, None                                     # cheap test first: this runs for every broadcast
    uuids, name, mfr = parse_ad(data)
    if 0xA827 in uuids and mfr:
        company, body = mfr[-1]
        return "F", decode_f(body), company
    if 0xA828 in uuids and mfr:
        company, body = mfr[-1]
        return "H", decode_h(company, body), company & 0xFF
    if "TPMS" in name.upper():
        return "?", None, None
    return None, None, None


class Windows:
    """Listening only when a sensor is about to speak. On the bike (21 Nov 2026) the two sensors broadcast every 94.4 s
    each, on their own clocks; listening all the time at 10% caught about half of the broadcasts and could be heard,
    slightly, in the music. So: listen all the time only until each sensor has been heard (the search), then open the
    radio a few seconds around the moment each one is expected, with a larger share of the time, and close it as soon
    as it has spoken.

    The period is learned from the sensors themselves: the gap between two broadcasts of the same sensor, or that gap
    divided by the number of broadcasts missed in between. TPMS_PERIOD_S gives it a starting value. A window is as wide as the margin
    plus what the sensor's clock may have drifted since it was last heard, so it widens by itself after a miss; a
    sensor not heard for LOST_PERIODS periods is searched for again. A learned period may be twice the real one
    (a broadcast missed during the search): for the first PROBES rounds the half-way moments are listened to as well."""
    LOST_PERIODS = 8
    PROBES = 3

    def __init__(self, period_s=0.0, sensors=2, search_s=360.0, margin_s=1.0, drift=0.004, again_every_s=900.0,
                 again_s=110.0, now=time.monotonic):
        self.period, self.fixed = (period_s or None), bool(period_s)
        self.sensors, self.search_s, self.margin, self.drift = sensors, search_s, margin_s, drift
        self.again_every, self.again_s = again_every_s, again_s
        self.t0 = now()
        self.heard = {}                          # address -> when it last spoke
        self.probes = 0 if self.fixed else self.PROBES

    def heard_sensor(self, addr, t):
        prev = self.heard.get(addr)
        self.heard[addr] = t
        if prev is None:
            return
        gap = t - prev
        if gap < 15:                             # the same broadcast heard twice
            return
        if self.period is None:
            if gap <= 300:
                self.period = gap
            return
        k = round(gap / self.period)
        if k >= 1 and abs(gap - k * self.period) <= 0.03 * gap + 1:
            self.period += 0.3 * (gap / k - self.period)          # the same period, measured again
            self.probes = max(0, self.probes - 1)
        elif gap < 0.8 * self.period:
            self.period = gap                    # what had been learned was a multiple: broadcasts were being missed
            self.probes = self.PROBES

    def listen(self, t):
        """(listen now?, why, seconds until this may change)."""
        for addr in [a for a in self.heard                       # not heard for too long: searched for again
                     if self.period and t - self.heard[a] > self.LOST_PERIODS * self.period]:
            del self.heard[addr]
        if self.period is None or len(self.heard) < self.sensors:
            since = t - self.t0
            if since < self.search_s:
                return True, "search", min(1.0, self.search_s - since)
            # nothing (or not everything) found in the search: try again now and then, the rest is windows
            phase = (since - self.search_s) % self.again_every
            if phase >= self.again_every - self.again_s:
                return True, "search", min(1.0, self.again_every - phase)
            wait = self.again_every - self.again_s - phase
        else:
            wait = 60.0
        if self.period:
            step = self.period / 2 if self.probes else self.period
            for addr in self.heard:
                last = self.heard[addr]
                n = max(1, round((t - last) / step))
                for i in (n, n + 1):             # the moment nearest to now, and the one after it
                    e, m = last + i * step, self.margin + i * step * self.drift
                    if e - m <= t <= e + m:
                        return True, "window", min(1.0, e + m - t)
                    if t < e - m:
                        wait = min(wait, e - m - t)
                        break
        return False, "between windows", max(0.05, min(1.0, wait))


class Radio:
    def __init__(self, dev):
        self.s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
        self.s.bind((dev,))
        # events only: command complete (0x0E), command status (0x0F), LE meta (0x3E)
        self.s.setsockopt(socket.SOL_HCI, socket.HCI_FILTER,
                          struct.pack("<IIIH2x", 1 << 4, (1 << 0x0E) | (1 << 0x0F), 1 << (0x3E - 32), 0))
        self.ext = False
        self.backlog = []

    def cmd(self, opcode, params=b"", timeout=2.0):
        """Sends a command and returns its status byte (None = no answer). Broadcasts that arrive meanwhile are kept."""
        self.s.send(struct.pack("<BHB", 1, opcode, len(params)) + params)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if not select.select([self.s], [], [], max(0.0, end - time.monotonic()))[0]:
                break
            p = self.s.recv(1024)
            if len(p) >= 7 and p[1] == 0x0E and struct.unpack_from("<H", p, 4)[0] == opcode:
                return p[6]
            if len(p) >= 7 and p[1] == 0x0F and struct.unpack_from("<H", p, 5)[0] == opcode:
                return p[3]
            if len(p) > 3 and p[1] == 0x3E:
                self.backlog.append(p)
        return None

    def enable(self, on):
        if self.ext:
            return self.cmd(OP_EXT_ENABLE, struct.pack("<BBHH", on, 0, 0, 0))
        return self.cmd(OP_SET_ENABLE, struct.pack("<BB", on, 0))            # 0 = report repeated broadcasts too

    def start(self, window_ms, interval_ms):
        """Passive scan with our share of radio time. Returns a short text, '' when the radio refused."""
        itv, win = max(4, round(interval_ms / 0.625)), max(4, round(window_ms / 0.625))
        win = min(win, itv)
        self.enable(0)                                                       # parameters cannot change while scanning
        st = self.cmd(OP_SET_PARAM, struct.pack("<BHHBB", 0, itv, win, 0, 0))
        self.ext = False
        if st == DISALLOWED:                                                 # the system talks "extended" to this radio
            self.ext = True
            self.enable(0)
            st = self.cmd(OP_EXT_PARAM, struct.pack("<BBBBHH", 0, 0, 1, 0, itv, win))
        if st != 0:
            return ""
        return ("extended" if self.ext else "legacy") if self.enable(1) == 0 else ""

    def in_call(self, dev):
        """True while a voice link (SCO or eSCO) is open on this radio: then we do not listen at all."""
        try:
            buf = bytearray(struct.pack("<HH", dev, 16) + bytes(16 * 16))
            fcntl.ioctl(self.s.fileno(), HCIGETCONNLIST, buf)
            n = struct.unpack_from("<H", buf, 2)[0]
            return any(buf[4 + 16 * i + 8] in (0, 2) for i in range(min(n, 16)))    # link type: 0 SCO, 1 ACL, 2 eSCO
        except OSError:
            return False

    def reports(self, p):
        """One event packet -> [(address, address type, rssi, advertising data)]."""
        out = []
        if len(p) < 5 or p[1] != 0x3E:
            return out
        sub, n, i = p[3], p[4], 5
        try:
            for _ in range(n):
                if sub == 0x02:                                              # legacy report
                    atype, addr, dlen = p[i + 1], p[i + 2:i + 8], p[i + 8]
                    data = p[i + 9:i + 9 + dlen]
                    rssi = struct.unpack_from("b", p, i + 9 + dlen)[0]
                    i += 10 + dlen
                elif sub == 0x0D:                                            # extended report
                    atype, addr = p[i + 2], p[i + 3:i + 9]
                    rssi, dlen = struct.unpack_from("b", p, i + 13)[0], p[i + 23]
                    data = p[i + 24:i + 24 + dlen]
                    i += 24 + dlen
                else:
                    break
                out.append((":".join("%02X" % b for b in reversed(addr)), atype, rssi, bytes(data)))
        except (IndexError, struct.error):
            pass
        return out


def main():
    env = os.environ.get
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dev", type=int, default=int(env("TPMS_HCI", "0")), help="radio number (hci0 = 0)")
    ap.add_argument("--window-ms", type=float, default=float(env("TPMS_WINDOW_MS", "30")))
    ap.add_argument("--interval-ms", type=float, default=float(env("TPMS_INTERVAL_MS", "300")))
    ap.add_argument("--on-s", type=float, default=float(env("TPMS_ON_S", "0")), help="listen this long, then rest --off-s")
    ap.add_argument("--off-s", type=float, default=float(env("TPMS_OFF_S", "0")), help="0 = never rest (default)")
    ap.add_argument("--period-s", default=env("TPMS_PERIOD_S", "auto"),
                    help="seconds between a sensor's broadcasts: listen only around them. auto = learn it (default), "
                         "0 = listen all the time")
    ap.add_argument("--sensors", type=int, default=int(env("TPMS_SENSORS", "2")), help="how many sensors to look for")
    ap.add_argument("--win-window-ms", type=float, default=float(env("TPMS_WIN_WINDOW_MS", "100")),
                    help="share of radio time inside a window around an expected broadcast")
    ap.add_argument("--win-interval-ms", type=float, default=float(env("TPMS_WIN_INTERVAL_MS", "200")))
    ap.add_argument("--margin-s", type=float, default=float(env("TPMS_MARGIN_S", "1.0")))
    ap.add_argument("--seconds", type=float, default=0, help="stop after this long and print a summary (0 = never)")
    ap.add_argument("--log-dir", default=env("TPMS_LOG_DIR") or env("MTSPI_LOG_DIR") or "/var/lib/mtspi")
    ap.add_argument("--max-mb", type=float, default=float(env("TPMS_LOG_MAX_MB", "4")))
    ap.add_argument("--repeat-s", type=float, default=float(env("TPMS_REPEAT_S", "60")))
    ap.add_argument("--no-file", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="check the decoders against known broadcasts and exit")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    # optional: TPMS_FRONT / TPMS_REAR = address of that wheel's sensor, or its last bytes as the maker's app shows
    # them (00:11:22)
    ends = {env("TPMS_" + w.upper()).upper().strip(): w for w in ("front", "rear") if env("TPMS_" + w.upper())}

    class Names(dict):
        def get(self, addr, default=None):
            return next((w for end, w in ends.items() if addr.upper().endswith(end)), default)
    names = Names()
    state_path = env("TPMS_STATE", "/run/mtspi-tpms/state.json")

    def save_state():
        """What was last heard, for the page on the dashboard (read by the bridge). In memory only (/run)."""
        doc = {"alive_up": round(time.monotonic(), 1), "period_s": round(wins.period, 1) if wins and wins.period else None,
               "sensors": {addr: {"wheel": names.get(addr), "up": round(s["at"], 1), "rssi": s.get("rssi"), **(s["last"] or {})}
                           for addr, s in sensors.items()}}
        try:
            tmp = state_path + ".new"
            with open(tmp, "w") as f:
                f.write(json.dumps(doc))
            os.replace(tmp, state_path)
        except OSError:
            pass

    out, limit, written = None, int(a.max_mb * 1e6), 0
    if not a.no_file:
        try:
            os.makedirs(a.log_dir, exist_ok=True)
            old = sorted(f for f in os.listdir(a.log_dir) if f.startswith("tpms-") and f.endswith(".jsonl"))
            for f in old[:-int(env("TPMS_LOG_KEEP", "10"))]:         # one file per start: keep the last ones only
                os.remove(os.path.join(a.log_dir, f))
            out = open(os.path.join(a.log_dir, time.strftime("tpms-%Y%m%d-%H%M%S.jsonl")), "w", buffering=1)
        except OSError as e:
            print(f"tpms: no log file ({e}), journal only", flush=True)

    def log(kind, room=0, **kw):
        """A JSON line. The survey stops at the size limit; sensor lines get twice that room."""
        nonlocal written
        if out is None or written > limit + room:
            return
        line = json.dumps({"t": round(time.time(), 2), "up": round(time.monotonic(), 1), "kind": kind, **kw})
        written += len(line) + 1
        try:
            out.write(line + "\n")
        except OSError:
            pass

    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))

    radio = None
    while radio is None and not stop:            # the radio may not be there yet at boot
        try:
            radio = Radio(a.dev)
        except OSError as e:
            print(f"tpms: radio hci{a.dev} not ready ({e}), retry in 10 s", flush=True)
            time.sleep(10)
    if radio is None:
        return 0
    mode = radio.start(a.window_ms, a.interval_ms)
    share = 100 * min(a.window_ms, a.interval_ms) / a.interval_ms
    print(f"tpms: listening on hci{a.dev}, {a.window_ms:g} ms every {a.interval_ms:g} ms ({share:.0f}% of radio time), "
          f"scan {mode or 'REFUSED (retrying)'}, log {out.name if out else 'none'}", flush=True)
    log("start", window_ms=a.window_ms, interval_ms=a.interval_ms, scan=mode)

    wins = None
    if str(a.period_s).strip().lower() not in ("0", "0.0", "off", ""):
        fixed = 0.0 if str(a.period_s).strip().lower() == "auto" else float(a.period_s)
        wins = Windows(period_s=fixed, sensors=a.sensors, margin_s=a.margin_s)
        print(f"tpms: after the search, listening only around each sensor's broadcasts "
              f"(period {'to be learned' if not fixed else f'{fixed:g} s'}, {a.win_window_ms:g} ms every "
              f"{a.win_interval_ms:g} ms inside a window)", flush=True)
    paused = ""
    scanning = "search"          # which share of radio time the scan is using now: "search" or "window"
    in_call = False
    windows = heard_in_window = 0
    wait = 1.0
    next_call = 0.0
    seen = {}                    # address -> [first, last, count, rssi sum, last data, last "stays around" line]
    sensors = {}                 # address -> {"last": values, "printed": time, "count": n, "first": time, ...}
    total = restarts = 0
    t0 = time.monotonic()
    next_check, next_stats = t0 + 30, t0 + 60
    save_state()
    while not stop:
        now = time.monotonic()
        if a.seconds and now - t0 >= a.seconds:
            break
        if now >= next_call:                     # a phone call has the radio to itself; TPMS_OFF_S adds rests
            next_call = now + 2
            in_call = radio.in_call(a.dev)
        rest = a.off_s > 0 and a.on_s > 0 and (now - t0) % (a.on_s + a.off_s) >= a.on_s
        want, kind, wait = wins.listen(now) if wins else (True, "search", 1.0)
        why = "phone call" if in_call else ("resting" if rest else ("" if want else kind))
        if why != paused or (not why and kind != scanning):
            quiet = "between windows" in (why, paused) or kind != scanning      # windows come and go all the time
            if why:
                radio.enable(0)
            else:
                scanning = kind
                if kind == "window":
                    windows += 1
                    mode = radio.start(a.win_window_ms, a.win_interval_ms)
                else:
                    mode = radio.start(a.window_ms, a.interval_ms)
            if why and not quiet:
                print(f"tpms: {why}, listening paused", flush=True)
                log("paused", why=why)
            elif not why and not quiet:
                print("tpms: listening again", flush=True)
                log("resumed", scan=mode)
            if wins and why == "between windows" and paused != why and scanning == "search" and wins.period:
                print(f"tpms: search over, {len(wins.heard)} sensors, period {wins.period:.1f} s: "
                      f"listening only around their broadcasts from now on", flush=True)
                log("windows", sensors=len(wins.heard), period_s=round(wins.period, 2))
            paused = why
        if now >= next_check and paused:
            next_check = now + 30
        if now >= next_check:
            # something else (the system, a connection being made) may have stopped the scan: if "enable" is accepted
            # it was off, and it is started again with our parameters
            next_check = now + 30
            if radio.enable(1) == 0 or not mode:
                restarts += 1
                mode = (radio.start(a.win_window_ms, a.win_interval_ms) if scanning == "window"
                        else radio.start(a.window_ms, a.interval_ms))
                log("scan-restarted", scan=mode)
        if now >= next_stats:
            next_stats = now + 60
            save_state()
            around = sum(1 for v in seen.values() if v[1] - v[0] > 120 and now - v[1] < 60)
            print(f"tpms: {total} broadcasts heard, {len(seen)} addresses ({around} staying around), "
                  f"{len(sensors)} tyre sensors, scan restarted {restarts} times"
                  + (f", windows {windows} (sensor heard in {heard_in_window}), period "
                     f"{f'{wins.period:.1f} s' if wins.period else 'not known yet'}" if wins else ""), flush=True)
            if len(seen) > 4000:                 # phones change address all the time: forget the ones long gone
                for k in [k for k, v in seen.items() if now - v[1] > 600]:
                    del seen[k]
        packets, radio.backlog = radio.backlog, []
        if not packets and select.select([radio.s], [], [], wait)[0]:
            try:
                packets = [radio.s.recv(1024)]
            except OSError as e:
                print(f"tpms: radio lost ({e})", flush=True)
                return 1                         # systemd starts us again
        for p in packets:
            for addr, atype, rssi, data in radio.reports(p):
                total += 1
                now = time.monotonic()
                v = seen.get(addr)
                if v is None:
                    v = seen[addr] = [now, now, 0, 0, b"", 0.0]
                    log("new", addr=addr, addr_type=atype, rssi=rssi, data=data.hex())
                v[1], v[2], v[3], v[4] = now, v[2] + 1, v[3] + rssi, data
                if now - v[0] > 120 and now - v[5] > 300:        # travels with us: say so every five minutes
                    v[5] = now
                    log("around", addr=addr, addr_type=atype, seconds=round(now - v[0]), count=v[2],
                        rssi_avg=round(v[3] / v[2]), data=data.hex())
                fmt, val, status = reading(data)
                if fmt is None:
                    continue
                s = sensors.setdefault(addr, {"first": now, "count": 0, "printed": 0.0, "last": None, "gaps": []})
                if s["count"]:
                    s["gaps"].append(now - s["at"])
                s["count"], s["at"], s["fmt"], s["rssi"] = s["count"] + 1, now, fmt, rssi
                if wins:
                    if scanning == "window" and not paused and now - wins.heard.get(addr, 0) > 15:
                        heard_in_window += 1
                    wins.heard_sensor(addr, now)
                log("sensor", room=limit, addr=addr, fmt=fmt, status=status, rssi=rssi, data=data.hex(), **(val or {}))
                if val:
                    s["last"] = val
                    save_state()
                if val != s.get("shown") or now - s["printed"] >= a.repeat_s:
                    s["shown"], s["printed"] = val, now
                    who = names.get(addr, addr)
                    if val:
                        print(f"tpms: {who} {val['bar']:.2f} bar, {val['temp_c']} C, battery {val['volt']:.2f} V "
                              f"(format {fmt}, status {status}, signal {rssi} dBm, heard {s['count']} times)", flush=True)
                    else:
                        print(f"tpms: {who} looks like a tyre sensor, bytes not understood: {data.hex()} "
                              f"(signal {rssi} dBm)", flush=True)
    radio.enable(0)
    span = time.monotonic() - t0
    print(f"tpms: stopped after {span:.0f} s: {total} broadcasts, {len(seen)} addresses, {len(sensors)} tyre sensors",
          flush=True)
    for addr, s in sensors.items():
        gaps = sorted(s["gaps"])
        mid = f"{gaps[len(gaps) // 2]:.1f}" if gaps else "-"
        print(f"tpms:   {names.get(addr, addr)} format {s['fmt']}: heard {s['count']} times, typical gap {mid} s, "
              f"last {s['last']}", flush=True)
    log("stop", seconds=round(span), broadcasts=total, addresses=len(seen), sensors=len(sensors))
    if a.seconds:                                # bench run: the busiest addresses, to judge how much is being heard
        for addr, v in sorted(seen.items(), key=lambda kv: -kv[1][2])[:8]:
            print(f"tpms:   {addr} heard {v[2]} times in {v[1] - v[0]:.0f} s, signal {v[3] / v[2]:.0f} dBm", flush=True)
    return 0


def selftest():
    """Broadcasts captured from real sensors by the tpms_ble project, wrapped the way they travel on the air."""
    def adv(uuid, company, body, name=b"TPMS"):
        m = struct.pack("<H", company) + body
        return (b"\x02\x01\x06" + b"\x03\x03" + struct.pack("<H", uuid) + bytes([len(m) + 1, 0xFF]) + m
                + bytes([len(name) + 1, 0x09]) + name)
    cases = [
        (adv(0xA827, 0x0002, bytes.fromhex("AF4500A7002D56A8B4")), "F", {"bar": 0.67, "temp_c": 19, "volt": 2.97}),
        (adv(0xA827, 0x0006, bytes.fromhex("B34A0064002FA0057D")), "F", {"bar": 0.0, "temp_c": 24, "volt": 3.01}),
        (adv(0xA827, 0x0002, bytes.fromhex("AF4501C3002D56A8B4")), "F", {"bar": 3.51, "temp_c": 19, "volt": 2.97}),
        (adv(0xA828, 0x1D00, bytes.fromhex("1401B3112233445566")), "H", {"bar": 1.99, "temp_c": 20, "volt": 2.9}),
        (adv(0xA827, 0x0002, bytes.fromhex("AF45")), "F", None),                      # right service, wrong length
        (b"\x02\x01\x06\x05\x09TPMS", "?", None),                                     # only the name
        (b"\x02\x01\x06\x03\x03\x0f\x18\x06\x09Watch", None, None),                   # something else
        (b"\x02\x01\x06\x1f\xff\x27\xa8", None, None),                                # cut short: must not crash
    ]
    bad = 0
    for data, want_fmt, want in cases:
        fmt, val, _ = reading(data)
        ok = fmt == want_fmt and val == want
        bad += not ok
        print("OK  " if ok else "FAIL", data.hex(), "->", fmt, val)
    # the same bytes through the event parser, as the radio delivers them
    data = cases[0][0]
    ev = bytes([0x04, 0x3E, 12 + len(data), 0x02, 1, 0x03, 0x00]) + bytes(reversed(bytes.fromhex("112233445566"))) \
        + bytes([len(data)]) + data + struct.pack("b", -61)
    r = Radio.reports(None, ev)
    ok = r == [("11:22:33:44:55:66", 0, -61, data)]
    bad += not ok
    print("OK  " if ok else "FAIL", "event parser", r[0][:3] if r else r)
    print("ALL OK" if not bad else f"{bad} FAILED")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
