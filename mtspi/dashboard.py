"""Pi home screen on the bike dashboard (inside Ducati Connect, when CarPlay is not on screen). Dark theme with iOS-style accents.

Two tabs, driven by the joystick (design decision, 22 Oct 2026: no data we might not have):
- CarPlay: status, Start / Back to CarPlay, Turn off;
- Connections: iPhone ↔ Pi ↔ Multistrada and the Carlinkit, status of each link, audio source,
  iPhone battery and signal (HFP indicators); Diagnostics and Settings buttons.

No clock, speed, gear or fuel: the Ducati dashboard always shows them above the mySPIN area.
"""
from __future__ import annotations

import time

from PIL import Image, ImageDraw

from . import i18n, ui
from .i18n import t
from .settings import FPS_CHOICES, IDLE_CHOICES, QUALITY_CHOICES, fps_label, idle_label, quality_label
from .ui import (BLACK, BLUE, CYAN, FILL, GREEN, ORANGE, RED, SECONDARY, TERTIARY, TEXT, WELL, font)



class Dashboard:
    def __init__(self, bridge):
        self.b = bridge
        self.focus = "carplay_main"
        self.page = "home"          # home | diag | settings | tyres
        self.set_row = 0
        self._v = 0

    # ---------------------------------------------------------------- navigation
    def focusables(self):
        out = ["carplay_main"]
        if self.b.carplay_state() != "off":
            out.append("carplay_off")
        if self.b.tyres_enabled():
            out.append("tyres")
        out += ["diag", "settings"]
        return out

    @property
    def version(self):
        """Changes only when what is on screen changes: every redraw costs CPU on the Pi (previously 10 per second even
        when static, ~40%) and the session sends the frame only if it differs from the last one."""
        b = self.b
        st = b.carplay_state()
        if self.page == "diag":
            tick = int(time.monotonic() * 2)            # live numbers
        elif self.page == "tyres":
            tick = int(time.monotonic() / 5)            # the age of the readings
        elif self.page == "home" and st == "connecting":
            tick = int(time.monotonic() * 10)           # spinning ring and scrolling dashes
        else:
            tick = 0
        cind = b.cind if b.iphone is not None else {}
        state = (st, b.dongle is not None, b.iphone is not None, b.bike is not None or b.session is not None,
                 b.audio_source(), cind.get("battchg"), cind.get("signal"))
        return (self._v, tick, state)

    def bump(self):
        self._v += 1

    def on_key(self, key: str) -> None:
        if self.page == "settings":
            self.settings_key(key)
            self.bump()
            return
        if self.page in ("diag", "tyres"):
            if key in ("BACK", "OK"):
                self.page = "home"
            self.bump()
            return
        f = self.focusables()
        if self.focus not in f:
            self.focus = f[0]
        i = f.index(self.focus)
        if key in ("RIGHT", "DOWN", "ROTATE_CW"):
            self.focus = f[min(len(f) - 1, i + 1)]
        elif key in ("LEFT", "UP", "ROTATE_CCW"):
            self.focus = f[max(0, i - 1)]
        elif key == "OK":
            if self.focus == "carplay_main":
                self.b.request_carplay()
            elif self.focus == "carplay_off":
                self.b.request_carplay_off()
                self.focus = "carplay_main"
            elif self.focus == "tyres":
                self.page = "tyres"
            elif self.focus == "diag":
                self.page = "diag"
            elif self.focus == "settings":
                self.page = "settings"
                self.set_row = 0
        self.bump()

    # ---------------------------------------------------------------- drawing
    def render(self, W: int, H: int) -> Image.Image:
        img = Image.new("RGB", (W, H), BLACK)
        d = ImageDraw.Draw(img)
        u = H / 304.0
        if self.page == "diag":
            self.render_diag(d, W, H, u)
            return img
        if self.page == "settings":
            self.render_settings(d, W, H, u)
            return img
        if self.page == "tyres":
            self.render_tyres(d, W, H, u)
            return img
        pad = int(10 * u)
        gap = int(10 * u)
        widths = [0.42, 0.58]
        avail = W - 2 * pad - gap * (len(widths) - 1)
        x = pad
        boxes = []
        for w in widths:
            ww = int(avail * w)
            boxes.append((x, pad, x + ww, H - pad))
            x += ww + gap
        self.card_carplay(d, boxes[0], u)
        self.card_links(d, boxes[1], u)
        return img

    def header(self, d, box, title, u, right=None, right_color=SECONDARY):
        x0, y0, x1, _ = box
        d.text((x0 + int(16 * u), y0 + int(14 * u)), title, font=font(int(17 * u), "semibold"), fill=TEXT)
        if right:
            d.text((x1 - int(16 * u), y0 + int(15 * u)), right, font=font(int(13 * u), "medium"), fill=right_color, anchor="ra")

    # CarPlay ------------------------------------------------------------------
    def card_carplay(self, d, box, u):
        x0, y0, x1, y1 = box
        st = self.b.carplay_state()
        has_dongle = self.b.dongle is not None
        ui.card(d, box, radius=int(18 * u))
        self.header(d, box, t("carplay"), u)
        # status ring
        r = int(42 * u)
        cx, cy = x0 + int(16 * u) + r, y0 + int(62 * u) + r
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=FILL, width=int(7 * u))
        if not has_dongle:
            color, title, sub = RED, t("cp_nodongle"), t("cp_nodongle_sub")
        elif st == "on":
            color, title, sub = GREEN, t("cp_on"), t("cp_on_sub")
            d.arc([cx - r, cy - r, cx + r, cy + r], 0, 360, fill=GREEN, width=int(7 * u))
        elif st == "connecting":
            secs = time.monotonic() - (self.b.carplay_since or time.monotonic())
            color, title, sub = ORANGE, t("cp_connecting"), f"{secs:.0f} s"
            a = (time.monotonic() * 360) % 360
            d.arc([cx - r, cy - r, cx + r, cy + r], a, a + 100, fill=ORANGE, width=int(7 * u))
        else:
            color, title, sub = TERTIARY, t("cp_off"), t("cp_off_sub")
        # icon: 2x2 app grid
        s = int(13 * u)
        for i in range(2):
            for j in range(2):
                gx = cx - s - int(2 * u) + i * (s + int(4 * u))
                gy = cy - s - int(2 * u) + j * (s + int(4 * u))
                d.rounded_rectangle([gx, gy, gx + s, gy + s], radius=int(4 * u), fill=color if st == "on" else (60, 60, 66))
        tx = cx + r + int(16 * u)
        d.text((tx, cy - int(20 * u)), title, font=font(int(22 * u) if len(title) > 8 else int(26 * u), "bold"), fill=TEXT)
        d.text((tx, cy + int(12 * u)), sub, font=font(int(14 * u), "medium"), fill=color if st != "off" else SECONDARY)
        # buttons
        by1 = y1 - int(14 * u)
        by0 = by1 - int(42 * u)
        if st == "off":
            label = t("btn_start")
        elif st == "connecting":
            label = t("btn_wait")
        else:
            label = t("btn_back_cp")
        if st == "off":
            ui.pill(d, [x0 + int(14 * u), by0, x1 - int(14 * u), by1], label, font(int(16 * u), "semibold"),
                    BLUE if has_dongle else FILL, focus=self.focus == "carplay_main")
        else:
            mid = x0 + int((x1 - x0) * 0.64)
            ui.pill(d, [x0 + int(14 * u), by0, mid - int(5 * u), by1], label, font(int(15 * u), "semibold"),
                    BLUE, focus=self.focus == "carplay_main")
            ui.pill(d, [mid + int(5 * u), by0, x1 - int(14 * u), by1], t("btn_off"), font(int(15 * u), "semibold"),
                    FILL, color=RED, focus=self.focus == "carplay_off")

    # Connections --------------------------------------------------------------
    def card_links(self, d, box, u):
        x0, y0, x1, y1 = box
        ui.card(d, box, radius=int(18 * u))
        st = self.b.carplay_state()
        # the bike counts as connected if there is Bluetooth audio or the mySPIN session (in the garage only the latter)
        iphone, dongle = self.b.iphone is not None, self.b.dongle is not None
        bike = self.b.bike is not None or self.b.session is not None
        # header: title and audio source
        src = self.b.audio_source()
        src_txt = f"{t('audio')} · {src}" if src != "none" else f"{t('audio')} · —"
        self.header(d, box, t("links"), u, right=src_txt,
                    right_color=CYAN if src == "CarPlay" else (GREEN if src == "Bluetooth" else TERTIARY))
        w = x1 - x0
        ny = y0 + int(76 * u)
        nodes = {"iphone": (x0 + int(w * 0.16), ny), "pi": (x0 + int(w * 0.5), ny), "bike": (x0 + int(w * 0.84), ny)}
        ck = (x0 + int(w * 0.5), y0 + int(150 * u))
        now = time.monotonic()

        def link(a, b, state, label, label_above=True):
            (ax, ay), (bx, by) = a, b
            col = {"ok": GREEN, "wait": ORANGE, "off": (52, 52, 58), "blue": BLUE}[state]
            wdt = int(4 * u)
            if state == "wait":     # scrolling dashes
                n = 10
                off = (now * 2) % 1
                for k in range(n):
                    s0, s1 = (k + off) / n, (k + off + 0.5) / n
                    if s1 > 1:
                        continue
                    d.line([ax + (bx - ax) * s0, ay + (by - ay) * s0, ax + (bx - ax) * s1, ay + (by - ay) * s1], fill=col, width=wdt)
            else:
                d.line([ax, ay, bx, by], fill=col, width=wdt)
            if label:
                mx, my = (ax + bx) / 2, (ay + by) / 2
                d.text((mx, my + (-int(13 * u) if label_above else int(12 * u))), label, font=font(int(11 * u), "medium"),
                       fill=SECONDARY if state != "off" else TERTIARY, anchor="mm")

        link(nodes["iphone"], nodes["pi"], "ok" if iphone else "off", t("bt"))
        link(nodes["pi"], nodes["bike"], "ok" if bike else "off", t("bt_wifi"))
        link(nodes["pi"], ck, "ok" if dongle else "off", "")
        cp_state = {"on": "blue", "connecting": "wait"}.get(st, "off")
        link(ck, nodes["iphone"], cp_state, "CarPlay" if st != "off" else "", label_above=False)
        self.node(d, nodes["iphone"], "iPhone", "phone", iphone, u)
        self.node(d, nodes["pi"], "Pi", "pi", True, u)
        self.node(d, nodes["bike"], t("node_bike"), "bike", bike, u)
        self.node(d, ck, "Carlinkit", "dongle", dongle, u, small=True)
        # iPhone battery and signal (HFP indicators) below its name
        cind = self.b.cind
        ix, iy = nodes["iphone"]
        bx, by = ix - int(22 * u), iy + int(40 * u)
        lvl = cind.get("battchg", 0) if iphone else 0
        d.rounded_rectangle([bx, by, bx + int(22 * u), by + int(11 * u)], radius=int(3 * u), outline=SECONDARY, width=max(1, int(1.5 * u)))
        d.rectangle([bx + int(22 * u), by + int(3 * u), bx + int(24 * u), by + int(8 * u)], fill=SECONDARY)
        if lvl:
            d.rectangle([bx + int(3 * u), by + int(3 * u), bx + int(3 * u) + int(16 * u * lvl / 5), by + int(8 * u)],
                        fill=GREEN if lvl > 1 else RED)
        sx = bx + int(30 * u)
        sig = cind.get("signal", 0) if iphone else 0
        for k in range(5):
            hgt = int((3 + k * 2) * u)
            d.rectangle([sx + k * int(4 * u), by + int(11 * u) - hgt, sx + k * int(4 * u) + int(2.5 * u), by + int(11 * u)],
                        fill=TEXT if k < sig else (52, 52, 58))
        # buttons
        py1 = y1 - int(14 * u)
        py0 = py1 - int(42 * u)
        buttons = [("diag", t("btn_diag")), ("settings", t("btn_settings"))]
        if self.b.tyres_enabled():
            buttons.insert(0, ("tyres", t("btn_tyres")))
        bx0, bx1, gap = x0 + int(14 * u), x1 - int(14 * u), int(8 * u)
        # widths in proportion to the labels, so that three buttons fit where two were
        f = font(int((15 if len(buttons) == 2 else 13.5) * u), "semibold")
        want = [d.textlength(label, font=f) + int(26 * u) for _, label in buttons]
        scale = (bx1 - bx0 - gap * (len(buttons) - 1)) / sum(want)
        x = bx0
        for (name, label), w in zip(buttons, want):
            x2 = x + int(w * scale)
            ui.pill(d, [x, py0, x2, py1], label, f, FILL, focus=self.focus == name)
            x = x2 + gap

    def node(self, d, xy, label, kind, ok, u, small=False):
        x, y = xy
        r = int((15 if small else 19) * u)
        d.ellipse([x - r, y - r, x + r, y + r], fill=WELL, outline=GREEN if ok else (60, 60, 66), width=max(2, int(2.5 * u)))
        c = TEXT if ok else TERTIARY
        if kind == "phone":
            d.rounded_rectangle([x - int(6 * u), y - int(11 * u), x + int(6 * u), y + int(11 * u)], radius=int(3 * u), outline=c, width=max(1, int(2 * u)))
        elif kind == "pi":
            d.rounded_rectangle([x - int(9 * u), y - int(9 * u), x + int(9 * u), y + int(9 * u)], radius=int(2 * u), outline=c, width=max(1, int(2 * u)))
            for k in (-5, 0, 5):
                for sx in (-1, 1):
                    d.line([x + sx * int(9 * u), y + int(k * u), x + sx * int(13 * u), y + int(k * u)], fill=c, width=max(1, int(1.5 * u)))
        elif kind == "bike":
            d.rounded_rectangle([x - int(11 * u), y - int(7 * u), x + int(11 * u), y + int(7 * u)], radius=int(4 * u), outline=c, width=max(1, int(2 * u)))
            d.line([x - int(15 * u), y - int(9 * u), x + int(15 * u), y - int(9 * u)], fill=c, width=max(1, int(2 * u)))
        else:
            d.rounded_rectangle([x - int(8 * u), y - int(5 * u), x + int(8 * u), y + int(5 * u)], radius=int(2 * u), outline=c, width=max(1, int(2 * u)))
        f = font(int(12 * u), "semibold")
        ly = y + r + int(10 * u)
        bx0, by0, bx1, by1 = d.textbbox((x, ly), label, font=f, anchor="mm")
        d.rectangle([bx0 - int(4 * u), by0 - int(2 * u), bx1 + int(4 * u), by1 + int(2 * u)], fill=ui.PANEL)
        d.text((x, ly), label, font=f, fill=TEXT if ok else TERTIARY, anchor="mm")

    # Diagnostics ---------------------------------------------------------------
    def render_diag(self, d, W, H, u):
        info = self.b.diag_info()
        pad = int(10 * u)
        d.text((pad + int(6 * u), int(10 * u)), t("diag"), font=font(int(18 * u), "bold"), fill=TEXT)
        d.text((W - pad - int(6 * u), int(12 * u)), t("uptime_back", up=info["uptime"]),
               font=font(int(12 * u), "medium"), fill=CYAN, anchor="ra")
        cols = [(t("col_screen"), info["myspin"]), (t("col_links"), info["links"]), (t("col_system"), info["system"])]
        gap = int(8 * u)
        top = int(38 * u)
        cw = (W - 2 * pad - 2 * gap) // 3
        for i, (title, rows) in enumerate(cols):
            x0 = pad + i * (cw + gap)
            box = (x0, top, x0 + cw, H - pad)
            ui.card(d, box, radius=int(14 * u))
            d.text((x0 + int(12 * u), top + int(9 * u)), title, font=font(int(13 * u), "semibold"), fill=SECONDARY)
            y = top + int(32 * u)
            for k, v, state in rows:
                if y > H - pad - int(16 * u):
                    break
                col = {"ok": GREEN, "warn": ORANGE, "bad": RED, "off": TERTIARY}.get(state)
                if col:
                    d.ellipse([x0 + int(12 * u), y + int(4 * u), x0 + int(18 * u), y + int(10 * u)], fill=col)
                d.text((x0 + int(24 * u), y), k, font=font(int(11 * u), "medium"), fill=TERTIARY)
                d.text((x0 + cw - int(12 * u), y), str(v), font=font(int(11 * u), "semibold"), fill=TEXT, anchor="ra")
                y += int(16.5 * u)

    # Tyres --------------------------------------------------------------------
    def render_tyres(self, d, W, H, u):
        pad = int(10 * u)
        d.text((pad + int(6 * u), int(10 * u)), t("tyres"), font=font(int(18 * u), "bold"), fill=TEXT)
        d.text((W - pad - int(6 * u), int(12 * u)), t("tyres_back"), font=font(int(12 * u), "medium"), fill=CYAN, anchor="ra")
        info = self.b.tyres()
        wheels = info["wheels"]
        top, gap = int(38 * u), int(10 * u)
        if not wheels:
            box = (pad, top, W - pad, H - pad)
            ui.card(d, box, radius=int(14 * u))
            cx, cy = W // 2, (top + H - pad) // 2
            d.text((cx, cy - int(12 * u)), t("ty_wait") if info["listening"] else t("ty_off"),
                   font=font(int(20 * u), "semibold"), fill=TEXT if info["listening"] else ORANGE, anchor="mm")
            if info["listening"]:
                d.text((cx, cy + int(18 * u)), t("ty_wait_sub"), font=font(int(13 * u), "medium"), fill=SECONDARY, anchor="mm")
            return
        n = min(len(wheels), 3)
        cw = (W - 2 * pad - gap * (n - 1)) // n
        for i, w in enumerate(wheels[:n]):
            x0 = pad + i * (cw + gap)
            box = (x0, top, x0 + cw, H - pad)
            ui.card(d, box, radius=int(14 * u))
            name = {"front": t("ty_front"), "rear": t("ty_rear")}.get(w["wheel"]) or t("ty_sensor")
            d.text((x0 + int(16 * u), top + int(12 * u)), name, font=font(int(16 * u), "semibold"), fill=TEXT)
            d.text((x0 + cw - int(16 * u), top + int(15 * u)), w["id"], font=font(int(11 * u), "medium"), fill=TERTIARY, anchor="ra")
            # the pressure, large; orange or red when it is under the recommended one
            rec = w.get("recommended")
            col = TEXT
            if rec and w["bar"] is not None:
                col = RED if w["bar"] < rec[0] - 0.4 else ORANGE if w["bar"] < rec[0] - 0.2 else GREEN
            stale = w["age_s"] > 600
            big = font(int(70 * u), "bold")
            txt = f"{w['bar']:.2f}" if w["bar"] is not None else "–"
            by = top + int(96 * u)
            d.text((x0 + int(16 * u), by), txt, font=big, fill=TERTIARY if stale else col, anchor="lm")
            tw = d.textlength(txt, font=big)
            d.text((x0 + int(22 * u) + tw, by + int(16 * u)), "bar", font=font(int(18 * u), "semibold"), fill=SECONDARY, anchor="lm")
            if w["temp_c"] is not None:
                d.text((x0 + cw - int(16 * u), by - int(4 * u)), f"{w['temp_c']:.0f} °C", font=font(int(26 * u), "semibold"),
                       fill=TERTIARY if stale else TEXT, anchor="rm")
            y = top + int(150 * u)
            age = w["age_s"]
            small = [t("ty_ago_s", v=int(age)) if age < 90 else t("ty_ago_min", v=int(age // 60))]
            if w["volt"] is not None:
                small.append(t("ty_battery", v=f"{w['volt']:.1f}"))
            d.text((x0 + int(16 * u), y), "  ·  ".join(small), font=font(int(13 * u), "medium"),
                   fill=ORANGE if stale else SECONDARY)
            if rec:
                a = f"{rec[0]:.1f}"
                line = t("ty_recommended", a=a, b=f"{rec[1]:.1f}") if len(rec) > 1 and rec[1] != rec[0] else t("ty_recommended_1", a=a)
                d.text((x0 + int(16 * u), y + int(22 * u)), line + " bar", font=font(int(13 * u), "medium"), fill=TERTIARY)

    # Settings ----------------------------------------------------------------
    def settings_rows(self):
        st = self.b.settings
        return [
            ("lang", t("s_lang"), i18n.LANG_NAMES[st.lang]),
            ("autostart", t("s_autostart"), t("s_auto") if st.carplay_autostart else t("s_on_demand")),
            ("idle", t("s_idle"), idle_label(st.carplay_idle_off)),
            ("quality", t("s_quality"), quality_label(st.jpeg_quality)),
        ] + ([("fps", t("s_fps"), fps_label(st.carplay_fps))] if self.b.fps_fixed() is None else []) + [
            ("back", t("s_back"), ""),           # the joystick has no Back key: a row that does it (and hold ↑ too)
        ]

    def settings_key(self, key):
        rows = self.settings_rows()
        st = self.b.settings
        if key == "BACK":
            self.b.save_settings()
            self.page = "home"
            return
        if key in ("UP", "ROTATE_CCW"):
            self.set_row = max(0, self.set_row - 1)
            return
        if key in ("DOWN", "ROTATE_CW"):
            self.set_row = min(len(rows) - 1, self.set_row + 1)
            return
        if key not in ("OK", "RIGHT", "LEFT"):
            return
        step = -1 if key == "LEFT" else 1
        what = rows[self.set_row][0]
        if what == "back":
            if key == "OK":
                self.b.save_settings()
                self.page = "home"
            return
        if what == "lang":
            langs = i18n.LANGS
            st.lang = langs[(langs.index(st.lang) + step) % len(langs)]
            i18n.set_lang(st.lang)
        elif what == "autostart":
            st.carplay_autostart = not st.carplay_autostart
        elif what == "idle":
            i = IDLE_CHOICES.index(st.carplay_idle_off) if st.carplay_idle_off in IDLE_CHOICES else 0
            st.carplay_idle_off = IDLE_CHOICES[(i + step) % len(IDLE_CHOICES)]
        elif what == "quality":
            i = QUALITY_CHOICES.index(st.jpeg_quality) if st.jpeg_quality in QUALITY_CHOICES else 0
            st.jpeg_quality = QUALITY_CHOICES[(i + step) % len(QUALITY_CHOICES)]
        elif what == "fps":
            i = FPS_CHOICES.index(st.carplay_fps) if st.carplay_fps in FPS_CHOICES else 0
            st.carplay_fps = FPS_CHOICES[(i + step) % len(FPS_CHOICES)]
        self.b.apply_settings()

    def render_settings(self, d, W, H, u):
        pad = int(10 * u)
        d.text((pad + int(6 * u), int(10 * u)), t("settings"), font=font(int(18 * u), "bold"), fill=TEXT)
        d.text((W - pad - int(6 * u), int(12 * u)), t("settings_help"), font=font(int(12 * u), "medium"), fill=CYAN, anchor="ra")
        rows = self.settings_rows()
        top = int(42 * u)
        sp = int(4 * u)
        rh = min(int(58 * u), (H - pad - top - sp * (len(rows) - 1)) // len(rows))      # every row fits, the last one too
        for i, (what, label, value) in enumerate(rows):
            y0 = top + i * (rh + sp)
            box = (pad, y0, W - pad, y0 + rh)
            ui.card(d, box, radius=int(14 * u), focus=i == self.set_row)
            if what == "back":
                d.text((W // 2, y0 + rh // 2), "‹  " + label, font=font(int(16 * u), "semibold"),
                       fill=CYAN if i == self.set_row else SECONDARY, anchor="mm")
                continue
            d.text((pad + int(18 * u), y0 + rh // 2), label, font=font(int(16 * u), "semibold"), fill=TEXT, anchor="lm")
            vx = W - pad - int(18 * u)
            d.text((vx, y0 + rh // 2), f"‹  {value}  ›" if i == self.set_row else value,
                   font=font(int(16 * u), "semibold"), fill=CYAN if i == self.set_row else SECONDARY, anchor="rm")
