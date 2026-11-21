"""The Pi acts as the PHONE (HFP Audio Gateway) towards the bike, with the iPhone's real state taken from oFono.

Why our own: oFono 2.16 offers the AG only with a real SIM (plugins/hfp_ag_bluez5.c, sim_state_watch), and the
iPhone "modem" (hfp_hf) has no SIM. Here:
- BlueZ profile 0x111F (Profile1) only on the bike's radio (internal, BT_NAME "MTS Pi");
- the bike's AT commands: indicators (service, call, signal, battery…), operator, call list,
  answer/hang up/dial → forwarded to oFono on the iPhone (VoiceCallManager);
- ringing: RING + +CLIP (name from the phonebook sent by the iPhone) every 3 s;
- voice: SCO to the bike in CVSD (no codec negotiation), 16-bit PCM 8 kHz.
  Bridge: iPhone voice (PipeWire node of the oFono hands-free) → bike SCO; bike microphone → iPhone.

Standalone test on the Pi (stop the bridge first if it uses the same radio):
  .venv/bin/python -m mtspi.hfp_ag --adapter hci0            # signalling + voice bridge
  .venv/bin/python -m mtspi.hfp_ag --adapter hci0 --tone     # during an active call: 440 Hz tone to the bike,
                                                               # bike microphone recorded to /tmp/bike-mic.raw
"""
# no "from __future__ import annotations": dbus-fast reads the D-Bus signatures from the annotations

import argparse
import asyncio
import collections
import json
import math
import os
import socket
import struct
import subprocess
import sys
import threading
import time

from dbus_fast import Variant
from mtspi import linkproto as lp
from dbus_fast.service import ServiceInterface, method

from mtspi.bluez import UUID_HFP_AG, _v, call, system_bus
from mtspi.tasks import spawn
from mtspi import alive

PATH = "/ducati/hfp_ag"
SOL_SCO, SCO_OPTIONS = 17, 1

# indicators in the order declared to +CIND=? (1-based indices for +CIEV)
INDICATORS = [("service", "(0,1)"), ("call", "(0,1)"), ("callsetup", "(0-3)"), ("callheld", "(0-2)"),
              ("signal", "(0-5)"), ("roam", "(0,1)"), ("battchg", "(0-5)")]
# AG +BRSF: reject call (32), enhanced call status CLCC (64), extended error codes (256).
# No codec negotiation (512) → CVSD; no three-way calling for now.
AG_FEATURES = 32 | 64 | 256
SDP_FEATURES = 0x0000
# HFP_VR=1: voice assistant (CarPlay Siri). Bit 2 of +BRSF and of the SDP record: the bike can send AT+BVRA=1 (long press
# of the phone button). Off by default: advertising it may change what the bike does with that button, needs testing.
VR_ENABLED = os.environ.get("HFP_VR", "0") == "1"
if VR_ENABLED:
    AG_FEATURES |= 4
    SDP_FEATURES |= 0x0004
# Siri shown to the bike as a call in progress. The Multistrada V4 S does not declare voice recognition (AT+BRSF=693,
# bit 3 off): with only the voice channel open it sent plain silence and nothing reached the helmet; during a call it
# does connect the helmet (tried on the bike, 21 Nov 2026). HFP_VR_CALL: "auto" (default) = as a call for a hands-free
# that does not declare voice recognition, +BVRA for one that does; "1" = always as a call; "0" = never.
VR_CALL_MODE = os.environ.get("HFP_VR_CALL", "auto")


class _Profile(ServiceInterface):
    def __init__(self, ag):
        super().__init__("org.bluez.Profile1")
        self.ag = ag

    @method()
    def Release(self):
        pass

    @method()
    def NewConnection(self, device: "o", fd: "h", properties: "a{sv}"):
        self.ag.incoming(device, fd)

    @method()
    def RequestDisconnection(self, device: "o"):
        self.ag.log("hfp_ag_disconnect_requested", device=device)


def mac_of(path):
    return path.rsplit("/", 1)[-1].replace("dev_", "").replace("_", ":")


