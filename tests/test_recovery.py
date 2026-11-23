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


def test_radio_dead_when_its_own_reset_gets_no_answer():
    """Bench, 21 Nov 2026, Bluetooth tethering traffic on top of music and Wi-Fi: the three lines as they came."""
    watch = recovery.RadioWatch(now=Clock())
    lines = [RESET, "3,3101,5056806456,-;Bluetooth: hci0: Opcode 0x0c03 failed: -110", RESET]
    assert [watch.feed(x) for x in lines] == ["reset", "dead", "reset"]


GHOST = "3,9105,14292916424,-;Bluetooth: hci0: SCO packet for unknown connection handle 6"


def test_voice_packets_for_an_unknown_link_twenty_in_a_minute():
    """Bench, 22 Nov 2026: the kernel writes ten of these lines every five seconds while the fault lasts."""
    now = Clock()
    watch = recovery.RadioWatch(now=now)
    seen = []
    for burst in range(2):
        for _ in range(10):
            seen.append(watch.feed(GHOST))
        now.t += 5
    assert seen == [None] * 19 + ["ghost"]
    assert watch.feed(GHOST) is None                 # the count starts again: the next call comes after another twenty


def test_a_few_stray_voice_packets_after_a_closing_are_nothing():
    """Same bench night, hours before the fault: one line, then four lines 25 minutes later."""
    now = Clock()
    watch = recovery.RadioWatch(now=now)
    assert watch.feed(GHOST) is None
    for _ in range(200):                             # a stray handful every minute and a half, for hours
        now.t += 90
        assert [watch.feed(GHOST) for _ in range(4)] == [None] * 4


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


def test_bluetooth_still_off_after_a_chip_restart_restarts_the_pi():
    import asyncio
    with tempfile.TemporaryDirectory() as tmp:
        rec, _, _, _, _ = make(tmp)
        bridge, me, logs, acted = _bridge_with(rec)
        answers = []

        async def radio_powered():
            return answers.pop(0)
        me.radio_powered = radio_powered
        real_sleep, saved = asyncio.sleep, bridge.asyncio.sleep

        async def fast_sleep(s, *a):
            await real_sleep(0)
        bridge.asyncio.sleep = fast_sleep
        try:
            for seen, expected in (([True], []), ([None], []), ([False, True], []), ([False, False], ["reboot"])):
                answers[:] = seen
                del acted[:]
                checking = [object()]
                asyncio.run(bridge.Bridge.radio_back_check(me, checking))
                assert acted == expected, (seen, acted)
                assert checking == []                    # the next chip restart gets its own look
        finally:
            bridge.asyncio.sleep = saved


def test_unknown_voice_link_restarts_the_pi_but_not_during_a_call():
    import asyncio
    with tempfile.TemporaryDirectory() as tmp:
        rec, _, _, _, _ = make(tmp)
        bridge, me, logs, acted = _bridge_with(rec, _ghost_noted=False, _ghost_bounce_t=-1e9)
        me.call = {"state": "active"}
        for _ in range(3):
            asyncio.run(bridge.Bridge.voice_ghost(me))
        assert acted == []                                               # the call is left alone
        assert [ev for ev, _ in logs] == ["radio_voice_ghost"]           # and noted once, not at every round
        me.call = None
        asyncio.run(bridge.Bridge.voice_ghost(me))
        assert acted == ["reboot"]

        # the restart is not allowed (the Pi restarted a few minutes ago): the bike's link is closed and reopened
        # instead, and not again within the minute
        async def refused(action, why):
            acted.append(action)
            return False if action == "reboot" else None
        me.recover = refused
        del acted[:]
        asyncio.run(bridge.Bridge.voice_ghost(me))
        asyncio.run(bridge.Bridge.voice_ghost(me))
        assert acted == ["reboot", "bounce", "reboot"]


# ------------------------------------------------------------ faults that are something lasting too long (22 Nov 2026)

GARBLED = "3,5120,7411000000,-;Bluetooth: hci0: Frame reassembly failed (-84)"


def test_a_burst_of_broken_frames_is_noted_once_and_a_few_are_nothing():
    now = Clock()
    w = recovery.RadioWatch(now=now)
    for _ in range(5):                                   # a handful, as after any hiccup
        assert w.feed(GARBLED) is None
        now.t += 3
    now.t += 60
    seen = []
    for _ in range(539):                                 # the burst of the night bench: 539 lines in under a second
        seen.append(w.feed(GARBLED))
        now.t += 0.001
    assert seen.count("garbled") == 1 and seen[recovery.GARBLED_COUNT - 1] == "garbled"
    assert w.feed(STUCK) is None                         # and they are not counted as an unanswering radio
    now.t += 60                                          # a second burst, a minute later, is said again
    assert [w.feed(GARBLED) for _ in range(40)].count("garbled") == 1


