"""Real Bluetooth on the Pi (BlueZ, oFono and obexd over D-Bus). Linux only; on the Mac the bench uses the emulated links.

- RfcommServer: the "MySpinBluetooth" service that the bike opens to negotiate Wi-Fi (mySPIN control channel);
- Agent: accepts pairings on its own (bike and iPhone) and marks the device as trusted;
- BtState: reads every 2 s who is connected and what is happening: iPhone (battery, signal, call, track), bike;
- pull_phonebook: downloads the phonebook and recent calls from the iPhone (PBAP, obexd on the session bus);
  towards the bike the phonebook is served by pbap.py (same RfcommServer, Phonebook Access Server profile).

The actual bridging is done by system services: PipeWire (A2DP music iPhone → bike), oFono (calls:
hands-free towards the iPhone, phone towards the bike); track title and AVRCP commands: avrcp_bridge.py.
No secrets in the logs: only names, addresses and states.
"""
# no "from __future__ import annotations": dbus-fast reads the D-Bus signatures from the annotations

import asyncio
import os
import socket
import time

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method
from mtspi.tasks import spawn

MYSPIN_UUID = "31fd954e-8138-4367-a201-0276b9716fb1"
# profiles that tell the two sides apart
UUID_HFP_AG = "0000111f-0000-1000-8000-00805f9b34fb"     # the phone (iPhone)
UUID_HFP_HF = "0000111e-0000-1000-8000-00805f9b34fb"     # the hands-free unit (bike, helmet intercom)
UUID_A2DP_SINK = "0000110b-0000-1000-8000-00805f9b34fb"


def _v(x):
    return x.value if isinstance(x, Variant) else x


async def system_bus():
    return await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()


async def call(bus, dest, path, iface, member, sig="", body=None):
    reply = await bus.call(Message(destination=dest, path=path, interface=iface, member=member,
                                   signature=sig, body=body or []))
    if reply.message_type == MessageType.ERROR:
        raise RuntimeError(f"{member}: {reply.error_name} {reply.body}")
    return reply.body


# ------------------------------------------------------------------ mySPIN control channel
class _Profile(ServiceInterface):
    def __init__(self, server):
        super().__init__("org.bluez.Profile1")
        self.server = server

    @method()
    def Release(self):
        pass

    @method()
    def NewConnection(self, device: "o", fd: "h", properties: "a{sv}"):
        self.server._incoming(device, fd)

    @method()
    def RequestDisconnection(self, device: "o"):
        self.server.log(f"{self.server.event}_disconnect_requested", device=device)


class RfcommServer:
    """Registers a server-side RFCOMM profile once; accept() returns (reader, writer) for each connection.
    By default it is the mySPIN control channel; the phonebook (pbap.py) uses it with its own UUID, channel and SDP record."""
    PATH = "/ducati/myspin"

    def __init__(self, log, path=None, uuid=MYSPIN_UUID, name="MySpinBluetooth", channel=22, record=None,
                 require_auth=False, event="control"):
        self.log = log
        self.bus = None
        self.queue = asyncio.Queue()
        self.path, self.uuid, self.name, self.channel = path or self.PATH, uuid, name, channel
        self.record, self.require_auth, self.event = record, require_auth, event

    async def start(self):
        if self.bus:
            return
        self.bus = await system_bus()
        self.bus.export(self.path, _Profile(self))
        # Channel is required: without it BlueZ advertises the service with no RFCOMM channel and the bike cannot open it
        opts = {"Name": Variant("s", self.name), "Role": Variant("s", "server"),
                "Channel": Variant("q", self.channel),
                "RequireAuthentication": Variant("b", self.require_auth), "RequireAuthorization": Variant("b", False),
                "AutoConnect": Variant("b", True)}
        if self.record:
            opts["ServiceRecord"] = Variant("s", self.record)
        await call(self.bus, "org.bluez", "/org/bluez", "org.bluez.ProfileManager1", "RegisterProfile",
                   "osa{sv}", [self.path, self.uuid, opts])
        self.log(f"{self.event}_listening", transport="rfcomm", uuid=self.uuid, channel=self.channel)

    def _incoming(self, device, fd):
        sock = socket.socket(fileno=os.dup(fd))
        os.close(fd)
        sock.setblocking(False)
        self.queue.put_nowait((device, sock))

    async def accept_device(self):
        await self.start()
        device, sock = await self.queue.get()
        r, w = await asyncio.open_connection(sock=sock)
        self.log(f"{self.event}_connected", peer=device.rsplit("/", 1)[-1].replace("dev_", "").replace("_", ":"))
        return device, r, w

    async def accept(self):
        _, r, w = await self.accept_device()
        return r, w


