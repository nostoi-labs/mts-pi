"""Carlinkit CPC200-CCPA protocol (host side = the Pi), ported from node-carplay
(rhysmorgan134/node-CarPlay, MIT license, src/modules/DongleDriver.ts and messages/*.ts).

Message: 16-byte little-endian header
    magic 0x55AA55AA | data length | type | type XOR 0xFFFFFFFF
followed by the data. On the real Carlinkit it travels over the USB bulk endpoints; on the bench over TCP (same bytes).

Startup as in node-carplay: configuration files, Open (resolution, fps…), settings, wifiEnable,
Wi-Fi band, microphone, audioTransferOn/Off; after 1 s wifiConnect; heartbeat every 2 s.
Shutdown: CloseDongle (disconnects the phone and closes; reopening requires Open again).
"""
from __future__ import annotations

import asyncio
import os
import json
import struct
from typing import Callable, Optional

MAGIC = 0x55AA55AA

# Message types (MessageType)
OPEN, PLUGGED, PHASE, UNPLUGGED, TOUCH, VIDEO_DATA, AUDIO_DATA, COMMAND = 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08
LOGO_TYPE, BT_ADDRESS, BT_PIN, BT_DEVICE_NAME, WIFI_DEVICE_NAME = 0x09, 0x0A, 0x0C, 0x0D, 0x0E
DISCONNECT_PHONE, BT_PAIRED_LIST, MANUFACTURER_INFO, CLOSE_DONGLE = 0x0F, 0x12, 0x14, 0x15
MULTI_TOUCH, HICAR_LINK, BOX_SETTINGS, MEDIA_DATA = 0x17, 0x18, 0x19, 0x2A
SEND_FILE, HEARTBEAT, SOFTWARE_VERSION = 0x99, 0xAA, 0xCC

# Commands (CommandMapping)
CMD = {
    "invalid": 0, "startRecordAudio": 1, "stopRecordAudio": 2, "requestHostUI": 3, "siri": 5, "siriRelease": 6, "mic": 7,
    "frame": 12, "boxMic": 15, "enableNightMode": 16, "disableNightMode": 17, "audioTransferOn": 22,
    "audioTransferOff": 23, "wifi24g": 24, "wifi5g": 25, "left": 100, "right": 101, "up": 102, "down": 103, "selectDown": 104,
    "selectUp": 105, "back": 106, "knobLeft": 111, "knobRight": 112, "knobUp": 113, "knobDown": 114, "home": 200, "play": 201, "pause": 202,
    "playOrPause": 203, "next": 204, "prev": 205, "acceptPhone": 300, "rejectPhone": 301,
    "requestVideoFocus": 500, "releaseVideoFocus": 501, "wifiEnable": 1000, "autoConnetEnable": 1001,
    "wifiConnect": 1002, "scanningDevice": 1003, "deviceFound": 1004, "deviceNotFound": 1005,
    "connectDeviceFailed": 1006, "btConnected": 1007, "btDisconnected": 1008, "wifiConnected": 1009,
    "wifiDisconnected": 1010, "btPairStart": 1011, "wifiPair": 1012,
}
CMD_NAME = {v: k for k, v in CMD.items()}

# Audio commands inside AudioData (AudioCommand)
AUDIO_CMD = {1: "output_start", 2: "output_stop", 3: "input_config", 4: "phonecall_start", 5: "phonecall_stop",
             6: "navi_start", 7: "navi_stop", 8: "siri_start", 9: "siri_stop", 10: "media_start", 11: "media_stop",
             12: "alert_start", 13: "alert_stop", 14: "incoming_call"}   # 14: seen with the real Carlinkit (ringing)
AUDIO_CMD_CODE = {v: k for k, v in AUDIO_CMD.items()}

# decodeType -> (Hz, channels)
DECODE_TYPES = {1: (44100, 2), 2: (44100, 2), 3: (8000, 1), 4: (48000, 2), 5: (16000, 1), 6: (24000, 1), 7: (16000, 2)}

PHONE_TYPES = {1: "AndroidMirror", 3: "CarPlay", 4: "iPhoneMirror", 5: "AndroidAuto", 6: "HiCar"}

