"""Test screen for the bike dashboard: a menu driven by the joystick.

Adapts to any resolution declared by the bike. Redraws only when something changes
(key, menu, clock each second): the mySPIN session reuses the last JPEG if the version does not change.
"""
from __future__ import annotations

import threading
import time

from PIL import Image, ImageDraw, ImageFont

MENU = ["Music", "Phone", "Key test", "Info"]

BG = (12, 14, 18)
FG = (235, 235, 235)
DIM = (130, 135, 145)
ACCENT = (204, 0, 0)       # Ducati red
SELECT_BG = (40, 44, 52)


def _font(size: int):
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                 "/Library/Fonts/Arial Bold.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


class TestScreen:
    def __init__(self, title: str = "MTS Pi test", clock: bool = True):
        self.title = title
        self.clock = clock
        self.selected = 0
        self.last_key = "—"
        self.message = "Use the joystick"
        self.keys_seen = set()
        self.key_count = 0
        self.frames_sent = 0
        self.started = time.monotonic()
        self._lock = threading.Lock()
        self._version = 0
        self._clock = -1
        self._fonts = {}

    # The mySPIN session reads .version to know whether it must re-encode the frame.
    @property
    def version(self) -> tuple:
        now = int(time.monotonic() - self.started) if self.clock else 0
        return (self._version, now)

    def _bump(self) -> None:
        self._version += 1

    def on_key(self, key: str, action: str) -> None:
        with self._lock:
            self.key_count += 1
            self.last_key = f"{key} {action}"
            if action not in ("PRESS", "CLICK", "LONG_PRESS"):
                self._bump()
                return
            self.keys_seen.add(key)
            if key in ("UP", "ROTATE_CCW", "LEFT"):
                self.selected = (self.selected - 1) % len(MENU)
            elif key in ("DOWN", "ROTATE_CW", "RIGHT"):
                self.selected = (self.selected + 1) % len(MENU)
            elif key == "OK":
                self.message = f"Selected: {MENU[self.selected]}"
            elif key == "BACK":
                self.message = "Back"
            self._bump()

    def on_frame_sent(self, size: int) -> None:
        self.frames_sent += 1

    def font(self, size: int):
        if size not in self._fonts:
            self._fonts[size] = _font(size)
        return self._fonts[size]

    def render(self, width: int, height: int) -> Image.Image:
        with self._lock:
            img = Image.new("RGB", (width, height), BG)
            d = ImageDraw.Draw(img)
            u = height / 304.0  # unit: the layout is designed for 848x304 and scales
            pad = int(16 * u)

            # Title
            d.rectangle([0, 0, width, int(6 * u)], fill=ACCENT)
            d.text((pad, int(14 * u)), self.title, font=self.font(int(46 * u)), fill=FG)
            d.text((pad, int(68 * u)), "MTS Pi — mySPIN test", font=self.font(int(18 * u)), fill=DIM)

            # Menu on the right
            mx = int(width * 0.56)
            row = int(48 * u)
            for i, item in enumerate(MENU):
                y = int(16 * u) + i * row
                if i == self.selected:
                    d.rectangle([mx - pad // 2, y, width - pad, y + row - int(6 * u)], fill=SELECT_BG, outline=ACCENT, width=max(1, int(3 * u)))
                d.text((mx, y + int(8 * u)), item, font=self.font(int(26 * u)), fill=FG if i == self.selected else DIM)

            # Status on the left
            up = int(time.monotonic() - self.started) if self.clock else 0
            short = {"UP": "UP", "DOWN": "DOWN", "LEFT": "LEFT", "RIGHT": "RIGHT", "OK": "OK", "BACK": "BACK",
                     "ROTATE_CW": "ROT+", "ROTATE_CCW": "ROT-"}
            seen = " ".join(v for k, v in short.items() if k in self.keys_seen)
            lines = [
                (self.message, FG, 24),
                (f"Last key: {self.last_key}", FG, 20),
                (f"Keys seen: {len(self.keys_seen)}/8  {seen}", DIM, 15),
                (f"Frames: {self.frames_sent}   up for {up // 60}:{up % 60:02d}", DIM, 15),
            ]
            y = int(118 * u)
            for text, color, size in lines:
                d.text((pad, y), text, font=self.font(int(size * u)), fill=color)
                y += int((size + 14) * u)
            return img
