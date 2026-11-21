#!/usr/bin/env python3
"""Test screen, Pi side: presents itself to the bike as a mySPIN phone and shows the test menu.

Sequence: Bluetooth control channel (RFCOMM) -> the bike's Wi-Fi credentials -> connection
to its access point -> UDP 8158 announcement -> mySPIN TCP session -> frames and keys.

On the bench (Mac) the RFCOMM channel is emulated with a TCP connection (--control tcp:PORT)
and Wi-Fi is left untouched (--wifi none).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from myspin.phone import PhoneControl, PhoneProjection, discover_vehicle  # noqa: E402
from mtspi.testui import TestScreen  # noqa: E402


class JsonLog:
    """One JSON line per event.
    MTSPI_LOG_SYNC=1 (default): every line is flushed to the card at once (fsync), so that the last lines survive
    when the bike cuts the power. With the log kept in memory (MTSPI_LOG_SYNC=0) that would be pointless work.
    MTSPI_LOG_MAX_MB: when the file grows past this size it is renamed to <name>.1 (the previous .1 is dropped)
    and a new one is started: a log kept in memory cannot be allowed to grow without limit. 0 = no limit.
    A log that cannot be written (card full or read-only) never stops the program: the file is given up, the
    lines still go to the system journal."""

    def __init__(self, path: str | None, quiet: bool = False):
        self.path = path
        self.quiet = quiet
        self.t0 = time.monotonic()
        self.sync = os.environ.get("MTSPI_LOG_SYNC", "1") != "0"
        try:
            self.max_bytes = int(float(os.environ.get("MTSPI_LOG_MAX_MB", "0")) * 1024 * 1024)
        except ValueError:
            self.max_bytes = 0
        self.size = 0
        self.f = None
        if path:
            try:
                self.f = open(path, "a", buffering=1)
                self.size = self.f.tell()
            except OSError as e:
                print(json.dumps({"event": "log_file_unavailable", "error": str(e)}), flush=True)

    def __call__(self, event: str, **fields) -> None:
        rec = {"t": round(time.time(), 3), "s": round(time.monotonic() - self.t0, 3), "side": "pi", "event": event, **fields}
        line = json.dumps(rec, ensure_ascii=False, default=str)
        if self.f:
            try:
                self.f.write(line + "\n")
                self.f.flush()
                if self.sync:
                    os.fsync(self.f.fileno())
                self.size += len(line) + 1
                if self.max_bytes and self.size > self.max_bytes:
                    self.f.close()
                    os.replace(self.path, self.path + ".1")
                    self.f = open(self.path, "a", buffering=1)
                    self.size = 0
            except (OSError, ValueError) as e:
                self.f = None
                print(json.dumps({"event": "log_file_given_up", "error": str(e)}), flush=True)
        if not self.quiet:
            print(line, flush=True)


def save_credentials(path: str, creds: dict, log) -> None:
    """Saves name/password/band of the bike's Wi-Fi to a file readable only by its owner.
    Each session appends a line: this also shows whether the password changes at every power-on."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps({"t": round(time.time()), **creds}) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.chmod(path, 0o600)
    log("access_point_saved", path=path)


# wpa_supplicant dedicated to Wi-Fi Direct (mtspi-p2p.service, outside NetworkManager: NM "adopted" the group
# as soon as it was formed and removed it). Empty WPA_CTRL = NetworkManager's one.
WPA = ["/usr/sbin/wpa_cli", "-p", os.environ.get("WPA_CTRL") or "/run/wpa_supplicant"]
P2P_DEV = os.environ.get("WPA_P2P_DEV", "p2p-dev-wlan0")


