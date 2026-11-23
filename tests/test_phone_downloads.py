"""Phonebook and messages from the iPhone (mtspi/bridge.py): they wait while a voice link to the bike is open, start
again only when it has been closed for a while, stop when the phone has gone or has connected again, and say so in the
log when they give up. With Bluetooth on the Pi's small serial port a download on top of the voice breaks the line.
python tests/test_phone_downloads.py"""
import asyncio
import os
import sys
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from mtspi import bridge as br  # noqa: E402
from mtspi import mns  # noqa: E402

PHONE = "AA:BB:CC:00:11:22"


def make_bridge():
    b = br.Bridge.__new__(br.Bridge)
    b.logs = []
    b.log = lambda event, **kw: b.logs.append((event, kw))
    b.call, b.siri_until, b.messages, b.contacts, b.recent = None, 0.0, [], [], []
    b.bt = types.SimpleNamespace(iphone={"mac": PHONE})
    b.phone_turn = 1
    b.audio_src = {"carplay": 0.0}
    b.a = types.SimpleNamespace(phonebook_cache=None)
    b.phonebook = types.SimpleNamespace(folders={}, update=lambda books: None)
    b._sms_event = asyncio.Event()
    b.VOICE_QUIET_S = 0.6
    return b


def events(b, name):
    return [kw for e, kw in b.logs if e == name]


class swapped:
    """An attribute replaced for the length of a with block."""

    def __init__(self, obj, name, value):
        self.obj, self.name, self.value = obj, name, value

    def __enter__(self):
        self.old = getattr(self.obj, self.name)
        setattr(self.obj, self.name, self.value)

    def __exit__(self, *exc):
        setattr(self.obj, self.name, self.old)


def test_the_same_connection_or_not():
    b = make_bridge()
    assert b.phone_still(PHONE, 1) and b.phone_still(PHONE)
    assert not b.phone_still("11:22:33:44:55:66", 1)
    b.phone_turn = 2                                    # the same phone left and came back: a new pair of downloads
    assert not b.phone_still(PHONE, 1) and b.phone_still(PHONE, 2)
    b.bt.iphone = None
    assert not b.phone_still(PHONE, 2)


def test_messages_are_not_read_while_a_voice_link_is_open():
    async def run():
        b = make_bridge()
        pulls = []

        async def pull(mac, log, source=None):
            pulls.append(time.monotonic())
            return []
        with swapped(mns, "pull_messages", pull):
            b.siri_until = time.monotonic() + 60        # Siri is listening
            task = asyncio.ensure_future(b.pull_sms(PHONE, None, 1))
            await asyncio.sleep(1.3)
            assert pulls == []
            b.siri_until = 0.0
            closed = time.monotonic()
            await asyncio.sleep(2.6)
            assert len(pulls) == 1 and pulls[0] >= closed
            assert events(b, "map_waited_for_voice")
            b.phone_turn = 2                            # the phone connects again: this download must end by itself
            b._sms_event.set()
            await asyncio.wait_for(task, 3)
    asyncio.run(run())


def test_messages_stop_waiting_for_the_voice_when_the_phone_has_gone():
    async def run():
        b = make_bridge()
        b.call = {"state": "active"}                    # a call that goes on and on

        async def pull(mac, log, source=None):
            raise AssertionError("nothing may be read during the call")
        with swapped(mns, "pull_messages", pull):
            task = asyncio.ensure_future(b.pull_sms(PHONE, None, 1))
            await asyncio.sleep(0.2)
            b.bt.iphone = None
            await asyncio.wait_for(task, 4)             # before, it sat in the wait for up to 15 minutes
    asyncio.run(run())


def test_a_dropped_phonebook_starts_again_only_after_the_voice_has_been_quiet():
    async def run():
        b = make_bridge()
        calls, quiet_from = [], []

        async def pull(mac, log, source=None, abort=None):
            calls.append(time.monotonic())
            if len(calls) == 1:
                b.siri_until = time.monotonic() + 60    # Siri starts while the phonebook is coming in
                raise br.bluez.PullAborted("pb")
            return {"pb": "BEGIN:VCARD", "cch": ""}

        async def siri():                               # closes, opens again 0.3 s later (as Siri does), then closes
            await asyncio.sleep(0.5)
            b.siri_until = 0.0
            await asyncio.sleep(0.3)
            b.siri_until = time.monotonic() + 60
            await asyncio.sleep(0.5)
            b.siri_until = 0.0
            quiet_from.append(time.monotonic())
        with swapped(br.bluez, "pull_phonebook", pull), swapped(br.bluez, "parse_vcards", lambda text, limit=None: [1] if text else []):
            asyncio.ensure_future(siri())
            await asyncio.wait_for(b.pull_contacts(PHONE, None, 1), 10)
        assert len(calls) == 2 and b.contacts == [1]
        assert calls[1] - quiet_from[0] >= b.VOICE_QUIET_S - 0.05      # not in the short gap, not right at the close
        assert events(b, "pbap_aborted_for_voice") == [{"folder": "pb", "times": 1}]
        assert not events(b, "pbap_gave_up")
    asyncio.run(run())


def test_a_phonebook_that_never_comes_ends_with_a_line_in_the_log():
    async def run():
        b = make_bridge()
        calls = []

        async def pull(mac, log, source=None, abort=None):
            calls.append(1)
            return {"pb": "", "cch": ""}                # "Sync Contacts" off on the iPhone: always empty
        real_sleep = asyncio.sleep
        with swapped(br.bluez, "pull_phonebook", pull), swapped(br.bluez, "parse_vcards", lambda text, limit=None: []), \
                swapped(br.asyncio, "sleep", lambda s: real_sleep(0)):
            await asyncio.wait_for(b.pull_contacts(PHONE, None, 1), 10)
        assert len(calls) == 8 and events(b, "pbap_gave_up") == [{"aborted_for_voice": 0}]
    asyncio.run(run())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
