"""The Pi getting back on its feet by itself (mtspi/recovery.py): the kernel lines recorded on the bike on 21 Nov 2026,
the order of the steps after a failed join, and the limits on the last one (restarting the Pi).
python -m pytest tests, or python tests/test_recovery.py"""
import json
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from mtspi import recovery  # noqa: E402

# as read from /dev/kmsg: "priority,sequence,microseconds,flags;text"
STUCK = "3,1503,325412059,-;Bluetooth: hci0: command tx timeout"
STUCK_OP = "3,1811,389209699,-;Bluetooth: hci0: command 0x041f tx timeout"
RESET = "3,2977,1596000000,-;Bluetooth: hci0: hardware error 0x00"
NOISE = ["3,1700,387168420,-;Bluetooth: hci0: killing stalled connection aa:bb:cc:dd:ee:ff",
         "3,1900,454656135,-;Bluetooth: hci0: ACL packet for unknown connection handle 11",
         "4,2100,660000000,-;ieee80211 phy0: brcmf_p2p_send_action_frame: Couldn't find peer's channel.",
         "6,90,12623051,-;Bluetooth: HCI UART driver ver 2.3"]


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def make(tmp, uptime=600.0, wall=1_795_270_000.0, run_code=0):
    logs, cmds = [], []

    def run(cmd):
        cmds.append(cmd)
        return types.SimpleNamespace(returncode=run_code, stdout="", stderr="" if run_code == 0 else "not allowed")
    now, w = Clock(), Clock(wall)
    rec = recovery.Recovery(lambda ev, **kw: logs.append((ev, kw)), state_dir=tmp, run=run, now=now, wall=w, uptime=lambda: uptime)
    return rec, logs, cmds, now, w


def test_radio_stuck_after_four_timeouts_in_half_a_minute():
    now = Clock()
    watch = recovery.RadioWatch(now=now)
    seen = []
    for line in (STUCK, STUCK, STUCK_OP):            # on the bike: one every 2 s
        seen.append(watch.feed(line))
        now.t += 2
    assert seen == [None, None, None]
    assert watch.feed(STUCK) == "stuck"
    assert watch.feed(STUCK) is None                 # the count starts again: one report, not one per line


def test_radio_timeouts_far_apart_are_not_a_stuck_radio():
    now = Clock()
    watch = recovery.RadioWatch(now=now)
    for _ in range(10):
        assert watch.feed(STUCK) is None
        now.t += 20


def test_radio_reset_and_other_lines():
    watch = recovery.RadioWatch(now=Clock())
    assert watch.feed(RESET) == "reset"
    assert [watch.feed(x) for x in NOISE] == [None] * len(NOISE)


def test_first_failed_join_just_waits_for_the_next_offer():
    with tempfile.TemporaryDirectory() as tmp:
        rec, logs, cmds, _, _ = make(tmp)
        assert rec.join_failed() is None and cmds == []


def test_steps_after_failed_joins_in_a_row():
    with tempfile.TemporaryDirectory() as tmp:
        rec, logs, cmds, _, _ = make(tmp)
        assert [rec.join_failed() for _ in range(4)] == [None, "restart_p2p", None, "reboot"]


def test_after_a_radio_reset_the_first_failure_restarts_wifi_direct():
    with tempfile.TemporaryDirectory() as tmp:
        rec, logs, cmds, now, _ = make(tmp)
        rec.radio_reset()
        now.t += 40                                  # 21 Nov: the join failed 40 s after the chip restarted
        assert rec.join_failed() == "restart_p2p"
        rec.join_ok()
        now.t += 600                                 # an old reset no longer counts
        assert rec.join_failed() is None


def test_a_good_join_ends_the_streak():
    with tempfile.TemporaryDirectory() as tmp:
        rec, logs, cmds, _, _ = make(tmp)
        rec.join_failed()
        rec.idle_action(25)
        rec.join_ok()
        assert rec.streak == 0 and rec.bounces == 0
        assert logs[-1] == ("mirror_recovered", {"after_failures": 1, "bluetooth_reconnections": 1})
        rec.join_ok()
        assert len(logs) == 1                        # nothing to say when nothing had failed


def test_silent_bike_is_reconnected_twice_then_the_pi_restarts():
    with tempfile.TemporaryDirectory() as tmp:
        rec, logs, cmds, _, _ = make(tmp)
        assert rec.idle_action(500) is None          # nothing failed: a bike that offers no Wi-Fi is left alone
        rec.join_failed()
        assert rec.idle_action(19) is None
        assert rec.idle_action(21) == "bounce"
        assert rec.idle_action(40) is None           # the bridge counts the silence again from each step
        assert rec.idle_action(61) == "bounce"
        assert rec.idle_action(89) is None
        assert rec.idle_action(91) == "reboot"


