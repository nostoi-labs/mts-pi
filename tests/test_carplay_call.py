"""CarPlay call from the Carlinkit audio commands (mtspi/bridge.py): moving the audio during an active call
(phonecall_stop and ~1 s later phonecall_start, 29 Oct 2026) must not turn into end + new call towards the bike.
python tests/test_carplay_call.py"""
import asyncio
import os
import struct
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from mtspi import carlinkit as ck  # noqa: E402
from mtspi import linkproto as lp  # noqa: E402
from mtspi.bridge import Bridge  # noqa: E402

GAP = 0.3     # window shortened for the test (2.5 s on the Pi)


class FakeLink:
    def __init__(self):
        self.sent = []

    async def send_json(self, msg):
        self.sent.append(msg)

    async def send(self, *msg):            # bike: (type, data); Carlinkit: whole message
        self.sent.append(msg if len(msg) > 1 else msg[0])


def make_bridge():
    b = Bridge.__new__(Bridge)
    b.logs = []
    b.log = lambda event, **kw: b.logs.append((event, kw))
    b.a = types.SimpleNamespace(bike_audio="none", bt_name="MTS Pi")
    b.call, b.cind, b.session, b.bt, b.contacts = None, {}, None, None, []
    b.media, b.messages, b.fast_bt_poll = None, [], False
    b.counts = {"bike_mic": 0}
    b.siri_until = 0.0
    b.launcher = types.SimpleNamespace(bump=lambda: None)
    b.dongle = FakeLink()
    b.dongle.state = "connected"
    b.bike = FakeLink()
    b.CALL_GAP_S = GAP
    return b


async def audio_cmd(b, cmd):
    await b.from_dongle(ck.AUDIO_DATA, struct.pack("<IfI", 5, 0.0, 3) + struct.pack("<b", ck.AUDIO_CMD_CODE[cmd]))
    await asyncio.sleep(0.01)                  # messages to the bike are sent from a task


def bike_events(b):
    return [m["event"] for m in b.bike.sent if isinstance(m, dict) and m.get("type") == "hfp"]


def test_audio_moved_same_call():
    async def main():
        b = make_bridge()
        await audio_cmd(b, "phonecall_start")             # call placed from CarPlay
        assert bike_events(b) == ["outgoing", "call_active"]
        await audio_cmd(b, "phonecall_stop")              # the iPhone moves the audio...
        await audio_cmd(b, "output_stop")
        await asyncio.sleep(GAP / 3)
        assert bike_events(b) == ["outgoing", "call_active"] and b.call["state"] == "active"
        for cmd in ("input_config", "phonecall_start", "output_start"):   # ...and puts it back: same call
            await audio_cmd(b, cmd)
        await asyncio.sleep(GAP * 1.5)                    # the window expires without closing anything
        assert bike_events(b) == ["outgoing", "call_active"]
        assert b.call and b.call["state"] == "active" and b.call["audio"] == "carplay" and "ending" not in b.call
        assert any(e == "carplay_call_resumed" for e, _ in b.logs)
        await audio_cmd(b, "phonecall_stop")              # real end: call_ended after the window
        assert bike_events(b)[-1] == "call_active" and b.call
        await asyncio.sleep(GAP * 1.5)
        assert bike_events(b) == ["outgoing", "call_active", "call_ended"] and b.call is None
        assert [kw["why"] for e, kw in b.logs if e == "carplay_call_end"] == ["timeout"]
    asyncio.run(main())


def test_media_start_ends_now():
    async def main():
        b = make_bridge()
        await audio_cmd(b, "phonecall_start")
        await audio_cmd(b, "phonecall_stop")
        await audio_cmd(b, "media_start")                 # music restarts: real end, no waiting
        assert bike_events(b) == ["outgoing", "call_active", "call_ended"] and b.call is None
        await asyncio.sleep(GAP * 1.5)                    # the expired timer does not close a second time
        assert bike_events(b).count("call_ended") == 1
    asyncio.run(main())


def test_rejected_ring_ends_now():
    async def main():
        b = make_bridge()
        await audio_cmd(b, "incoming_call")               # ring (without iPhone Bluetooth: immediate, no number)
        await asyncio.sleep(0.05)
        assert bike_events(b) == ["ring"] and b.call["state"] == "incoming"
        await audio_cmd(b, "phonecall_stop")              # rejected/missed: no window
        assert bike_events(b) == ["ring", "call_ended"] and b.call is None
    asyncio.run(main())


