"""Which paired phones the Pi calls back by itself (mtspi/bluez.py). A blocked phone is not one of them: on 21 Nov 2026
a phone blocked on purpose was called back every 30 seconds.
python -m pytest tests, or python tests/test_reconnect.py"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from mtspi import bluez  # noqa: E402

RADIO = "/org/bluez/hci0"
PHONE_A = RADIO + "/dev_AA_BB_CC_00_00_01"
PHONE_B = RADIO + "/dev_AA_BB_CC_00_00_02"


def objects(blocked_a):
    def phone(addr, blocked):
        return {"org.bluez.Device1": {"Address": addr, "Alias": "phone", "Adapter": RADIO, "Paired": True, "Connected": False,
                                      "Blocked": blocked, "UUIDs": [bluez.UUID_HFP_AG]}}
    return {RADIO: {"org.bluez.Adapter1": {"Address": "AA:BB:CC:00:00:00",
                                           "UUIDs": [bluez.UUID_A2DP_SOURCE, bluez.UUID_A2DP_SINK]}},
            PHONE_A: phone("AA:BB:CC:00:00:01", blocked_a), PHONE_B: phone("AA:BB:CC:00:00:02", False)}


def called_back(blocked_a):
    calls, logs = [], []

    class Bus:
        def disconnect(self):
            pass

    async def system_bus():
        return Bus()

    async def call(bus, dest, path, iface, member, sig="", body=None):
        if member == "GetManagedObjects":
            return [objects(blocked_a)]
        calls.append((path, member))
        return []
    real_sleep = asyncio.sleep

    async def fast_sleep(s):
        await real_sleep(0.001)
    saved = bluez.system_bus, bluez.call, bluez.asyncio.sleep
    bluez.system_bus, bluez.call, bluez.asyncio.sleep = system_bus, call, fast_sleep
    try:
        async def main():
            task = asyncio.ensure_future(bluez.keep_connected(lambda e, **kw: logs.append(e)))
            await real_sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await bluez.connect_paired_phones(lambda e, **kw: logs.append(e))
        asyncio.run(main())
    finally:
        bluez.system_bus, bluez.call, bluez.asyncio.sleep = saved
    return {p for p, m in calls if m == "ConnectProfile"}


def test_paired_phones_are_called_back():
    assert called_back(blocked_a=False) == {PHONE_A, PHONE_B}


def test_a_blocked_phone_is_left_alone():
    assert called_back(blocked_a=True) == {PHONE_B}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
