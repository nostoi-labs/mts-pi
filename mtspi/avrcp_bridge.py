"""Music bridge to the bike: the dashboard's Music menu shows the title, artist and playback state of the iPhone's
track, and its buttons (play, pause, next, previous) reach the iPhone.

24 Oct 2026 (btmon): on its link with the Pi, the bike requests GetElementAttributes, GetPlayStatus and
RegisterNotification and sends PLAY; BlueZ answered with an empty title and STOPPED because there was no player
towards the bike ("not available"). Here an MPRIS player is registered with BlueZ (Media1.RegisterPlayer) only on
the bike's radio; BlueZ turns it into the AVRCP Target. The data comes from the iPhone's AVRCP player (MediaPlayer1 on
its radio) and the commands go back there. With the Carlinkit the source will become CarPlay (same interface).

Linux only (dbus-fast); the data conversion (player_state, mpris_metadata) is pure and can be tested anywhere.
"""
# no "from __future__ import annotations": dbus-fast reads the D-Bus signatures from the annotations

import asyncio
from typing import Optional
from mtspi.tasks import spawn

PLAYER_PATH = "/ducati/player"
STATUS = {"playing": "Playing", "paused": "Paused", "stopped": "Stopped",
          "forward-seek": "Playing", "reverse-seek": "Playing", "error": "Stopped"}
COMMANDS = ("Play", "Pause", "Next", "Previous", "Stop")


def player_state(props: dict) -> dict:
    """MediaPlayer1 (values already unpacked) → neutral state: title, artist, album, duration_ms, number, status, position_ms."""
    track = props.get("Track") or {}
    return {"title": track.get("Title") or "", "artist": track.get("Artist") or "", "album": track.get("Album") or "",
            "duration_ms": int(track.get("Duration") or 0), "number": int(track.get("TrackNumber") or 0),
            "status": STATUS.get(props.get("Status") or "stopped", "Stopped"),
            "position_ms": int(props.get("Position") or 0)}


def mpris_metadata(st: dict, track_id: int):
    """Neutral state → MPRIS Metadata as BlueZ reads it (media.c): title, artist (list), album, length in µs."""
    from dbus_fast import Variant
    md = {"mpris:trackid": Variant("o", f"{PLAYER_PATH}/track{track_id}"),
          "xesam:title": Variant("s", st["title"]),
          "xesam:artist": Variant("as", [st["artist"]] if st["artist"] else []),
          "xesam:album": Variant("s", st["album"])}
    if st["duration_ms"]:
        md["mpris:length"] = Variant("x", st["duration_ms"] * 1000)
    if st["number"]:
        md["xesam:trackNumber"] = Variant("i", st["number"])
    return md