# ------------------------------------------------------------------ pairing agent
class _Agent(ServiceInterface):
    def __init__(self, bus, log):
        super().__init__("org.bluez.Agent1")
        self.bus, self.log = bus, log

    def _trust(self, device):
        spawn(call(self.bus, "org.bluez", device, "org.freedesktop.DBus.Properties", "Set",
                                   "ssv", ["org.bluez.Device1", "Trusted", Variant("b", True)]))

    @method()
    def Release(self):
        pass

    @method()
    def RequestPinCode(self, device: "o") -> "s":
        self.log("pair_pin", device=device)
        self._trust(device)
        return "0000"

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):
        self.log("pair_display_pin", device=device, pin=pincode)

    @method()
    def RequestPasskey(self, device: "o") -> "u":
        self._trust(device)
        return 0

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):
        self.log("pair_display_passkey", device=device, passkey=passkey)

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):
        self.log("pair_confirm", device=device, passkey=f"{passkey:06d}")     # the same number appears on iPhone/bike
        self._trust(device)

    @method()
    def RequestAuthorization(self, device: "o"):
        self._trust(device)

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):
        self.log("pair_authorize_service", device=device, uuid=uuid)

    @method()
    def Cancel(self):
        self.log("pair_cancelled")


async def run_agent(log):
    bus = await system_bus()
    path = "/ducati/agent"
    bus.export(path, _Agent(bus, log))
    await call(bus, "org.bluez", "/org/bluez", "org.bluez.AgentManager1", "RegisterAgent", "os", [path, "DisplayYesNo"])
    await call(bus, "org.bluez", "/org/bluez", "org.bluez.AgentManager1", "RequestDefaultAgent", "o", [path])
    log("agent_ready", capability="DisplayYesNo")
    await asyncio.Event().wait()