def test_hangup_from_bike_during_gap():
    """AT+CHUP from the helmet while waiting for phonecall_start: ends immediately (and ends on CarPlay too)."""
    async def main():
        conns = []
        srv = await asyncio.start_server(lambda r, w: conns.append(lp.Link(r, w, "bike")), "127.0.0.1", 0)
        b = make_bridge()
        b.bike = None
        b.a.bike_audio = "127.0.0.1:%d" % srv.sockets[0].getsockname()[1]
        task = asyncio.ensure_future(b.bike_audio())
        while not conns or b.bike is None:
            await asyncio.sleep(0.01)
        bike = conns[0]
        got = []

        async def read():
            while True:
                kind, data = await bike.recv()
                if kind is None:
                    return
                if kind == lp.JSON and data.get("type") == "hfp":
                    got.append(data["event"])
        reader = asyncio.ensure_future(read())
        await audio_cmd(b, "phonecall_start")
        await audio_cmd(b, "phonecall_stop")
        await bike.send(lp.MIC, b"\0" * 320)              # microphone idle during the window
        await bike.send_json({"type": "hfp", "cmd": "hangup"})
        await asyncio.sleep(0.1)
        assert got == ["indicators", "outgoing", "call_active", "call_ended"] and b.call is None
        assert b.dongle.sent == [ck.send_command("rejectPhone")]
        await asyncio.sleep(GAP * 1.5)
        assert got.count("call_ended") == 1
        task.cancel()
        reader.cancel()
        srv.close()
    asyncio.run(main())


def phone(call):
    """The iPhone as its own Bluetooth link reports it: connected, answering about its calls, in a call or not."""
    return types.SimpleNamespace(iphone={"mac": "AA:BB:CC:00:11:22"}, modem="/hfp/org/bluez/hci0/dev_AA", call=call,
                                 calls_known=True)


def test_carplay_dropping_for_a_moment_does_not_end_the_call():
    """CarPlay goes and comes back during a call (the wireless link hiccups): the call goes on. Ended at the drop, it
    could not be picked up again when CarPlay returned: no sound in the helmet for the rest of the call."""
    async def run():
        b = make_bridge()
        b.spawn = lambda coro: asyncio.ensure_future(coro)
        b.bt = phone({"state": "active", "number": "1"})
        b.ORPHAN_QUIET_S, b.ORPHAN_MAX_S = 0.5, 1.0
        await audio_cmd(b, "phonecall_start")
        b.carplay_gone("unplugged")
        assert b.call and b.call.get("orphan") and b.voice_open()
        await asyncio.sleep(1.2)                            # longer than both limits: the phone says it is still on
        assert b.call and "call_ended" not in bike_events(b)
        await audio_cmd(b, "phonecall_start")               # CarPlay is back, with the call
        assert b.call["state"] == "active" and "orphan" not in b.call and "call_ended" not in bike_events(b)
        assert [e for e, _ in b.logs if e == "carplay_call_back_on_carplay"]
        await asyncio.sleep(2.2)                            # and nobody ends it afterwards
        assert b.call and "call_ended" not in bike_events(b)
    asyncio.run(run())


def test_carplay_gone_for_good_the_call_ends_when_the_phone_says_so():
    """Before 23 Nov 2026 nothing ended it: the bike stayed "in a call" until the key was turned."""
    async def run():
        b = make_bridge()
        b.spawn = lambda coro: asyncio.ensure_future(coro)
        b.bt = phone({"state": "active", "number": "1"})
        b.ORPHAN_QUIET_S, b.ORPHAN_MAX_S = 1.5, 30
        await audio_cmd(b, "phonecall_start")
        b.carplay_gone("carlinkit_lost")
        b.carplay_gone("unplugged")                         # told twice: one watch, not two
        await asyncio.sleep(1.2)
        b.bt.call = None                                    # the phone reports no call for a moment (it does, when the audio moves)
        await asyncio.sleep(1.1)
        b.bt.call = {"state": "active", "number": "1"}
        await asyncio.sleep(1.1)
        assert b.call and "call_ended" not in bike_events(b)
        b.bt.call = None                                    # now the call is really over
        await asyncio.sleep(3.2)
        assert b.call is None and bike_events(b).count("call_ended") == 1
        assert [kw["why"] for e, kw in b.logs if e == "carplay_call_end"] == ["CarPlay gone and the phone reports no call"]
    asyncio.run(run())


