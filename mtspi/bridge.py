#!/usr/bin/env python3
"""iPhone/CarPlay -> Multistrada bridge (the program that runs on the Pi), bench version.

Design decided on 22 Oct 2026:
- Bluetooth with the iPhone ALWAYS on (the Pi is its car stereo): A2DP + AVRCP music, HFP calls, PBAP
  phonebook, MAP messages. Towards the bike the Pi is the phone: everything works from the native menu even
  without a screen (as it does today with the Ducati Connect app closed).
- CarPlay ON DEMAND: in the Pi menu on the dashboard (Ducati Connect), "Start CarPlay" turns the Carlinkit on
  (Open + wifiConnect); the Ducati icon inside CarPlay goes back to the menu (requestHostUI); "Turn CarPlay off" or
  leaving Ducati Connect for --carplay-idle-off seconds closes it (CloseDongle).
- Audio to the bike comes from whichever source is sending it (Bluetooth or Carlinkit): switching is automatic.

On the bench: iPhone Bluetooth, Carlinkit USB and bike Bluetooth are TCP connections to the emulated programs.
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import os
import re
import struct
import sys
import time

import av  # loaded up front: importing it on the first frame froze the Pi for ~0.3 s (gap in the music)
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from mtspi import session as hv  # noqa: E402
from mtspi import carlinkit as ck  # noqa: E402
from mtspi import linkproto as lp  # noqa: E402
try:
    from mtspi import bluez  # noqa: E402  (Pi only: needs dbus-fast)
except ImportError:
    bluez = None
from mtspi import pbap  # noqa: E402
from mtspi.dashboard import Dashboard  # noqa: E402
from mtspi.i18n import t  # noqa: E402
from mtspi.settings import Settings  # noqa: E402
from myspin.phone import PhoneControl, PhoneProjection, discover_vehicle  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402
from mtspi.tasks import spawn
from mtspi import alive  # noqa: E402
from mtspi import recovery  # noqa: E402

# Siri from the helmet (the joystick held down, the bike's phone button): works on the bench since 17 Nov 2026, not yet
# tried on the bike, so it is off unless HFP_VR=1 (the same switch that lets the hands-free open the voice without a call).
SIRI_ENABLED = os.environ.get("HFP_VR", "0") == "1"
# SIRI_WAIT_MIC=1: the command goes to CarPlay only when the helmet microphone is open. Off by default: on the bike
# (21 Nov 2026) Siri was started at once and it worked, because the bike plays a tone in the helmet when it has
# connected it, and that is when the rider speaks. The wait has been tried on the bench only.
SIRI_WAIT_MIC = os.environ.get("SIRI_WAIT_MIC", "0") == "1"


def oem_icon_png(label: str, path: str = "") -> bytes:
    """Manufacturer icon for CarPlay (256x256 PNG): the user-chosen file if present (e.g. the bike logo,
    which is not in the repo: it is a trademark), otherwise a red square with the initial."""
    import io
    if path and os.path.isfile(path):
        try:
            img = Image.open(path).convert("RGB").resize((256, 256))
            buf = io.BytesIO()
            img.save(buf, "PNG")
            return buf.getvalue()
        except Exception:
            pass
    from mtspi.testui import _font
    img = Image.new("RGB", (256, 256), (204, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([18, 18, 238, 238], radius=48, outline=(255, 255, 255), width=10)
    d.text((128, 132), (label or "M")[0].upper(), font=_font(150), fill=(255, 255, 255), anchor="mm")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class Display:
    """What the bike sees over mySPIN: the Pi menu or CarPlay."""

    def __init__(self, bridge):
        self.b = bridge
        self.mode = "launcher"
        self.carplay_frame = None
        self.last_cp_image = None      # last CarPlay image prepared for the bike (for the snap test command)
        self.held = None               # UP or DOWN pressed and not yet released (see on_key)
        self.carplay_seq = 0
        self.frames_sent = 0
        self.key_count = 0

    @property
    def version(self):
        if self.mode == "carplay" and self.carplay_frame is not None:
            return ("cp", self.carplay_seq)
        return ("menu", self.b.launcher.version)

    def render_in_thread(self):
        """CarPlay is rendered outside the main loop (conversion and scaling in C); the menu is not."""
        return self.mode == "carplay" and self.carplay_frame is not None

    def render(self, w, h):
        if self.mode == "carplay" and self.carplay_frame is not None:
            fr = self.carplay_frame                  # raw av.VideoFrame: only what goes to the bike gets converted
            # CarPlay at the TFT height, aspect ratio kept; leftover space = bars (mockup A/B):
            # little (A, 1280x480 → 811 px) = two thin bars on the sides, a lot (B, 1100x480 → 697 px) = one bar on the right
            nw = min(w, round(fr.width * h / fr.height)) & ~1
            if nw >= w - 4:
                canvas = fr.reformat(width=w, height=h, format="rgb24").to_image()
            else:
                canvas = Image.new("RGB", (w, h))
                canvas.paste(fr.reformat(width=nw, height=h, format="rgb24").to_image(),
                             ((w - nw) // 2 if w - nw < 60 else 0, 0))
            self.last_cp_image = canvas          # from here on nobody modifies it any more
            return canvas
        return self.b.launcher.render(w, h)

    def on_key(self, key, action):
        self.key_count += 1
        if action not in ("PRESS", "CLICK", "LONG_PRESS", "RELEASE"):
            return
        if self.mode != "carplay":
            # The Pi's own pages: the joystick held up is Back here too. On the bike (21 Nov 2026) there was no way
            # out of Settings, which waited for a Back key the joystick does not have. UP and DOWN act when released,
            # as in CarPlay, so that a hold is Back alone.
            if key in ("UP", "DOWN") and action != "CLICK":
                if action == "PRESS":
                    self.held = key
                elif action == "LONG_PRESS":
                    self.held = None
                    if key == "UP":
                        self.b.launcher.on_key("BACK")
                elif self.held == key:             # RELEASE of a short press
                    self.held = None
                    self.b.launcher.on_key(key)
                return
            if action != "RELEASE":
                self.b.launcher.on_key(key)
            return
        # On the bike (16 Nov 2026) the joystick has no Back key: a long LEFT is the dashboard's own "leave Ducati
        # Connect", a long centre press never reaches us, LEFT/RIGHT/OK arrive only as clicks. UP and DOWN are the
        # only keys with a hold: PRESS, then LONG_PRESS after about 0.9 s, then RELEASE. So: UP held = Back,
        # DOWN held = Siri (where it is switched on). UP and DOWN act when they are released: a hold is then Back
        # or Siri alone, without first moving the selection by one step (17 Nov 2026).
        if key in ("UP", "DOWN") and action != "CLICK":
            if action == "PRESS":
                self.held = key
            elif action == "LONG_PRESS":
                self.held = None
                if key == "UP":
                    self.b.carplay_key("BACK")
                elif SIRI_ENABLED:
                    self.b.spawn(self.b.bike_siri(True))
            elif self.held == key:                 # RELEASE of a short press
                self.held = None
                self.b.carplay_key(key)
            return
        if action in ("PRESS", "CLICK"):           # any other LONG_PRESS is dropped (it used to repeat the key)
            self.b.carplay_key(key)

    def on_frame_sent(self, size):
        self.frames_sent += 1


class _Present:
    """Device connected over real Bluetooth: it is present, but has no emulated link to send messages to."""

    def __init__(self, info):
        self.info = info

    async def send_json(self, msg):
        pass

    async def send(self, kind, payload):
        # Carlinkit audio goes to the bike via bike_audio (mtspi-hfp in the standard variant, ESP32 in the pro variant), not from here
        pass


class Bridge:
    recovery = None                    # mtspi/recovery.py, set in __init__ when the real radios are in use
    siri_mic_open = None               # an Event while Siri waits for the helmet microphone
    _control_idle_since = None
    _ghost_noted = False               # the unknown voice link was already written in the log
    _ghost_bounce_t = -1e9

    def __init__(self, a):
        self.a = a
        self.log = hv.JsonLog(a.log, a.quiet)
        self.settings_path = os.path.expanduser(a.settings)
        self.settings = Settings.load(self.settings_path)
        if not os.path.exists(self.settings_path):
            self.settings.carplay_idle_off = int(a.carplay_idle_off)
        self.launcher = Dashboard(self)
        self.display = Display(self)
        self.iphone = None             # Bluetooth link with the iPhone
        self.iphone_name = None
        self.bike = None               # Bluetooth link with the bike (audio, HFP, PBAP/MAP)
        self.dongle = None             # CarlinkitHost
        self.session = None            # mySPIN session
        self.media = None
        self.call = None               # {"state", "name", "number", "audio": "bt"|"carplay"}
        self.carplay_user_off = False  # CarPlay turned off from the menu (do not turn it back on automatically)
        self.contacts, self.recent, self.messages = [], [], []
        # phonebook served to the bike (PBAP): the last one downloaded from the iPhone stays on the SD card, so it is available right at startup
        self.phonebook = pbap.Phonebook()
        self.player_ready = asyncio.Event()
        self.music = None                  # AVRCP player towards the bike in the standard variant (music_bridge)
        if a.phonebook_cache and self.phonebook.load(a.phonebook_cache):
            self.log("phonebook_cached", contacts=len(self.phonebook.folders["pb"]), recent=len(self.phonebook.folders["cch"]))
            # also for caller names (name_for) and the home screen: the download from the iPhone now waits for the music to stop
            if bluez:
                self.contacts = bluez.parse_vcards(self.phonebook.books.get("pb", ""))
                self.recent = bluez.parse_vcards(self.phonebook.books.get("cch", ""), limit=50)
        self.cind = {}
        self.carplay_wanted = False
        self.carplay_since = None
        self.audio_src = {"bt": 0.0, "carplay": 0.0}
        self._cp_types, self._cp_mix = {}, bytearray()     # active CarPlay audio streams (mix_carplay)
        self.bench_route = None
        self.bt = None                 # BtState with real Bluetooth (--bt real)
        self.idle_task = None
        self.cp_audio_s = 0.0
        self.siri_until = 0.0          # CarPlay Siri active until this instant (0 = no): helmet microphone open
        self._cp_audible = 0.0         # last NON-silent CarPlay audio (after a pause the Carlinkit sends zeros for ~5 s)
        self.cp_media = {}             # CarPlay track (Carlinkit MediaData 1): title, artist, album, duration, status
        self._media_sent = None
        self._aclk = 0.0
        self._burst_until = 0.0
        # gets the Pi back on its feet when the bike's Wi-Fi or the radio fail (mtspi/recovery.py); only with the real radios
        self.recovery = (recovery.Recovery(self.log) if a.bt == "real" and a.wifi != "none"
                         and os.environ.get("MIRROR_RECOVERY", "1") != "0" else None)
        self._control_idle_since = None   # when the bike's control channel last ended (None while one is open)
        self._iphone_retry = 0.0       # pro: last attempt to reconnect to the iPhone        # end of the anti-burst window (3 s after connecting or resuming)               # CarPlay music clock (to drop bursts)
        self._sms_event = asyncio.Event()   # "new message" from the iPhone (MNS): the inbox is re-read
        self.fast_bt_poll = False      # during a CarPlay ring the iPhone is polled more often (number)
        self.counts = {"cp_video": 0, "cp_audio": 0, "bt_music": 0, "bt_voice": 0, "bike_mic": 0}
        self.decoder = None
        self._dec_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="h264")
        self.t_start = time.monotonic()
        self.dongle_info = {}          # Carlinkit software version, BT/Wi-Fi name
        self._rates = {}               # per-second measurements for the diagnostics page
        self._sys_cache = (0, {})

    # ------------------------------------------------------------ utilities
    def spawn(self, coro):
        return spawn(coro)               # kept alive until it ends (see mtspi/tasks.py)

    def to_phone_bt(self, msg):
        if self.iphone:
            self.spawn(self.iphone.send_json(msg))

    def to_bike(self, msg):
        if self.bike:
            self.spawn(self.bike.send_json(msg))

    def carplay_state(self):
        if not self.dongle or self.dongle.state == "closed":
            return "off"
        return "on" if self.dongle.state == "connected" else "connecting"

    def audio_source(self):
        if time.monotonic() - self.audio_src["carplay"] < 0.5:     # CarPlay (Carlinkit) takes precedence
            return "CarPlay"
        if self.bt:                       # real Bluetooth: playing if the iPhone player is playing
            return "Bluetooth" if (self.bt.media or {}).get("status") == "playing" else "none"
        src, t = max(self.audio_src.items(), key=lambda kv: kv[1])
        return {"bt": "Bluetooth", "carplay": "CarPlay"}[src] if time.monotonic() - t < 0.5 else "none"

    def panel(self, what):
        st = self.carplay_state()
        if what == "carplay":
            if st == "connecting":
                return [("Connecting CarPlay…", True), (f"{time.monotonic() - (self.carplay_since or time.monotonic()):.0f} s", False)]
            return [("CarPlay " + ("on" if st == "on" else "off"), True),
                    ("OK = " + ("back to CarPlay" if st == "on" else "turn the Carlinkit on"), False),
                    ("MTS icon in CarPlay = back here", False)]
        if what == "carplay_off":
            return [("Turns the Carlinkit off", True), ("Music and calls stay on Bluetooth", False)]
        if what == "music":
            m = self.media or {}
            return [(m.get("title", "—"), True), (m.get("artist", ""), False),
                    (("playing" if m.get("status") == "playing" else "paused") + f" · audio: {self.audio_source()}", False),
                    ("OK = play/pause", False)]
        if what == "phone":
            c = self.call
            first = [(f"{c['name']}: {c['state']}", True)] if c else [(f"{len(self.contacts)} contacts", True)]
            return first + [(f"{len(self.messages)} messages, {len(self.recent)} recent calls", False),
                            ("Phonebook and messages also in the native Phone menu", False)]
        return [(f"iPhone: {self.iphone_name or 'not connected'}", True),
                (f"Carlinkit: {st}" + (f" · routing {self.bench_route}" if self.bench_route else ""), False),
                (f"Audio to the bike: {self.audio_source()}", False),
                (f"Bike Bluetooth: {'connected' if self.bike else 'no'}", False)]

    # ------------------------------------------------------------ settings
    def apply_settings(self):
        if self.session:
            self.session.jpeg_quality = self.settings.jpeg_quality
        self.log("settings", **self.settings.__dict__)

    def save_settings(self):
        try:
            self.settings.save(self.settings_path)
        except OSError as e:
            self.log("settings_save_error", error=str(e))

    # ------------------------------------------------------------ CarPlay on demand
    def request_carplay(self):
        self.carplay_user_off = False
        st = self.carplay_state()
        if st == "on":
            self.display.mode = "carplay"
            self.log("display", mode="carplay")
            self.to_bike({"type": "bench_display", "mode": "carplay"})
        elif st == "off" and self.dongle:
            self.carplay_wanted = True
            self.carplay_since = time.monotonic()
            self.dongle.state = "opening"
            self.spawn(self.dongle.start())
            self.log("carplay_requested")

    def request_carplay_off(self, user=True):
        self.carplay_user_off = user     # from the menu: stays off; after inactivity: turns back on at the next session
        if self.dongle and self.carplay_state() != "off":
            self.carplay_wanted = False
            self.display.mode = "launcher"
            self.to_bike({"type": "bench_display", "mode": "launcher"})
            self.spawn(self.dongle.stop())
            self.log("carplay_off_requested")

    async def manual_cmds(self):
        """Bench tests: echo 107 > /var/tmp/ck_cmd sends that command to the Carlinkit (undocumented codes)."""
        path = "/var/tmp/ck_cmd"
        while True:
            await asyncio.sleep(0.3)
            if not os.path.exists(path):
                continue
            try:
                words = open(path).read().split()
                os.remove(path)
            except OSError:
                continue
            for w in words:
                if w == "snap":                       # current CarPlay image (testing the buttons with nobody at the bike)
                    # Never touch the video frame here: the thread preparing the images for the bike is using it
                    # (11 Nov 2026: "free or corruption", bridge dead). Save the last image already prepared,
                    # and outside the main loop (the PNG was blocking it: stuttering music).
                    img = self.display.last_cp_image
                    if img is not None:
                        await asyncio.get_running_loop().run_in_executor(None, img.save, "/var/tmp/cp.png")
                    self.log("manual_snap", ok=img is not None)
                    continue
                if w.startswith(("callfps=", "musicfps=")):   # live video limit: "callfps=5", "musicfps=0" (0 = none)
                    k, v = w.split("=", 1)
                    setattr(self, "call_fps_live" if k == "callfps" else "music_fps", int(v))
                    self.log("manual_fps", **{k: int(v)})
                    continue
                if w.startswith("jpegq=") and self.session:   # live JPEG quality (2.4 GHz tests): "jpegq=60"
                    self.session.jpeg_quality = int(w[6:])
                    self.log("manual_jpeg", q=self.session.jpeg_quality)
                    continue
                if w.startswith("keymap=") and self.dongle:   # live key map: "keymap=tilt" / "keymap=arrows"
                    self.dongle.keymap = w[7:]
                    self.log("manual_keymap", keymap=w[7:])
                    continue
                if w == "siri":                       # Siri as the joystick held down starts it (press and release)
                    await self.bike_siri(True)
                    self.log("manual_cmd", code="siri")
                    continue
                if w.startswith("wait"):              # pause in seconds: "wait0.3"
                    await asyncio.sleep(float(w[4:] or 0.3))
                    continue
                if self.dongle:
                    code = int(w) if w.isdigit() else ck.CMD.get(w, 0)
                    await self.dongle.send(ck.message(ck.COMMAND, struct.pack("<I", code)))
                    self.log("manual_cmd", code=code)

    def carplay_key(self, key):
        if self.dongle:
            self.spawn(self.dongle.key(key))

    async def carlinkit(self):
        while True:
            if self.a.carlinkit == "usb":           # real Carlinkit on the Pi's USB
                from mtspi import carlinkit_usb
                link = await carlinkit_usb.connect(self.log)
            else:                                    # bench: Carlinkit emulated over the network
                host, port = self.a.carlinkit.rsplit(":", 1)
                link = await lp.connect(host, int(port), "carlinkit-usb", retry_s=3600)
            w, h = (int(x) for x in self.a.screen.lower().split("x"))
            self.dongle = ck.CarlinkitHost(link.reader, link.writer, self.log, w, h, self.a.fps,
                                           audio_transfer=self.a.audio_transfer, on_message=self.from_dongle,
                                           oem_label=self.a.oem_label, oem_icon_png=oem_icon_png(self.a.oem_label, self.a.oem_icon))
            self.log("carlinkit_usb_connected", note="dongle powered but closed: CarPlay on demand only")
            try:
                await self.dongle.configure_icon()
                if self.a.carplay_auto or self.settings.carplay_autostart:
                    self.request_carplay()
                await self.dongle.run()
            except (ConnectionError, OSError, asyncio.IncompleteReadError) as e:
                # dongle unplugged or link dropped: the bridge does not stop, it picks the dongle up again
                # (bad headers after a bridge restart: read_message skips them)
                self.log("carlinkit_lost", error=f"{type(e).__name__}: {e}")
            self.dongle = None
            self.display.mode = "launcher"
            await asyncio.sleep(1)

    @staticmethod
    def _decode(decoder, payload):
        # the dongle sends one whole frame per message: no parser (it would hold back the last one until the next)
        return list(decoder.decode(av.Packet(payload)))

    async def from_dongle(self, mtype, data):
        """A malformed Carlinkit message (JSON, header) must not restart the whole bridge."""
        try:
            await self._from_dongle(mtype, data)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.log("dongle_message_error", mtype=mtype, error=f"{type(e).__name__}: {e}"[:200])

    async def _from_dongle(self, mtype, data):
        if mtype in (ck.VIDEO_DATA, ck.AUDIO_DATA) and self.dongle and self.dongle.state == "opening":
            # bridge restarted with the dongle already in session (no USB reset): no "Plugged", but CarPlay is already up.
            # Request a full frame, otherwise the decoder waits for the next screen change
            self.dongle.state = "connected"
            self._burst_until, self._aclk = time.monotonic() + 3, 0.0
            self.log("carlinkit_session_resumed")
            self.spawn(self.dongle.send(ck.send_command("frame")))
        if mtype == ck.VIDEO_DATA:
            self.counts["cp_video"] += 1
            w, h, flags, length, _ = struct.unpack("<5I", data[:20])
            if self.decoder is None:
                self.decoder = av.CodecContext.create("h264", "r")
                # no frame threading: each extra thread delays a frame's output by one, and CarPlay sends
                # images only when the screen changes → the dashboard would lag a few moves behind
                self.decoder.thread_type = "SLICE"
                self.decoder.flags |= av.codec.context.Flags.low_delay if hasattr(av.codec.context, "Flags") else 0
            try:
                # decode in its own thread (always the same one: H.264 packet order matters), the loop stays free
                frames = await asyncio.get_running_loop().run_in_executor(self._dec_pool, self._decode, self.decoder, data[20:])
            except av.error.FFmpegError as e:
                # broken frame (dongle restarted midway, USB interference): drop it and restart the decoder
                self.log("carplay_decode_error", error=str(e)[:120])
                self.decoder = None
                frames = []
            for fr in frames:
                self.display.carplay_frame = fr
                self.display.carplay_seq += 1
                if self.session is not None and self.display.mode == "carplay":
                    self.session.screen_changed()
                if self.carplay_wanted and self.display.mode != "carplay":
                    self.display.mode = "carplay"
                    self.log("display", mode="carplay", connect_s=round(time.monotonic() - (self.carplay_since or 0), 1))
                    self.to_bike({"type": "bench_display", "mode": "carplay"})
                    self.carplay_wanted = False
        elif mtype == ck.AUDIO_DATA:
            au = ck.parse_audio(data)
            if "command" in au:
                cmd = au["command"]
                self.log("carlinkit_audio_cmd", cmd=cmd)
                if cmd in ("siri_start", "siri_stop"):
                    self.siri_set(cmd == "siri_start", self.SIRI_MAX_S)
                # the real Carlinkit (A15W, 26 Oct 2026) does not send CallStatus (MediaData 100): the call is inferred
                # from the audio commands. 14 = ringing, phonecall_start = active (outgoing too), phonecall_stop = ended
                ev = None
                cp_call = self.call if self.call and self.call.get("src") == "carplay" else None
                if cmd == "incoming_call" and not cp_call:
                    ev = "ring"
                elif cmd == "phonecall_start" and cp_call and cp_call.get("ending"):
                    # 29 Oct 2026: the iPhone moves the audio during an active call (stop and, ~1 s later, start): it is the same call
                    self.log("carplay_call_resumed", gap_s=round(time.monotonic() - cp_call.pop("ending"), 2))
                elif cmd == "phonecall_start" and (not self.call or self.call.get("state") != "active"):
                    ev = "call_active"
                elif cmd == "phonecall_stop" and cp_call and cp_call.get("state") == "active":
                    if not cp_call.get("ending"):     # real end or audio moved: the next start (or its absence) tells
                        cp_call["ending"] = time.monotonic()
                        self.spawn(self.carplay_call_end_after(cp_call))
                elif cmd == "phonecall_stop" and cp_call:
                    ev = "call_ended"                 # ring rejected or missed: immediately
                elif cmd == "media_start" and cp_call and cp_call.get("ending"):
                    await self.carplay_call_end("media_start")   # music restarts: the call has really ended
                if ev == "ring":
                    self.spawn(self.carplay_ring())       # waits for the number from the iPhone (at most 2.5 s)
                    ev = None
                if ev and not (self.call and self.call.get("src") not in (None, "carplay")):
                    if ev == "call_active" and not self.call:     # call placed from CarPlay: "outgoing" first
                        await self.from_iphone({"type": "hfp", "event": "outgoing", "name": None, "number": None,
                                                "audio": "carplay", "cind": {"callsetup": 2}})
                        self.call["src"] = "carplay"
                    await self.from_iphone({"type": "hfp", "event": ev, "name": None, "number": None, "audio": "carplay",
                                            "cind": {"call": int(ev == "call_active"), "callsetup": 1 if ev == "ring" else 0}})
                    if self.call:
                        self.call["src"] = "carplay"
                if cmd == "phonecall_start" and self.call:
                    self.call["audio"] = "carplay"
                return
            pcm = au.get("pcm") or b""
            rate, ch = ck.DECODE_TYPES.get(au["decode_type"], (44100, 2))
            self.counts["cp_audio"] += 1
            self.cp_audio_s += len(pcm) / (rate * ch * 2)   # seconds of audio (packets vary: 10-65 ms)
            if self.counts["cp_audio"] % 500 == 1:        # check: format and level of the CarPlay audio
                peak = int(np.abs(np.frombuffer(pcm[: len(pcm) & ~1], dtype="<i2")).max()) if len(pcm) > 1 else 0
                self.log("carplay_audio", n=self.counts["cp_audio"], decode_type=au["decode_type"],
                         audio_type=au["audio_type"], volume=au["volume"], bytes=len(pcm), peak=peak, bike=self.bike is not None)
            if ch == 1 and rate == 16000:
                await self.voice_to_bike(pcm)
            else:
                out = self.mix_carplay(self.to_44k_stereo(pcm, rate, ch), au["audio_type"])
                if out:
                    await self.music_to_bike(out, "carplay")
        elif mtype == ck.MEDIA_DATA:
            kind = struct.unpack("<I", data[:4])[0]
            if kind == 1:
                import json
                info = json.loads(data[4:].rstrip(b"\0").decode("utf-8", "replace"))
                # partial updates: keep the latest value of each field
                # (the title sometimes arrives without the artist, which follows in another message): new track = drop the old fields
                if info.get("MediaSongName") and info["MediaSongName"] != self.cp_media.get("MediaSongName"):
                    for k in ("MediaArtistName", "MediaAlbumName", "MediaSongDuration"):
                        self.cp_media.pop(k, None)
                self.cp_media.update({k: v for k, v in info.items() if v not in (None, "")})
                if set(info) - {"MediaSongPlayTime"}:
                    self.log("carlinkit_media", info=info)
                await self.media_to_bike()
            elif kind == 100:
                # calls from CarPlay (the iPhone talks only to the Carlinkit): same path as the iPhone's HFP events
                import json
                info = json.loads(data[4:].rstrip(b"\0").decode("utf-8", "replace"))
                self.log("carlinkit_call", raw=info)
                st = info.get("CallStatus")
                name, number = info.get("CallName") or info.get("CallNumber"), info.get("CallNumber")
                ev = {ck.CALL_RINGING: "ring", ck.CALL_DIALING: "outgoing", ck.CALL_ACTIVE: "call_active"}.get(
                    st, "call_ended" if st in (ck.CALL_IDLE, ck.CALL_ENDED) else None)
                if ev == "call_ended" and not self.call:
                    ev = None
                if self.call and self.call.get("src") != "carplay":
                    ev = None                   # call already known from the iPhone's Bluetooth: that path handles it
                if ev:
                    await self.from_iphone({"type": "hfp", "event": ev, "name": name, "number": number, "audio": "carplay",
                                            "cind": {"call": int(ev == "call_active"),
                                                     "callsetup": {"ring": 1, "outgoing": 2}.get(ev, 0)}})
                    if self.call:
                        self.call["src"] = "carplay"
        elif mtype == ck.PLUGGED:
            self._burst_until, self._aclk = time.monotonic() + 3, 0.0
            pt, wifi = struct.unpack("<II", data[:8])
            self.log("carlinkit_plugged", phone=ck.PHONE_TYPES.get(pt, pt), wifi=wifi,
                     connect_s=round(time.monotonic() - (self.carplay_since or time.monotonic()), 1))
        elif mtype == ck.UNPLUGGED:
            self.log("carlinkit_unplugged")
            self.display.mode = "launcher"
            self.to_bike({"type": "bench_display", "mode": "launcher"})
            self.display.carplay_frame = None
            self.decoder = None
        elif mtype == ck.COMMAND:
            name = ck.CMD_NAME.get(struct.unpack("<I", data[:4])[0], "?")
            self.log("carlinkit_command", cmd=name)
            if name == "requestHostUI":
                self.display.mode = "launcher"
                self.log("display", mode="launcher", why="MTS icon in CarPlay")
                self.to_bike({"type": "bench_display", "mode": "launcher"})
        elif mtype in (ck.OPEN, ck.BOX_SETTINGS, ck.SOFTWARE_VERSION, ck.BT_DEVICE_NAME, ck.WIFI_DEVICE_NAME):
            key = {ck.SOFTWARE_VERSION: "software", ck.BT_DEVICE_NAME: "bt_name", ck.WIFI_DEVICE_NAME: "wifi_name"}.get(mtype)
            if key:
                self.dongle_info[key] = data.decode("utf-8", "replace").strip("\x00")
            self.log("carlinkit_info", type=hex(mtype), data=data[:120].decode("utf-8", "replace") if mtype != ck.OPEN else "Opened")

    def mix_carplay(self, pcm, audio_type):
        """CarPlay sends different streams at the same time (audio_type: 1 music, the others system sounds, alerts,
        navigation: commands 15/16 when the iPhone screen turns on, 6 Nov 2026). Queued one after the other,
        the bike receives twice the data and crackles: the secondary stream is mixed into the main one.
        Main = music (1) if it arrived in the last 0.3 s, otherwise the stream itself passes through on its own."""
        now = time.monotonic()
        if audio_type not in self._cp_types:
            self.log("carplay_audio_type", audio_type=audio_type)
        self._cp_types[audio_type] = now
        music_on = now - self._cp_types.get(1, 0) < 0.3
        if audio_type != 1 and music_on:
            self._cp_mix += pcm
            if len(self._cp_mix) > 352800:              # over 2 s: keep the most recent part
                del self._cp_mix[: len(self._cp_mix) - 352800]
            return None
        if not self._cp_mix:
            return pcm
        if audio_type != 1:                             # the music stopped: the rest of the secondary stream passes through on its own
            out, self._cp_mix = bytes(self._cp_mix) + pcm, bytearray()
            return out
        n = min(len(pcm), len(self._cp_mix)) & ~3
        a = np.frombuffer(pcm, dtype="<i2").astype(np.int32)
        a[: n // 2] += np.frombuffer(bytes(self._cp_mix[:n]), dtype="<i2")
        del self._cp_mix[:n]
        return np.clip(a, -32768, 32767).astype("<i2").tobytes()

    @staticmethod
    def to_44k_stereo(pcm, rate, ch):
        if rate == 44100 and ch == 2:
            return pcm
        a = np.frombuffer(pcm, dtype="<i2").reshape(-1, ch).astype(np.float32)
        if ch == 1:
            a = np.repeat(a, 2, axis=1)
        n = int(len(a) * 44100 / rate)
        idx = np.linspace(0, len(a) - 1, n)
        a = np.stack([np.interp(idx, np.arange(len(a)), a[:, c]) for c in range(2)], -1)
        return a.astype("<i2").tobytes()

    # ------------------------------------------------------------ Bluetooth with the iPhone (car stereo)
    async def iphone_bt(self):
        host, port = self.a.iphone.rsplit(":", 1)
        while True:
            self.iphone = await lp.connect(host, int(port), "iphone-bt", retry_s=3600)
            self.log("iphone_bt_connected")
            await self.iphone.send_json({"type": "pbap_pull"})
            while True:
                kind, data = await self.iphone.recv()
                if kind is None:
                    break
                if kind == lp.MUSIC:
                    self.counts["bt_music"] += 1
                    await self.music_to_bike(data, "bt")
                elif kind == lp.VOICE:
                    self.counts["bt_voice"] += 1
                    await self.voice_to_bike(data)
                elif kind == lp.JSON:
                    await self.from_iphone(data)
            self.iphone = None
            self.iphone_name = None
            self.log("iphone_bt_closed")
            await asyncio.sleep(1)

    async def from_iphone(self, m):
        t = m.get("type")
        if t == "hello":
            self.iphone_name = m.get("device")
        elif t == "avrcp":
            changed = not self.media or (self.media.get("title"), self.media.get("status")) != (m.get("title"), m.get("status"))
            self.media = m
            self.to_bike(m)
            if changed:
                self.launcher.bump()
                self.log("media", title=m.get("title"), status=m.get("status"))
        elif t == "hfp":
            ev = m.get("event")
            self.cind.update(m.get("cind", {}))
            if ev == "indicators":
                self.to_bike({"type": "hfp", "event": "indicators", "cind": dict(self.cind), "device": self.a.bt_name})
            elif ev in ("ring", "outgoing"):
                self.call = {"state": "incoming" if ev == "ring" else "outgoing", "name": m.get("name"), "number": m.get("number"), "audio": "bt"}
                self.to_bike({"type": "hfp", "event": ev, "name": m.get("name"), "number": m.get("number"), "cind": m.get("cind", {})})
            elif ev == "call_active":
                if self.call:
                    self.call["state"] = "active"
                    self.call["audio"] = m.get("audio", self.call.get("audio", "bt"))
                self.to_bike({"type": "hfp", "event": "call_active", "name": m.get("name"), "cind": m.get("cind", {})})
                if self.session:
                    await self.session.server_state(1, 1)
            elif ev == "call_ended":
                self.call = None
                self.to_bike({"type": "hfp", "event": "call_ended", "cind": m.get("cind", {})})
                if self.session:
                    await self.session.server_state(1, 0)
            self.log("hfp", ev=ev, name=m.get("name"), audio=m.get("audio"))
            self.launcher.bump()
        elif t == "pbap":
            self.contacts, self.recent = m.get("contacts", []), m.get("recent", [])
            self.phonebook.update_simple(self.contacts, self.recent)
            self.to_bike({"type": "pbap", "contacts": self.contacts, "recent": self.recent})
            self.log("pbap", contacts=len(self.contacts), recent=len(self.recent))
        elif t == "map":
            self.messages = m.get("messages", self.messages)
            self.to_bike({"type": "map", "messages": self.messages, "new": m.get("new")})
            self.log("map", messages=len(self.messages), new=bool(m.get("new")),
                     top=[f'{x.get("time")} {str(x.get("from"))[:20]}: {str(x.get("text"))[:30]}' for x in self.messages[:3]])
        elif t == "bench_route":
            self.bench_route = f"music {m['media']}, calls {m['call']}"
            self.log("iphone_route", media=m["media"], call=m["call"])

    # ------------------------------------------------------------ towards the bike
    def name_for(self, number):
        """Phonebook name for a number (last 9 digits: +39, 0039 and spaces do not matter)."""
        tail = "".join(ch for ch in str(number or "") if ch.isdigit())[-9:]
        if len(tail) < 6:
            return None
        for c in self.contacts:
            for n in c.get("numbers", []):
                if "".join(ch for ch in n if ch.isdigit()).endswith(tail):
                    return c.get("name")
        return None

    async def carplay_ring(self):
        """Ring reported by the Carlinkit (which does not send the number): number and name from the iPhone over Bluetooth
        (oFono) and the phonebook, then the ring to the bike/helmet. Without iPhone Bluetooth it rings at once, no number."""
        number = name = None
        self.fast_bt_poll = True
        t0 = time.monotonic()
        while self.bt and self.bt.iphone and time.monotonic() - t0 < 2.5:
            try:
                await self.bt._poll_ofono()         # only the iPhone calls, without waiting for the 2 s poll cycle
            except Exception:
                pass
            c = self.bt.call
            if c and c.get("number"):
                number, name = c["number"], c.get("name")
                break
            await asyncio.sleep(0.2)
        self.fast_bt_poll = False
        name = name or self.name_for(number)
        if self.call and self.call.get("src") == "carplay":
            return                                  # already handled (answered in the meantime)
        self.log("carplay_ring", number=number, name=name, wait_s=round(time.monotonic() - t0, 2))
        await self.from_iphone({"type": "hfp", "event": "ring", "name": name, "number": number, "audio": "carplay",
                                "cind": {"call": 0, "callsetup": 1}})
        if self.call:
            self.call["src"] = "carplay"

    CALL_GAP_S = 2.5     # phonecall_stop during an active CarPlay call: how long to wait for a phonecall_start (audio moved)

    async def carplay_call_end_after(self, c):
        """Pending end of a CarPlay call: if no phonecall_start comes back within CALL_GAP_S, it was the real end.
        Until then the call continues as far as the bike is concerned (no music, video limit, microphone paused).
        The iPhone's oFono state does not help shorten this: during the move it also reports [] and then ["active"]."""
        await asyncio.sleep(self.CALL_GAP_S)
        if self.call is c and c.get("ending"):
            await self.carplay_call_end("timeout")

    async def carplay_call_end(self, why):
        c = self.call
        if not c:
            return               # already closed by another path (hangup from the helmet and timeout together)
        self.log("carplay_call_end", why=why, wait_s=round(time.monotonic() - c.get("ending", time.monotonic()), 2))
        await self.from_iphone({"type": "hfp", "event": "call_ended", "name": None, "number": None, "audio": "carplay",
                                "cind": {"call": 0, "callsetup": 0}})

    async def media_to_bike(self):
        """CarPlay track to the bike's Music menu (AVRCP): via the ESP32 in the pro variant, via the bike port in the standard variant."""
        m = self.cp_media
        title = m.get("MediaSongName") or ""
        status = "playing" if m.get("MediaPlayStatus") in (1, "1", True) else "paused"
        if status == "paused" and time.monotonic() - self._cp_audible < 1.0:
            status = "playing"     # CarPlay music arriving: the iPhone under CarPlay sometimes says "paused" (helmet intercom silent)
            # but if it is a real pause (intercom button) the audio stops: check again, otherwise the bike/intercom stays on
            # "playing" and its single button sends Pause again instead of Play (29 Oct 2026)
            asyncio.get_event_loop().call_later(1.2, lambda: self.spawn(self.media_to_bike()))
        key = (title, m.get("MediaArtistName"), m.get("MediaAlbumName"), status)
        if key == self._media_sent or not (title or "MediaPlayStatus" in m or status == "playing"):
            return       # without a title (bridge restarted with a track already playing) at least the status is sent: a helmet intercom uses it for Play/Pause
        self._media_sent = key
        msg = {"type": "avrcp", "title": title, "artist": m.get("MediaArtistName") or "",
               "album": m.get("MediaAlbumName") or "", "duration_ms": int(m.get("MediaSongDuration") or 0), "status": status}
        self.log("media", title=title, artist=msg["artist"], status=status, src="carplay")
        self.launcher.bump()
        if self.music:
            self.music.set_carplay(title, msg["artist"], msg["album"], msg["duration_ms"], status)
        if self.bike:
            await self.bike.send_json(msg)

    async def music_to_bike(self, pcm, src):
        now = time.monotonic()
        self.audio_src[src] = now
        if src == "carplay" and pcm.strip(b"\0"):
            self._cp_audible = now
            if not self._media_sent or self._media_sent[3] != "playing":
                # real music arriving but the bike/intercom thinks we are stopped (bridge restarted, state lost): the intercom stays
                # silent until AVRCP says "playing" (11 Nov 2026, bench)
                self.spawn(self.media_to_bike())
        if src == "carplay" and now < self._burst_until:
            # right after connecting or resuming, the Carlinkit dumps the buffered music all at once: anything more than
            # 0.4 s ahead of real time is dropped (otherwise the helmet plays it "sped up"). Only in the first 3 s: after that,
            # the small drift between the iPhone and Pi clocks would drop a small chunk every now and then (audible gaps)
            self._aclk = max(self._aclk, now)
            if self._aclk - now > 0.4:
                self.counts["cp_audio_dropped"] = self.counts.get("cp_audio_dropped", 0) + 1
                return
            self._aclk += len(pcm) / 176400

        if self.bike and not (self.call and self.call["state"] == "active"):
            await self.bike.send(lp.MUSIC, pcm)

    async def voice_to_bike(self, pcm):
        if self.bike:
            await self.bike.send(lp.VOICE, pcm)

    # ------------------------------------------------------------ CarPlay Siri
    SIRI_START_S = 6     # after the command: if the Carlinkit does not say siri_start within this time, close again
    SIRI_MAX_S = 120     # maximum duration without siri_stop (safeguard: the voice channel does not stay open forever)

    def siri_set(self, on, max_s=0):
        """Siri state towards the bike: on siri_start the hands-free opens the voice channel without a call, on siri_stop it closes it."""
        was, self.siri_until = bool(self.siri_until), (time.monotonic() + max_s) if on else 0.0
        self.log("siri", on=on)
        self.to_bike({"type": "hfp", "event": "siri_start" if on else "siri_stop"})
        if on and not was:
            self.spawn(self.siri_watch())

    async def siri_watch(self):
        while self.siri_until:
            await asyncio.sleep(0.5)
            if self.siri_until and time.monotonic() > self.siri_until:
                self.log("siri_timeout")
                self.siri_set(False)

    SIRI_PRESS_S = 0.3   # how long CarPlay's voice button stays "pressed"

    async def siri_release(self):
        """The release that turns "voice button held" into a short press. With the press alone (code 5) Siri listens for
        as long as the button is "held": it hears the voice and never answers (17 Nov 2026, bench). With the release
        right after, Siri ends the listening by itself."""
        await asyncio.sleep(self.SIRI_PRESS_S)
        if self.dongle:
            await self.dongle.send(ck.send_command("siriRelease"))

    async def bike_siri(self, on):
        """Siri asked from our side: AT+BVRA from the bike (long press of the phone button) or the joystick held down."""
        if on and self.dongle and self.carplay_state() == "on" and not self.call:
            if self.siri_until:                # already listening: a second request changes nothing
                return
            if self.a.bike_audio == "none" or not SIRI_WAIT_MIC:
                await self.dongle.send(ck.send_command("siri"))
                self.siri_set(True, self.SIRI_START_S)
                self.spawn(self.siri_release())
            else:
                # SIRI_WAIT_MIC: the bike opens the voice channel now; Siri starts when the helmet microphone is there
                self.siri_set(True, self.SIRI_MIC_WAIT_S + self.SIRI_START_S)
                self.spawn(self.siri_when_mic_open())
        else:                                  # CarPlay off, call in progress or close request
            self.siri_set(False)

    SIRI_MIC_WAIT_S = 4.5    # the bike connects the helmet about 3.3 s after the voice channel opens (21 Nov 2026)
    SIRI_MIC_LEVEL = 4       # until then the bike sends plain silence (samples of 0 or 1); an open microphone hisses at 10 or more

    async def siri_when_mic_open(self):
        """Only with SIRI_WAIT_MIC=1. Siri started at once listens to about three seconds of silence before the bike
        has connected the helmet. Here the command goes to CarPlay when the first real sound comes from the helmet,
        or after SIRI_MIC_WAIT_S if none does."""
        t0 = time.monotonic()
        self.siri_mic_open = asyncio.Event()
        try:
            await asyncio.wait_for(self.siri_mic_open.wait(), self.SIRI_MIC_WAIT_S)
            why = "microphone open"
        except asyncio.TimeoutError:
            why = "no sound from the microphone"
        finally:
            self.siri_mic_open = None
        if not self.siri_until or not self.dongle or self.call:
            return                             # closed, or a call arrived, while waiting
        self.log("siri_command", waited_s=round(time.monotonic() - t0, 2), why=why)
        await self.dongle.send(ck.send_command("siri"))
        self.siri_until = time.monotonic() + self.SIRI_START_S
        await self.siri_release()

    async def bike_audio(self):
        while True:
            if self.a.bike_audio.startswith("esp32"):
                # pro variant: music, calls and microphone to/from the bike go through the ESP32 over serial
                from mtspi.esp32_bike import Esp32Bike
                dev = self.a.bike_audio.split(":", 1)[1] if ":" in self.a.bike_audio else "/dev/ttyAMA0"
                self.bike = Esp32Bike(self.log, dev)
            else:
                host, port = self.a.bike_audio.rsplit(":", 1)
                self.bike = await lp.connect(host, int(port), "bike-bt", retry_s=3600)
            self.log("bike_bt_connected")
            await self.bike.send_json({"type": "hfp", "event": "indicators", "cind": dict(self.cind) or {"service": 0},
                                       "device": self.a.bt_name})
            if self.media:
                await self.bike.send_json(self.media)
            if self.contacts:
                await self.bike.send_json({"type": "pbap", "contacts": self.contacts, "recent": self.recent})
            if self.messages:
                await self.bike.send_json({"type": "map", "messages": self.messages})
            while True:
                kind, data = await self.bike.recv()
                if kind is None:
                    break
                if kind == lp.MIC:
                    self.counts["bike_mic"] += 1
                    if self.counts["bike_mic"] % 250 == 1:     # check: level of the helmet microphone (is a voice arriving?)
                        peak = int(np.abs(np.frombuffer(data[: len(data) & ~1], dtype="<i2")).max()) if len(data) > 1 else 0
                        self.log("bike_mic", n=self.counts["bike_mic"], bytes=len(data), peak=peak,
                                 to="call" if self.call else "siri" if self.siri_until else None)
                    if self.call and self.call["state"] == "active" and not self.call.get("ending"):
                        if self.call.get("audio") == "carplay" and self.dongle:
                            await self.dongle.send(ck.send_mic(data))
                        elif self.iphone:
                            await self.iphone.send(lp.MIC, data)
                    elif self.siri_mic_open is not None:           # Siri asked for, not started yet: is the helmet there?
                        if len(data) > 1 and int(np.abs(np.frombuffer(data[: len(data) & ~1], dtype="<i2")).max()) >= self.SIRI_MIC_LEVEL:
                            self.siri_mic_open.set()
                    elif self.siri_until and self.dongle:          # Siri: the helmet microphone goes to CarPlay
                        await self.dongle.send(ck.send_mic(data))
                elif kind == lp.JSON:
                    t = data.get("type")
                    if t == "avrcp":
                        cmd = data.get("cmd")
                        self.log("avrcp_cmd", cmd=cmd)
                        ck_cmd = {"play": "play", "pause": "pause", "play_pause": "playOrPause", "next": "next",
                                  "prev": "prev", "stop": "pause"}.get(cmd)
                        if self.dongle and self.carplay_state() == "on" and ck_cmd:
                            await self.dongle.send(ck.send_command(ck_cmd))     # CarPlay is playing the music
                        else:
                            self.to_phone_bt({"type": "avrcp_cmd", "cmd": cmd})
                    elif t == "hfp":
                        self.log("hfp_cmd", cmd=data.get("cmd"), number=data.get("number"))
                        if data.get("cmd") == "siri":
                            await self.bike_siri(bool(data.get("on")))
                        elif data.get("cmd") == "answer" and self.call and self.call.get("state") == "active":
                            # 29 Oct 2026, helmet intercom: a second ATA during an already active call became acceptPhone and CarPlay hung up
                            self.log("hfp_answer_ignored", reason="call already active")
                        elif data.get("cmd") == "hangup" and self.call and self.call.get("ending"):
                            # hung up from the helmet while waiting for phonecall_start: immediately, on CarPlay too
                            if self.dongle:
                                await self.dongle.send(ck.send_command("rejectPhone"))
                            await self.carplay_call_end("hangup")
                        elif self.call and self.call.get("src") == "carplay" and self.dongle and \
                                data.get("cmd") in ("answer", "hangup"):
                            # answer/hangup from the helmet → to the Carlinkit (CarPlay owns the call)
                            await self.dongle.send(ck.send_command("acceptPhone" if data["cmd"] == "answer" else "rejectPhone"))
                        else:
                            self.to_phone_bt({"type": "hfp_cmd", **{k: v for k, v in data.items() if k != "type"}})
                    elif t == "pbap_pull":
                        self.to_bike({"type": "pbap", "contacts": self.contacts, "recent": self.recent})
            self.bike = None
            self.log("bike_bt_closed")
            await asyncio.sleep(1)

    # ------------------------------------------------------------ screen (mySPIN)
    async def myspin(self):
        if self.a.control != "none":
            self.spawn(self.control_loop())
            if self.recovery:
                self.spawn(self.mirror_guard())
                self.spawn(self.radio_watch())
        while True:
            try:
                ep = await discover_vehicle(self.log, self.a.discovery_port, timeout=3600)
            except (asyncio.TimeoutError, TimeoutError):
                continue                 # an hour without a dashboard (bike off, bench idle): wait again, no restart
            except OSError as e:
                self.log("discovery_error", error=f"{type(e).__name__}: {e}")
                await asyncio.sleep(3)
                continue
            if self.idle_task:
                self.idle_task.cancel()
                self.idle_task = None
            writer = None
            try:
                # CarPlay turned off after inactivity (or by the Carlinkit being reattached): with autostart it turns back on,
                # since without CarPlay no audio reaches the bike in the Carlinkit setup. Turned off by hand from the menu: stays off.
                if self.settings.carplay_autostart and not self.carplay_user_off and self.carplay_state() == "off" and self.dongle:
                    self.log("carplay_resume", why="session_start")
                    self.request_carplay()
                reader, writer = await asyncio.wait_for(asyncio.open_connection(ep["host"], ep["port"]), 10)
                self.log("tcp_connected", host=ep["host"], port=ep["port"])
                self.session = PhoneProjection(reader, writer, self.display, self.log, self.settings.jpeg_quality, self.a.bt_info,
                                               list(range(1, self.a.vehicle_keys + 1)))
                await self.session.run()
            except Exception as e:
                self.log("session_error", error=f"{type(e).__name__}: {e}")
            finally:
                self.session = None
                if writer is not None:
                    writer.close()       # session closed by an error: the socket used to stay open
                # with CarPlay on, stay on CarPlay: at the next session (Wi-Fi drop, recovered in ~1 s)
                # the dashboard goes back to CarPlay by itself instead of the Pi home screen (24 Oct 2026 on the bike: 2 drops in 5 min)
                if self.display.mode != "carplay" or self.carplay_state() == "off":
                    self.display.mode = "launcher"
                self.log("session_end", frames=self.display.frames_sent, display=self.display.mode)
                if self.carplay_state() != "off":
                    self.idle_task = self.spawn(self.carplay_idle_off())

    async def control_loop(self):
        """Control channel always listening: the bike reopens it at every ignition (dashboard power-on) or after a radio
        dropout (previously it was negotiated only once and then the Pi stayed deaf until a restart)."""
        async def until_closed(r):
            while await r.read(4096):              # stays open as long as the bike keeps it open
                pass

        waiting = None                             # pending wait for the bike's next control connection
        while True:
            if waiting is None:
                waiting = asyncio.ensure_future(hv.control_channel(self.a.control, self.log))
            r, w = await waiting
            waiting = None
            self._control_idle_since = None
            joining = False
            try:
                control = PhoneControl(r, w, self.log, device_name=self.a.bt_name)
                creds = await control.negotiate()
                if self.a.ap_credentials_file:
                    hv.save_credentials(self.a.ap_credentials_file, creds, self.log)
                joining = True
                await hv.join_wifi(self.a.wifi, creds, self.log)
                joining = False
                if self.recovery:
                    self.recovery.join_ok()
                await control.notify_wlan("CONNECTED")
                if self.a.control == "rfcomm":
                    # On the bike (16 Nov 2026): after a radio dropout the dashboard opened the channel again, but the
                    # old one never reported its end and the Pi stayed deaf until a restart. A new connection now
                    # replaces the old one.
                    closed = asyncio.ensure_future(until_closed(r))
                    waiting = asyncio.ensure_future(hv.control_channel(self.a.control, self.log))
                    try:
                        await asyncio.wait({closed, waiting}, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        if not closed.done():
                            closed.cancel()
                    if waiting.done():
                        if closed.done() and not closed.cancelled():
                            closed.exception()     # the old channel's own end no longer matters
                        self.log("control_replaced")
                    else:
                        closed.result()            # raises here if the old channel ended with an error
                        self.log("control_closed")
                else:
                    await until_closed(r)
                    self.log("control_closed")
            except (Exception, asyncio.TimeoutError) as e:
                # a half-finished negotiation (dashboard off or gone) must not stop the Pi: wait again
                self.log("control_error", error=f"{type(e).__name__}: {e}")
                if joining and self.recovery:
                    # the bike offered its Wi-Fi and the Pi could not get on it: next step of the recovery
                    await self.recover(self.recovery.join_failed(), why=f"{e}")
            finally:
                w.close()
                self._control_idle_since = time.monotonic()
            await asyncio.sleep(1)

    async def recover(self, action, why):
        rec, loop = self.recovery, asyncio.get_running_loop()
        if action == "restart_p2p":
            await loop.run_in_executor(None, rec.restart_p2p)
        elif action == "reboot":
            return await loop.run_in_executor(None, rec.reboot, why)     # False: not allowed now, or it failed
        elif action == "bounce":
            bike = self.bt.bike if self.bt else None
            if not bike:
                return
            self.log("mirror_bike_reconnect", failures=rec.streak, attempt=rec.bounces)
            try:
                # closing the link is enough: mtspi-wifi-mode reconnects the bike within a few seconds, and the
                # dashboard then opens its control channel again as it does at every ignition
                bus = await bluez.system_bus()
                try:
                    await asyncio.wait_for(bluez.call(bus, "org.bluez", bike["path"], "org.bluez.Device1", "Disconnect"), 8)
                finally:
                    bus.disconnect()
            except Exception as e:
                self.log("mirror_bike_reconnect_failed", error=(str(e) or type(e).__name__)[:120])

    async def mirror_guard(self):
        """After a failed join the bike normally offers its Wi-Fi again within a second. When it stays silent
        (21 Nov 2026: no address from the bike, then nothing for four minutes) the Pi moves by itself."""
        while True:
            await asyncio.sleep(3)
            rec = self.recovery
            if rec.streak == 0 or self._control_idle_since is None or not (self.bt and self.bt.bike):
                continue                 # nothing failed, a negotiation is running, or the bike is not there at all
            action = rec.idle_action(time.monotonic() - self._control_idle_since, in_call=bool(self.call))
            if action:
                self._control_idle_since = time.monotonic()
                await self.recover(action, why="the bike did not offer its Wi-Fi again")

    async def radio_watch(self):
        """Follows what the kernel says about the Bluetooth radio (mtspi/recovery.py)."""
        try:
            fd = os.open("/dev/kmsg", os.O_RDONLY | os.O_NONBLOCK)
            os.lseek(fd, 0, os.SEEK_END)
        except OSError as e:
            self.log("radio_watch_off", error=str(e))
            return
        watch, loop, lines = recovery.RadioWatch(), asyncio.get_running_loop(), asyncio.Queue()
        checking = []                              # the pending "is Bluetooth back?" look, one at a time

        def readable():
            try:
                while True:
                    lines.put_nowait(os.read(fd, 8192).decode("utf-8", "replace"))
            except BlockingIOError:
                pass
            except OSError as e:            # EPIPE: the kernel overwrote records we had not read yet; go on from the next
                if e.errno != 32:
                    loop.remove_reader(fd)
                    lines.put_nowait(None)
        loop.add_reader(fd, readable)
        while True:
            line = await lines.get()
            if line is None:
                self.log("radio_watch_off", error="kernel log no longer readable")
                return
            what = watch.feed(line)
            if what == "reset":
                self.log("radio_reset", kernel=line.split(";", 1)[-1].strip()[:80])
                self.recovery.radio_reset()
                if not checking:
                    checking.append(self.spawn(self.radio_back_check(checking)))
            elif what in ("stuck", "dead"):
                self.log("radio_stuck", kernel=line.split(";", 1)[-1].strip()[:80])
                await self.recover("reboot", why="the Bluetooth radio no longer answers")
            elif what == "ghost":
                await self.voice_ghost()

    async def voice_ghost(self):
        """The chip carries a voice link the system does not know (bench, 22 Nov 2026: from then on the voice channel
        to the helmet no longer opened, so no Siri and no sound in calls). Only a restart of the Pi cured it. During
        a call nothing is done: the lines keep coming, so this is asked again every few seconds until the call ends."""
        if self.call:
            if not self._ghost_noted:
                self._ghost_noted = True
                self.log("radio_voice_ghost", waiting="call in progress")
            return
        if not self._ghost_noted:
            self.log("radio_voice_ghost")
        self._ghost_noted = True
        if await self.recover("reboot", why="the radio chip carries a voice link that nobody opened") is False:
            # restarted less than 15 minutes ago, or just started: closing the bike's link at least ends that voice link
            now = time.monotonic()
            if now - self._ghost_bounce_t >= recovery.GHOST_BOUNCE_EVERY_S:
                self._ghost_bounce_t = now
                await self.recover("bounce", why="the radio chip carries a voice link that nobody opened")

    async def radio_powered(self):
        """True/False = Bluetooth switched on or off as BlueZ sees it, None = BlueZ did not answer."""
        try:
            bus = await bluez.system_bus()
            try:
                objs = (await asyncio.wait_for(bluez.call(bus, "org.bluez", "/", "org.freedesktop.DBus.ObjectManager",
                                                          "GetManagedObjects"), 8))[0]
            finally:
                bus.disconnect()
            radios = [i["org.bluez.Adapter1"] for i in objs.values() if "org.bluez.Adapter1" in i]
            return any(bluez._v(r.get("Powered")) for r in radios)      # no radio at all counts as off
        except Exception:
            return None

    async def radio_back_check(self, checking):
        """After the chip restarted itself Bluetooth normally is back in a few seconds (bike, 21 Nov 2026). On the bench
        the same night it stayed switched off for good: then only a restart of the Pi brought it back."""
        try:
            await asyncio.sleep(recovery.RADIO_BACK_S)
            if await self.radio_powered() is False:
                await asyncio.sleep(5)                      # a second look, in case it was just coming up
                if await self.radio_powered() is False:
                    self.log("radio_stuck", kernel="Bluetooth still switched off after the chip restarted itself")
                    await self.recover("reboot", why="Bluetooth did not come back after the radio chip restarted itself")
        finally:
            checking.clear()

    async def carplay_idle_off(self):
        """After leaving Ducati Connect: turns CarPlay off after a while, so the audio goes back to Bluetooth."""
        delay = self.settings.carplay_idle_off
        if not delay:
            return                       # "never" setting
        await asyncio.sleep(delay)
        if self.session is None and self.carplay_state() != "off":
            self.log("carplay_idle_off", after_s=delay)
            self.request_carplay_off(user=False)

    def rate(self, name, count):
        """Per-second rate (averaged over the last ~2 s) of an increasing counter."""
        now = time.monotonic()
        if name not in self._rates:
            self._rates[name] = (now, count, 0.0)
        t0, c0, last = self._rates[name]
        if now - t0 >= 2.0:
            last = (count - c0) / (now - t0)
            self._rates[name] = (now, count, last)
        return last

    # ------------------------------------------------------------ tyre pressure page
    TYRES_STATE = os.environ.get("TPMS_STATE", "/run/mtspi-tpms/state.json")
    # recommended pressures of the Multistrada V4 (normal/fully loaded), unless TPMS_FRONT_BAR / TPMS_REAR_BAR say otherwise
    TYRES_BAR = {"front": "2.4/2.4", "rear": "2.5/2.9"}

    def tyres_enabled(self):
        return os.environ.get("TPMS", "0") == "1"

    def tyres(self):
        """What the listener (mtspi/tpms.py) last heard, for the page on the dashboard: front first, then rear."""
        import json
        now = time.monotonic()
        try:
            st = json.loads(open(self.TYRES_STATE).read())
        except (OSError, ValueError):
            return {"listening": False, "wheels": []}

        def recommended(wheel):                  # TPMS_FRONT_BAR=2.4/2.4: normal and fully loaded
            try:
                text = os.environ.get(f"TPMS_{wheel.upper()}_BAR") or self.TYRES_BAR[wheel]
                text = text.replace(",", ".").replace("/", " ")
                return [float(x) for x in text.split()][:2] or None
            except ValueError:
                return None
        wheels = []
        for addr, v in (st.get("sensors") or {}).items():
            wheel = v.get("wheel")
            wheels.append({"wheel": wheel, "id": addr[-8:], "bar": v.get("bar"), "temp_c": v.get("temp_c"),
                           "volt": v.get("volt"), "age_s": max(0.0, now - v.get("up", now)),
                           "recommended": recommended(wheel) if wheel else None})
        wheels.sort(key=lambda w: ({"front": 0, "rear": 1}.get(w["wheel"], 2), w["id"]))
        return {"listening": now - st.get("alive_up", 0) < 150, "wheels": wheels}

    def system_info(self):
        """Pi status (on the bench Mac some values do not exist: "n/a")."""
        import subprocess
        now = time.monotonic()
        if now - self._sys_cache[0] < 2.0:
            return self._sys_cache[1]
        out = {}
        try:
            out["temp"] = f"{int(open('/sys/class/thermal/thermal_zone0/temp').read()) / 1000:.1f} °C"
        except OSError:
            out["temp"] = None
        try:
            out["load"] = " ".join(f"{x:.2f}" for x in os.getloadavg())
        except OSError:
            out["load"] = "n/a"
        try:
            mem = dict(l.split(":", 1) for l in open("/proc/meminfo"))
            tot, avail = int(mem["MemTotal"].split()[0]), int(mem["MemAvailable"].split()[0])
            out["mem"] = f"{(tot - avail) // 1024} / {tot // 1024} MB"
        except (OSError, KeyError, ValueError):
            out["mem"] = None
        try:
            r = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=1)
            val = int(r.stdout.strip().split("=")[1], 16)
            out["throttled"] = val
        except (OSError, IndexError, ValueError, subprocess.SubprocessError):
            out["throttled"] = None
        try:
            r = subprocess.run(["iw", "dev", "wlan0", "link"], capture_output=True, text=True, timeout=1)
            ssid = re.search(r"SSID: (.+)", r.stdout)
            freq = re.search(r"freq: ([0-9.]+)", r.stdout)
            sig = re.search(r"signal: (-?[0-9]+) dBm", r.stdout)
            out["wifi"] = (ssid.group(1) if ssid else "—",
                           f"{freq.group(1)} MHz" if freq else "—", f"{sig.group(1)} dBm" if sig else "—")
        except (OSError, subprocess.SubprocessError):
            out["wifi"] = (None, "—", "—")
        self._sys_cache = (now, out)
        return out

    def vehicle_summary(self, sess):
        """mySPIN vehicle data received: how many codes and the first values (test 13 in the garage)."""
        if not sess:
            return "—"
        vd = sess.vehicle_data
        if not vd:
            return t("v_none") if sess.vehicle_keys and not sess.v11 else t("v_unsupported") if sess.v11 else "—"
        first = ", ".join(f"{k}={v[:8]}" for k, v in sorted(vd.items())[:2])
        return f"{len(vd)} · {first}"

    def diag_info(self):
        """All the figures for the Diagnostics page (translated labels)."""
        up = int(time.monotonic() - self.t_start)
        sess = self.session
        scr = sess.screen if sess else None
        sysi = self.system_info()
        na = t("na_bench")
        ver = "—"
        if sess:
            ver = "2.0" if sess.v2 else ("1.1" if sess.v11 else "1.2")
        src = self.audio_source()
        src = t("src_none") if src == "none" else src
        myspin = [
            (t("d_session"), t("active") if sess else t("absent"), "ok" if sess else "off"),
            (t("d_version"), ver, None),
            (t("d_screen"), f"{scr.width}×{scr.height}" if scr else "—", None),
            (t("d_pixfmt"), {2: "RGB565", 3: "ARGB32", 4: "RGBA32"}.get(scr.pixel_format, "?") if scr else "—", None),
            (t("d_fps"), f"{self.rate('frames', self.display.frames_sent):.1f}", None),
            (t("d_jpeg"), f"{(sess.bytes_sent / max(1, sess.frames_sent)) / 1024:.1f} KB · q{sess.jpeg_quality}" if sess and sess.frames_sent else "—", None),
            (t("d_frames"), self.display.frames_sent, None),
            (t("d_keys"), self.display.key_count, None),
            (t("d_view"), "CarPlay" if self.display.mode == "carplay" else t("home"), None),
            (t("d_pings"), sess.ping_seq if sess else 0, None),
            (t("d_vehicle"), self.vehicle_summary(sess), "ok" if sess and sess.vehicle_data else None),
        ]
        st = self.carplay_state()
        wifi = sysi["wifi"]
        links = [
            # with the Carlinkit the iPhone goes through CarPlay: its Bluetooth link to the Pi is only for the phonebook
            (t("d_iphone"), self.iphone_name or ("CarPlay" if st == "on" else t("not_connected")),
             "ok" if self.iphone or st == "on" else "off"),
            (t("d_profiles"), "HFP·A2DP·AVRCP·PBAP·MAP" if self.iphone else ("Carlinkit" if st == "on" else "—"), None),
            (t("d_bike_bt"), t("connected") if self.bike else t("no"), "ok" if self.bike else "off"),
            (t("d_bike_wifi"), t("emulated") if wifi[0] is None else wifi[0], "ok" if sess else "off"),
            (t("d_band"), f"{wifi[1]} · {wifi[2]}", None),
            (t("d_ck_usb"), t("present") if self.dongle else t("missing"), "ok" if self.dongle else "bad"),
            (t("d_cp"), t("st_" + st), {"off": "off", "connecting": "warn", "on": "ok"}[st]),
            (t("d_software"), self.dongle_info.get("software", "—"), None),
            (t("d_ck_video"), f"{self.rate('cp_video', self.counts['cp_video']):.1f} fps", None),
            (t("d_audio_src"), src, None),
            (t("d_music"), f"{self.rate('bt_music', self.counts['bt_music']) * 0.02:.2f} · "
                           f"{self.rate('cp_audio_s', self.cp_audio_s):.2f} s/s", None),
        ]
        system = [
            (t("d_temp"), sysi["temp"] or na, None),
            (t("d_load"), sysi["load"], None),
            (t("d_mem"), sysi["mem"] or na, None),
            (t("d_volt"), (t("none") if sysi["throttled"] == 0 else f"0x{sysi['throttled']:x}") if sysi["throttled"] is not None else na,
             None if sysi["throttled"] is None else ("ok" if sysi["throttled"] == 0 else "bad")),
            (t("d_uptime"), f"{up // 3600}:{up % 3600 // 60:02d}:{up % 60:02d}", None),
            (t("d_ibat"), f"{self.cind.get('battchg', '—')}/5", None),
            (t("d_isig"), f"{self.cind.get('signal', '—')}/5", None),
            (t("d_call"), (self.call or {}).get("state", t("none")), None),
            (t("d_contacts"), f"{len(self.contacts)} · {len(self.messages)}", None),
        ]
        return {"uptime": f"{up // 3600}:{up % 3600 // 60:02d}:{up % 60:02d}", "myspin": myspin, "links": links, "system": system}

    async def stats(self):
        while True:
            await asyncio.sleep(5)
            sess = self.session
            jpeg = {"kb_frame": round(sess.bytes_sent / sess.frames_sent / 1024, 1), "bytes": sess.bytes_sent, "q": sess.jpeg_quality} \
                if sess and getattr(sess, "frames_sent", 0) else {}
            self.log("bridge_stats", display=self.display.mode, carplay=self.carplay_state(),
                     audio_source=self.audio_source(), frames_sent=self.display.frames_sent, **jpeg, **self.counts)

    async def main(self):
        # "none" = peripheral absent (garage: no emulated iPhone or Carlinkit, dashboard only)
        parts = [(self.a.iphone, self.iphone_bt), (self.a.carlinkit, self.carlinkit), (self.a.bike_audio, self.bike_audio)]
        if self.a.bt == "real":            # iPhone and bike audio go through BlueZ/oFono/PipeWire, not the emulated links
            # with the Carlinkit (standard variant): CarPlay audio and calls go to the mtspi-hfp service (HFP_SOURCE=bridge)
            # on the "bike" port (--bike-audio 127.0.0.1:9160), which carries them to the bike over A2DP and SCO
            parts = [p for p in parts if p[1] == self.carlinkit or p[1] == self.bike_audio]
            extra = [self.bt_real(), self.bt_agent(), self.music_bridge()]   # the HFP phone towards the bike is the mtspi-hfp service
            if not self.a.bike_audio.startswith("esp32") and os.environ.get("BT_RECONNECT", "1") != "0":
                extra.append(self.bt_reconnect())    # standard variant: the Pi reconnects the iPhone (and the helmet on the bench)
        else:
            extra = []
        if self.a.pbap != "none":
            extra.append(self.pbap_server())
        self._alive = alive.start()         # a stuck main loop gets the program restarted by systemd (WatchdogSec)
        await asyncio.gather(*(fn() for spec, fn in parts if spec != "none"), self.myspin(), self.stats(), self.manual_cmds(),
                             self.call_video_limit(), *extra)

    async def music_bridge(self):
        """Dashboard Music menu: title and status from the iPhone to the bike, bike buttons to the iPhone."""
        from mtspi.avrcp_bridge import AvrcpBridge
        self.music = AvrcpBridge(self.log, self.player_ready, on_command=self.music_key)
        await self.music.run()

    def music_key(self, name):
        """Bike Music button (standard variant, BlueZ player) → Carlinkit, as the ESP32 does in the pro variant. False = not sent."""
        ck_cmd = {"Play": "play", "Pause": "pause", "Next": "next", "Previous": "prev", "Stop": "pause"}.get(name)
        if not (self.dongle and self.carplay_state() == "on" and ck_cmd):
            return False
        if self.siri_until:
            # Siri holds the voice channel: the intercom sends "Pause" as soon as the voice opens, and any play/pause
            # key during Siri dismisses it after about one second (17 Nov 2026, bench with the helmet intercom)
            self.log("music_key_ignored", asked=name, why="siri")
            return False
        if (ck_cmd == "pause" and self.cp_media.get("MediaPlayStatus") not in (1, "1", True)
                and time.monotonic() - self._cp_audible > 1.0):
            # single button on the intercom/bike: "Pause" with CarPlay already stopped (stale state) = Play was meant (11 Nov 2026, bench)
            self.log("music_key_toggle", sent="play", asked=name)
            ck_cmd = "play"
        elif ck_cmd == "play" and time.monotonic() - self._cp_audible < 1.0:
            # and the reverse: "Play" while the music is playing (stale state on the intercom) = Pause was meant
            self.log("music_key_toggle", sent="pause", asked=name)
            ck_cmd = "pause"
        self.spawn(self.dongle.send(ck.send_command(ck_cmd)))
        return True

    async def pbap_server(self):
        """Phonebook and recent calls for the dashboard Phone menu (the bike requests them on every connection)."""
        if self.a.bt == "real":         # music player first: mtspi-wifi-mode connects the bike once the phonebook is there
            try:
                await asyncio.wait_for(self.player_ready.wait(), 15)
            except asyncio.TimeoutError:
                self.log("music_player_late")
        if self.a.pbap == "esp32":              # pro variant: the bike opens the phonebook on the ESP32, the bytes arrive here
            from mtspi.esp32_bike import Esp32Bike
            bike = await Esp32Bike.wait_instance()
            self.log("pbap_listening", transport="esp32")
            while True:
                r, w = await bike.pbap_accept()
                self.log("pbap_connected", peer="esp32")
                spawn(pbap.ObexPbapServer(self.phonebook, self.log, "esp32").serve(r, w))
        if self.a.pbap.startswith("tcp:"):
            await pbap.serve_tcp(self.phonebook, self.log, int(self.a.pbap[4:]))
            await asyncio.Event().wait()
        while True:
            try:
                await pbap.serve_bluez(self.phonebook, self.log)
            except Exception as e:
                self.log("pbap_server_error", error=f"{type(e).__name__}: {e}")
                await asyncio.sleep(10)

    # ------------------------------------------------------------ real Bluetooth (Pi)
    async def bt_real(self):
        """Every 2 s reads the state from BlueZ/oFono and brings it to the home screen; phonebook when the iPhone connects,
        call audio linked in PipeWire when the call is active."""
        self.bt = st = bluez.BtState(self.log)
        from mtspi import mns
        self.spawn(mns.serve_mns(self.log, lambda ev: self._sms_event.set() if ev.get("type") == "NewMessage" else None))
        routed_call = None
        pulled_for = None
        levels_sent = None
        while True:
            try:
                await st.poll()
            except Exception as e:
                self.log("bt_poll_error", error=f"{type(e).__name__}: {e}")
                st.bus = None
                await asyncio.sleep(5)
                continue
            self.iphone = _Present(st.iphone) if st.iphone else None
            self.iphone_name = (st.iphone or {}).get("name")
            if self.a.bike_audio == "none":         # with the bike port connected (standard variant + Carlinkit) self.bike is that port
                self.bike = _Present(st.bike) if st.bike else None
            self.cind = dict(st.cind)
            # The iPhone's real battery and signal go on to the bike whenever they change. Until 20 Nov 2026 they stopped
            # here: the bike side kept the values it starts with (signal 5, battery 5) for the whole ride.
            levels = {k: st.cind[k] for k in ("service", "signal", "roam", "battchg") if k in st.cind}
            if levels and self.bike and self.a.bike_audio != "none" and (levels, id(self.bike)) != levels_sent:
                levels_sent = (levels, id(self.bike))
                self.to_bike({"type": "hfp", "event": "indicators", "cind": levels, "device": self.a.bt_name})
            if not (self.call and self.call.get("src") == "carplay"):   # the CarPlay call is handled by the Carlinkit
                self.call = dict(st.call, audio="bt") if st.call else None
            self.media = st.media
            if st.media and st.media.get("title") and not self.cp_media.get("MediaSongName"):
                # the Carlinkit sends the track only when it changes: until then, use the one read from the iPhone (AVRCP)
                self.cp_media.update(MediaSongName=st.media["title"], MediaArtistName=st.media.get("artist") or "",
                                     MediaPlayStatus=1 if st.media.get("status") == "playing" else 0)
                await self.media_to_bike()
            if st.iphone and pulled_for != st.iphone["mac"]:
                pulled_for = st.iphone["mac"]
                self.spawn(self.pull_contacts(st.iphone["mac"], st.iphone.get("adapter")))
                self.spawn(self.pull_sms(st.iphone["mac"], st.iphone.get("adapter")))
                if os.environ.get("PHONE_TIME", "1") != "0":
                    self.spawn(self.phone_time(st.iphone["mac"]))
            if not st.iphone:
                pulled_for = None
                # pro: the internal radio is neither discoverable nor listening, so the Pi reconnects the paired iPhone
                if self.a.bike_audio.startswith("esp32") and time.monotonic() - self._iphone_retry > 60:
                    self._iphone_retry = time.monotonic()
                    self.spawn(bluez.connect_paired_phones(self.log))
            key = (st.iphone or {}).get("mac"), (st.bike or {}).get("mac")
            if st.call and st.call["state"] == "active" and all(key):
                if routed_call != key:
                    routed_call = key
                    self.spawn(bluez.route_call_audio(key[0], key[1], self.log))
            else:
                routed_call = None
            await asyncio.sleep(0.3 if self.fast_bt_poll else 2)

    async def call_video_limit(self):
        """During a call (BT or CarPlay) the Wi-Fi video to the bike can drop to --call-fps. It was 2 in the standard variant
        (internal radio with Wi-Fi and SCO voice together: "hardware error" with full video, 23 and 25 Oct 2026); since 29 Oct
        the default is 0 = no limit there too (CALL_FPS=2 or live "callfps=2" to go back)."""
        self.call_fps_live = self.a.call_fps
        # with CarPlay music: no cap by default, in the standard variant too. On the night of 29 Oct 2026 at 2.4 GHz 6 fps
        # were needed (skips without a limit), but the same evening with sniff after 2 s, Pi central on all links and JPEG q55:
        # 13 fps and clean music. MUSIC_FPS=6 or live "musicfps=6" to go back
        self.music_fps = self.a.music_fps if self.a.music_fps is not None else 0
        limited = None
        while True:
            fps = 0
            if self.call:
                fps = self.call_fps_live
            elif self.music_fps and time.monotonic() - self.audio_src.get("carplay", 0) < 1.0:
                fps = self.music_fps
            if self.session is not None:
                self.session.min_interval = 1.0 / fps if fps > 0 else 0.0
            if fps != (limited or 0):
                limited = fps
                self.log("call_video_limit" if self.call else "music_video_limit", fps=fps or None)
            await asyncio.sleep(0.25)

    async def bt_reconnect(self):
        """Standard variant: after a restart the iPhone does not come back by itself, nor does the helmet on the bench
        (MODE_LOCK=home) even if already on (29 Oct 2026): the Pi reconnects them until connected (bluez.keep_connected). BT_RECONNECT=0 = no."""
        while True:
            try:
                await bluez.keep_connected(self.log, in_call=lambda: bool(self.call),
                                           headsets=os.environ.get("MODE_LOCK") == "home")
            except Exception as e:
                self.log("bt_reconnect_error", error=f"{type(e).__name__}: {e}"[:160])
                await asyncio.sleep(10)

    async def bt_agent(self):
        """Pairing agent: if it fails to start (BlueZ down) it retries, the home screen stays up."""
        while True:
            try:
                await bluez.run_agent(self.log)
            except Exception as e:
                self.log("agent_error", error=f"{type(e).__name__}: {e}")
                await asyncio.sleep(10)

    async def pull_sms(self, mac, adapter=None):
        """Messages from the iPhone (MAP). Without "Show Message Notifications" the iPhone answers Forbidden (0x43): retry slowly.
        Then re-read on every new message (MNS) and in any case every 5 minutes."""
        from mtspi import mns
        wait = 10
        while self.bt and self.bt.iphone and self.bt.iphone.get("mac") == mac:
            try:
                msgs = await mns.pull_messages(mac, self.log, source=adapter)
                for m in msgs:                        # the iPhone often gives only the number: name from the phonebook
                    if m.get("from") == m.get("number") or str(m.get("from", "")).startswith("+"):
                        m["from"] = self.name_for(m.get("number") or m.get("from")) or m["from"]
                if [m["text"] for m in msgs] != [m.get("text") for m in self.messages]:
                    new = bool(self.messages) and msgs and msgs[0].get("text") != self.messages[0].get("text")
                    await self.from_iphone({"type": "map", "messages": msgs, "new": msgs[0] if new else None})
                wait = 300                             # MNS notifications do arrive: fallback re-read (the internal radio disturbs the ESP32 if it polls often)
            except Exception as e:
                self.log("map_error", error=f"{type(e).__name__}: {e}"[:160], retry_in=wait)
                wait = min(300, wait * 2)
            self._sms_event.clear()
            try:
                await asyncio.wait_for(self._sms_event.wait(), wait)
                await asyncio.sleep(1)                # the iPhone announces the message before it is in the listing
            except asyncio.TimeoutError:
                pass

    async def phone_time(self, mac):
        """Once per connection: the time of day from the iPhone, for a Pi that has no internet to ask (mtspi/phonetime.py)."""
        from mtspi import phonetime
        loop = asyncio.get_running_loop()
        for wait in (4, 20, 60):                    # not in the first seconds of the link; two more tries if no answer
            await asyncio.sleep(wait)
            if not self.bt or not self.bt.iphone or self.bt.iphone.get("mac") != mac:
                return
            events = []                             # the read runs in a thread: its log lines are written from here
            done = await loop.run_in_executor(None, lambda: phonetime.sync(mac, lambda ev, **kw: events.append((ev, kw))))
            for ev, kw in events:
                self.log(ev, **kw)
            if done:
                return

    async def pull_contacts(self, mac, adapter=None):
        # Right after connecting obexd may not be ready, and without "Sync Contacts" the iPhone answers with 0 entries:
        # retry until the phonebook arrives (then every 5 minutes, for anyone who grants the permission later).
        # With the phonebook already on the SD card, the download (~15 s of saturated radio: stuttering helmet music, 29 Oct 2026)
        # waits until music and calls have stopped, at most 15 minutes.
        if self.phonebook.folders.get("pb"):
            t0 = time.monotonic()
            while time.monotonic() - t0 < 900 and (self.call or time.monotonic() - max(self.audio_src.values()) < 5):
                await asyncio.sleep(5)
                if not self.bt or not self.bt.iphone or self.bt.iphone.get("mac") != mac:
                    return
            self.log("pbap_pull_start", waited_s=round(time.monotonic() - t0))
        for wait in (5, 15, 30, 60, 300, 300, 300, 300):
            try:
                books = await bluez.pull_phonebook(mac, self.log, source=adapter)
                self.contacts = bluez.parse_vcards(books.get("pb", ""))
                self.recent = bluez.parse_vcards(books.get("cch", ""), limit=50)
                if self.contacts:
                    self.phonebook.update(books)
                    if self.a.phonebook_cache:
                        try:
                            written = self.phonebook.save(self.a.phonebook_cache,
                                                          calls=os.environ.get("PHONEBOOK_SAVE_CALLS", "1") != "0")
                            self.log("phonebook_saved", written=written)
                        except OSError as e:            # card full or read-only: the phonebook stays in memory
                            self.log("phonebook_save_error", error=str(e))
                    return
                self.log("pbap_empty", retry_in=wait)
            except Exception as e:
                self.log("pbap_error", error=f"{type(e).__name__}: {e}", retry_in=wait)
            await asyncio.sleep(wait)
            if not self.bt or not self.bt.iphone or self.bt.iphone.get("mac") != mac:
                return


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--iphone", default="127.0.0.1:9172", help="emulated iPhone Bluetooth (on the real Pi: BlueZ); none = absent")
    ap.add_argument("--carlinkit", default="127.0.0.1:9175", help="usb = real Carlinkit; HOST:PORT = emulated; none = absent")
    ap.add_argument("--bike-audio", default="127.0.0.1:9160", help="emulated bike Bluetooth (on the real Pi: BlueZ); none = absent")
    ap.add_argument("--bt", default="emulated", choices=["emulated", "real"],
                    help="real = real Bluetooth on the Pi (BlueZ, oFono, PipeWire); emulated = bench")
    ap.add_argument("--ap-credentials-file", default=None,
                    help="where to save the bike's Wi-Fi SSID/password (mode 600 file; on the Pi: in the folder of the run's log)")
    ap.add_argument("--vehicle-keys", type=int, default=128, help="ask the bike for vehicle data with codes 1..N (0 = no)")
    ap.add_argument("--control", default="tcp:9150")
    ap.add_argument("--wifi", default="none", choices=["none", "nmcli"])
    ap.add_argument("--discovery-port", type=int, default=8158)
    ap.add_argument("--screen", default="848x304")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--music-fps", type=int, default=None,
                    help="video frame rate cap with CarPlay music (default 0 = none)")
    ap.add_argument("--call-fps", type=float, default=0,
                    help="frames per second to the bike during calls (default 0 = no limit)")
    ap.add_argument("--audio-transfer", action="store_true", help="ask the Carlinkit for audioTransferOn (iPhone audio over Bluetooth)")
    ap.add_argument("--carplay-auto", action="store_true", help="start CarPlay as soon as the Carlinkit is present (instead of on demand)")
    ap.add_argument("--carplay-idle-off", type=float, default=0, help="seconds outside Ducati Connect before turning CarPlay off")
    ap.add_argument("--settings", default="~/.config/mtspi/settings.json",
                    help="settings file (on the Pi: /var/lib/mtspi/settings.json)")
    ap.add_argument("--bt-name", default=os.environ.get("BT_NAME", "MTS Pi"),
                    help="Bluetooth name of the Pi towards the bike (BT_NAME in /etc/default/mtspi)")
    ap.add_argument("--oem-label", default=os.environ.get("OEM_LABEL", "MTS"),
                    help="label of the icon inside CarPlay that goes back to the Pi menu")
    ap.add_argument("--oem-icon", default=os.environ.get("OEM_ICON", "/var/lib/mtspi/oem_icon.png"),
                    help="PNG of the icon inside CarPlay; if missing: red square with the initial")
    ap.add_argument("--jpeg-quality", type=int, default=80)
    ap.add_argument("--pbap", default="none", help="phonebook for the bike: bluez (real Pi), tcp:PORT (bench), none")
    ap.add_argument("--phonebook-cache", default=None, help="mode 600 file with the last iPhone phonebook (on the Pi: /var/lib/mtspi/phonebook.json)")
    ap.add_argument("--bt-info", default=None, help='profiles + name for the bike (default "[HFP,A2DP]" + --bt-name)')
    ap.add_argument("--log", default=None)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    a.bt_info = a.bt_info or f"[HFP,A2DP]{a.bt_name}"
    try:
        asyncio.run(Bridge(a).main())
    except KeyboardInterrupt:
        pass