class IphoneState:
    """iPhone state from oFono (hfp modem): indicators, operator, calls. Updated by poll()."""

    def __init__(self, bus):
        self.bus = bus
        self.modem = None
        self.ind = {"service": 0, "call": 0, "callsetup": 0, "callheld": 0, "signal": 0, "roam": 0, "battchg": 3}
        self.operator = ""
        self.calls = []                    # [(path, {"state", "number", "name", "dir"})]

    async def poll(self):
        try:
            modems = (await call(self.bus, "org.ofono", "/", "org.ofono.Manager", "GetModems"))[0]
        except Exception:
            modems = []
        self.modem = None
        for path, props in modems:
            if _v(props.get("Type")) == "hfp" and _v(props.get("Online")):
                self.modem, ifaces = path, _v(props.get("Interfaces")) or []
                break
        if not self.modem:
            self.ind.update(service=0, call=0, callsetup=0, callheld=0, signal=0)
            self.calls = []
            return
        if "org.ofono.Handsfree" in ifaces:
            hf = (await call(self.bus, "org.ofono", self.modem, "org.ofono.Handsfree", "GetProperties"))[0]
            if "BatteryChargeLevel" in hf:
                self.ind["battchg"] = max(0, min(5, int(_v(hf["BatteryChargeLevel"]))))
        if "org.ofono.NetworkRegistration" in ifaces:
            nr = (await call(self.bus, "org.ofono", self.modem, "org.ofono.NetworkRegistration", "GetProperties"))[0]
            st = _v(nr.get("Status"))
            self.ind["service"] = 1 if st in ("registered", "roaming") else 0
            self.ind["roam"] = 1 if st == "roaming" else 0
            if "Strength" in nr:
                self.ind["signal"] = max(0, min(5, round(int(_v(nr["Strength"])) / 20)))
            self.operator = _v(nr.get("Name")) or ""
        calls = []
        if "org.ofono.VoiceCallManager" in ifaces:
            for path, cp in (await call(self.bus, "org.ofono", self.modem, "org.ofono.VoiceCallManager", "GetCalls"))[0]:
                st = _v(cp.get("State"))
                calls.append((path, {"state": st, "number": _v(cp.get("LineIdentification")) or "",
                                     "name": _v(cp.get("Name")) or "",
                                     "dir": 1 if st in ("incoming", "waiting") or _v(cp.get("Incoming")) else 0}))
        self.calls = calls
        states = [c["state"] for _, c in calls]
        self.ind["call"] = 1 if any(s in ("active", "held") for s in states) else 0
        self.ind["callheld"] = (1 if "active" in states else 2) if "held" in states else 0
        self.ind["callsetup"] = (1 if any(s in ("incoming", "waiting") for s in states) else
                                 3 if "alerting" in states else 2 if "dialing" in states else 0)

    def first(self, *states):
        for path, c in self.calls:
            if c["state"] in states:
                return path, c
        return None, None

    async def answer(self):
        path, _ = self.first("incoming", "waiting")
        if not path:
            return False
        await call(self.bus, "org.ofono", path, "org.ofono.VoiceCall", "Answer")
        return True

    async def hangup(self):
        path, _ = self.first("incoming", "waiting", "dialing", "alerting", "active", "held")
        if not path:
            return False
        await call(self.bus, "org.ofono", path, "org.ofono.VoiceCall", "Hangup")
        return True

    async def dial(self, number):
        if not self.modem:
            return False
        await call(self.bus, "org.ofono", self.modem, "org.ofono.VoiceCallManager", "Dial", "ss", [number, "default"])
        return True


class AudioQueue:
    """Queue of audio blocks for a writer thread: at most max_bytes; when full, the oldest blocks are dropped
    (a gap is better than delayed music). put never blocks: the link receiver is never held up by pw-cat."""

    def __init__(self, max_bytes):
        self.max_bytes = max_bytes
        self.blocks = collections.deque()
        self.size = 0
        self.dropped = 0
        self.cv = threading.Condition()

    def put(self, data):
        with self.cv:
            self.blocks.append(data)
            self.size += len(data)
            while self.size > self.max_bytes and len(self.blocks) > 1:
                self.size -= len(self.blocks.popleft())
                self.dropped += 1
            self.cv.notify()

    def get(self, timeout=None):
        """The oldest block, or None if nothing arrives within timeout seconds."""
        with self.cv:
            if not self.blocks:
                self.cv.wait(timeout)
            if not self.blocks:
                return None
            data = self.blocks.popleft()
            self.size -= len(data)
            return data

    def clear(self):
        with self.cv:
            self.blocks.clear()
            self.size = 0