async def join_p2p(creds: dict, log) -> None:
    """Wi-Fi Direct to the dashboard (V4 S): the bike is the group owner, the Pi joins with the PIN the bike gave us
    (GO intent 0, WPS keypad). IP address from the bike's DHCP, no default route."""
    loop = asyncio.get_event_loop()

    def run(cmd, timeout=30):
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    def cli(*a, iface=None):
        iface = iface or P2P_DEV
        return run(WPA + ["-i", iface] + list(a)).stdout.strip()

    def group_iface():
        out = run(["ip", "-br", "addr"]).stdout.splitlines()
        for line in out:
            parts = line.split()
            if parts and parts[0].startswith("p2p-wlan0-"):
                return parts[0], any(x.count(".") == 3 for x in parts[2:])
        return None, False

    name, pin = creds["device_name"], creds.get("pin")
    # if the Pi was in home mode, mtspi-wifi-mode is now starting the Wi-Fi Direct wpa_supplicant: wait for it
    for _ in range(20):
        if await loop.run_in_executor(None, lambda: cli("ping")) == "PONG":
            break
        await asyncio.sleep(1)
    iface, has_ip = await loop.run_in_executor(None, group_iface)
    if iface and has_ip and "wpa_state=COMPLETED" in (await loop.run_in_executor(None, lambda: cli("status", iface=iface))):
        log("p2p_already_joined", iface=iface, peer=name)
        return
    if iface:                                   # stale group without IP: remove it, interface included (only one is allowed)
        await loop.run_in_executor(None, lambda: cli("p2p_group_remove", iface))
        await asyncio.sleep(1)
        await loop.run_in_executor(None, lambda: run(["sudo", "-n", "/usr/sbin/iw", "dev", iface, "del"]))
    log("p2p_joining", peer=name)
    addr, is_go = None, 0
    for attempt in range(1, 4):
        await loop.run_in_executor(None, lambda: cli("p2p_find", "20"))
        deadline = time.monotonic() + 20
        while addr is None and time.monotonic() < deadline:
            await asyncio.sleep(1.5)
            peers = (await loop.run_in_executor(None, lambda: cli("p2p_peers"))).split()
            for a in peers:
                info = await loop.run_in_executor(None, lambda a=a: cli("p2p_peer", a))
                if f"device_name={name}" in info.splitlines():
                    addr = a
                    caps = dict(x.split("=", 1) for x in info.splitlines() if "=" in x)
                    is_go = int(caps.get("group_capab", "0"), 16) & 1
                    break
        if addr:
            break
        log("p2p_peer_not_found", attempt=attempt, peer=name)
    if addr and not is_go:
        # On the bike (16 Nov 2026), right after the dashboard says its Wi-Fi is ready, it can show up for a few seconds
        # without its group: a negotiation started then never completes (40 s lost). The V4 S always owns the group,
        # so wait for it to appear before falling back to a negotiation.
        deadline = time.monotonic() + 12
        while not is_go and time.monotonic() < deadline:
            await asyncio.sleep(1.5)
            info = await loop.run_in_executor(None, lambda: cli("p2p_peer", addr))
            caps = dict(x.split("=", 1) for x in info.splitlines() if "=" in x)
            is_go = int(caps.get("group_capab", "0"), 16) & 1
        log("p2p_group_owner_wait", peer=name, owner=bool(is_go))
    if not addr:
        raise ConnectionError("Wi-Fi Direct: bike not found")
    await loop.run_in_executor(None, lambda: cli("p2p_stop_find"))
    method = [pin, "keypad"] if pin else ["pbc"]
    # the V4 S already has an active group (persistent group owner, "DIRECT-..-p2p-mw-myspin"): join it, no negotiation
    extra = ["join"] if is_go else ["go_intent=0"]
    res = await loop.run_in_executor(None, lambda: cli("p2p_connect", addr, *method, *extra))
    log("p2p_connect", peer=name, join=bool(is_go), result=res[:40])
    deadline = time.monotonic() + 40
    iface = None
    while time.monotonic() < deadline:
        await asyncio.sleep(1)
        iface, _ = await loop.run_in_executor(None, group_iface)
        if iface and "wpa_state=COMPLETED" in (await loop.run_in_executor(None, lambda: cli("status", iface=iface))):
            break
    else:
        raise ConnectionError("Wi-Fi Direct: group not formed")
    # NetworkManager ignores p2p-wlan0-* (conf.d/99-mtspi-p2p.conf): "nmcli device set ... managed no" here
    # tore down the freshly formed group. IP from the bike, no gateway (the default route stays where it is)
    # -A: no ARP probe of the offered address. The bike always offers the same one and the probe alone cost 4.6-4.9 s
    # in each of the three joins of the 16/11 ride (of 6-9 s spent getting the address).
    # Two tries of 12 s: on the bike the address comes in 2-5 s, and once (21 Nov 2026) it did not come at all in 20 s.
    t_group = time.monotonic()
    for attempt in (1, 2):
        proc = await loop.run_in_executor(
            None, lambda: run(["sudo", "-n", "/usr/sbin/dhcpcd", "-1", "-4", "-A", "-G", "-t", "12", iface], timeout=30))
        _, has_ip = await loop.run_in_executor(None, group_iface)
        if has_ip:
            break
        log("p2p_dhcp_retry", iface=iface, attempt=attempt, code=proc.returncode, output=(proc.stdout + proc.stderr).strip()[-120:])
    # network name and frequency of the bike's group: on 16/11 the name was different at every join
    net = {}
    try:
        st = await loop.run_in_executor(None, lambda: cli("status", iface=iface))
        net = {k: v for k, v in (ln.split("=", 1) for ln in st.splitlines() if "=" in ln) if k in ("ssid", "freq")}
    except Exception:
        pass
    log("p2p_joined" if has_ip else "p2p_dhcp_failed", iface=iface, code=proc.returncode,
        address_s=round(time.monotonic() - t_group, 1), attempts=attempt, **net,
        output=(proc.stdout + proc.stderr).strip()[-200:])
    if not has_ip:
        raise ConnectionError("Wi-Fi Direct: no IP address from the bike")