# ------------------------------------------------------------------ real state
class BtState:
    """Snapshot of iPhone and bike for the home screen and diagnostics (updated by poll())."""

    def __init__(self, log):
        self.log = log
        self.bus = None
        self.iphone = None        # {"mac", "name", "adapter"}
        self.bike = None
        self.cind = {}            # battchg 0-5, signal 0-5, service
        self.operator = None
        self.call = None          # {"state", "name", "number"}
        self.media = None         # {"title", "artist", "status"}
        self.modem = None
        self._last = {}

    async def poll(self):
        if not self.bus:
            self.bus = await system_bus()
        objs = (await call(self.bus, "org.bluez", "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects"))[0]
        iphone = bike = None
        players = {}
        # the Pi's own radios are not phones (when they were paired with each other for a test, one of the Pi's radios looked like an iPhone)
        own = {_v(i["org.bluez.Adapter1"].get("Address")) for i in objs.values() if "org.bluez.Adapter1" in i}
        for path, ifaces in objs.items():
            dev = ifaces.get("org.bluez.Device1")
            if dev and _v(dev.get("Connected")) and _v(dev.get("Address")) not in own:
                uuids = set(_v(dev.get("UUIDs")) or [])
                info = {"mac": _v(dev.get("Address")), "name": _v(dev.get("Alias")) or _v(dev.get("Name")),
                        "adapter": _v(dev.get("Adapter")).rsplit("/", 1)[-1], "path": path}
                if UUID_HFP_AG in uuids:
                    iphone = info
                elif UUID_HFP_HF in uuids or UUID_A2DP_SINK in uuids or str(info["name"]).upper().startswith("DUCATI"):
                    bike = info
            mp = ifaces.get("org.bluez.MediaPlayer1")
            if mp:
                players[path] = mp
        self.iphone, self.bike = iphone, bike
        player = next((mp for p, mp in players.items() if iphone and p.startswith(iphone["path"] + "/")), None)
        if player and iphone:
            track = _v(player.get("Track")) or {}
            self.media = {"title": _v(track.get("Title")), "artist": _v(track.get("Artist")),
                          "status": _v(player.get("Status"))}
        else:
            self.media = None
        await self._poll_ofono()
        self._log_changes()

    async def _poll_ofono(self):
        self.cind, self.call, self.operator, self.modem = {}, None, None, None
        if not self.iphone:
            return
        try:
            modems = (await call(self.bus, "org.ofono", "/", "org.ofono.Manager", "GetModems"))[0]
        except Exception:
            return                                   # oFono not running
        mac = self.iphone["mac"].upper()
        for path, props in modems:
            if _v(props.get("Type")) != "hfp" or mac not in str(_v(props.get("Serial", ""))).upper() + path.upper().replace("_", ":"):
                continue
            self.modem = path
            ifaces = _v(props.get("Interfaces")) or []
            if "org.ofono.Handsfree" in ifaces:
                hf = (await call(self.bus, "org.ofono", path, "org.ofono.Handsfree", "GetProperties"))[0]
                if "BatteryChargeLevel" in hf:
                    self.cind["battchg"] = int(_v(hf["BatteryChargeLevel"]))
            if "org.ofono.NetworkRegistration" in ifaces:
                nr = (await call(self.bus, "org.ofono", path, "org.ofono.NetworkRegistration", "GetProperties"))[0]
                if "Strength" in nr:
                    self.cind["signal"] = min(5, round(int(_v(nr["Strength"])) / 20))
                self.cind["service"] = 1 if _v(nr.get("Status")) in ("registered", "roaming") else 0
                self.operator = _v(nr.get("Name"))
            if "org.ofono.VoiceCallManager" in ifaces:
                calls = (await call(self.bus, "org.ofono", path, "org.ofono.VoiceCallManager", "GetCalls"))[0]
                if calls:
                    _, cp = calls[0]
                    st = _v(cp.get("State"))
                    self.call = {"state": {"incoming": "incoming", "waiting": "incoming", "dialing": "outgoing",
                                           "alerting": "outgoing"}.get(st, "active" if st == "active" else st),
                                 "name": _v(cp.get("Name")) or None, "number": _v(cp.get("LineIdentification"))}
            break

    def _log_changes(self):
        now = {"iphone": (self.iphone or {}).get("mac"), "bike": (self.bike or {}).get("mac"),
               "call": (self.call or {}).get("state"), "playing": (self.media or {}).get("status"),
               "title": (self.media or {}).get("title")}
        for k, v in now.items():
            if self._last.get(k) != v:
                self.log("bt_state", item=k, value=v)
        self._last = now