# Ducati joystick -> CarPlay knob (Carlinkit commands)
# CarPlay keys (firmware A15W, table in lvalen91/CPC200-CCPA_resources, 02_Protocol_Reference/command_details.md,
# verified on 27 Oct 2026 with screenshots): 100-103 = left/right/up/down arrows, 104 = OK, 105 = RELEASE, to be
# sent after every press (without it, CarPlay thinks the key is still pressed and ignores the next ones), 106 = back,
# 111/112 = wheel counter-clockwise/clockwise, 113/114 = knob pushed up/down. 107-110 do not exist.
KNOB = {"ROTATE_CW": ["knobRight"], "ROTATE_CCW": ["knobLeft"],
        "UP": ["up", "selectUp"], "DOWN": ["down", "selectUp"], "LEFT": ["left", "selectUp"], "RIGHT": ["right", "selectUp"],
        "OK": ["selectDown", "selectUp"], "BACK": ["back"]}
# Test on 11 Nov 2026: 100/101 (left/right) step through items in sequence (in the left column "right" moves up), while
# 112 jumps from the column to the content. Alternative map to try live (echo keymap=tilt > /var/tmp/ck_cmd)
KNOB_TILT = dict(KNOB, LEFT=["knobLeft", "selectUp"], RIGHT=["knobRight", "selectUp"],
                 ROTATE_CW=["right", "selectUp"], ROTATE_CCW=["left", "selectUp"])
# "mix": left/right = knob (jumps between zones, track change during playback), up/down = wheel (steps through all
# items in sequence, reaches everything: 101 in the column moves up, 100 moves down)
KNOB_MIX = dict(KNOB_TILT, UP=["right", "selectUp"], DOWN=["left", "selectUp"])
# Systematic test on 17 Nov 2026 (bench, snapshots after every code; home grid, Phone lists, Apple Maps while navigating):
#   100/101 = the wheel: previous/next item in sequence, everywhere. In the grid it runs along the row and wraps to the
#             next one; in a list it is one row; in Apple Maps it is the only way to reach the buttons on the map.
#   102/103 = up/down by a page: in a list they jump many rows (the "lists jump" seen on the bike on 16 Nov).
#   111-114 = the knob tilted left/right/up/down: one step in that direction (grid rows and columns, one row in a
#             list); in a map application they pan the map instead of moving between the buttons.
# "joy": up/down = knob tilted (one step, never a page), left/right = the wheel (reaches everything, map buttons included).
KNOB_JOY = dict(KNOB, UP=["knobUp", "selectUp"], DOWN=["knobDown", "selectUp"])
KEYMAPS = {"arrows": KNOB, "tilt": KNOB_TILT, "mix": KNOB_MIX, "joy": KNOB_JOY}
# Default since 17 Nov 2026: "joy" (tried by hand in the grid, in the contacts, in Apple Maps and Google Maps while
# navigating). Before: "tilt", chosen on 11 Nov 2026 (from the column "right" enters the content; during playback
# left/right change track), which could not reach the map buttons and jumped in lists. "mix" rejected: in the grid
# up/down move sideways, in lists down went up. CARPLAY_KEYMAP changes it.
DEFAULT_KEYMAP = os.environ.get("CARPLAY_KEYMAP", "joy")

FILE_DPI, FILE_NIGHT, FILE_HAND, FILE_CHARGE = "/tmp/screen_dpi", "/tmp/night_mode", "/tmp/hand_drive_mode", "/tmp/charge_mode"
FILE_BOX_NAME = "/etc/box_name"
FILE_OEM_ICON, FILE_AIRPLAY_CONF = "/etc/oem_icon.png", "/etc/airplay.conf"
FILE_ICONS = ("/etc/icon_120x120.png", "/etc/icon_180x180.png", "/etc/icon_256x256.png")


def header(mtype: int, length: int) -> bytes:
    return struct.pack("<IIII", MAGIC, length, mtype, (mtype ^ 0xFFFFFFFF) & 0xFFFFFFFF)


def message(mtype: int, payload: bytes = b"") -> bytes:
    return header(mtype, len(payload)) + payload


def parse_header(data: bytes):
    magic, length, mtype, check = struct.unpack("<IIII", data)
    if magic != MAGIC:
        raise ValueError(f"wrong magic {magic:#x}")
    if check != (mtype ^ 0xFFFFFFFF) & 0xFFFFFFFF:
        raise ValueError(f"wrong type check for {mtype}")
    return mtype, length


async def read_message(reader: asyncio.StreamReader):
    """(type, data), or (None, None) when the connection is closed."""
    try:
        h = await reader.readexactly(16)
        # after a bridge restart the dongle may be in the middle of a message (29 Oct 2026): bytes are discarded
        # until a valid header comes back, instead of reopening the USB device (which stays busy: Resource busy)
        skipped = 0
        while True:
            try:
                mtype, length = parse_header(h)
                break
            except ValueError:
                if skipped > 4 << 20:
                    raise
                h = h[1:] + await reader.readexactly(1)
                skipped += 1
    except (asyncio.IncompleteReadError, ConnectionError):
        return None, None
    data = await reader.readexactly(length) if length else b""
    return mtype, data


