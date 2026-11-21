"""Tyre sensors heard only around their broadcasts (mtspi/tpms.py, class Windows), on a simulated ride: two sensors
broadcasting every 94.4 s on their own clocks, as measured on the bike on 21 Nov 2026.
python -m pytest tests, or python tests/test_tpms_windows.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from mtspi import tpms  # noqa: E402

STEP = 0.1


def ride(wins, sensors, seconds, search_misses=(), start=1000.0, drift=1.0):
    """Runs the clock: each sensor speaks at its moments and is heard when the radio is listening, except for the
    broadcasts listed in search_misses (the search listens 10% of the time and loses about half of them).
    Returns (seconds listened by kind, [(time, address, kind)] heard, [(time, address)] missed)."""
    said = {a: 0 for a in sensors}
    listened, heard, missed = {"search": 0.0, "window": 0.0}, [], []
    steps = int(seconds / STEP)
    for i in range(steps):
        t = start + i * STEP
        on, kind, wait = wins.listen(t)
        assert 0 < wait <= 1.0
        if on:
            listened[kind] += STEP
        for addr, (first, period) in sensors.items():
            due = start + first + said[addr] * period * drift
            if t >= due:
                said[addr] += 1
                if on and (addr, said[addr]) not in search_misses:
                    heard.append((round(t - start, 1), addr, kind))
                    wins.heard_sensor(addr, t)
                else:
                    missed.append((round(t - start, 1), addr))
    return listened, heard, missed


def new(period=0.0, **kw):
    clock = [1000.0]
    return tpms.Windows(period_s=period, now=lambda: clock[0], **kw)


def test_search_then_windows_with_the_period_given():
    wins = new(period=94.4)
    sensors = {"front": (20.0, 94.4), "rear": (90.0, 94.4)}
    listened, heard, missed = ride(wins, sensors, 3600)
    assert missed == []                                   # every broadcast of the hour is heard
    assert [h[2] for h in heard[:2]] == ["search", "search"]
    assert all(h[2] == "window" for h in heard[2:])
    assert listened["search"] < 95                        # the search ends when both have spoken
    # the radio is asked for about 1.4 s before each broadcast: 3% of the hour, instead of all of it
    assert listened["window"] / 3600 < 0.035, listened


def test_period_learned_from_the_sensors():
    wins = new()
    sensors = {"front": (20.0, 94.4), "rear": (90.0, 94.4)}
    listened, heard, missed = ride(wins, sensors, 3600)
    assert abs(wins.period - 94.4) < 0.3
    assert len(missed) == 0
    assert listened["search"] < 120                       # until the front one has spoken twice
    assert listened["window"] / 3600 < 0.07               # the half-way moments are tried for the first rounds


def test_a_broadcast_missed_in_the_search_does_not_double_the_period():
    wins = new()
    sensors = {"front": (20.0, 94.4), "rear": (90.0, 94.4)}
    # the search loses the second broadcast of each sensor: the first gaps measured are 188.8 s
    listened, heard, missed = ride(wins, sensors, 3600, search_misses={("front", 2), ("rear", 2)})
    assert abs(wins.period - 94.4) < 0.3, wins.period
    late = [m for m in missed if m[0] > 600]
    assert late == [], late                               # once learned, nothing more is lost


def test_window_widens_after_a_miss_and_follows_a_slow_clock():
    wins = new(period=94.4)
    sensors = {"front": (20.0, 94.4), "rear": (90.0, 94.4)}
    # sensors 0.8% slow (cold tyres): 0.75 s later at every round, inside the margin; and one broadcast lost on the way
    listened, heard, missed = ride(wins, sensors, 3600, search_misses={("front", 6)}, drift=1.008)
    assert [m[1] for m in missed] == ["front"]
    assert sum(1 for h in heard if h[1] == "front") >= 35


def test_a_sensor_gone_is_searched_for_again():
    wins = new(period=94.4)
    listened, heard, missed = ride(wins, {"front": (20.0, 94.4), "rear": (90.0, 94.4)}, 400)
    assert len(wins.heard) == 2
    t = 1000.0 + 400
    # the rear sensor stops (flat battery): after eight periods it is forgotten and the search comes back in turns
    listened, heard, missed = ride(wins, {"front": (20.0 + 5 * 94.4 - 400, 94.4)}, 3600, start=t)
    assert "rear" not in wins.heard and "front" in wins.heard
    assert listened["search"] > 100                       # some searching for the missing one
    assert (listened["search"] + listened["window"]) / 3600 < 0.2


def test_no_sensor_at_all_listens_in_turns_not_for_ever():
    wins = new()
    listened, heard, missed = ride(wins, {}, 7200)
    assert heard == []
    assert 360 <= listened["search"] <= 360 + 8 * 115     # the first six minutes, then 110 s every quarter of an hour


def test_listening_all_the_time_stays_possible():
    assert tpms.Windows(period_s=94.4).fixed and not tpms.Windows().fixed


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