async def connect_paired_phones(log):
    """Connects paired phones (HFP AG) that are not connected: needed when the radio is not listening (pro variant)."""
    bus = await system_bus()
    try:
        objs = (await call(bus, "org.bluez", "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects"))[0]
        for path, ifaces in objs.items():
            dev = ifaces.get("org.bluez.Device1")
            if dev and _v(dev.get("Paired")) and not _v(dev.get("Connected")) and UUID_HFP_AG in (_v(dev.get("UUIDs")) or []):
                try:
                    # hands-free only: Connect of all profiles towards an iPhone in CarPlay hangs (29 Oct 2026)
                    await asyncio.wait_for(call(bus, "org.bluez", path, "org.bluez.Device1", "ConnectProfile",
                                                "s", [UUID_HFP_AG]), 15)
                    log("iphone_reconnected", device=path.rsplit("/", 1)[-1])
                except Exception as e:
                    if isinstance(e, asyncio.TimeoutError):   # no half-open connections
                        try:
                            await asyncio.wait_for(call(bus, "org.bluez", path, "org.bluez.Device1", "Disconnect"), 5)
                        except Exception:
                            pass
                    log("iphone_reconnect_failed", error=(str(e) or type(e).__name__)[:100])
    finally:
        bus.disconnect()


UUID_A2DP_SOURCE = "0000110a-0000-1000-8000-00805f9b34fb"
RECONNECT_STEPS = (10, 20, 40, 80, 120)     # seconds between a failed attempt and the next one, per device


def _is_bike(dev, bike_addr):
    """The bike as mtspi-wifi-mode (system/wifi-mode.sh) looks for it: BIKE_ADDR, otherwise the name DUCATI…"""
    if bike_addr:
        return str(_v(dev.get("Address"))).upper() == bike_addr
    return str(_v(dev.get("Name")) or "").upper().startswith("DUCATI")


