"""Pi settings (JSON file on the writable partition; on the bench in ~/.config)."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass

from . import i18n

IDLE_CHOICES = [30, 60, 120, 0]          # seconds; 0 = never
QUALITY_CHOICES = [90, 80, 55]           # JPEG: maximum, high, low (10 Nov 2026, standard variant at 2.4 GHz, moving map: q90 20 fps, q80 22, q55 24; CarPlay ~18)


@dataclass
class Settings:
    lang: str = "en"
    carplay_autostart: bool = True
    carplay_idle_off: int = 0            # 0 = never: without CarPlay (Carlinkit config) no audio reaches the bike
    jpeg_quality: int = 80

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
    return {90: i18n.t("s_q_max"), 80: i18n.t("s_q_high"), 55: i18n.t("s_q_low")}.get(v, str(v))