async def join_wifi(mode: str, creds: dict, log) -> None:
    if mode == "none":
        log("wifi_join_skipped", ssid=creds["ssid"], band=creds["band"])
        return
    if mode == "nmcli" and creds.get("p2p"):
        await join_p2p(creds, log)
        return
    if mode == "nmcli":
        loop = asyncio.get_event_loop()

        def run(cmd):
            return subprocess.run(cmd, capture_output=True, text=True)
        ssid = creds["ssid"]
        # already on the bike's network (the bike renegotiates after a radio dropout): do not reconnect, the session stays up
        cur = await loop.run_in_executor(None, lambda: run(["nmcli", "-t", "-f", "ACTIVE,SSID", "device", "wifi"]))
        if f"yes:{ssid}" in cur.stdout.splitlines():
            log("wifi_already_joined", ssid=ssid, band=creds["band"])
            return
        log("wifi_joining", ssid=ssid, band=creds["band"])
        proc = None
        for attempt in range(1, 4):
            # the dashboard AP may have just come up: without a fresh scan nmcli answers "No network with SSID"
            await loop.run_in_executor(None, lambda: run(["nmcli", "device", "wifi", "rescan", "ssid", ssid]))
            await asyncio.sleep(3)
            cmd = ["nmcli", "--wait", "20", "device", "wifi", "connect", ssid,
                   "password", creds["password"], "ifname", "wlan0"]
            proc = await loop.run_in_executor(None, lambda: run(cmd))
            log("wifi_joined" if proc.returncode == 0 else "wifi_join_failed", attempt=attempt,
                code=proc.returncode, output=(proc.stdout + proc.stderr).strip()[-300:])
            if proc.returncode == 0:
                break
        if proc is None or proc.returncode != 0:
            raise ConnectionError("Wi-Fi join failed")
        # The bike's network has no internet: it must not become the default route, otherwise remote access
        # (iPhone Bluetooth hotspot, remote debugging) tries to go out through the bike and gets lost.
        fix = ["nmcli", "connection", "modify", "id", ssid, "ipv4.never-default", "yes",
               "ipv6.never-default", "yes"]
        res = await loop.run_in_executor(
            None, lambda: (run(fix), run(["nmcli", "device", "reapply", "wlan0"])))
        log("wifi_never_default", code=[r.returncode for r in res])
        return
    raise ValueError(f"unknown wifi mode {mode}")


