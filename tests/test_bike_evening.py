"""What the first evening on the bike with the ready-made card taught us (16 Nov 2026):
- the joystick has no Back key: UP held (LONG_PRESS from the dashboard) is Back in CarPlay, other long presses are dropped;
- after a radio dropout the dashboard opens the control channel again while the old one never reports its end:
  the new connection must replace the old one;
- that old task had in fact been garbage collected: background tasks are now kept alive until they end.
python tests/test_bike_evening.py"""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from mtspi import bridge as br  # noqa: E402
from test_carplay_call import make_bridge  # noqa: E402


def test_up_held_is_back():
    sent, menu = [], []
    b = types.SimpleNamespace(carplay_key=sent.append, launcher=types.SimpleNamespace(on_key=menu.append))
    d = br.Display(b)
    d.mode = "carplay"
    for key, action in [("UP", "PRESS"), ("UP", "LONG_PRESS"), ("UP", "RELEASE"),          # held: up, then Back
                        ("DOWN", "PRESS"), ("DOWN", "LONG_PRESS"), ("DOWN", "RELEASE"),    # held: not repeated
                        ("UP", "PRESS"), ("UP", "RELEASE"),                                # short: just up
                        ("LEFT", "PRESS"), ("LEFT", "RELEASE"), ("OK", "PRESS"), ("OK", "RELEASE")]:
        d.on_key(key, action)
    assert sent == ["UP", "BACK", "DOWN", "UP", "LEFT", "OK"], sent
    assert menu == [] and d.key_count == 12


class FakeReader:
    """Control channel as the bridge reads it: silent until end() is called."""

    def __init__(self):
        self.ended = asyncio.Event()

    async def read(self, n):
        await self.ended.wait()
        return b""


class FakeWriter:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def control_bridge(kind):
    b = make_bridge()
    b.a.control, b.a.ap_credentials_file, b.a.wifi = kind, None, "none"
    return b


def patched(incoming, joined):
    async def control_channel(spec, log):
        return await incoming.get()

    async def join_wifi(mode, creds, log):
        joined.append(creds["n"])

    class Control:
        count = 0

        def __init__(self, r, w, log, device_name=None):
            pass

        async def negotiate(self):
            Control.count += 1
            return {"n": Control.count}

        async def notify_wlan(self, state):
            pass

    saved = br.hv.control_channel, br.hv.join_wifi, br.PhoneControl
    br.hv.control_channel, br.hv.join_wifi, br.PhoneControl = control_channel, join_wifi, Control
    return saved


def events(b):
    return [e for e, _ in b.logs if e.startswith("control_")]


def test_new_control_connection_replaces_a_silent_one():
    async def main():
        incoming, joined = asyncio.Queue(), []
        saved = patched(incoming, joined)
        try:
            b = control_bridge("rfcomm")
            task = asyncio.ensure_future(b.control_loop())
            first, second, third = (FakeReader(), FakeWriter()), (FakeReader(), FakeWriter()), (FakeReader(), FakeWriter())
            incoming.put_nowait(first)
            await asyncio.sleep(0.05)
            assert joined == [1] and events(b) == []
            incoming.put_nowait(second)                 # the dashboard comes back; the first channel never ends
            await asyncio.sleep(1.2)
            assert joined == [1, 2] and events(b) == ["control_replaced"] and first[1].closed
            second[0].ended.set()                       # an ordinary end is still noticed
            await asyncio.sleep(1.2)
            assert events(b) == ["control_replaced", "control_closed"] and second[1].closed
            incoming.put_nowait(third)                  # and the next connection is served
            await asyncio.sleep(0.05)
            assert joined == [1, 2, 3]
            task.cancel()
        finally:
            br.hv.control_channel, br.hv.join_wifi, br.PhoneControl = saved
    asyncio.run(main())


def test_other_control_transports_wait_for_the_end():
    async def main():
        incoming, joined = asyncio.Queue(), []
        saved = patched(incoming, joined)
        try:
            b = control_bridge("tcp:7000")
            task = asyncio.ensure_future(b.control_loop())
            first = (FakeReader(), FakeWriter())
            incoming.put_nowait(first)
            await asyncio.sleep(0.05)
            assert joined == [1] and incoming.empty() and events(b) == []
            first[0].ended.set()
            await asyncio.sleep(0.05)
            assert events(b) == ["control_closed"] and first[1].closed
            task.cancel()
        finally:
            br.hv.control_channel, br.hv.join_wifi, br.PhoneControl = saved
    asyncio.run(main())


def test_spawned_task_survives_garbage_collection():
    """The control task was collected while it waited on its stream ("Task was destroyed but it is pending!")."""
    import gc
    import weakref
    from mtspi.tasks import spawn

    async def main():
        async def wait_forever():
            await asyncio.get_running_loop().create_future()     # referenced only by the task itself
        ref = weakref.ref(spawn(wait_forever()))
        await asyncio.sleep(0)
        gc.collect()
        assert ref() is not None and not ref().done()
        ref().cancel()
        await asyncio.sleep(0)
        gc.collect()
        assert ref() is None or ref().done()
    asyncio.run(main())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