# --- builders for host messages (sendable.ts)
def send_command(name: str) -> bytes:
    return message(COMMAND, struct.pack("<I", CMD[name]))


def send_file(path: str, content: bytes) -> bytes:
    name = path.encode("ascii") + b"\0"
    return message(SEND_FILE, struct.pack("<I", len(name)) + name + struct.pack("<I", len(content)) + content)


def send_number(path: str, value: int) -> bytes:
    return send_file(path, struct.pack("<I", value))


def send_icon_config(label: str) -> bytes:
    """Manufacturer icon in CarPlay (like SendIconConfig): tapping it makes the dongle send requestHostUI."""
    conf = {"oemIconVisible": 1, "name": "AutoBox", "model": "Magic-Car-Link-1.00",
            "oemIconPath": FILE_OEM_ICON, "oemIconLabel": label}
    text = "\n".join(f"{k} = {v}" for k, v in conf.items()) + "\n"
    return send_file(FILE_AIRPLAY_CONF, text.encode("ascii"))


def send_open(width, height, fps, fmt=5, packet_max=49152, ibox=2, phone_mode=2) -> bytes:
    return message(OPEN, struct.pack("<7I", width, height, fps, fmt, packet_max, ibox, phone_mode))


def send_box_settings(width, height, media_delay=300, sync_time_ms=None, brand=None) -> bytes:
    import time
    # HiCarConnectMode 0: otherwise multi-system dongles (A15W, HiCar 1) keep waiting for HiCar
    # and do not show up to the iPhone over Bluetooth (fields as in carlinkit-cxx)
    js = {"mediaDelay": media_delay, "syncTime": sync_time_ms or int(time.time() * 1000),
          "androidAutoSizeW": width, "androidAutoSizeH": height, "drivePosition": 0,
          "HiCarConnectMode": 0, "GNSSCapability": 0, "DashboardInfo": 1, "UseBTPhone": 0}
    if brand:
        # name of the CarPlay icon via the firmware route (brand → CarBrand: the firmware itself writes oemIconLabel and
        # oemIconVisible to /etc/airplay.conf). Letters, digits and spaces only: the firmware passes it to a shell.
        js["brand"] = "".join(c for c in brand if c.isalnum() or c == " ")[:31]
    return message(BOX_SETTINGS, json.dumps(js).encode("ascii"))


def send_mic(pcm16k_mono: bytes) -> bytes:
    return message(AUDIO_DATA, struct.pack("<IfI", 5, 0.0, 3) + pcm16k_mono)


# --- builders for dongle messages (readable.ts), used by the emulated Carlinkit
def video_data(width, height, h264: bytes, flags=0) -> bytes:
    return message(VIDEO_DATA, struct.pack("<5I", width, height, flags, len(h264), 0) + h264)


def audio_data(decode_type: int, audio_type: int, pcm: bytes = b"", command: Optional[str] = None, volume=0.0) -> bytes:
    head = struct.pack("<IfI", decode_type, volume, audio_type)
    if command:
        return message(AUDIO_DATA, head + struct.pack("<b", AUDIO_CMD_CODE[command]))
    return message(AUDIO_DATA, head + pcm)


def media_data(info: dict) -> bytes:
    return message(MEDIA_DATA, struct.pack("<I", 1) + json.dumps(info).encode("utf-8") + b"\0")


# Call state from the dongle: MediaData subtype 100, JSON {CallStatus, CallDirection, CallName, CallNumber}.
# CallStatus codes to be verified with the real Carlinkit (26 Oct 2026): 1 ringing, 2 outgoing, 3 active, 0/4 ended.
CALL_IDLE, CALL_RINGING, CALL_DIALING, CALL_ACTIVE, CALL_ENDED = 0, 1, 2, 3, 4


def call_status(status: int, direction: int = 0, name: str = "", number: str = "") -> bytes:
    info = {"CallStatus": status, "CallDirection": direction, "CallName": name, "CallNumber": number}
    return message(MEDIA_DATA, struct.pack("<I", 100) + json.dumps(info).encode("utf-8") + b"\0")


def plugged(phone_type=3, wifi=1) -> bytes:
    return message(PLUGGED, struct.pack("<II", phone_type, wifi))


def command(name: str) -> bytes:
    return message(COMMAND, struct.pack("<I", CMD[name]))


