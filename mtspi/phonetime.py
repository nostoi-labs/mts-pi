"""The time of day from the iPhone, over the Bluetooth link that is already there.

The Pi has no clock of its own: it starts from the last time it knew and corrects itself from the internet. On the
bike there is usually no internet, so the Pi's clock (and the times in its logs) can be hours or days behind.
The iPhone offers the standard Current Time service to paired devices, also on the classic Bluetooth link
(ATT over BR/EDR, L2CAP PSM 31): one short read gives the phone's local time and its time zone.

read() opens its own channel straight to the phone, asks two values and closes: BlueZ is not involved and nothing is
written on the phone. Measured on 20 Nov 2026 (iPhone, iOS 26): channel open in 0.4 s, answer within 0.1 s of the
internet time. sync() sets the Pi's clock from it, only when the internet has not already done so.
"""
import os
import socket
import struct
import subprocess
import time

ATT_PSM = 0x1F
UUID_CURRENT_TIME = 0x2A2B
UUID_LOCAL_TIME_INFO = 0x2A0F
NTP_FLAG = "/run/systemd/timesync/synchronized"
# what the phone may ask us on the same channel: requests get "not supported", the rest is ignored
_REQUESTS = {0x02, 0x04, 0x06, 0x08, 0x0A, 0x0C, 0x0E, 0x10, 0x12, 0x16, 0x18, 0x20}
_ERRORS = {0x02: "read not permitted", 0x05: "authentication needed", 0x06: "not supported", 0x08: "authorization needed",
           0x0A: "not found", 0x0F: "encryption needed"}


def parse_current_time(v):
    """Current Time value → (year, month, day, hour, minute, second, fraction of a second), in the phone's local time."""
    if len(v) < 9:
        raise ValueError(f"current time too short: {v.hex()}")
    y, mo, d, h, mi, s, _dow, frac = struct.unpack("<HBBBBBBB", v[:9])
    if not (2024 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31 and h < 24 and mi < 60 and s < 60):
        raise ValueError(f"current time not valid: {v.hex()}")
    return y, mo, d, h, mi, s, frac / 256


def parse_local_time_info(v):
    """Local Time Information value → minutes to add to UTC to get the phone's local time (zone + daylight saving)."""
    if len(v) < 2:
        raise ValueError(f"local time information too short: {v.hex()}")
    tz, dst = struct.unpack("<bB", v[:2])
    if tz == -128 or dst == 255:
        raise ValueError("the phone does not know its time zone")
    return tz * 15 + dst * 15


def to_epoch(local, offset_min):
    """Local time from the phone and its offset in minutes → seconds since 1970 (UTC)."""
    import calendar
    y, mo, d, h, mi, s, frac = local
    return calendar.timegm((y, mo, d, h, mi, s)) + frac - offset_min * 60


def _read_by_type(sock, uuid, deadline):
    sock.send(struct.pack("<BHHH", 0x08, 1, 0xFFFF, uuid))
    while time.monotonic() < deadline:
        r = sock.recv(1024)
        if not r:
            break
        op = r[0]
        if op == 0x09 and len(r) >= 4:                      # read by type response: length, handle, value
            return r[4:2 + r[1]]
        if op == 0x01 and len(r) >= 5 and r[1] == 0x08:     # error for our request
            raise RuntimeError(_ERRORS.get(r[4], f"error {r[4]:#x}"))
        if op in _REQUESTS:                                 # the phone looks at us too: we offer nothing here
            sock.send(struct.pack("<BBHB", 0x01, op, 0, 0x06))
    raise TimeoutError("no answer from the phone")


def read(mac, timeout=6.0, connect=None):
    """Blocking. Returns {"epoch": seconds UTC, "offset_min": zone + daylight saving, "took_s": seconds}."""
    t0 = time.monotonic()
    if connect:
        sock = connect(mac)
    else:
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
        sock.settimeout(timeout)
        sock.connect((mac, ATT_PSM))
    try:
        deadline = t0 + timeout
        offset = parse_local_time_info(_read_by_type(sock, UUID_LOCAL_TIME_INFO, deadline))
        local = parse_current_time(_read_by_type(sock, UUID_CURRENT_TIME, deadline))   # last, so nothing ages it
        return {"epoch": to_epoch(local, offset), "offset_min": offset, "took_s": round(time.monotonic() - t0, 2)}
    finally:
        sock.close()


def sync(mac, log, min_step_s=2.0, read_fn=read, now=time.time, set_clock=None, ntp_flag=NTP_FLAG):
    """Blocking. Reads the phone's time and sets the Pi's clock when the internet has not set it and they differ.
    Returns "set", "kept" (read fine, clock left alone) or None (no answer: nothing changed). Never raises."""
    try:
        got = read_fn(mac)
    except Exception as e:
        log("phone_time_error", error=f"{type(e).__name__}: {e}")
        return None
    step = got["epoch"] - now()
    ntp = os.path.exists(ntp_flag)
    do_set = not ntp and abs(step) >= min_step_s
    ok = bool(do_set and (set_clock or _set_clock)(got["epoch"]))
    log("phone_time", step_s=round(step, 2), utc_offset_min=got["offset_min"], took_s=got["took_s"],
        internet_time=ntp, set=ok, **({"set_failed": True} if do_set and not ok else {}))
    return "set" if ok else "kept"


def _set_clock(epoch):
    try:
        r = subprocess.run(["sudo", "-n", "/usr/bin/date", "-u", "-s", f"@{epoch:.3f}"], capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False