_RFCOMM = None


async def control_channel(spec: str, log):
    """Returns (reader, writer) of the control channel, already connected by the bike."""
    if spec.startswith("tcp:"):
        port = int(spec[4:])
        got = asyncio.get_event_loop().create_future()

        async def on_conn(r, w):
            if not got.done():
                got.set_result((r, w))
        server = await asyncio.start_server(on_conn, "0.0.0.0", port)
        log("control_listening", transport="tcp-emulated-rfcomm", port=port)
        r, w = await got
        server.close()
        log("control_connected", peer=str(w.get_extra_info("peername")))
        return r, w
    if spec == "rfcomm":
        # "MySpinBluetooth" service registered once in BlueZ; each call waits for the next connection
        global _RFCOMM
        if _RFCOMM is None:
            from mtspi.bluez import RfcommServer
            _RFCOMM = RfcommServer(log)
        return await _RFCOMM.accept()
    if spec == "esp32":
        # pro variant: the mySPIN service runs on the ESP32 (esp32/btstack_phone), the bytes arrive over serial
        from mtspi.esp32_bike import Esp32Bike
        bike = await Esp32Bike.wait_instance()
        log("control_listening", transport="esp32")
        return await bike.myspin_accept()
    raise ValueError(f"unknown control {spec}")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--control", default="rfcomm", help="rfcomm | tcp:PORT (emulated) | none (skip it, go straight to UDP)")
    ap.add_argument("--wifi", default="nmcli", choices=["nmcli", "none"])
    ap.add_argument("--discovery-port", type=int, default=8158)
    ap.add_argument("--jpeg-quality", type=int, default=80)
    ap.add_argument("--log", default=None, help="JSONL file (on the Pi: on the SD card)")
    ap.add_argument("--ap-credentials-file", default=None,
                    help="where to save the bike's Wi-Fi SSID/password (mode 600 file; on the Pi: in the folder of the run's log)")
    ap.add_argument("--once", action="store_true", help="one session only, then exit")
    ap.add_argument("--vehicle-keys", type=int, default=128, help="ask the bike for vehicle data with codes 1..N (0 = no)")
    ap.add_argument("--bt-info", default=None, help='string for the bike, e.g. "[HFP,A2DP]MTS Pi" (connected audio profiles + BT name)')
    ap.add_argument("--no-clock", action="store_true", help="no on-screen clock (bench: the screen changes only with keys)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    log = JsonLog(args.log, args.quiet)
    ui = TestScreen(clock=not args.no_clock)
    log("start", control=args.control, wifi=args.wifi, pid=os.getpid())

    control = None
    if args.control != "none":
        r, w = await control_channel(args.control, log)
        control = PhoneControl(r, w, log)
        creds = await control.negotiate()
        if args.ap_credentials_file:
            save_credentials(args.ap_credentials_file, creds, log)
        await join_wifi(args.wifi, creds, log)
        await control.notify_wlan("CONNECTED")

    while True:
        ep = await discover_vehicle(log, args.discovery_port)
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(ep["host"], ep["port"]), 10)
            sock = writer.get_extra_info("socket")
            if sock is not None:
                import socket
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            log("tcp_connected", host=ep["host"], port=ep["port"])
            session = PhoneProjection(reader, writer, ui, log, args.jpeg_quality, args.bt_info,
                                       list(range(1, args.vehicle_keys + 1)))
            await session.run()
        except Exception as e:
            log("session_error", error=f"{type(e).__name__}: {e}")
        finally:
            try:
                writer.close()
            except Exception:
                pass
            log("session_end", frames=ui.frames_sent, keys=ui.key_count)
        if args.once:
            return 0
        await asyncio.sleep(1)


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