def parse_audio(data: bytes) -> dict:
    decode_type, volume, audio_type = struct.unpack("<IfI", data[:12])
    rest = data[12:]
    out = {"decode_type": decode_type, "volume": volume, "audio_type": audio_type}
    if len(rest) == 1:
        out["command"] = AUDIO_CMD.get(struct.unpack("<b", rest)[0], rest[0])
    elif len(rest) == 4:
        out["volume_duration"] = struct.unpack("<f", rest)[0]
    else:
        out["pcm"] = rest
    return out


class CarlinkitHost:
    """Carlinkit driver on the Pi side (port of DongleDriver.start/close from node-carplay).

    The transport is a StreamReader/Writer: TCP on the bench, a USB bulk adapter on the real Pi.
    """

    def __init__(self, reader, writer, log: Callable, width=848, height=304, fps=30, audio_transfer=False,
                 box_name="Multistrada", on_message: Optional[Callable] = None,
                 oem_label: Optional[str] = None, oem_icon_png: Optional[bytes] = None):
        self.reader, self.writer, self.log = reader, writer, log
        self.width, self.height, self.fps = width, height, fps
        self.audio_transfer = audio_transfer
        self.box_name = box_name
        self.on_message = on_message
        self.oem_label, self.oem_icon_png = oem_label, oem_icon_png
        self.state = "closed"        # closed -> opening -> connected
        self._hb = None
        self._lock = asyncio.Lock()

    async def send(self, raw: bytes) -> None:
        async with self._lock:
            self.writer.write(raw)
            await self.writer.drain()

    async def configure_icon(self) -> None:
        """One-off (the dongle keeps it): manufacturer icon and name inside CarPlay."""
        if not self.oem_label:
            return
        await self.send(send_icon_config(self.oem_label))
        if self.oem_icon_png:
            for path in (FILE_OEM_ICON,) + FILE_ICONS:
                await self.send(send_file(path, self.oem_icon_png))
        self.log("carlinkit_icon_configured", label=self.oem_label, icon_bytes=len(self.oem_icon_png or b""))

    async def start(self) -> None:
        """CarPlay on: node-carplay startup sequence."""
        self.state = "opening"
        msgs = [send_number(FILE_DPI, 160), send_open(self.width, self.height, self.fps),
                send_number(FILE_NIGHT, 1), send_number(FILE_HAND, 0), send_number(FILE_CHARGE, 1),
                send_file(FILE_BOX_NAME, self.box_name.encode("ascii")[:16]),
                send_box_settings(self.width, self.height, brand=self.oem_label), send_command("wifiEnable"),
                send_command("wifi5g"),
                send_command("mic"), send_command("audioTransferOn" if self.audio_transfer else "audioTransferOff")]
        for m in msgs:
            await self.send(m)
        self.log("carlinkit_open_sent", width=self.width, height=self.height, fps=self.fps,
                 audio_transfer=self.audio_transfer)
        await asyncio.sleep(1.0)
        await self.configure_icon()      # after "brand": on some hardware the firmware forces the name "Car"
        await self.send(send_command("wifiConnect"))
        await self.send(send_command("frame"))         # full frame immediately (session already open)
        if self._hb is None:
            self._hb = asyncio.ensure_future(self._heartbeat())

    async def stop(self) -> None:
        """CarPlay off: CloseDongle (the dongle disconnects the phone and stays closed until the next Open)."""
        await self.send(message(CLOSE_DONGLE))
        self.state = "closed"
        if self._hb:
            self._hb.cancel()
            self._hb = None
        self.log("carlinkit_close_sent")

    async def _heartbeat(self):
        while True:
            await asyncio.sleep(2.0)
            await self.send(message(HEARTBEAT))

    async def key(self, joystick_key: str) -> None:
        for i, name in enumerate(KEYMAPS.get(getattr(self, "keymap", DEFAULT_KEYMAP), KNOB).get(joystick_key, [])):
            if i:
                await asyncio.sleep(0.1)               # release shortly after the press (as in the tests)
            await self.send(send_command(name))

    async def run(self) -> None:
        """Reads the dongle's messages and passes them to on_message(type, data)."""
        while True:
            mtype, data = await read_message(self.reader)
            if mtype is None:
                self.log("carlinkit_usb_closed")
                return
            if mtype == PLUGGED:
                self.state = "connected"
            elif mtype == UNPLUGGED:
                self.state = "closed" if self.state == "closed" else "opening"
            if self.on_message:
                await self.on_message(mtype, data)