def test_nothing_is_done_during_a_call():
    with tempfile.TemporaryDirectory() as tmp:
        rec, logs, cmds, _, _ = make(tmp)
        rec.join_failed()
        assert rec.idle_action(300, in_call=True) is None and rec.bounces == 0


def test_restart_of_wifi_direct_runs_one_command():
    with tempfile.TemporaryDirectory() as tmp:
        rec, logs, cmds, _, _ = make(tmp)
        assert rec.restart_p2p() is True
        assert cmds == [["sudo", "-n", "/usr/bin/systemctl", "restart", "mtspi-p2p.service"]]
        assert logs[-1][0] == "mirror_p2p_restarted" and logs[-1][1]["code"] == 0


def test_reboot_is_recorded_and_not_repeated_for_fifteen_minutes():
    with tempfile.TemporaryDirectory() as tmp:
        rec, logs, cmds, _, wall = make(tmp)
        assert rec.reboot("the Bluetooth radio no longer answers") is True
        assert cmds == [["sudo", "-n", "/usr/bin/systemctl", "reboot"]]
        line = json.loads(open(os.path.join(tmp, "recovery.log")).read().splitlines()[-1])
        assert line["what"] == "reboot" and line["why"].startswith("the Bluetooth radio") and line["uptime_s"] == 600
        # after the restart: a new program, the same card
        rec2, logs2, cmds2, _, wall2 = make(tmp, wall=wall.t + 300)
        assert rec2.reboot("again") is False and cmds2 == []
        assert logs2[-1][0] == "recovery_reboot_skipped"
        rec2.reboot("again")
        assert len(logs2) == 1                       # said once, not at every try
        wall2.t = wall.t + 901
        assert rec2.reboot("again") is True


def test_no_reboot_right_after_the_start():
    with tempfile.TemporaryDirectory() as tmp:
        rec, logs, cmds, _, _ = make(tmp, uptime=45.0)
        assert rec.reboot("x") is False and cmds == []
        assert not os.path.exists(os.path.join(tmp, "recovery.log"))


def test_clock_that_went_back_does_not_block_the_reboot():
    with tempfile.TemporaryDirectory() as tmp:
        rec, logs, cmds, _, wall = make(tmp)
        rec.reboot("x")
        rec2, _, cmds2, _, _ = make(tmp, wall=wall.t - 5000)      # no clock battery: the time can restart from before
        assert rec2.reboot("y") is True


def test_record_keeps_the_last_fifty_lines():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "recovery.log")
        with open(path, "w") as f:
            for i in range(80):
                f.write(json.dumps({"t": i, "what": "reboot", "why": "old"}) + "\n")
        rec, logs, cmds, _, _ = make(tmp)
        assert rec.reboot("new") is True
        lines = open(path).read().splitlines()
        assert len(lines) == 50 and json.loads(lines[-1])["why"] == "new" and json.loads(lines[0])["t"] == 31


def test_refused_reboot_is_logged():
    with tempfile.TemporaryDirectory() as tmp:
        rec, logs, cmds, _, _ = make(tmp, run_code=1)
        assert rec.reboot("x") is False
        assert logs[-1][0] == "recovery_reboot_failed"


def _bridge_with(rec, **extra):
    """The bridge's own methods on a stand-in object: only what the control loop and the guard touch."""
    from mtspi import bridge
    logs = []
    acted = []

    async def recover(action, why):
        if action:
            acted.append(action)
    me = types.SimpleNamespace(
        a=types.SimpleNamespace(control="rfcomm", bt_name="MTS Pi", ap_credentials_file=None, wifi="nmcli"),
        log=lambda ev, **kw: logs.append((ev, kw)), recovery=rec, recover=recover, _control_idle_since=None,
        bt=types.SimpleNamespace(bike={"mac": "AA:BB:CC:DD:EE:FF", "path": "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"}),
        call=None, **extra)
    return bridge, me, logs, acted


