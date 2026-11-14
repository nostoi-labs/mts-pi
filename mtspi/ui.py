"""Shared visual style (dark theme with iOS-style accents): colours, fonts, cards."""
from __future__ import annotations

from PIL import ImageFont

# Palette: black background, dark cards, iOS accents.
BLACK = (0, 0, 0)
PANEL = (11, 11, 13)
PANEL_BORDER = (28, 28, 31)
WELL = (6, 6, 7)
FILL = (28, 28, 30)
TEXT = (255, 255, 255)
SECONDARY = (161, 161, 166)
TERTIARY = (110, 110, 116)
BLUE = (10, 132, 255)
CYAN = (99, 209, 255)
ORANGE = (255, 158, 10)
GREEN = (48, 209, 88)
RED = (255, 69, 58)
VIOLET = (191, 89, 242)
DUCATI_RED = (204, 0, 0)

_FONTS = {}

# Mac: SF Pro (variable). Pi: Inter (fonts-inter package). Fallback: DejaVu.
_CANDIDATES = [
    ("/System/Library/Fonts/SFNS.ttf", True),
    ("/usr/share/fonts/opentype/inter/InterVariable.ttf", True),
    ("/usr/share/fonts/truetype/inter/InterVariable.ttf", True),
    ("/usr/share/fonts/opentype/inter/Inter-Regular.otf", False),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", False),
]
_WEIGHTS = {"regular": b"Regular", "medium": b"Medium", "semibold": b"Semibold", "bold": b"Bold", "heavy": b"Heavy"}
_INTER_AXIS = {"regular": 400, "medium": 500, "semibold": 600, "bold": 700, "heavy": 800}


def font(size: int, weight: str = "semibold"):
    key = (int(size), weight)
    if key in _FONTS:
        return _FONTS[key]
    f = None
    for path, variable in _CANDIDATES:
        try:
            f = ImageFont.truetype(path, int(size))
        except OSError:
            continue
        if variable:
            try:
                names = f.get_variation_names()
                want = _WEIGHTS.get(weight, b"Semibold")
                if want in names:
                    f.set_variation_by_name(want)
                else:
                    f.set_variation_by_axes([_INTER_AXIS.get(weight, 600)])
            except Exception:
                pass
        break
    if f is None:
        f = ImageFont.load_default()
    _FONTS[key] = f
    return f


def card(d, box, radius=18, fill=PANEL, outline=PANEL_BORDER, focus=False):
    d.rounded_rectangle(box, radius=radius, fill=fill, outline=BLUE if focus else outline, width=3 if focus else 1)


def pill(d, box, text, f, fill, color=TEXT, focus=False):
    x0, y0, x1, y1 = box
    d.rounded_rectangle(box, radius=(y1 - y0) // 2, fill=fill, outline=TEXT if focus else fill, width=3 if focus else 1)
    d.text(((x0 + x1) // 2, (y0 + y1) // 2), text, font=f, fill=color, anchor="mm")
