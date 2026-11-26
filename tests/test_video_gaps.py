"""The short stops of the picture in the five-second log line (26 Nov 2026): the longest wait between two pictures
and how many waits were longer than a quarter of a second, for the pictures that arrive and for the ones sent."""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from mtspi import bridge as br  # noqa: E402


def test_longest_wait_and_pauses_since_the_last_reading():
    g = br._Gaps()
    assert g.take() is None                                   # nothing seen: nothing to say
    for t in (0.00, 0.05, 0.10, 0.45, 0.50, 1.20, 1.25):      # two waits over 0.25 s: 0.35 and 0.70
        g.mark(t)
    assert g.take() == (700, 2)
    g.mark(1.30)                                              # the wait across two readings is still measured
    g.mark(1.90)
    assert g.take() == (600, 1)
    assert g.take() is None                                   # a silent period...
    g.mark(60.0)                                              # ...then the stream starts again: not a pause
    g.mark(60.05)
    assert g.take() == (50, 0)


def test_the_log_line_has_arrival_and_sent_only_when_they_mean_something():
    b = br.Bridge.__new__(br.Bridge)
    b._cp_gaps = br._Gaps()
    b.display = types.SimpleNamespace(gaps=br._Gaps(), mode="carplay")
    assert b.video_gaps() == {}
    for t in (0.0, 0.05, 0.40):
        b._cp_gaps.mark(t)
        b.display.gaps.mark(t + 0.01)
    assert b.video_gaps() == {"cp_gap_ms": 350, "cp_pauses": 1, "tx_gap_ms": 350, "tx_pauses": 1}
    b.display.mode = "launcher"                               # the Pi's own page: pictures sent when it changes
    for t in (1.0, 3.0):
        b.display.gaps.mark(t)
    assert b.video_gaps() == {}
    b.display.mode = "carplay"                                # and the launcher's waits are not carried over
    b.display.gaps.mark(3.05)
    assert b.video_gaps() == {"tx_gap_ms": 50, "tx_pauses": 0}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
