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


def waiting(fn):
    """Runs a test with SIRI_WAIT_MIC=1 (off by default: on the bike Siri was started at once, and it worked)."""
    def run():
        from mtspi import bridge as br
        was, br.SIRI_WAIT_MIC = br.SIRI_WAIT_MIC, True
        try:
            fn()
        finally:
            br.SIRI_WAIT_MIC = was
    run.__name__ = fn.__name__
    return run


def test_bridge_siri_starts_at_once_as_it_did_on_the_bike():
    async def main():
        b = bridge()
        b.a.bike_audio = "127.0.0.1:9160"              # the real setup: the bike's audio goes through the hands-free service
        await b.bike_siri(True)
        await asyncio.sleep(0.02)
        assert b.dongle.sent[0] == ck.send_command("siri") and bike_events(b) == ["siri_start"]
        assert b.siri_mic_open is None
    asyncio.run(main())


@waiting
def test_bridge_siri_waits_for_the_helmet_microphone():
    """With SIRI_WAIT_MIC=1 the voice channel opens first and Siri starts when the helmet microphone is there: on the
    bike that takes about 3.3 s (21 Nov 2026)."""
    async def main():
        b = bridge()
        b.a.bike_audio = "127.0.0.1:9160"
        await b.bike_siri(True)
        await asyncio.sleep(0.05)
        assert bike_events(b) == ["siri_start"]          # the bike is told at once
        assert b.dongle.sent == [] and b.siri_mic_open is not None   # CarPlay is not: nothing to listen to yet
        b.siri_mic_open.set()                            # first real sound from the helmet
        await asyncio.sleep(b.SIRI_PRESS_S + 0.1)
        assert b.dongle.sent == [ck.send_command("siri"), ck.send_command("siriRelease")]
        assert b.siri_mic_open is None and b.siri_until
        assert [kw["why"] for e, kw in b.logs if e == "siri_command"] == ["microphone open"]
    asyncio.run(main())


@waiting
def test_bridge_siri_starts_anyway_if_the_microphone_stays_silent():
    async def main():
        b = bridge()
        b.a.bike_audio = "127.0.0.1:9160"
        b.SIRI_MIC_WAIT_S = 0.15
        await b.bike_siri(True)
        await asyncio.sleep(0.3)
        assert b.dongle.sent[0] == ck.send_command("siri")
        assert [kw["why"] for e, kw in b.logs if e == "siri_command"] == ["no sound from the microphone"]
    asyncio.run(main())


@waiting
def test_bridge_siri_closed_while_waiting_sends_nothing_to_carplay():
    async def main():
        b = bridge()
        b.a.bike_audio = "127.0.0.1:9160"
        await b.bike_siri(True)
        await asyncio.sleep(0.02)
        waiting = b.siri_mic_open
        await b.bike_siri(False)                         # the rider lets go, or the bike closes
        waiting.set()
        await asyncio.sleep(0.05)
        assert b.dongle.sent == [] and bike_events(b) == ["siri_start", "siri_stop"]
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


def _siri_start(link, st, features, mode):
    was = hfp_ag.VR_CALL_MODE
    hfp_ag.VR_CALL_MODE = mode
    try:
        link.hf_features = features
        st.ag.links = {"/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF": link}
        st.on_json({"type": "hfp", "event": "siri_start"})
        return list(st.calls), st.ind["call"]
    finally:
        hfp_ag.VR_CALL_MODE = was


def test_hfp_siri_is_a_call_for_a_bike_without_voice_recognition():
    link, st, _ = make_link()
    calls, call = _siri_start(link, st, 693, "auto")     # the Multistrada V4 S: bit 3 off
    assert call == 1 and calls[0][0] == "siri" and calls[0][1]["state"] == "active"
    st.on_json({"type": "hfp", "event": "indicators", "cind": {"signal": 3}})   # levels arriving do not end it
    assert st.ind["call"] == 1
    st.on_json({"type": "hfp", "event": "siri_stop"})
    assert st.calls == [] and st.ind["call"] == 0


def test_hfp_siri_is_not_a_call_for_a_hands_free_with_voice_recognition():
    link, st, _ = make_link()
    calls, call = _siri_start(link, st, 693 | 8, "auto")
    assert calls == [] and call == 0 and st.vr


def test_hfp_siri_call_can_be_forced_or_switched_off():
    link, st, _ = make_link()
    assert _siri_start(link, st, 693 | 8, "1")[1] == 1
    link, st, _ = make_link()
    assert _siri_start(link, st, 693, "0")[1] == 0


def test_hfp_hang_up_from_the_bike_closes_siri():
    async def main():
        link, st, _ = make_link()
        _siri_start(link, st, 693, "auto")
        await link.command("AT+CHUP")
        assert st.link.sent[-1] == {"type": "hfp", "cmd": "siri", "on": False}
    asyncio.run(main())


def test_hfp_real_call_during_siri_is_not_named_siri():
    link, st, _ = make_link()
    _siri_start(link, st, 693, "auto")
    st.on_json({"type": "hfp", "event": "call_active", "name": "Mamma"})
    assert st.calls[0][0] == "bridge" and st.calls[0][1]["name"] == "Mamma"
    st.on_json({"type": "hfp", "event": "siri_stop"})    # Siri ends: the real call stays
    assert st.ind["call"] == 1


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
