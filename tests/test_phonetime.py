"""The time of day read from the iPhone (mtspi/phonetime.py): the answers recorded from a real iPhone on 20 Nov 2026,
the phone's own questions on the same channel, and when the Pi's clock is set and when it is left alone.
python -m pytest tests, or python tests/test_phonetime.py"""
import calendar
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from mtspi import phonetime  # noqa: E402

# recorded: read-by-type answers for Current Time (2026-11-20 21:23:27 local) and Local Time Information (UTC+1, no DST)
REAL_TIME = bytes.fromhex("090c1f00ea070b1415171b051602")
REAL_INFO = bytes.fromhex("090422000400")
REAL_EPOCH = calendar.timegm((2026, 11, 20, 20, 23, 27)) + 0x16 / 256


class FakePhone:
    """Answers like the iPhone: first its own question (which services do you have?), then what we asked."""

    def __init__(self, answers, chatter=(bytes.fromhex("100100ffff0028"),)):
        self.answers, self.chatter, self.sent, self.queue, self.closed = dict(answers), list(chatter), [], [], False

    def send(self, p):
        self.sent.append(bytes(p))
        if p[0] == 0x08:
            uuid = int.from_bytes(p[5:7], "little")
            self.queue += self.chatter + [self.answers[uuid]]
            self.chatter = []

    def recv(self, n):
        if not self.queue:
            raise TimeoutError("timed out")
        return self.queue.pop(0)

    def close(self):
        self.closed = True


def _phone(**kw):
    return FakePhone({phonetime.UUID_CURRENT_TIME: REAL_TIME, phonetime.UUID_LOCAL_TIME_INFO: REAL_INFO}, **kw)


def test_real_answers_give_the_right_utc_time():
    p = _phone()
    got = phonetime.read("AA:BB:CC:DD:EE:FF", connect=lambda mac: p)
    assert abs(got["epoch"] - REAL_EPOCH) < 0.001, got
    assert got["offset_min"] == 60 and p.closed


def test_the_phones_own_questions_are_refused_not_mistaken_for_answers():
    p = _phone()
    phonetime.read("AA:BB:CC:DD:EE:FF", connect=lambda mac: p)
    assert bytes.fromhex("0110000006") in p.sent, [s.hex() for s in p.sent]


def test_daylight_saving_and_negative_zones():
    assert phonetime.parse_local_time_info(bytes([4, 4])) == 120                 # Italy in summer
    assert phonetime.parse_local_time_info(bytes([0xEC, 0])) == -300             # UTC-5
    local = phonetime.parse_current_time(bytes.fromhex("ea0707010030000300"))     # 2026-07-01 00:48:00 local
    assert phonetime.to_epoch(local, 120) == calendar.timegm((2026, 6, 30, 22, 48, 0))


def test_unknown_zone_or_nonsense_date_is_an_error_not_a_wrong_clock():
    for bad in (lambda: phonetime.parse_local_time_info(bytes([0x80, 0])),
                lambda: phonetime.parse_local_time_info(bytes([4, 255])),
                lambda: phonetime.parse_current_time(bytes.fromhex("d00701010000000100")),   # year 2000
                lambda: phonetime.parse_current_time(b"\x00\x00")):
        try:
            bad()
        except ValueError:
            continue
        raise AssertionError("accepted")


def test_error_from_the_phone_and_silence():
    p = FakePhone({phonetime.UUID_LOCAL_TIME_INFO: bytes.fromhex("010801000a")})
    try:
        phonetime.read("AA:BB:CC:DD:EE:FF", connect=lambda mac: p)
    except RuntimeError as e:
        assert "not found" in str(e) and p.closed
    else:
        raise AssertionError("no error")


def _sync(pi_now, ntp, read_fn=None):
    logs, sets = [], []
    flag = os.path.join(tempfile.mkdtemp(), "synchronized")
    if ntp:
        open(flag, "w").close()
    done = phonetime.sync("AA:BB:CC:DD:EE:FF", lambda ev, **kw: logs.append((ev, kw)), now=lambda: pi_now, ntp_flag=flag,
                          read_fn=read_fn or (lambda mac: {"epoch": REAL_EPOCH, "offset_min": 60, "took_s": 0.4}),
                          set_clock=lambda e: sets.append(e) or True)
    return done, logs, sets


def test_clock_hours_behind_and_no_internet_is_set():
    done, logs, sets = _sync(REAL_EPOCH - 6 * 3600, ntp=False)
    assert done == "set" and sets == [REAL_EPOCH] and logs[0][1]["set"] is True and logs[0][1]["step_s"] == 21600.0


def test_clock_already_set_by_the_internet_is_left_alone():
    done, logs, sets = _sync(REAL_EPOCH - 6 * 3600, ntp=True)
    assert done == "kept" and sets == [] and logs[0][1]["set"] is False and logs[0][1]["internet_time"] is True


def test_clock_already_right_is_left_alone():
    done, logs, sets = _sync(REAL_EPOCH - 0.5, ntp=False)
    assert done == "kept" and sets == []


def test_a_phone_that_does_not_answer_changes_nothing():
    def boom(mac):
        raise OSError("Host is down")
    done, logs, sets = _sync(REAL_EPOCH - 6 * 3600, ntp=False, read_fn=boom)
    assert done is None and sets == [] and logs[0][0] == "phone_time_error"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
