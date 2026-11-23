"""The watch that puts the music link to the bike back when it dies by itself (mtspi/hfp_ag.py)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import json
import types

try:
    from mtspi import hfp_ag
    from mtspi.hfp_ag import MusicLinkWatch
except ImportError as e:                      # dbus_fast missing on this machine
    MusicLinkWatch = hfp_ag = None
    WHY = str(e)


@unittest.skipIf(MusicLinkWatch is None, "mtspi.hfp_ag cannot be imported here")
class Watch(unittest.TestCase):
    def setUp(self):
        self.t = 100.0
        self.w = MusicLinkWatch(now=lambda: self.t)

    def tick(self, seconds, connected=True):
        steps = []
        for _ in range(seconds):
            self.t += 1
            s = self.w.no_sink(connected)
            if s:
                steps.append((round(self.t - 100), s))
        return steps

    def test_nothing_while_the_bike_is_not_connected(self):
        self.assertEqual(self.tick(120, connected=False), [])

    def test_a_slow_start_is_not_a_fault(self):
        self.assertEqual(self.tick(7), [])
        self.assertFalse(self.w.back())

    def test_gentle_first_then_reconnect_then_gives_up(self):
        steps = self.tick(300)
        self.assertEqual([s for _, s in steps], ["connect", "reconnect", "reconnect", "reconnect"])
        times = [t for t, _ in steps]
        self.assertTrue(8 <= times[0] <= 10)
        self.assertTrue(all(b - a >= 30 for a, b in zip(times, times[1:])))
        self.assertTrue(self.w.back())

    def test_bike_gone_in_the_middle_starts_the_count_again(self):
        self.tick(6)
        self.tick(1, connected=False)
        self.assertEqual(self.tick(7), [])


def output(name, profile="a2dp-sink"):
    return {"info": {"props": {"node.name": name, "api.bluez5.profile": profile}}}


@unittest.skipIf(MusicLinkWatch is None, "mtspi.hfp_ag cannot be imported here")
class Outputs(unittest.TestCase):
    """Where the music goes: the list of audio outputs is read every few seconds in the thread that plays the music."""
    BIKE = "AA:BB:CC:00:11:22"

    def sink(self, nodes):
        return hfp_ag.bike_sink([self.BIKE], dump=lambda: json.dumps(nodes).encode())

    def test_the_bikes_music_output_is_found_among_the_others(self):
        nodes = [output("alsa_output.platform"), output("bluez_output.11_22_33_44_55_66.1"),
                 output("bluez_output.AA_BB_CC_00_11_22.1", "headset-head-unit"),
                 output("bluez_output.AA_BB_CC_00_11_22.1")]
        self.assertEqual(self.sink(nodes), "bluez_output.AA_BB_CC_00_11_22.1")
        self.assertIsNone(self.sink(nodes[:3]))                 # only the voice profile: no music output
        self.assertIsNone(self.sink([]))

    def test_a_list_that_cannot_be_read_is_not_taken_for_no_output(self):
        def stuck():
            raise hfp_ag.subprocess.TimeoutExpired("pw-dump", 5)
        for dump in (lambda: b'[{"info": {"props"', stuck, lambda: (_ for _ in ()).throw(OSError("no pw-dump"))):
            with self.assertRaises(hfp_ag.SinkUnknown):
                hfp_ag.bike_sink([self.BIKE], dump=dump)
        self.assertIsNone(self.sink({"not": "a list"}))         # odd answers do not end the music thread either
        self.assertIsNone(self.sink(["text", {"info": None}]))

    def test_with_no_hands_free_link_the_music_still_looks_for_the_bike_it_was_going_to(self):
        asked, real = [], hfp_ag.bike_sink
        hfp_ag.bike_sink = lambda macs, dump=None: asked.append(list(macs)) or None
        try:
            state = types.SimpleNamespace(ag=types.SimpleNamespace(links={"/org/bluez/hci0/dev_AA_BB_CC_00_11_22": 1}))
            hfp_ag.BridgeState._bike_sink(state)
            state.ag.links.clear()                  # the hands-free link dropped; the music link may still be up
            hfp_ag.BridgeState._bike_sink(state)
            fresh = types.SimpleNamespace(ag=types.SimpleNamespace(links={}))       # never connected: any Bluetooth output
            hfp_ag.BridgeState._bike_sink(fresh)
        finally:
            hfp_ag.bike_sink = real
        self.assertEqual(asked, [[self.BIKE], [self.BIKE], []])

    def test_a_closed_link_leaves_the_table_but_not_the_one_that_took_its_place(self):
        ag = types.SimpleNamespace(links={})
        old, new = types.SimpleNamespace(device="/dev_1"), types.SimpleNamespace(device="/dev_1")
        ag.links["/dev_1"] = old
        hfp_ag.HfpAg.forget(ag, old)
        self.assertEqual(ag.links, {})                          # "the bike is connected" is false again
        ag.links["/dev_1"] = new                                # the bike came back before the old link finished closing
        hfp_ag.HfpAg.forget(ag, old)
        self.assertIs(ag.links["/dev_1"], new)


if __name__ == "__main__":
    unittest.main()