def test_lasting_says_so_at_the_limit_and_again_every_limit_while_it_lasts():
    now = Clock()
    x = recovery.Lasting(60, now=now)
    said = []
    for second in range(200):
        now.t += 1
        if x.feed(True):
            said.append(second + 1)
    assert said == [61, 121, 181] and x.times == 3
    assert x.feed(False) is False and x.times == 0       # a break: the count starts again from nothing
    now.t += 59
    assert x.feed(True) is False
    now.t += 59
    assert x.feed(True) is False
    now.t += 2
    assert x.feed(True) is True


def test_a_missing_carlinkit_restarts_the_pi_once_until_it_is_seen_again():
    with tempfile.TemporaryDirectory() as tmp:
        rec, logs, cmds, now, wall = make(tmp)
        assert not rec.carlinkit_tried()
        assert rec.reboot("the Carlinkit is not on the USB port", key="carlinkit")
        assert rec.carlinkit_tried()                     # it did not come back with a restart: nobody tries again
        rec.carlinkit_seen()                             # ... until it has been there once
        assert not rec.carlinkit_tried()
        lines = [json.loads(x) for x in open(os.path.join(tmp, "recovery.log")).read().splitlines()]
        assert [x["what"] for x in lines] == ["reboot", "carlinkit_seen"] and lines[0]["key"] == "carlinkit"
        rec.carlinkit_seen()                             # seen at every start: no line more, nothing written to the card
        assert len(open(os.path.join(tmp, "recovery.log")).read().splitlines()) == 2
        wall.t += 3600
        assert rec.reboot("the Bluetooth radio no longer answers")          # another reason is another matter
        assert not rec.carlinkit_tried()


def _slow_guard(tmp, limits, ticks, guard="slow_guard", **state):
    """Runs the bridge's guard for a few of its turns, with the three limits given and a stand-in bridge."""
    import asyncio
    rec, _, cmds, _, _ = make(tmp)
    bridge, me, logs, acted = _bridge_with(rec)
    me.a.carlinkit = state.pop("carlinkit", "usb")
    for name, value in state.items():
        setattr(me, name, value)
    unstuck = []

    async def carplay_unstick(times):
        unstuck.append(times)
    me.carplay_unstick = carplay_unstick
    me.carplay_waiting = lambda: bridge.Bridge.carplay_waiting(me)
    real_sleep, saved = asyncio.sleep, bridge.asyncio.sleep
    saved_limits = (recovery.BT_OFF_S, recovery.NO_CARLINKIT_S, recovery.CONNECTING_S)
    recovery.BT_OFF_S, recovery.NO_CARLINKIT_S, recovery.CONNECTING_S = limits
    turns = []

    async def fast_sleep(s, *a):
        turns.append(1)
        if len(turns) > ticks:
            raise asyncio.CancelledError
        await real_sleep(0.02)

    async def run():
        try:
            await getattr(bridge.Bridge, guard)(me)
        except asyncio.CancelledError:
            pass
    bridge.asyncio.sleep = fast_sleep
    try:
        asyncio.run(run())
    finally:
        bridge.asyncio.sleep = saved
        recovery.BT_OFF_S, recovery.NO_CARLINKIT_S, recovery.CONNECTING_S = saved_limits
    return rec, logs, acted, cmds, unstuck


def _radio(answer):
    async def radio_powered():
        return answer
    return radio_powered


def test_bluetooth_that_stays_switched_off_restarts_the_pi_and_no_answer_does_not():
    with tempfile.TemporaryDirectory() as tmp:
        connected = types.SimpleNamespace(state="connected")
        for answer, expected in ((False, ["reboot"]), (True, []), (None, [])):
            _, logs, acted, _, _ = _slow_guard(tmp, (0.05, 999, 999), 9, radio_powered=_radio(answer), dongle=connected,
                                               carplay_state=lambda: "on")
            assert acted[:1] == expected, (answer, acted)
            assert bool([e for e, _ in logs if e == "radio_off"]) == bool(expected)


def test_the_carlinkit_gone_from_usb_restarts_the_pi_once():
    with tempfile.TemporaryDirectory() as tmp:
        state = dict(radio_powered=_radio(True), dongle=None, carplay_state=lambda: "off")
        rec, logs, _, cmds, _ = _slow_guard(tmp, (999, 0.05, 999), 6, **state)
        assert [c for c in cmds if c[-1] == "reboot"] and rec.carlinkit_tried()
        # the start after that restart: still not there -> said once, no second restart
        rec2, logs2, _, cmds2, _ = _slow_guard(tmp, (999, 0.05, 999), 12, **state)
        assert not [c for c in cmds2 if c[-1] == "reboot"]
        assert len([kw for e, kw in logs2 if e == "carlinkit_absent" and "note" in kw]) == 1
        # nothing at all during a call, or when the card is set up without the Carlinkit
        for extra in (dict(call={"state": "active"}), dict(carlinkit="none")):
            with tempfile.TemporaryDirectory() as other:
                _, logs3, _, cmds3, _ = _slow_guard(other, (999, 0.05, 999), 6, **dict(state, **extra))
                assert not cmds3 and not [e for e, _ in logs3 if e == "carlinkit_absent"]


