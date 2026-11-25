"""A CarPlay session that cannot be used is closed and opened again (Bridge.carplay_fresh_session).

Seen on the bench on 24-25 Nov 2026, twice out of twice: the program restarted while CarPlay was connected, the
Carlinkit went on sending the pictures of the session it already had, none of them could be read, and when it had
answered the new Open no picture came at all. CarPlay looked "on", the dashboard stayed on the home screen, and
nothing recovered by itself. Closing the session and opening it again five seconds later brought it back each time.

    python tests/test_fresh_session.py
"""
import asyncio
import struct
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import av  # noqa: E402
from mtspi import bridge, carlinkit as ck  # noqa: E402


def make(state="opening", display="launcher", wanted=True):
    log, sent = [], []

    async def send(raw):
        sent.append(("send", raw))

    async def stop():
        sent.append(("stop",))
        me.dongle.state = "closed"

    async def start():
        sent.append(("start",))

    me = types.SimpleNamespace(
        dongle=types.SimpleNamespace(state=state, send=send, stop=stop, start=start, fps=19),
        counts={"cp_video": 0, "cp_audio": 0}, decoder=None, _dec_pool=None, session=None,
        display=types.SimpleNamespace(mode=display, carplay_frame=None, carplay_seq=0),
        carplay_wanted=wanted, carplay_user_off=False, carplay_since=None, dongle_info={},
        _fresh_busy=False, _fresh_at=-1e9, _dongle_opened=None, _bad_frames=0, _burst_until=0, _aclk=0.0,
        log=lambda event, **kw: log.append((event, kw)), tasks=[])
    me.spawn = lambda coro: me.tasks.append(asyncio.ensure_future(coro)) or me.tasks[-1]
    me.carplay_fresh_session = lambda why: bridge.Bridge.carplay_fresh_session(me, why)
    me.fresh_session = lambda why: bridge.Bridge.fresh_session(me, why)
    me._decode = lambda decoder, payload: (_ for _ in ()).throw(av.error.InvalidDataError(1094995529, "Invalid data"))
    return me, log, sent


VIDEO = struct.pack("<5I", 848, 304, 3, 1000, 0) + b"\x00\x00\x00\x01\x41" + bytes(40)


def run(coro):
    real = asyncio.sleep

    async def fast(s, *a):
        await real(0)
    saved, bridge.asyncio.sleep = bridge.asyncio.sleep, fast
    try:
        return asyncio.run(coro)
    finally:
        bridge.asyncio.sleep = saved


def events(log, name):
    return [kw for e, kw in log if e == name]


def test_a_session_found_alive_at_the_start_is_closed_and_opened_again():
    me, log, sent = make(state="opening")

    async def go():
        for _ in range(5):                                   # pictures of the session that was already there
            await bridge.Bridge._from_dongle(me, ck.VIDEO_DATA, VIDEO)
        assert me._fresh_busy and me.dongle.state == "opening"
        assert not events(log, "carplay_decode_error"), "the pictures of the old session must not be read"
        assert not any(x[0] == "stop" for x in sent), "the closing waits for the Carlinkit's answer to the Open"
        await bridge.Bridge._from_dongle(me, ck.OPEN, b"")   # the Carlinkit answers the Open sent at the start
        for _ in range(40):
            await asyncio.sleep(0)
        await asyncio.gather(*me.tasks)
    run(go())
    assert [x[0] for x in sent if x[0] in ("stop", "start")] == ["stop", "start"], sent
    assert me.dongle.state == "opening" and not me._fresh_busy
    assert me.carplay_wanted, "the dashboard must go to CarPlay when the new session shows its first picture"
    assert me.counts["cp_video"] == 5
    steps = [kw.get("step") for kw in events(log, "carplay_fresh_session")]
    assert steps == ["closing", "session opened again"], steps


def test_many_unreadable_pictures_in_a_row_open_the_session_again():
    me, log, sent = make(state="connected", display="carplay", wanted=False)

    async def go():
        for _ in range(bridge.UNREADABLE_FRAMES - 1):
            await bridge.Bridge._from_dongle(me, ck.VIDEO_DATA, VIDEO)
        assert not me.tasks, "one picture short of the threshold: nothing is done"
        await bridge.Bridge._from_dongle(me, ck.VIDEO_DATA, VIDEO)
        await asyncio.gather(*me.tasks)
    run(go())
    assert [x[0] for x in sent if x[0] in ("stop", "start")] == ["stop", "start"], sent
    assert me.carplay_wanted, "CarPlay was on the dashboard: it must come back there by itself"
    assert len(events(log, "carplay_decode_error")) == bridge.UNREADABLE_FRAMES


def test_a_good_picture_starts_the_count_again():
    me, log, sent = make(state="connected", display="carplay", wanted=False)
    good = types.SimpleNamespace()

    async def go():
        for i in range(bridge.UNREADABLE_FRAMES * 3):
            if i % 10 == 9:
                me._decode = lambda decoder, payload: [good]
            else:
                me._decode = lambda decoder, payload: (_ for _ in ()).throw(av.error.InvalidDataError(1, "x"))
            await bridge.Bridge._from_dongle(me, ck.VIDEO_DATA, VIDEO)
    run(go())
    assert not me.tasks and not sent, "broken pictures now and then are only dropped"
    assert me.display.carplay_frame is good


def test_not_twice_in_a_minute_and_not_against_the_rider():
    me, log, sent = make(state="opening")

    async def go():
        await bridge.Bridge._from_dongle(me, ck.VIDEO_DATA, VIDEO)
        await bridge.Bridge._from_dongle(me, ck.OPEN, b"")
        await asyncio.gather(*me.tasks)
        n = len(sent)
        await bridge.Bridge._from_dongle(me, ck.VIDEO_DATA, VIDEO)       # the same again, seconds later
        await asyncio.gather(*me.tasks)
        assert me.dongle.state == "connected", "the second time the session is taken as it is"
        assert [x[0] for x in sent[n:]] == ["send"], sent[n:]              # only the request for a full picture
    run(go())
    assert [kw.get("step") for kw in events(log, "carlinkit_session_resumed")] == ["closed, a new one is opened", "kept"]

    me, log, sent = make(state="connected", display="carplay", wanted=False)

    async def off():
        assert bridge.Bridge.fresh_session(me, "unreadable")
        task = me.tasks[-1]
        await asyncio.sleep(0)
        me.carplay_user_off = True                                        # CarPlay switched off from the menu meanwhile
        await task
    run(off())
    assert [x[0] for x in sent] == ["stop"], "closed, and left closed: the rider asked for it"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