def test_call_audio_stopping_together_with_carplay_does_not_end_the_call_either():
    """The Carlinkit says "call audio stopped" as CarPlay goes, before or after saying that CarPlay has gone: the 2.5 s
    rule for a call that ended must not apply, the phone's word does."""
    async def run():
        for order in ("stop then gone", "gone then stop"):
            b = make_bridge()
            b.spawn = lambda coro: asyncio.ensure_future(coro)
            b.bt = phone({"state": "active", "number": "1"})
            b.ORPHAN_QUIET_S, b.ORPHAN_MAX_S = 5, 30
            await audio_cmd(b, "phonecall_start")
            if order == "stop then gone":
                await audio_cmd(b, "phonecall_stop")
                b.carplay_gone("unplugged")
            else:
                b.carplay_gone("unplugged")
                await audio_cmd(b, "phonecall_stop")
            await asyncio.sleep(GAP + 0.4)
            assert b.call and "call_ended" not in bike_events(b), order
            await audio_cmd(b, "phonecall_start")           # CarPlay back: the same call, with its sound
            assert b.call["state"] == "active" and "orphan" not in b.call and "ending" not in b.call, order
            await audio_cmd(b, "phonecall_stop")            # and when it really ends, with CarPlay there, the usual rule applies
            await asyncio.sleep(GAP + 0.3)
            assert b.call is None and bike_events(b).count("call_ended") == 1, order
    asyncio.run(run())


def test_a_phone_that_does_not_answer_about_its_calls_is_not_taken_to_say_no_call():
    async def run():
        b = make_bridge()
        b.spawn = lambda coro: asyncio.ensure_future(coro)
        b.bt = phone(None)
        b.bt.calls_known = False                            # the look at the phone's calls failed (oFono busy, an error)
        b.ORPHAN_QUIET_S, b.ORPHAN_MAX_S = 0.5, 30
        await audio_cmd(b, "phonecall_start")
        b.carplay_gone("unplugged")
        await asyncio.sleep(2.3)
        assert b.call and "call_ended" not in bike_events(b)
    asyncio.run(run())


def test_a_new_call_ringing_while_an_old_one_is_still_shown_without_carplay():
    async def run():
        b = make_bridge()                                   # no Bluetooth link to the phone: the old call is kept 45 s
        b.spawn = lambda coro: asyncio.ensure_future(coro)
        b.ORPHAN_QUIET_S, b.ORPHAN_MAX_S = 5, 30
        await audio_cmd(b, "phonecall_start")
        b.carplay_gone("unplugged")
        await audio_cmd(b, "incoming_call")                 # CarPlay is back, and it is another call that rings
        await asyncio.sleep(0.05)
        events = bike_events(b)
        assert events.count("call_ended") == 1 and events[-1] == "ring", events
        assert b.call and b.call["state"] == "incoming" and "orphan" not in b.call
    asyncio.run(run())


def test_carplay_gone_and_no_phone_to_ask_the_call_is_kept_a_while_then_ended():
    async def run():
        b = make_bridge()                                   # b.bt is None: no Bluetooth link to the phone
        b.spawn = lambda coro: asyncio.ensure_future(coro)
        b.ORPHAN_QUIET_S, b.ORPHAN_MAX_S = 0.5, 1.5
        await audio_cmd(b, "phonecall_start")
        b.carplay_gone("unplugged")
        await asyncio.sleep(1.0)
        assert b.call and "call_ended" not in bike_events(b)
        await asyncio.sleep(1.8)
        assert b.call is None and bike_events(b)[-1] == "call_ended"
    asyncio.run(run())


def test_carplay_gone_closes_siri_at_once_and_leaves_a_bluetooth_call_alone():
    async def run():
        b = make_bridge()
        b.spawn = lambda coro: asyncio.ensure_future(coro)
        b.siri_set(True, 60)
        assert b.voice_open()
        b.carplay_gone("carlinkit_lost")
        await asyncio.sleep(0.01)
        assert not b.siri_until and bike_events(b)[-1] == "siri_stop"
        b.call = {"state": "active", "audio": "bt"}         # a call the iPhone reported over Bluetooth
        b.carplay_gone("unplugged")
        await asyncio.sleep(0.01)
        assert b.call == {"state": "active", "audio": "bt"} and "call_ended" not in bike_events(b)
    asyncio.run(run())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