class BridgeState:
    """Call state and audio from the bridge (Carlinkit/CarPlay) instead of from the iPhone via oFono: HFP_SOURCE=bridge.

    The bridge (mtsmtsmtspi/bridge.py --bike-audio 127.0.0.1:9160) connects here with the same protocol as the emulated bike
    (myspin/linkproto): 44.1 kHz stereo music → A2DP to the bike (pw-cat), 16 kHz voice → 8 kHz SCO, events
    ring/outgoing/call_active/call_ended; from the bike: microphone 8 → 16 kHz and answer/hangup/dial commands.
    Same interface as IphoneState for Link (AT, indicators, ringing)."""
    PORT = 9160
    MUSIC_QUEUE = lp.Link.MUSIC_BACKLOG              # 0.5 s of music queued for pw-cat, anything beyond is dropped

    def __init__(self, ag):
        self.ag = ag
        self.modem = "bridge"
        self.ind = {"service": 1, "call": 0, "callsetup": 0, "callheld": 0, "signal": 5, "roam": 0, "battchg": 5}
        self.operator = "CarPlay"
        self.calls = []
        self.link = None
        self.voice = bytearray()
        self.voice_lock = threading.Lock()
        self.music_proc = None
        self.music_sink = None
        self.music_q = AudioQueue(self.MUSIC_QUEUE)
        self.loop = None
        self.vr = False                # voice assistant active: voice channel open without a call

    async def poll(self):
        pass

    def first(self, *states):
        for path, c in self.calls:
            if c["state"] in states:
                return path, c
        return None, None

    async def _cmd(self, **kw):
        if self.link:
            await self.link.send_json({"type": "hfp", **kw})
            return True
        return False

    async def answer(self):
        return await self._cmd(cmd="answer")

    def vr_as_call(self):
        if not VR_ENABLED or VR_CALL_MODE == "0":
            return False
        if VR_CALL_MODE == "1":
            return True
        return not any(getattr(link, "hf_features", 0) & 8 for link in getattr(self.ag, "links", {}).values())

    async def hangup(self):
        if self.calls and self.calls[0][0] == "siri":      # the "call" is Siri: the bike's hang-up closes Siri
            return await self._cmd(cmd="siri", on=False)
        return await self._cmd(cmd="hangup")

    async def dial(self, number):
        return await self._cmd(cmd="dial", number=number)

    async def voice_recognition(self, on):
        """AT+BVRA from the bike: the bridge starts Siri on the Carlinkit. If Siri then does not start, the bridge sends siri_stop."""
        ok = await self._cmd(cmd="siri", on=bool(on))
        self.vr = bool(ok and on)
        return ok

    def mic(self, data8k):                          # "sco" thread: bike microphone → bridge (16 kHz)
        if self.link and self.loop:
            s = struct.unpack(f"<{len(data8k) // 2}h", data8k)
            up = struct.pack(f"<{len(s) * 2}h", *[v for x in s for v in (x, x)])
            self.loop.call_soon_threadsafe(lambda: spawn(self.link.send(lp.MIC, up)))

    def _bike_sink(self):
        macs = [mac_of(d).replace(":", "_") for d in self.ag.links]
        out = subprocess.run(["pw-dump"], capture_output=True).stdout
        for o in json.loads(out or b"[]"):
            props = ((o.get("info") or {}).get("props") or {})
            name = props.get("node.name", "")
            if name.startswith("bluez_output") and str(props.get("api.bluez5.profile", "")).startswith("a2dp") and \
                    (not macs or any(m in name for m in macs)):
                return name
        return None

    MUSIC_IDLE_S = 2.0      # no music for this long (pause): pw-cat is closed

    def _close_music(self):
        p, self.music_proc = self.music_proc, None
        try:
            p.stdin.close()
            p.wait(timeout=2)
        except Exception:
            p.kill()

    def music_writer(self):
        """The "music" thread: queue → pw-cat stdin. If A2DP to the bike slows down (2.4 GHz) it blocks here, not in the
        link loop: JSON messages (ring, callsetup…) arrive immediately instead of waiting for the music (29 Oct 2026: ring +2.2 s)."""
        dropped, last_log, no_sink, last_pcm = 0, 0.0, 0.0, 0.0
        while True:
            pcm = self.music_q.get(1.0)
            if self.music_q.dropped != dropped and time.monotonic() - last_log > 5:
                dropped, last_log = self.music_q.dropped, time.monotonic()
                self.ag.log("hfp_bridge_music_dropped", blocks=dropped)
            if getattr(self, "vr", False):
                # Siri: the music stream is closed at once instead of after MUSIC_IDLE_S, and nothing reopens it while
                # Siri is on. The intercom gives its microphone only some time after the voice channel opens
                # (1.3 to 2.8 s on 17 Nov 2026): what is said before that is lost.
                if self.music_proc is not None:
                    self._close_music()
                    self.ag.log("hfp_bridge_music_idle", why="siri")
                continue
            if pcm is None:
                if self.music_proc is not None and time.monotonic() - last_pcm > self.MUSIC_IDLE_S:
                    # pause: pw-cat is closed, so A2DP to the bike goes to SUSPEND and on Play restarts with START.
                    # When kept open, the helmet intercom stayed silent after the pause even with music arriving again (11 Nov 2026)
                    self._close_music()
                    self.ag.log("hfp_bridge_music_idle")
                continue
            last_pcm = time.monotonic()
            if self.music_proc is None or self.music_proc.poll() is not None:
                if time.monotonic() - no_sink < 1:
                    continue                        # pw-dump at most once per second
                self.music_sink = self._bike_sink()
                if not self.music_sink:
                    no_sink = time.monotonic()
                    continue                        # bike not connected over A2DP: the music is lost
                self.music_proc = subprocess.Popen(["pw-cat", "--playback", "--raw", "--target", self.music_sink, "--rate", "44100",
                                                    "--channels", "2", "--format", "s16", "-"],
                                                   stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
                self.ag.log("hfp_bridge_music", sink=self.music_sink)
            try:
                self.music_proc.stdin.write(pcm)
                self.music_proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self.music_proc = None

    def on_json(self, m):
        if m.get("type") != "hfp":
            return
        ev = m.get("event")
        self.ind.update({k: v for k, v in (m.get("cind") or {}).items() if k in self.ind})
        if ev == "ring":
            self.calls = [("bridge", {"state": "incoming", "number": m.get("number") or "", "name": m.get("name") or "", "dir": 1})]
        elif ev == "outgoing":
            self.calls = [("bridge", {"state": "dialing", "number": m.get("number") or "", "name": m.get("name") or "", "dir": 0})]
        elif ev == "call_active":
            real = self.calls and self.calls[0][0] != "siri"     # a call that starts while Siri listens is not "Siri"
            c = dict(self.calls[0][1]) if real else {"number": "", "name": m.get("name") or "", "dir": 1}
            c["state"] = "active"
            self.calls = [("bridge", c)]
        elif ev == "call_ended":
            self.calls = []
        elif ev in ("siri_start", "siri_stop") and VR_ENABLED:
            self.vr = ev == "siri_start"
            if self.vr:
                self.music_q.put(b"")              # wakes the "music" thread: it closes the stream without waiting
            if self.vr and not self.calls and self.vr_as_call():
                self.calls = [("siri", {"state": "active", "number": "", "name": "Siri", "dir": 0})]
            elif not self.vr and self.calls and self.calls[0][0] == "siri":
                self.calls = []
        states = [c["state"] for _, c in self.calls]
        self.ind["call"] = 1 if "active" in states else 0
        self.ind["callsetup"] = 1 if "incoming" in states else 2 if "dialing" in states else 0
        self.ag.log("hfp_bridge_event", ev=ev, name=m.get("name"), **({"cind": m.get("cind")} if ev == "indicators" else {}))

    async def serve(self):
        self.loop = asyncio.get_running_loop()
        threading.Thread(target=self.music_writer, name="music", daemon=True).start()

        async def on_conn(r, w):
            self.link = lp.Link(r, w, "bridge")
            self.music_q.clear()
            self.ag.log("hfp_bridge_connected")
            try:
                while True:
                    kind, data = await self.link.recv()
                    if kind is None:
                        break
                    if kind == lp.MUSIC:                # never blocking: the "music" thread writes it
                        self.music_q.put(data)
                    elif kind == lp.VOICE:              # 16 kHz → 8 kHz for SCO
                        s = struct.unpack(f"<{len(data) // 2}h", data)
                        d = struct.pack(f"<{len(s) // 2}h", *[(s[i] + s[i + 1]) // 2 for i in range(0, len(s) - 1, 2)])
                        with self.voice_lock:
                            self.voice.extend(d)
                            if len(self.voice) > 8000:
                                del self.voice[:len(self.voice) - 1600]
                    elif kind == lp.JSON:
                        self.on_json(data)
            finally:
                self.link = None
                self.vr = False
                self.ag.log("hfp_bridge_closed")
        await asyncio.start_server(on_conn, "127.0.0.1", self.PORT)
        self.ag.log("hfp_bridge_listening", port=self.PORT)


class Link:
    """One HFP connection with the bike (RFCOMM channel): AT commands, indicators, ringing, SCO."""

    def __init__(self, ag, device, sock):
        self.ag, self.device, self.sock = ag, device, sock
        self.mac = mac_of(device)
        self.adapter = device.split("/")[3]
        self.writer = None
        self.cmer = False
        self.clip = False
        self.slc = False
        self.hf_features = 0
        self.bvra_told = False
        self.sent = {}
        self.sco = None
        self.audio_task = None
        self.last_number = ""
        self.vr_sent = False           # voice assistant state already known to the bike

    def log(self, event, **kw):
        self.ag.log(event, bike=self.mac, **kw)

    def send(self, line):
        self.writer.write(f"\r\n{line}\r\n".encode())
        if line not in ("OK", "RING") and not line.startswith("+CIEV"):
            self.log("hfp_ag_tx", line=line[:80])

    async def run(self):
        reader, self.writer = await asyncio.open_connection(sock=self.sock)
        self.log("hfp_ag_connected", adapter=self.adapter)
        ticker = asyncio.ensure_future(self.ticker())
        buf = b""
        try:
            while True:
                data = await reader.read(512)
                if not data:
                    break
                buf += data
                while b"\r" in buf:
                    line, buf = buf.split(b"\r", 1)
                    line = line.strip().decode(errors="replace")
                    if line:
                        await self.command(line)
                await self.writer.drain()
        except (ConnectionError, OSError) as e:
            self.log("hfp_ag_error", error=f"{type(e).__name__}: {e}")
        finally:
            ticker.cancel()
            self.sco_close()
            self.writer.close()
            self.log("hfp_ag_disconnected")

    # -------------------------------------------------------------- commands from the bike
    async def command(self, line):
        st = self.ag.state
        up = line.upper()
        self.log("hfp_ag_rx", line=line[:80])
        ok = True
        if up.startswith("AT+BRSF="):
            self.hf_features = int(up[8:] or 0)
            self.send(f"+BRSF: {AG_FEATURES}")
        elif up == "AT+CIND=?":
            self.send("+CIND: " + ",".join(f'("{n}",{r})' for n, r in INDICATORS))
        elif up == "AT+CIND?":
            self.send("+CIND: " + ",".join(str(st.ind[n]) for n, _ in INDICATORS))
            self.sent = dict(st.ind)
        elif up.startswith("AT+CMER="):
            parts = up[8:].split(",")
            self.cmer = len(parts) >= 4 and parts[3].strip() == "1"
            self.send("OK")
            ok = None                              # OK already sent: indicators start only after it
            self.slc = True
            self.log("hfp_ag_slc", hf_features=self.hf_features)
        elif up == "AT+CHLD=?":
            self.send("+CHLD: (0,1,2)")
        elif up.startswith("AT+CLIP="):
            self.clip = up.endswith("1")
        elif up.startswith(("AT+CCWA=", "AT+NREC=", "AT+VGS=", "AT+VGM=", "AT+CMEE=", "AT+BIA=", "AT+COPS=3")):
            pass
        elif up == "AT+COPS?":
            self.send(f'+COPS: 0,0,"{st.operator[:16]}"')
        elif up == "AT+CREG?":                     # the dashboard asks for it every minute
            self.send(f"+CREG: 0,{1 if st.ind['service'] else 0}")
        elif up in ("AT+CGMR", "AT+CGMM", "AT+CGMI", "AT+CGSN"):
            self.send({"AT+CGMR": "1.0", "AT+CGMM": os.environ.get("BT_NAME", "MTS Pi"), "AT+CGMI": "Raspberry Pi",
                       "AT+CGSN": "000000000000000"}[up])
        elif up == "AT+CNUM":
            pass                                   # own number: not provided
        elif up == "AT+BTRH?":
            pass
        elif up == "AT+CLCC":
            for i, (_, c) in enumerate(st.calls, 1):
                stat = {"active": 0, "held": 1, "dialing": 2, "alerting": 3, "incoming": 4, "waiting": 5}.get(c["state"], 0)
                num = c["number"]
                self.send(f'+CLCC: {i},{c["dir"]},{stat},0,0,"{num}",{145 if num.startswith("+") else 129}')
        elif up == "ATA":
            ok = await self.ag.safe(st.answer())
        elif up == "AT+CHUP":
            ok = await self.ag.safe(st.hangup())
        elif up.startswith("ATD") and up.endswith(";"):
            num = line[3:-1]
            if num.startswith(">"):                # memory dialling: not supported
                ok = False
            else:
                self.last_number = num
                ok = await self.ag.safe(st.dial(num))
        elif up == "AT+BLDN":
            ok = bool(self.last_number) and await self.ag.safe(st.dial(self.last_number))
        elif up.startswith("AT+BVRA="):            # voice assistant: only with HFP_VR=1 and bridge source (CarPlay)
            fn, on = getattr(st, "voice_recognition", None), up[8:9] == "1"
            ok = bool(VR_ENABLED and fn) and await self.ag.safe(fn(on))
            if ok:
                self.vr_sent = on                  # requested by the bike: no +BVRA sent back
            self.log("hfp_ag_vr", on=on, ok=ok, source="bike")
        else:
            ok = False
            self.log("hfp_ag_unknown", line=line[:80])
        if ok is not None:
            self.send("OK" if ok else "ERROR")

    # -------------------------------------------------------------- indicators, ringing, voice
    async def ticker(self):
        n = 0
        while True:
            await asyncio.sleep(0.5)
            n += 1
            if not self.slc:
                continue
            st = self.ag.state
            if self.cmer:
                for i, (name, _) in enumerate(INDICATORS, 1):
                    if self.sent.get(name) != st.ind[name]:
                        self.send(f"+CIEV: {i},{st.ind[name]}")
                        if name in ("call", "callsetup", "callheld"):
                            self.log("hfp_ag_indicator", name=name, value=st.ind[name])
                        self.sent[name] = st.ind[name]
            path, inc = st.first("incoming")
            if inc and n % 6 == 1:                 # every 3 s
                self.send("RING")
                if self.clip:
                    num = inc["number"]
                    name = inc["name"].replace('"', "")
                    self.send(f'+CLIP: "{num}",{145 if num.startswith("+") else 129},,,"{name}"')
            vr = bool(getattr(st, "vr", False))
            if vr != self.vr_sent:                 # Siri started or ended on the CarPlay side
                as_call = any(p == "siri" for p, _ in st.calls)
                # the bike declares voice assistant support: tell it (and take it back only if it was told)
                if self.hf_features & 8 and ((vr and not as_call) or (not vr and self.bvra_told)):
                    self.send(f"+BVRA: {int(vr)}")
                    self.bvra_told = vr
                self.log("hfp_ag_vr", on=vr, ok=True, source="carplay")
                self.vr_sent = vr
            want_audio = st.ind["call"] == 1 or st.ind["callsetup"] in (2, 3) or self.ag.test_sco or vr
            if want_audio and not self.sco and self.ag.sco_enabled:
                await self.sco_open()
            elif not want_audio and self.sco:
                self.sco_close()

    async def sco_open(self):
        try:
            local = _v((await call(self.ag.bus, "org.bluez", f"/org/bluez/{self.adapter}",
                                   "org.freedesktop.DBus.Properties", "Get", "ss",
                                   ["org.bluez.Adapter1", "Address"]))[0])
            s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_SCO)
            s.bind(local.encode() if isinstance(local, str) else local)
            s.setblocking(False)
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(loop.sock_connect(s, self.mac.encode()), 8)
            try:
                mtu = struct.unpack("H", s.getsockopt(SOL_SCO, SCO_OPTIONS, 2))[0]
            except OSError:
                mtu = 48
            self.sco = s
            self.log("hfp_ag_sco_open", mtu=mtu)
            # voice in its own thread: the asyncio loop (AT, oFono) must never delay it (bursts = internal radio
            # in "hardware error", analysis of 24 Oct 2026)
            self.audio_stop = threading.Event()
            self.audio_task = threading.Thread(target=self.ag.audio, args=(self, s, self.audio_stop),
                                               name="sco", daemon=True)
            self.audio_task.start()
        except Exception as e:
            self.log("hfp_ag_sco_error", error=f"{type(e).__name__}: {e}")
            await asyncio.sleep(3)

    def sco_close(self):
        if self.audio_task:
            self.audio_stop.set()
            self.audio_task = None
        if self.sco:
            self.sco.close()
            self.sco = None
            self.log("hfp_ag_sco_closed")


class HfpAg:
    def __init__(self, log, adapters=("hci0",), tone=False):
        self.log = log
        self.adapters = set(adapters)
        self.tone = tone
        # HFP_SCO=0: signalling only (icons, ringing, answer) and never voice to the bike: on the Pi 3 voice + Wi-Fi
        # hang the internal radio (tests of 23 Oct 2026)
        self.sco_enabled = os.environ.get("HFP_SCO", "1") != "0"
        # HFP_TEST_SCO=1: voice channel to the bike opened right after connecting, without a call (stability test
        # of the internal radio with Wi-Fi video, with no iPhone involved)
        self.test_sco = os.environ.get("HFP_TEST_SCO") == "1"
        self.bus = None
        self.state = None
        self.links = {}

    async def safe(self, coro):
        try:
            return bool(await coro)
        except Exception as e:
            self.log("hfp_ag_ofono_error", error=f"{type(e).__name__}: {e}")
            return False

    def incoming(self, device, fd):
        sock = socket.socket(fileno=os.dup(fd))
        os.close(fd)
        adapter = device.split("/")[3]
        if adapter not in self.adapters:           # the AG is only for the bike: connections from the other radio are rejected
            self.log("hfp_ag_rejected", device=mac_of(device), adapter=adapter)
            sock.close()
            return
        sock.setblocking(False)
        link = Link(self, device, sock)
        self.links[device] = link
        spawn(link.run())

    async def run(self):
        self.bus = await system_bus()
        self.bridge_mode = os.environ.get("HFP_SOURCE") == "bridge"
        self.state = BridgeState(self) if self.bridge_mode else IphoneState(self.bus)
        if self.bridge_mode:
            await self.state.serve()
        self.bus.export(PATH, _Profile(self))
        # no Role: BlueZ both accepts connections AND opens them itself towards the hands-free (bike/intercom) when it connects
        opts = {"Name": Variant("s", "Voice Gateway"),
                "Channel": Variant("q", 13), "Version": Variant("q", 0x0107),
                "Features": Variant("q", SDP_FEATURES),
                "RequireAuthentication": Variant("b", False), "RequireAuthorization": Variant("b", False),
                "AutoConnect": Variant("b", True)}
        await call(self.bus, "org.bluez", "/org/bluez", "org.bluez.ProfileManager1", "RegisterProfile",
                   "osa{sv}", [PATH, UUID_HFP_AG, opts])
        self.log("hfp_ag_listening", adapters=sorted(self.adapters), sco=self.sco_enabled)
        self._alive = alive.start()         # a stuck main loop gets the program restarted by systemd (WatchdogSec)
        last = None
        while True:
            try:
                await self.state.poll()
            except Exception as e:
                self.log("hfp_ag_poll_error", error=f"{type(e).__name__}: {e}")
            now = (self.state.modem, tuple(c["state"] for _, c in self.state.calls))
            if now != last:
                self.log("hfp_ag_iphone", modem=bool(self.state.modem), calls=list(now[1]))
                last = now
            await asyncio.sleep(0.5)

    # -------------------------------------------------------------- voice ("sco" thread, blocking socket)
    def audio(self, link, s, stop):
        """The radio sets the pace: for each packet received from the bike, ONE packet of the same size is sent,
        immediately. The BCM43455 has a single SCO buffer: never packets ahead of time, never bursts."""
        try:
            s.setblocking(True)
            s.settimeout(1.0)
            if self.tone:
                return self._pump(link, s, stop, None)
            if getattr(self, "bridge_mode", False):
                return self._pump(link, s, stop, "bridge")
            iphone = None
            for _ in range(16):                    # the iPhone opens its voice channel a few moments after answering
                iphone = self.iphone_nodes()
                if iphone or stop.is_set():
                    break
                time.sleep(0.5)
            if not iphone:
                link.log("hfp_ag_audio_no_iphone_nodes")
                return self._pump(link, s, stop, None)
            self._pump(link, s, stop, iphone)
        except Exception as e:
            link.log("hfp_ag_audio_end", error=f"{type(e).__name__}: {e}")
        finally:
            try:
                s.close()
            except OSError:
                pass

    def _pump(self, link, s, stop, iphone):
        """iphone = PipeWire (source, sink) of the hands-free link to the iPhone, or None = 440 Hz test tone
        (bike microphone to /tmp/bike-mic.raw). Every 500 packets it logs the maximum gap between two arrivals."""
        rec = play = None
        buf = bytearray()
        lock = threading.Lock()
        if iphone == "bridge":                      # voice from the bridge (Carlinkit), microphone to the bridge
            buf, lock = self.state.voice, self.state.voice_lock
            link.log("hfp_ag_audio_bridge", source="carplay")
        elif iphone:
            fmt = ["--rate", "8000", "--channels", "1", "--format", "s16"]
            rec = subprocess.Popen(["pw-record", "--target", iphone[0], *fmt, "-"], stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL)
            play = subprocess.Popen(["pw-play", "--target", iphone[1], *fmt, "-"], stdin=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL)
            link.log("hfp_ag_audio_bridge", source=iphone[0], sink=iphone[1])

            def from_iphone():
                while not stop.is_set():
                    chunk = rec.stdout.read1(4096)
                    if not chunk:
                        return
                    with lock:
                        buf.extend(chunk)
                        if len(buf) > 8000:        # more than 0.5 s of delay: the old data is discarded
                            del buf[:len(buf) - 1600]
            threading.Thread(target=from_iphone, name="sco-iphone", daemon=True).start()
        mic = None if iphone else open("/tmp/bike-mic.raw", "wb")
        # Default (HFP_PACE=rx): one packet sent for each one received. The internal BCM43455 radio has a single SCO buffer:
        # clock-paced it goes into "hardware error" within 1-8 s (25 Oct 2026); paced by received packets it holds up with Wi-Fi video at 2 fps.
        # HFP_PACE=clock (16 KB/s) was for the UB500 over USB, where "rx" gave ~13.5 KB/s and a wavering tone (24 Oct 2026).
        if os.environ.get("HFP_PACE", "rx") == "clock":
            def clock_sender():
                # 48 B = internal radio; the ESP32 (radio.sh esp32) receives and transmits in 120 B packets
                size, k2, sent, t0 = int(os.environ.get("HFP_SCO_SIZE", "48")), 0, 0, time.monotonic()
                while not stop.is_set():
                    if iphone:
                        with lock:
                            out = bytes(buf[:size]) if len(buf) >= size else bytes(size)
                            del buf[:size]
                    else:
                        out = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * (k2 + i) / 8000)))
                                       for i in range(size // 2))
                        k2 += size // 2
                    try:
                        s.send(out)
                    except OSError:
                        return
                    sent += 1
                    if sent % 1000 == 0:
                        link.log("hfp_ag_clock", packets=sent, bytes_s=round(sent * size / (time.monotonic() - t0)))
                    wait = t0 + sent * size / 16000 - time.monotonic()
                    if wait > 0:
                        time.sleep(wait)
            threading.Thread(target=clock_sender, name="sco-clock", daemon=True).start()
        clock = os.environ.get("HFP_PACE", "rx") == "clock"
        rx_bytes, rx_t0 = 0, time.monotonic()
        k = got = 0
        last = time.monotonic()
        gap_max = 0.0
        try:
            while not stop.is_set():
                try:
                    data = s.recv(512)
                except socket.timeout:
                    continue
                if not data:
                    return
                now = time.monotonic()
                if not rx_bytes:                            # how long the bike or the intercom takes to give its microphone
                    link.log("hfp_ag_voice_first", after_ms=round((now - rx_t0) * 1000))
                gap_max = max(gap_max, now - last)
                last = now
                n = len(data)
                rx_bytes += n
                if clock:                                   # sending is done by clock_sender: microphone only here
                    if iphone == "bridge":
                        self.state.mic(data)
                    elif iphone:
                        try:
                            play.stdin.write(data)
                            play.stdin.flush()
                        except (BrokenPipeError, OSError):
                            pass
                    else:
                        mic.write(data)
                    got += 1
                    if got % 1000 == 0:
                        link.log("hfp_ag_rx_rate", packets=got, size=n, bytes_s=round(rx_bytes / (now - rx_t0)),
                                 gap_max_ms=round(gap_max * 1000, 1))
                        gap_max = 0.0
                    continue
                if iphone:
                    with lock:
                        out = bytes(buf[:n]) if len(buf) >= n else bytes(n)
                        del buf[:n]
                    if iphone == "bridge":
                        self.state.mic(data)
                    else:
                        try:
                            play.stdin.write(data)
                            play.stdin.flush()
                        except (BrokenPipeError, OSError):
                            pass
                else:
                    mic.write(data)
                    out = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * (k + i) / 8000)))
                                   for i in range(n // 2))
                    k += n // 2
                s.send(out)
                got += 1
                if got % 500 == 0:
                    link.log("hfp_ag_tone" if not iphone else "hfp_ag_voice", packets=got, size=n,
                             gap_max_ms=round(gap_max * 1000, 1))
                    gap_max = 0.0
        finally:
            for p in (rec, play):
                if p and p.poll() is None:
                    p.kill()
            if mic:
                mic.close()
            link.log("hfp_ag_audio_stopped", packets=got)

    def iphone_nodes(self):
        """PipeWire nodes of the hands-free link to the iPhone (oFono): source = the iPhone's voice, sink = microphone towards it."""
        mac = (self.state.modem or "").rsplit("dev_", 1)[-1]
        if not mac:
            return None
        out = subprocess.run(["pw-dump"], capture_output=True).stdout
        src = sink = None
        for o in json.loads(out or b"[]"):
            props = ((o.get("info") or {}).get("props") or {})
            name = props.get("node.name", "")
            if mac not in name or str(props.get("api.bluez5.profile", "")).startswith("a2dp"):
                continue                           # music (A2DP) is not the call voice
            if props.get("media.class") == "Audio/Source" and name.startswith("bluez_input"):
                src = name
            elif props.get("media.class") == "Audio/Sink" and name.startswith("bluez_output"):
                sink = name
        return (src, sink) if src and sink else None


def bike_adapters():
    """The bike's radio = the internal one (UART); hciN names change at every boot or USB reset."""
    return [h for h in sorted(os.listdir("/sys/class/bluetooth")) if ":" not in h and
            "usb" not in os.path.realpath(f"/sys/class/bluetooth/{h}/device")] or ["hci0"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--adapter", action="append", help="radio towards the bike (default: the internal one)")
    ap.add_argument("--tone", action="store_true", help="during an active call: test tone to the bike instead of the iPhone voice")
    a = ap.parse_args()

    def log(event, **kw):
        print(json.dumps({"t": round(time.time(), 3), "event": event, **kw}, ensure_ascii=False), flush=True)
    tone = a.tone or os.environ.get("HFP_TONE") == "1"
    asyncio.run(HfpAg(log, a.adapter or bike_adapters(), tone).run())


if __name__ == "__main__":
    sys.exit(main())
