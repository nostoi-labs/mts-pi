"""CarPlay Siri from the bike's phone button (HFP_VR=1): AT+BVRA=1 → 'siri' command to the Carlinkit, voice channel
opened without a call, helmet microphone towards CarPlay, closed on siri_stop or if Siri does not start.
python tests/test_siri.py"""
import asyncio
import os
import sys
import types

os.environ["HFP_VR"] = "1"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from mtspi import carlinkit as ck  # noqa: E402
from mtspi import hfp_ag  # noqa: E402
from test_carplay_call import audio_cmd, bike_events, make_bridge  # noqa: E402


def bridge():
    b = make_bridge()
    b.siri_until = 0.0
    return b


def test_bridge_siri_from_bike():
    async def main():
        b = bridge()
        await b.bike_siri(True)                          # long press of the phone button
        await asyncio.sleep(0.01)
        assert b.dongle.sent == [ck.send_command("siri")]
        assert bike_events(b) == ["siri_start"] and b.siri_until
        await b.bike_siri(True)                          # asked again while it is listening: nothing more is sent
        await asyncio.sleep(b.SIRI_PRESS_S + 0.1)        # a short press: the release follows (alone, Siri never answers)
        assert b.dongle.sent == [ck.send_command("siri"), ck.send_command("siriRelease")]
        await audio_cmd(b, "siri_start")                 # the Carlinkit confirms: Siri is listening
        assert b.siri_until - asyncio.get_running_loop().time() < b.SIRI_MAX_S + 1
        await audio_cmd(b, "siri_stop")
        assert bike_events(b)[-1] == "siri_stop" and not b.siri_until
    asyncio.run(main())


def test_bridge_siri_never_starts():
    async def main():
        b = bridge()
        b.SIRI_START_S = 0.2
        await b.bike_siri(True)
        await asyncio.sleep(1.0)                         # no siri_start from the Carlinkit: it closes by itself
        assert bike_events(b) == ["siri_start", "siri_stop"] and not b.siri_until
        assert ("siri_timeout", {}) in b.logs
    asyncio.run(main())


def test_bridge_siri_refused():
    async def main():
        b = bridge()
        b.dongle.state = "closed"                        # CarPlay off: no command, the bike closes
        await b.bike_siri(True)
        await asyncio.sleep(0.01)
        assert b.dongle.sent == [] and bike_events(b) == ["siri_stop"]
        b = bridge()
        b.call = {"state": "active"}                     # in a call: Siri does not start
        await b.bike_siri(True)
        await asyncio.sleep(0.01)
        assert b.dongle.sent == [] and bike_events(b) == ["siri_stop"]
    asyncio.run(main())


class FakeBridgeLink:
    def __init__(self):
        self.sent = []

    async def send_json(self, m):
        self.sent.append(m)


def make_link():
    logs = []
    ag = types.SimpleNamespace(log=lambda e, **kw: logs.append((e, kw)), test_sco=False, sco_enabled=False, links={})

    async def safe(coro):
        return bool(await coro)
    ag.safe = safe
    st = hfp_ag.BridgeState(ag)
    st.link = FakeBridgeLink()
    ag.state = st
    link = hfp_ag.Link(ag, "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF", None)
    link.out = []
    link.send = link.out.append
    link.log = lambda e, **kw: logs.append((e, kw))
    return link, st, logs


def test_hfp_bvra():
    async def main():
        assert hfp_ag.AG_FEATURES & 4                    # the bike sees the voice assistant in +BRSF
        link, st, _ = make_link()
        await link.command("AT+BVRA=1")
        assert link.out == ["OK"] and st.vr and link.vr_sent
        assert st.link.sent == [{"type": "hfp", "cmd": "siri", "on": True}]
        st.on_json({"type": "hfp", "event": "siri_stop"})          # Siri has finished
        assert not st.vr
        await link.command("AT+BVRA=0")
        assert link.out[-1] == "OK" and not st.vr
        st.link = None                                   # bridge disconnected: the bike gets ERROR
        await link.command("AT+BVRA=1")
        assert link.out[-1] == "ERROR" and not st.vr
    asyncio.run(main())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
