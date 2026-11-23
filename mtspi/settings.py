"""Pi settings (JSON file on the writable partition; on the bench in ~/.config)."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass

from . import i18n

IDLE_CHOICES = [30, 60, 120, 0]          # seconds; 0 = never
QUALITY_CHOICES = [80, 55]               # JPEG: high, low (10 Nov 2026, standard variant at 2.4 GHz, moving map: q80 22 fps, q55 24; CarPlay ~18)
FPS_CHOICES = [15, 30]                   # pictures a second asked of CarPlay; 30 is the most the Carlinkit gives
# 15 by default since 23 Nov 2026: the bike has never asked for more than 15, and every picture CarPlay sends is
# decoded whether the dashboard takes it or not. Bench, moving map and music: the bridge's work went from 141% of one
# processor to 96%. 30 is kept as a choice, to compare the two on the bike.
# There was a "maximum" (q90, 20 fps) until 22 Nov 2026: twice the data to the dashboard and about 5 °C more, for a
# picture that looks the same on the bike. Removed; a card that had it saved goes back to high.


@dataclass
class Settings:
    lang: str = "en"
    carplay_autostart: bool = True
    carplay_idle_off: int = 0            # 0 = never: without CarPlay (Carlinkit config) no audio reaches the bike
    jpeg_quality: int = 80
    carplay_fps: int = 15

    @classmethod
    def load(cls, path: str) -> "Settings":
        s = cls()
        try:
            with open(path) as f:
                data = json.load(f)
            for k, v in data.items():
                if hasattr(s, k):
                    setattr(s, k, v)
        except (OSError, ValueError):
            pass
        if s.jpeg_quality not in QUALITY_CHOICES:
            s.jpeg_quality = QUALITY_CHOICES[0]
        if s.carplay_fps not in FPS_CHOICES:
            s.carplay_fps = FPS_CHOICES[0]
        i18n.set_lang(s.lang)
        return s

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(asdict(self), f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)


def idle_label(v: int) -> str:
    if v == 0:
        return i18n.t("s_never")
    return i18n.t("s_after", v=f"{v} s" if v < 60 else f"{v // 60} min")


def quality_label(v: int) -> str:
    return {80: i18n.t("s_q_high"), 55: i18n.t("s_q_low")}.get(v, str(v))


def fps_label(v: int) -> str:
    return i18n.t("s_fps_max", v=v) if v == max(FPS_CHOICES) else i18n.t("s_fps_n", v=v)