async def keep_connected(log, in_call=lambda: False, headsets=False):
    """Standard variant: after a Pi reboot the iPhone does not come back on its own, nor does the helmet even if it was
    already on (29 Oct 2026): the Pi calls them back until they are connected.
    - iPhone (paired with HFP AG): only ConnectProfile hands-free (oFono HFP HF): Connect of all profiles
      towards an iPhone in CarPlay hangs and leaves a half-open connection that clogs the radio (29 Oct 2026: helmet
      A2DP at 50-100 kbps); phonebook and SMS (PBAP/MAP) are opened by bt_real as soon as it sees it connected;
    - helmet (A2DP sink or HFP HF, not the bike) only with headsets=True (bench, MODE_LOCK=home) and no bike connected:
      on the bike the radio belongs to the bike, mtspi-wifi-mode connects it, and the helmet is paired with the bike.
    Direct Connect to the paired address (no scan), one at a time, increasing wait RECONNECT_STEPS per
    device; never during a call and, with music playing on that radio, at most one every 2 minutes
    (an attempt towards a device that is not there occupies the radio for ~5 s: stuttering music, 24 Oct 2026).
    At startup it waits for PipeWire to have registered A2DP: before that Connect fails ("Protocol not available")."""
    bus = await system_bus()
    bike_addr = os.environ.get("BIKE_ADDR", "").upper()
    started = time.monotonic()
    plan = {}            # path → [next attempt (monotonic), failed attempts]
    seen = set()         # already connected once: after a disconnection retry in 30 s, not immediately
    last_try = 0.0
    try:
        while True:
            await asyncio.sleep(5)
            objs = (await call(bus, "org.bluez", "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects"))[0]
            radios = {p: set(_v(i["org.bluez.Adapter1"].get("UUIDs")) or []) for p, i in objs.items()
                      if "org.bluez.Adapter1" in i}
            own = {_v(i["org.bluez.Adapter1"].get("Address")) for i in objs.values() if "org.bluez.Adapter1" in i}
            playing = {p.split("/dev_")[0] for p, i in objs.items()
                       if _v(i.get("org.bluez.MediaTransport1", {}).get("State")) == "active"}
            devs = {p: i["org.bluez.Device1"] for p, i in objs.items() if "org.bluez.Device1" in i}
            bike_on = any(_v(d.get("Connected")) and _is_bike(d, bike_addr) for d in devs.values())
            now = time.monotonic()
            for path, d in devs.items():
                uuids = set(_v(d.get("UUIDs")) or [])
                phone = UUID_HFP_AG in uuids
                headset = headsets and not phone and not bike_on and not _is_bike(d, bike_addr) \
                    and bool(uuids & {UUID_HFP_HF, UUID_A2DP_SINK})
                if _v(d.get("Connected")) or not _v(d.get("Paired")) or _v(d.get("Address")) in own \
                        or not (phone or headset):
                    if _v(d.get("Connected")):
                        seen.add(path)
                    plan.pop(path, None)
                    continue
                radio = _v(d.get("Adapter"))
                if not {UUID_A2DP_SOURCE, UUID_A2DP_SINK} <= radios.get(radio, set()) and now - started < 90:
                    continue                                 # PipeWire not ready yet (max 90 s, then try anyway)
                p = plan.setdefault(path, [now + (30 if path in seen else 0), 0])
                # with music playing: the first 3 attempts 30 s apart (after startup the first one often finds the radio
                # busy: 'br-connection-busy', and with 2 minutes the iPhone came back after 2.5 min, 29 Oct 2026), then one every 2 minutes
                gap = 30 if p[1] < 3 and path not in seen else 120
                if now < p[0] or in_call() or (radio in playing and p[1] > 0 and now - last_try < gap):
                    continue
                name = _v(d.get("Alias")) or _v(d.get("Name"))
                kind = "iphone" if phone else "headset"
                last_try = now
                try:
                    if phone:
                        await asyncio.wait_for(call(bus, "org.bluez", path, "org.bluez.Device1", "ConnectProfile",
                                                    "s", [UUID_HFP_AG]), 15)
                    else:
                        await asyncio.wait_for(call(bus, "org.bluez", path, "org.bluez.Device1", "Connect"), 20)
                    log("bt_reconnected", device=path.rsplit("/", 1)[-1], name=name, kind=kind, tries=p[1] + 1)
                    plan.pop(path, None)
                    seen.add(path)
                except Exception as e:
                    # no half-open connections: if the attempt timed out, disconnect (the pairing stays); not with
                    # other errors (InProgress, busy…), it could be a good connection in progress
                    if isinstance(e, asyncio.TimeoutError):
                        try:
                            await asyncio.wait_for(call(bus, "org.bluez", path, "org.bluez.Device1", "Disconnect"), 5)
                        except Exception:
                            pass
                    wait = RECONNECT_STEPS[min(p[1], len(RECONNECT_STEPS) - 1)]
                    p[:] = [time.monotonic() + wait, p[1] + 1]
                    log("bt_reconnect_failed", device=path.rsplit("/", 1)[-1], name=name, kind=kind,
                        error=(str(e) or type(e).__name__)[:100], retry_s=wait)
                break                                        # one attempt per round: then the state is re-read
    finally:
        bus.disconnect()


# ------------------------------------------------------------------ call audio
async def route_call_audio(iphone_mac, bike_mac, log):
    """Links in PipeWire iPhone voice → bike and bike microphone → iPhone (bluez_* nodes of the two addresses)."""
    a, b = iphone_mac.replace(":", "_"), bike_mac.replace(":", "_")

    async def ports(flag):
        p = await asyncio.create_subprocess_exec("pw-link", flag, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.DEVNULL)
        out, _ = await p.communicate()
        return out.decode().split()

    outs, ins = await ports("-o"), await ports("-i")
    pairs = []
    for src_mac, dst_mac in ((a, b), (b, a)):
        srcs = [o for o in outs if src_mac in o and o.startswith("bluez_input")]
        dsts = [i for i in ins if dst_mac in i and i.startswith("bluez_output")]
        for s_, d_ in zip(sorted(srcs), sorted(dsts)):
            pairs.append((s_, d_))
        if srcs and len(dsts) > len(srcs):             # mono → stereo
            pairs += [(srcs[0], d_) for d_ in sorted(dsts)[len(srcs):]]
    for s_, d_ in pairs:
        p = await asyncio.create_subprocess_exec("pw-link", s_, d_, stdout=asyncio.subprocess.DEVNULL,
                                                 stderr=asyncio.subprocess.DEVNULL)
        await p.wait()
    log("call_audio_routed", links=len(pairs), outs=len(outs), ins=len(ins))
    return len(pairs)


