"""Music bridge (avrcp_bridge.py): data conversion and updates to BlueZ (python tests/test_avrcp.py)."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from mtspi import avrcp_bridge as ab  # noqa: E402

IPHONE = {"Status": "playing", "Position": 61000,
          "Track": {"Title": "Les Nuits", "Artist": "Nightmares on Wax", "Album": "Carboot Soul", "Duration": 287000,
                    "TrackNumber": 4}}


class FakePlayer:
    def __init__(self):
        self.changes = []

    def emit_properties_changed(self, changed):
        self.changes.append(changed)


def test_player_state():
    st = ab.player_state(IPHONE)
    assert st == {"title": "Les Nuits", "artist": "Nightmares on Wax", "album": "Carboot Soul", "duration_ms": 287000,
                  "number": 4, "status": "Playing", "position_ms": 61000}
    assert ab.player_state({})["status"] == "Stopped" and ab.player_state({})["title"] == ""


def test_metadata_like_bluez_wants():
    md = ab.mpris_metadata(ab.player_state(IPHONE), 3)
    assert md["xesam:title"].value == "Les Nuits" and md["xesam:artist"].signature == "as"
    assert md["mpris:length"].value == 287000 * 1000 and md["xesam:trackNumber"].value == 4
    assert md["mpris:trackid"].value == "/ducati/player/track3"
    empty = ab.mpris_metadata(ab.player_state({}), 0)
    assert "mpris:length" not in empty and empty["xesam:artist"].value == []


def test_updates_and_commands():
    async def main():
        logs = []
        b = ab.AvrcpBridge(lambda ev, **kw: logs.append((ev, kw)))
        b.player = FakePlayer()
        b._update(ab.player_state(IPHONE))                       # new track: metadata, status, position
        first = b.player.changes[-1]
        assert set(first) == {"Metadata", "PlaybackStatus", "Position"} and b.track_id == 1
        same = dict(IPHONE, Position=62000)                       # one second later: nothing to tell the bike
        b._update(ab.player_state(same))
        assert len(b.player.changes) == 1
        paused = dict(IPHONE, Status="paused", Position=62000)
        b._update(ab.player_state(paused))
        assert b.player.changes[-1]["PlaybackStatus"] == "Paused" and set(b.player.changes[-1]) == {"PlaybackStatus", "Position"}
        jump = dict(paused, Position=200000)                      # skip forward on the iPhone: new position
        b._update(ab.player_state(jump))
        assert set(b.player.changes[-1]) == {"Position"} and b.position_us() == 200000 * 1000
        b.command("Play")                                         # no iPhone: log only, no errors
        assert ("music_command", {"command": "Play", "to": None}) in logs
        assert [e for e, _ in logs].count("music_track") == 1
    asyncio.run(main())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