def _make_player(bridge):
    from dbus_fast.service import PropertyAccess, ServiceInterface, dbus_property, method

    class Player(ServiceInterface):
        def __init__(self):
            super().__init__("org.mpris.MediaPlayer2.Player")

        # --- commands from the bike (BlueZ calls these when an AVRCP PASS THROUGH or command arrives)
        @method()
        def Play(self):
            bridge.command("Play")

        @method()
        def Pause(self):
            bridge.command("Pause")

        @method()
        def PlayPause(self):
            bridge.command("Pause" if bridge.state["status"] == "Playing" else "Play")

        @method()
        def Stop(self):
            bridge.command("Pause")         # on Stop the iPhone closes the music session: a pause is better

        @method()
        def Next(self):
            bridge.command("Next")

        @method()
        def Previous(self):
            bridge.command("Previous")

        @method()
        def Seek(self, offset: "x"):
            pass

        @method()
        def SetPosition(self, track: "o", position: "x"):
            pass

        # --- state read by BlueZ
        @dbus_property(access=PropertyAccess.READ)
        def PlaybackStatus(self) -> "s":
            return bridge.state["status"]

        @dbus_property(access=PropertyAccess.READ)
        def Metadata(self) -> "a{sv}":
            return mpris_metadata(bridge.state, bridge.track_id)

        @dbus_property(access=PropertyAccess.READ)
        def Position(self) -> "x":
            return bridge.position_us()

        @dbus_property(access=PropertyAccess.READ)
        def LoopStatus(self) -> "s":
            return "None"

        @dbus_property(access=PropertyAccess.READ)
        def Shuffle(self) -> "b":
            return False

        @dbus_property(access=PropertyAccess.READ)
        def Rate(self) -> "d":
            return 1.0

        @dbus_property(access=PropertyAccess.READ)
        def CanGoNext(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanGoPrevious(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanPlay(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanPause(self) -> "b":
            return True

        @dbus_property(access=PropertyAccess.READ)
        def CanSeek(self) -> "b":
            return False

        @dbus_property(access=PropertyAccess.READ)
        def CanControl(self) -> "b":
            return True

    return Player()


class AvrcpBridge:
    """Registers the player on the bike's radio and keeps it in sync with the iPhone's player (every second)."""

    def __init__(self, log, ready: Optional[asyncio.Event] = None, on_command=None):
        self.log = log
        self.ready = ready or asyncio.Event()
        # standard variant with the Carlinkit: music is played by CarPlay, there is no iPhone AVRCP player on the Pi.
        # The track comes from the bridge (set_carplay) and the bike's buttons go back to the bridge (on_command → Carlinkit)
        self.on_command = on_command
        self.carplay = None
        self.state = player_state({})
        self.track_id = 0
        self.source = None            # path of the iPhone's MediaPlayer1
        self.bus = None
        self.player = None
        self.adapter = None
        self._pos_at = 0.0            # when position_ms was read (to compute the position in the meantime)

    def position_us(self) -> int:
        pos = self.state["position_ms"]
        if self.state["status"] == "Playing":
            pos += int((asyncio.get_event_loop().time() - self._pos_at) * 1000)
        return pos * 1000

    def command(self, name: str) -> None:
        if self.source:
            self.log("music_command", command=name, to="iphone")
            spawn(self._send(name))
        elif self.on_command and self.on_command(name):
            self.log("music_command", command=name, to="carplay")
        else:
            self.log("music_command", command=name, to=None)

    def set_carplay(self, title: str, artist: str, album: str, duration_ms: int, status: str) -> None:
        """CarPlay track (Carlinkit MediaData): used when there is no iPhone AVRCP player."""
        self.carplay = {"title": title, "artist": artist, "album": album, "duration_ms": duration_ms, "number": 0,
                        "status": STATUS.get(status, "Stopped"), "position_ms": 0}
        if not self.source:
            self._update(self.carplay)

    async def _send(self, name: str) -> None:
        from mtspi.bluez import call
        try:
            await call(self.bus, "org.bluez", self.source, "org.bluez.MediaPlayer1", name)
        except Exception as e:
            self.log("music_command_error", command=name, error=f"{type(e).__name__}: {e}")

    async def _find(self):
        """(bike radio, iPhone player). The bike: the paired device whose name starts with DUCATI…;
        the player: a MediaPlayer1 on another radio (the iPhone's)."""
        from mtspi.bluez import _v, call
        objs = (await call(self.bus, "org.bluez", "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects"))[0]
        bike_adapter = None
        for path, ifaces in objs.items():
            dev = ifaces.get("org.bluez.Device1")
            if dev and _v(dev.get("Paired")) and str(_v(dev.get("Name")) or "").upper().startswith("DUCATI"):
                bike_adapter = _v(dev.get("Adapter"))
        # never a player belonging to one of the Pi's own radios (seen in a test with both radios paired: the bridge was reading itself)
        own = {_v(i["org.bluez.Adapter1"].get("Address")).replace(":", "_") for i in objs.values() if "org.bluez.Adapter1" in i}
        source = None
        for path, ifaces in objs.items():
            if "org.bluez.MediaPlayer1" not in ifaces or (bike_adapter and path.startswith(bike_adapter + "/")):
                continue
            if any(f"/dev_{a}/" in path for a in own):
                continue
            source = path
        return bike_adapter, source

    async def register(self, adapter: str) -> None:
        from dbus_fast import Variant
        from mtspi.bluez import call
        if self.player is None:
            self.player = _make_player(self)
            self.bus.export(PLAYER_PATH, self.player)
        props = {"PlaybackStatus": Variant("s", self.state["status"]),
                 "Metadata": Variant("a{sv}", mpris_metadata(self.state, self.track_id)),
                 "Position": Variant("x", 0), "Rate": Variant("d", 1.0),
                 "LoopStatus": Variant("s", "None"), "Shuffle": Variant("b", False),
                 "CanGoNext": Variant("b", True), "CanGoPrevious": Variant("b", True),
                 "CanPlay": Variant("b", True), "CanPause": Variant("b", True),
                 "CanSeek": Variant("b", False), "CanControl": Variant("b", True)}
        await call(self.bus, "org.bluez", adapter, "org.bluez.Media1", "RegisterPlayer", "oa{sv}", [PLAYER_PATH, props])
        self.adapter = adapter
        self.log("music_player_registered", adapter=adapter.rsplit("/", 1)[-1])

    def _update(self, new: dict) -> None:
        old = self.state
        changed = {}
        track_changed = any(new[k] != old[k] for k in ("title", "artist", "album", "duration_ms", "number"))
        if track_changed:
            self.track_id += 1
        expected = self.position_us() // 1000
        self.state = new
        self._pos_at = asyncio.get_event_loop().time()
        if track_changed:
            changed["Metadata"] = mpris_metadata(new, self.track_id)
            self.log("music_track", title=bool(new["title"]), artist=bool(new["artist"]), status=new["status"])
        if new["status"] != old["status"]:
            changed["PlaybackStatus"] = new["status"]
            if not track_changed:
                self.log("music_status", status=new["status"])
        if track_changed or new["status"] != old["status"] or abs(new["position_ms"] - expected) > 3000:
            changed["Position"] = self.position_us()
        if changed and self.player is not None:
            self.player.emit_properties_changed(changed)

    async def run(self) -> None:
        from mtspi.bluez import _v, call, system_bus
        while True:
            try:
                if not self.bus:
                    self.bus = await system_bus()
                    self.player, self.adapter = None, None
                adapter, self.source = await self._find()
                if adapter and adapter != self.adapter:
                    await self.register(adapter)
                if self.adapter or not adapter:
                    self.ready.set()             # with no paired bike, do not wait (phonebook and mySPIN start)
                for _ in range(10):              # devices are rechecked every 10 s, the track every second
                    if self.source:
                        try:
                            props = (await call(self.bus, "org.bluez", self.source, "org.freedesktop.DBus.Properties",
                                                "GetAll", "s", ["org.bluez.MediaPlayer1"]))[0]
                            self._update(player_state({k: _v(v) if k != "Track" else
                                                       {tk: _v(tv) for tk, tv in _v(v).items()} for k, v in props.items()}))
                        except RuntimeError:
                            self.source = None
                            self._update(player_state({}))
                    elif self.carplay:
                        if self.state is not self.carplay:
                            self._update(self.carplay)
                    elif self.state["title"] or self.state["status"] != "Stopped":
                        self._update(player_state({}))
                    await asyncio.sleep(1)
            except Exception as e:
                self.log("music_bridge_error", error=f"{type(e).__name__}: {e}")
                self.ready.set()
                self.bus = None
                await asyncio.sleep(10)
