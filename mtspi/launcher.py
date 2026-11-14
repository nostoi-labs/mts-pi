"""Pi menu on the bike dashboard (inside Ducati Connect, when CarPlay is not on screen).

Items: CarPlay (start or go back), Turn off CarPlay, Music (track and source; OK = play/pause),
Phone (contacts, messages), Status (iPhone Bluetooth, Carlinkit, audio).
"""
from __future__ import annotations

import threading
import time

from PIL import Image, ImageDraw

from .testui import _font

BG, FG, DIM, RED, SEL = (12, 14, 18), (235, 235, 235), (130, 135, 145), (204, 0, 0), (40, 44, 52)


class Launcher:
    def __init__(self, bridge):
        self.b = bridge
        self.sel = 0
        self.message = ""
        self._v = 0
        self._lock = threading.Lock()
        self._fonts = {}

    def items(self):
        st = self.b.carplay_state()
        first = {"off": "Start CarPlay", "connecting": "CarPlay: connecting…", "on": "Back to CarPlay"}[st]
        out = [("carplay", first)]
        if st != "off":
            out.append(("carplay_off", "Turn CarPlay off"))
        out += [("music", "Music"), ("phone", "Phone"), ("status", "Status")]
        return out

    @property
    def version(self):
        return (self._v, int(time.monotonic()))     # redraws at least once per second (status, timers)

    def bump(self):
        self._v += 1

    def on_key(self, key: str) -> None:
        items = self.items()
        self.sel = min(self.sel, len(items) - 1)
        if key in ("UP", "ROTATE_CCW", "LEFT"):
            self.sel = max(0, self.sel - 1)
        elif key in ("DOWN", "ROTATE_CW", "RIGHT"):
            self.sel = min(len(items) - 1, self.sel + 1)
        elif key == "OK":
            what = items[self.sel][0]
            if what == "carplay":
                self.b.request_carplay()
            elif what == "carplay_off":
                self.b.request_carplay_off()
                self.sel = 0
            elif what == "music":
                self.b.to_phone_bt({"type": "avrcp_cmd", "cmd": "play_pause"})
        self.bump()

    def font(self, s):
        if s not in self._fonts:
            self._fonts[s] = _font(s)
        return self._fonts[s]

    def render(self, W: int, H: int) -> Image.Image:
        u = H / 304.0
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, W, int(5 * u)], fill=RED)
        d.text((int(16 * u), int(12 * u)), "Multistrada · Pi", font=self.font(int(26 * u)), fill=FG)
        items = self.items()
        self.sel = min(self.sel, len(items) - 1)
        row = int(46 * u)
        for i, (_, label) in enumerate(items):
            y = int(52 * u) + i * row
            if i == self.sel:
                d.rectangle([int(10 * u), y, int(W * 0.46), y + row - int(6 * u)], fill=SEL, outline=RED, width=max(1, int(3 * u)))
            d.text((int(22 * u), y + int(8 * u)), label, font=self.font(int(24 * u)), fill=FG if i == self.sel else DIM)
        # right panel: details of the selected item
        x = int(W * 0.5)
        what = items[self.sel][0]
        lines = self.b.panel(what)
        y = int(52 * u)
        for text, big in lines:
            d.text((x, y), text, font=self.font(int((24 if big else 18) * u)), fill=FG if big else DIM)
            y += int((34 if big else 26) * u)
        return img