def test_carplay_left_on_connecting_is_helped_only_with_the_iphone_in_reach():
    with tempfile.TemporaryDirectory() as tmp:
        opening = types.SimpleNamespace(state="opening")
        phone = types.SimpleNamespace(bike={"mac": "AA:BB:CC:DD:EE:FF"}, iphone={"mac": "AA:BB:CC:00:11:22"})
        no_phone = types.SimpleNamespace(bike={"mac": "AA:BB:CC:DD:EE:FF"}, iphone=None)
        cases = ((dict(bt=phone), True), (dict(bt=no_phone), False), (dict(bt=phone, call={"state": "active"}), False))
        for extra, helped in cases:
            state = dict(radio_powered=_radio(True), dongle=opening, carplay_state=lambda: "connecting")
            state.update(extra)
            _, _, _, cmds, unstuck = _slow_guard(tmp, (999, 999, 0.05), 12, guard="connecting_guard", **state)
            assert (unstuck[:2] == [1, 2]) == helped, (extra.keys(), unstuck)
            assert not cmds                               # never the Pi's restart for this


def test_carplay_stuck_the_session_is_opened_again_and_nothing_harsher_is_done():
    """Restarting the program is not done: "connecting" for minutes is also what a locked phone in a pocket looks like."""
    import asyncio
    with tempfile.TemporaryDirectory() as tmp:
        rec, _, cmds, _, _ = make(tmp)
        bridge, me, logs, _ = _bridge_with(rec)
        sent, killed, asked = [], [], []

        async def stop():
            sent.append("stop")
        me.dongle = types.SimpleNamespace(stop=stop)
        me.request_carplay = lambda: asked.append(1)
        real_sleep, saved_sleep, saved_kill = asyncio.sleep, bridge.asyncio.sleep, bridge.os.kill

        async def fast_sleep(s, *a):
            await real_sleep(0)
        bridge.asyncio.sleep, bridge.os.kill = fast_sleep, lambda pid, sig: killed.append(sig)
        try:
            for times in (1, 2, 3, 4):
                asyncio.run(bridge.Bridge.carplay_unstick(me, times))
            assert sent == ["stop"] and asked == [1]                # closed and asked again, once
            assert not killed and not cmds                          # never the program, never the Pi
            assert [kw["step"] for e, kw in logs if e == "carplay_stuck"] == ["session opened again", "left alone"]
        finally:
            bridge.asyncio.sleep, bridge.os.kill = saved_sleep, saved_kill


def test_a_restart_meant_to_happen_once_is_not_made_when_once_cannot_be_remembered():
    """No Carlinkit plugged in is not a fault. If the record of the one restart would be lost, the Pi would restart
    every few minutes for as long as the Carlinkit is missing."""
    with tempfile.TemporaryDirectory() as tmp:
        # the record cannot be written at all (card full, data partition read-only)
        rec, logs, cmds, _, _ = make(os.path.join(tmp, "no-such-folder"))
        assert rec.reboot("the Carlinkit is not on the USB port", key="carlinkit") is False and cmds == []
        assert rec.reboot("the Bluetooth radio no longer answers") is True          # a dead radio restarts all the same
        # the record can be written, but on the layer in memory of a protected card: gone at the restart
        rec, logs, cmds, _, _ = make(tmp)
        mounts = os.path.join(tmp, "mounts")
        open(mounts, "w").write("overlayroot / overlay rw,relatime 0 0\n/dev/mmcblk0p3 /data ext4 rw 0 0\n")
        real = os.stat
        try:
            recovery.os.stat = lambda p, *a, **k: types.SimpleNamespace(st_dev=7)       # the folder is on the same device as /
            assert rec.record_kept(mounts) is False
            recovery.os.stat = lambda p, *a, **k: types.SimpleNamespace(st_dev=7 if p == "/" else 9)   # on the data partition
            assert rec.record_kept(mounts) is True
        finally:
            recovery.os.stat = real
        open(mounts, "w").write("/dev/mmcblk0p2 / ext4 rw,noatime 0 0\n")              # a card that is not protected: all of it is kept
        assert rec.record_kept(mounts) is True
        rec.record_kept = lambda mounts=None: False
        assert rec.reboot("the Carlinkit is not on the USB port", key="carlinkit") is False and cmds == []
        assert not os.path.exists(os.path.join(tmp, "recovery.log"))        # and no line for a restart that was not made:
        assert not rec.carlinkit_tried() and rec.last_reboot() is None      # it would have held back every other restart
        assert [kw["because"] for e, kw in logs if e == "recovery_reboot_skipped"] == ["the record would not survive the restart"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