def test_control_loop_counts_failed_joins_and_asks_for_the_next_step():
    import asyncio
    with tempfile.TemporaryDirectory() as tmp:
        rec, _, _, _, _ = make(tmp)
        bridge, me, logs, acted = _bridge_with(rec)
        opened = []

        class Writer:
            def close(self):
                pass

        async def control_channel(spec, log):
            opened.append(1)
            if len(opened) > 4:
                await asyncio.sleep(3600)            # the bike stops offering
            return object(), Writer()

        class Control:
            def __init__(self, *a, **kw):
                pass

            async def negotiate(self):
                return {"device_name": "WLAN DUCATI", "pin": "1234", "p2p": True}

            async def notify_wlan(self, state):
                pass

        async def join_wifi(mode, creds, log):
            raise ConnectionError("Wi-Fi Direct: no IP address from the bike")
        saved = bridge.hv.control_channel, bridge.PhoneControl, bridge.hv.join_wifi, asyncio.sleep
        real_sleep = asyncio.sleep

        async def fast_sleep(s, *a):
            await real_sleep(0 if s <= 5 else s)
        bridge.hv.control_channel, bridge.PhoneControl, bridge.hv.join_wifi = control_channel, Control, join_wifi
        bridge.asyncio.sleep = fast_sleep
        try:
            async def run():
                task = asyncio.ensure_future(bridge.Bridge.control_loop(me))
                for _ in range(200):
                    await real_sleep(0.005)
                    if len(opened) > 4:
                        break
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            asyncio.run(run())
        finally:
            bridge.hv.control_channel, bridge.PhoneControl, bridge.hv.join_wifi, bridge.asyncio.sleep = saved
        assert rec.streak == 4
        assert acted == ["restart_p2p", "reboot"]
        assert [e for e, _ in logs].count("control_error") == 4
        assert me._control_idle_since is not None    # the guard counts the bike's silence from here


def test_control_loop_good_join_clears_the_streak_and_other_errors_do_not_count():
    import asyncio
    with tempfile.TemporaryDirectory() as tmp:
        rec, rlogs, _, _, _ = make(tmp)
        rec.join_failed()
        bridge, me, logs, acted = _bridge_with(rec)
        steps = []

        class Writer:
            def close(self):
                pass

        async def control_channel(spec, log):
            steps.append("open")
            if len(steps) > 2:
                await asyncio.sleep(3600)
            return object(), Writer()

        class Control:
            def __init__(self, *a, **kw):
                pass

            async def negotiate(self):
                if len(steps) == 1:
                    raise TimeoutError("no Wi-Fi details from the dashboard within 30 s")   # ignition off: not a failed join
                return {"device_name": "WLAN DUCATI", "pin": "1234", "p2p": True}

            async def notify_wlan(self, state):
                raise ConnectionResetError("closed")  # ends this round of the loop

        async def join_wifi(mode, creds, log):
            return None
        saved = bridge.hv.control_channel, bridge.PhoneControl, bridge.hv.join_wifi, asyncio.sleep
        real_sleep = asyncio.sleep

        async def fast_sleep(s, *a):
            await real_sleep(0 if s <= 5 else s)
        bridge.hv.control_channel, bridge.PhoneControl, bridge.hv.join_wifi = control_channel, Control, join_wifi
        bridge.asyncio.sleep = fast_sleep
        try:
            async def run():
                task = asyncio.ensure_future(bridge.Bridge.control_loop(me))
                for _ in range(200):
                    await real_sleep(0.005)
                    if len(steps) > 2:
                        break
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                return None
            asyncio.run(run())
        finally:
            bridge.hv.control_channel, bridge.PhoneControl, bridge.hv.join_wifi, bridge.asyncio.sleep = saved
        assert rec.streak == 0 and acted == []
        assert rlogs[-1][0] == "mirror_recovered"


def test_guard_reconnects_a_silent_bike_only_after_a_failure():
    import asyncio
    import time as _time
    with tempfile.TemporaryDirectory() as tmp:
        rec, _, _, _, _ = make(tmp)
        bridge, me, logs, acted = _bridge_with(rec)
        real_sleep = asyncio.sleep
        saved = bridge.asyncio.sleep

        async def fast_sleep(s, *a):
            await real_sleep(0.002)
        bridge.asyncio.sleep = fast_sleep
        try:
            async def run(prepare):
                prepare()
                task = asyncio.ensure_future(bridge.Bridge.mirror_guard(me))
                await real_sleep(0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            # nothing failed: the guard never moves, however long the bike stays silent
            asyncio.run(run(lambda: setattr(me, "_control_idle_since", _time.monotonic() - 500)))
            assert acted == []
            # a failed join, the bike silent for 25 s: one Bluetooth reconnection, then the wait starts again
            def failed():
                rec.join_failed()
                me._control_idle_since = _time.monotonic() - 25
            asyncio.run(run(failed))
            assert acted == ["bounce"]
            # during a call nothing is touched
            me.call = {"state": "active"}
            asyncio.run(run(lambda: setattr(me, "_control_idle_since", _time.monotonic() - 500)))
            assert acted == ["bounce"]
        finally:
            bridge.asyncio.sleep = saved


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
