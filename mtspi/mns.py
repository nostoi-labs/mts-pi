"""MAP MNS server (Message Notification Server) towards the iPhone, and message reading with obexd (MAP client).

Why it is needed: the iPhone exposes messages (MAP) only if "Show Notifications" is enabled in the device's
Bluetooth settings, and that switch appears only if the accessory publishes the MNS service (0x1133).
The obexd of Raspberry Pi OS (BlueZ 5.82) does not register its servers with BlueZ, so we publish the record ourselves
with an RFCOMM profile and answer the minimal OBEX (Connect, Put of event reports, Disconnect).
The actual messages are read by obexd as a client (CreateSession Target=map → MessageAccess1.ListMessages).
"""
import asyncio
import os
import re
import socket
import time

from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus

from mtspi.bluez import RfcommServer, _v, call, system_bus
from mtspi.tasks import spawn

MNS_UUID = "00001133-0000-1000-8000-00805f9b34fb"
MNS_TARGET = bytes.fromhex("bb582b41420c11dbb0de0800200c9a66")
MNS_CHANNEL = 17


def _hdr_bytes(hid, data):
    return bytes([hid]) + (len(data) + 3).to_bytes(2, "big") + data


async def _read_packet(r):
    head = await r.readexactly(3)
    n = int.from_bytes(head[1:3], "big")
    return head[0], head + await r.readexactly(n - 3)


def _headers(pkt, start):
    """OBEX headers from start: {id: value} (body and end-of-body concatenated under 0x48)."""
    out, i = {}, start
    while i < len(pkt):
        hid = pkt[i]
        kind = hid & 0xC0
        if kind in (0x00, 0x40):                    # unicode / byte sequence: 2-byte length
            n = int.from_bytes(pkt[i + 1:i + 3], "big")
            val = pkt[i + 3:i + n]
            i += n
        elif kind == 0x80:                          # 1 byte
            val = pkt[i + 1:i + 2]
            i += 2
        else:                                       # 4 byte
            val = pkt[i + 1:i + 5]
            i += 5
        if hid in (0x48, 0x49):
            out[0x48] = out.get(0x48, b"") + val
        else:
            out[hid] = val
    return out


async def serve_mns(log, on_event):
    """Publishes the MNS and accepts the iPhone's connections; on_event(dict) for every event report."""
    server = RfcommServer(log, path="/ducati/mns", uuid=MNS_UUID, name="MAP MNS", channel=MNS_CHANNEL, event="mns")
    await server.start()
    while True:
        device, sock = await server.queue.get()
        log("mns_connected", peer=device.rsplit("/", 1)[-1], l2cap=sock.type == socket.SOCK_SEQPACKET)
        if sock.type == socket.SOCK_SEQPACKET:
            # the iPhone uses OBEX over L2CAP (GoepL2capPsm in BlueZ's record): one OBEX packet per read
            spawn(_session(_SeqReader(sock), _SeqWriter(sock), log, on_event))
        else:
            r, w = await asyncio.open_connection(sock=sock)
            spawn(_session(r, w, log, on_event))


class _SeqReader:
    def __init__(self, sock):
        self.sock, self.buf = sock, b""

    async def readexactly(self, n):
        loop = asyncio.get_running_loop()
        while len(self.buf) < n:
            chunk = await loop.sock_recv(self.sock, 65536)
            if not chunk:
                raise asyncio.IncompleteReadError(self.buf, n)
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


class _SeqWriter:
    def __init__(self, sock):
        self.sock, self.pending = sock, []

    def write(self, data):
        self.pending.append(data)

    async def drain(self):
        loop = asyncio.get_running_loop()
        while self.pending:
            await loop.sock_sendall(self.sock, self.pending.pop(0))

    def close(self):
        self.sock.close()


async def _session(r, w, log, on_event):
    body = b""
    try:
        while True:
            op, pkt = await _read_packet(r)
            if op == 0x80:                          # Connect: version, flags, max packet + Who + Connection ID
                resp = bytes([0x10, 0x00]) + (0x2000).to_bytes(2, "big")
                resp += _hdr_bytes(0x4A, MNS_TARGET) + bytes([0xCB]) + (1).to_bytes(4, "big")
                w.write(bytes([0xA0]) + (len(resp) + 3).to_bytes(2, "big") + resp)
            elif op in (0x02, 0x82):                # Put (event report in XML, possibly in several chunks)
                body += _headers(pkt, 3).get(0x48, b"")
                if op == 0x82:
                    for ev in re.finditer(rb"<event\s+([^>]*)/?>", body):
                        attrs = dict(re.findall(rb'(\w+)\s*=\s*"([^"]*)"', ev.group(1)))
                        info = {k.decode(): v.decode("utf-8", "replace") for k, v in attrs.items()}
                        log("mns_event", **{k: info.get(k) for k in ("type", "handle", "folder", "msg_type")})
                        on_event(info)
                    body = b""
                w.write(bytes([0xA0 if op == 0x82 else 0x90, 0x00, 0x03]))
            elif op == 0x81:                        # Disconnect
                w.write(bytes([0xA0, 0x00, 0x03]))
                await w.drain()
                break
            else:
                w.write(bytes([0xD1, 0x00, 0x03]))  # not implemented
            await w.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        w.close()


async def pull_messages(mac, log, source=None, count=20):
    """Latest messages in the iPhone's inbox: [{"from", "number", "time", "text", "read"}]."""
    if source and source.startswith("hci"):
        sbus = await system_bus()
        source = _v((await call(sbus, "org.bluez", f"/org/bluez/{source}", "org.freedesktop.DBus.Properties",
                                "Get", "ss", ["org.bluez.Adapter1", "Address"]))[0])
        sbus.disconnect()
    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    out = []
    try:
        sess = (await call(bus, "org.bluez.obex", "/org/bluez/obex", "org.bluez.obex.Client1", "CreateSession",
                           "sa{sv}", [mac, {"Target": Variant("s", "map"),
                                            **({"Source": Variant("s", source)} if source else {})}]))[0]
        try:
            M = "org.bluez.obex.MessageAccess1"
            for f in ("telecom", "msg"):
                await call(bus, "org.bluez.obex", sess, M, "SetFolder", "s", [f])
            msgs = (await call(bus, "org.bluez.obex", sess, M, "ListMessages", "sa{sv}",
                               ["inbox", {"MaxCount": Variant("q", count)}]))[0]
            items = msgs.items() if isinstance(msgs, dict) else msgs     # a{oa{sv}}: dbus_fast returns a dict
            items = sorted(items, key=lambda kv: str(_v(kv[1].get("Timestamp")) or ""), reverse=True)
            for _, props in items:
                p = {k: _v(v) for k, v in props.items()}
                ts = str(p.get("Timestamp") or "")
                out.append({"from": p.get("Sender") or p.get("SenderAddress") or "?", "number": p.get("SenderAddress"),
                            "time": f"{ts[9:11]}:{ts[11:13]}" if len(ts) >= 13 else ts,
                            "text": p.get("Subject") or "", "read": bool(p.get("Read"))})
        finally:
            await call(bus, "org.bluez.obex", "/org/bluez/obex", "org.bluez.obex.Client1", "RemoveSession", "o", [sess])
    finally:
        bus.disconnect()
    log("map_pulled", messages=len(out))
    return out
