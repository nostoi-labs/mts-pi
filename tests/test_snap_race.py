#!/usr/bin/env python3
"""The "snap" test command must not touch the video frame in the main loop (11 Nov 2026 on the Pi: "free or
corruption", bridge dead, while the bike thread was converting the same frame). It now saves the last image
already prepared. Here: one thread replaces the frames, one prepares them for the bike, and meanwhile 200
images are saved the way snap does: it must finish without errors and with valid PNGs.
Note: on the Mac not even the old snap crashes with this test (3 clean runs out of 3), so this checks the
new code path, it does not reproduce the failure: that has to be confirmed on the Pi."""
import os
import sys
import tempfile
import threading
import time
import types

import av
import numpy as np
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from mtspi import bridge  # noqa: E402

d = bridge.Display(types.SimpleNamespace(launcher=None))
d.mode = "carplay"
stop = threading.Event()
errors = []


def decoder():
    i = 0
    while not stop.is_set():
        arr = np.full((480, 1280, 3), i % 255, dtype=np.uint8)
        d.carplay_frame = av.VideoFrame.from_ndarray(arr, format="rgb24").reformat(format="yuv420p")
        d.carplay_seq += 1
        i += 1


def frame_thread():
    while not stop.is_set():
        try:
            if d.carplay_frame is not None:
                d.render(848, 304)
        except Exception as e:      # pragma: no cover
            errors.append(repr(e))


threads = [threading.Thread(target=decoder, daemon=True), threading.Thread(target=frame_thread, daemon=True)]
for t in threads:
    t.start()
time.sleep(0.3)
path = os.path.join(tempfile.mkdtemp(), "cp.png")
saved = 0
for _ in range(200):
    img = d.last_cp_image          # like the snap command: only the last image already prepared
    if img is not None:
        img.save(path)
        with Image.open(path) as chk:
            assert chk.size == (848, 304), chk.size
        saved += 1
stop.set()
for t in threads:
    t.join(2)
assert not errors, errors[:3]
assert saved >= 190, saved
print(f"OK snap without race: {saved} images saved")