# ------------------------------------------------------------------ phonebook from the iPhone (PBAP)
PULL_FIELDS = ["VERSION", "FN", "N", "TEL", "X-IRMC-CALL-DATETIME"]


async def pull_phonebook(mac, log, folders=("pb", "cch"), timeout=180, source=None):
    """Downloads phonebook and recent calls from the iPhone as vCard (obexd on the session bus). Returns {folder: text}."""
    if source and source.startswith("hci"):           # radio name → address (obexd Source)
        try:
            sbus = await system_bus()
            source = _v((await call(sbus, "org.bluez", f"/org/bluez/{source}", "org.freedesktop.DBus.Properties",
                                    "Get", "ss", ["org.bluez.Adapter1", "Address"]))[0])
            sbus.disconnect()
        except Exception:
            source = None
    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    out = {}
    try:
        sess = (await call(bus, "org.bluez.obex", "/org/bluez/obex", "org.bluez.obex.Client1", "CreateSession",
                           "sa{sv}", [mac, {"Target": Variant("s", "PBAP"),
                                            # without Source obexd may start from the bike's radio and request
                                            # a pairing with the iPhone from there (a "ghost" iPhone on one of the Pi's radios)
                                            **({"Source": Variant("s", source)} if source else {})}]))[0]
        for folder in folders:
            await call(bus, "org.bluez.obex", sess, "org.bluez.obex.PhonebookAccess1", "Select", "ss", ["int", folder])
            # only the fields the bike needs: with photos and notes the phonebook weighed 4.4 MB and the transfer
            # was read half-way when the wait expired (418 cards out of 644, 24 Oct 2026)
            tpath, props = await call(bus, "org.bluez.obex", sess, "org.bluez.obex.PhonebookAccess1", "PullAll",
                                      "sa{sv}", ["", {"Format": Variant("s", "vcard30"),
                                                      "Fields": Variant("as", PULL_FIELDS)}])
            fname = _v(props.get("Filename"))
            t0 = time.monotonic()
            while time.monotonic() - t0 < timeout:
                try:
                    st = (await call(bus, "org.bluez.obex", tpath, "org.freedesktop.DBus.Properties", "Get",
                                     "ss", ["org.bluez.obex.Transfer1", "Status"]))[0]
                    st = _v(st)
                except RuntimeError:
                    st = "complete"                    # a finished transfer disappears from the bus
                if st in ("complete", "error"):
                    break
                await asyncio.sleep(0.3)
            if st != "complete":
                log("pbap_incomplete", folder=folder, status=st)
                continue
            if fname and os.path.exists(fname):
                with open(fname, encoding="utf-8", errors="replace") as f:
                    out[folder] = f.read()
                os.remove(fname)
            log("pbap_pulled", folder=folder, cards=out.get(folder, "").count("BEGIN:VCARD"))
        await call(bus, "org.bluez.obex", "/org/bluez/obex", "org.bluez.obex.Client1", "RemoveSession", "o", [sess])
    finally:
        bus.disconnect()
    return out


def parse_vcards(text, limit=2000):
    """Names and numbers from the vCards (enough for diagnostics and for serving them to the bike)."""
    cards, cur = [], None
    for line in text.splitlines():
        if line.startswith("BEGIN:VCARD"):
            cur = {"name": None, "numbers": []}
        elif line.startswith("END:VCARD") and cur is not None:
            cards.append(cur)
            cur = None
            if len(cards) >= limit:
                break
        elif cur is not None:
            key, _, val = line.partition(":")
            if key.split(";")[0] == "FN":
                cur["name"] = val
            elif key.split(";")[0] == "TEL":
                cur["numbers"].append(val)
    return cards
