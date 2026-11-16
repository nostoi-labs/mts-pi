"""The Pi's phonebook for the bike: PBAP server (Phone Book Access, phone side) over OBEX.

On every connection the bike (obexd 0.48 client side, BlueZ 5.42 in the ALPS module) looks for the
"Phonebook Access Server" service (0x112F): if it is missing, the dashboard's Phone button stays greyed out (24 Oct 2026).
Here the Pi answers with the phonebook and recent calls it has downloaded from the iPhone (bluez.pull_phonebook).

- Phonebook: folders pb (0.vcf = owner's card, as the iPhone sends it), ich/och/mch/cch
  (received, dialed, missed, all: from the iPhone's recent calls); vCard 2.1 or 3.0 on request.
- ObexPbapServer: one OBEX connection (CONNECT with PBAP Target, SETPATH, chunked GET, DISCONNECT, ABORT)
  over any asyncio reader/writer pair: real RFCOMM on the Pi, TCP on the bench.
- serve_bluez: registers the profile with BlueZ (RFCOMM channel 19, PBAP 1.1 SDP record) and serves every connection.

No personal data in the logs: only folders, counts and sizes.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import struct
from typing import Optional
from mtspi.tasks import spawn

PBAP_PSE_UUID = "0000112f-0000-1000-8000-00805f9b34fb"
PBAP_TARGET = bytes.fromhex("796135f0f0c511d809660800200c9a66")
PBAP_CHANNEL = 19

# OBEX operations and responses
OP_CONNECT, OP_DISCONNECT, OP_PUT, OP_GET, OP_SETPATH, OP_ABORT = 0x80, 0x81, 0x02, 0x03, 0x85, 0xFF
FINAL = 0x80
R_CONTINUE, R_OK, R_BAD_REQUEST, R_FORBIDDEN, R_NOT_FOUND, R_NOT_IMPLEMENTED = 0x90, 0xA0, 0xC0, 0xC3, 0xC4, 0xD1
# headers
H_NAME, H_TYPE, H_BODY, H_END_BODY, H_TARGET, H_WHO, H_APP, H_CONN_ID = 0x01, 0x42, 0x48, 0x49, 0x46, 0x4A, 0x4C, 0xCB
# PBAP application parameters
A_ORDER, A_SEARCH_VALUE, A_SEARCH_ATTR, A_MAX_COUNT, A_OFFSET, A_FILTER, A_FORMAT, A_SIZE, A_NEW_MISSED = range(1, 10)

FOLDERS = ("pb", "ich", "och", "mch", "cch")
CALL_FOLDER = {"RECEIVED": "ich", "DIALED": "och", "MISSED": "mch"}
MY_MAX_PACKET = 0x2000

SDP_RECORD = f"""<?xml version="1.0" encoding="UTF-8" ?>
<record>
  <attribute id="0x0001"><sequence><uuid value="0x112f"/></sequence></attribute>
  <attribute id="0x0004"><sequence>
    <sequence><uuid value="0x0100"/></sequence>
    <sequence><uuid value="0x0003"/><uint8 value="0x{PBAP_CHANNEL:02x}"/></sequence>
    <sequence><uuid value="0x0008"/></sequence>
  </sequence></attribute>
  <attribute id="0x0005"><sequence><uuid value="0x1002"/></sequence></attribute>
  <attribute id="0x0009"><sequence><sequence><uuid value="0x1130"/><uint16 value="0x0101"/></sequence></sequence></attribute>
  <attribute id="0x0100"><text value="Phonebook"/></attribute>
  <attribute id="0x0314"><uint8 value="0x01"/></attribute>
</record>
"""


# --------------------------------------------------------------------------- vCard
def parse_cards(text: str) -> list:
    """The iPhone's vCards reduced to what the bike needs: N, FN, numbers (with type) and call date/type."""
    lines, cards, cur = [], [], None
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:            # folded line
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    for line in lines:
        key, sep, val = line.partition(":")
        if not sep:
            continue
        name, *params = key.split(";")
        name = name.upper().split(".")[-1]              # "item1.TEL" → TEL
        if name == "BEGIN" and val.strip().upper() == "VCARD":
            cur = {"n": None, "fn": None, "tels": [], "call": None}
        elif name == "END" and cur is not None:
            cards.append(cur)
            cur = None
        elif cur is None:
            continue
        elif name == "N":
            cur["n"] = val
        elif name == "FN":
            cur["fn"] = val
        elif name == "TEL":
            types = [t for p in params for t in re.split(r"[=,]", p.upper()) if t and t != "TYPE"]
            cur["tels"].append([[t for t in types if t not in ("PREF", "VOICE")][:1], val])
        elif name == "X-IRMC-CALL-DATETIME":
            kind = next((t for p in params for t in re.split(r"[=,]", p.upper()) if t in CALL_FOLDER), "")
            cur["call"] = [kind, val]
    return cards


def render_card(card: dict, fmt: int) -> str:
    """vCard 2.1 (fmt 0) or 3.0 (fmt 1) with the mandatory PBAP properties: N (+ FN in 3.0), TEL."""
    v3 = fmt == 1
    out = ["BEGIN:VCARD", "VERSION:3.0" if v3 else "VERSION:2.1"]
    n = card.get("n") or ""
    fn = card.get("fn") or ""
    if not n and fn:
        n = fn
    charset = "" if v3 or (n + fn).isascii() else ";CHARSET=UTF-8"
    out.append(f"N{charset}:{n}")
    if v3 or fn:
        out.append(f"FN{charset}:{fn}")
    for types, number in card.get("tels") or []:
        t = types[0] if types else "VOICE"
        out.append(f"TEL;TYPE={t}:{number}" if v3 else f"TEL;{t}:{number}")
    if card.get("call"):
        kind, when = card["call"]
        p = (f";TYPE={kind}" if v3 else f";{kind}") if kind else ""
        out.append(f"X-IRMC-CALL-DATETIME{p}:{when}")
    out.append("END:VCARD")
    return "\r\n".join(out) + "\r\n"


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _xml_attr(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


class Phonebook:
    """The folders the bike can read. pb starts at 0.vcf (owner), the call folders at 1.vcf."""

    def __init__(self):
        self.folders = {f: [] for f in FOLDERS}
        self.books: dict = {}

    def update(self, books: dict) -> None:
        self.books = {k: v for k, v in books.items() if isinstance(v, str)}
        self.folders = {f: [] for f in FOLDERS}
        for f in FOLDERS:
            if books.get(f):
                self.folders[f] = parse_cards(books[f])
        for card in self.folders["cch"]:                # received/dialed/missed from the recent calls, if not downloaded separately
            f = CALL_FOLDER.get((card.get("call") or [""])[0])
            if f and not books.get(f):
                self.folders[f].append(card)

    def update_simple(self, contacts: list, recent: list) -> None:
        """From the bench's emulated phone: [{"name", "number"}] and [{"name", "number", "kind"}]."""
        kinds = {"received": "RECEIVED", "dialed": "DIALED", "missed": "MISSED"}
        card = lambda c, call=None: {"n": c.get("name") or "", "fn": c.get("name") or "",
                                     "tels": [[["CELL"], c.get("number") or ""]], "call": call}
        self.books = {}
        self.folders = {f: [] for f in FOLDERS}
        self.folders["pb"] = [card({"name": "", "number": ""})] + [card(c) for c in contacts]
        for c in recent:
            kind = kinds.get(c.get("kind"), "")
            self.folders["cch"].append(card(c, [kind, ""]))
            if kind:
                self.folders[CALL_FOLDER[kind]].append(card(c, [kind, ""]))

    def base(self, folder: str) -> int:
        return 0 if folder == "pb" else 1

    def load(self, path: str) -> bool:
        try:
            with open(path, encoding="utf-8") as f:
                self.update(json.load(f))
            return True
        except (OSError, ValueError):
            return False

    def save(self, path: str) -> None:
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.books, f)
        os.replace(tmp, path)

    def search(self, folder: str, attr: int, value: str) -> list:
        """Matching (handle, card) pairs: attr 0 name, 1 number (last 9 digits: with or without +39 is the same)."""
        cards = self.folders.get(folder, [])
        b = self.base(folder)
        items = list(enumerate(cards, b))
        if not value:
            return items
        if attr == 1:
            want = _digits(value)[-9:]
            return [(h, c) for h, c in items if want and any(_digits(n).endswith(want) for _, n in c["tels"])]
        v = value.lower()
        return [(h, c) for h, c in items if v in (c.get("n") or "").lower() or v in (c.get("fn") or "").lower()]


# --------------------------------------------------------------------------- OBEX
def _hdr(hi: int, value) -> bytes:
    kind = hi >> 6
    if kind == 0:                                       # unicode text, null-terminated
        data = (value + "\0").encode("utf-16-be") if value is not None else b""
        return bytes([hi]) + struct.pack(">H", 3 + len(data)) + data
    if kind == 1:
        return bytes([hi]) + struct.pack(">H", 3 + len(value)) + value
    if kind == 2:
        return bytes([hi, value & 0xFF])
    return bytes([hi]) + struct.pack(">I", value)


def parse_headers(data: bytes) -> dict:
    out, i = {}, 0
    while i < len(data):
        hi = data[i]
        kind = hi >> 6
        if kind in (0, 1):
            ln = struct.unpack(">H", data[i + 1:i + 3])[0]
            if ln < 3:
                raise ValueError("bad header length")
            raw = data[i + 3:i + ln]
            if kind == 0:
                val = raw.decode("utf-16-be").rstrip("\0") if raw else ""
            else:
                val = raw
            i += ln
        elif kind == 2:
            val, i = data[i + 1], i + 2
        else:
            val, i = struct.unpack(">I", data[i + 1:i + 5])[0], i + 5
        if hi in (H_BODY, H_END_BODY) and hi in out:
            out[hi] += val
        else:
            out[hi] = val
    return out


def parse_app_params(data: bytes) -> dict:
    out, i = {}, 0
    while i + 2 <= len(data):
        tag, ln = data[i], data[i + 1]
        val = data[i + 2:i + 2 + ln]
        out[tag] = val.decode("utf-8", "replace").rstrip("\0") if tag == A_SEARCH_VALUE else int.from_bytes(val, "big")
        i += 2 + ln
    return out


def app_params(**kw) -> bytes:
    out = b""
    if "size" in kw:
        out += bytes([A_SIZE, 2]) + struct.pack(">H", kw["size"])
    if "new_missed" in kw:
        out += bytes([A_NEW_MISSED, 1, kw["new_missed"] & 0xFF])
    return out


async def read_packet(reader) -> tuple:
    head = await reader.readexactly(3)
    op, ln = head[0], struct.unpack(">H", head[1:3])[0]
    if ln < 3:
        raise ValueError("bad packet length")
    return op, await reader.readexactly(ln - 3)


def packet(code: int, payload: bytes = b"") -> bytes:
    return bytes([code]) + struct.pack(">H", 3 + len(payload)) + payload


class ObexPbapServer:
    """One OBEX connection with the bike. Current path as a list ("telecom", "pb")."""
    CONN_ID = 1

    def __init__(self, book: Phonebook, log, peer: str = ""):
        self.book, self.log, self.peer = book, log, peer
        self.path: list = []
        self.peer_max = 255
        self.connected = False
        self.get_headers: dict = {}
        self.pending: Optional[bytes] = None       # body of the GET response still to be sent
        self.pending_app = b""

    async def serve(self, reader, writer) -> None:
        try:
            while True:
                op, data = await read_packet(reader)
                writer.write(self.handle(op, data))
                await writer.drain()
                if op == OP_DISCONNECT:
                    self.connected = False
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    # --- one request → one response (testable without a socket)
    def handle(self, op: int, data: bytes) -> bytes:
        try:
            if op == OP_CONNECT:
                return self._connect(data)
            if op == OP_DISCONNECT:
                self.log("pbap_disconnect", peer=self.peer)
                return packet(R_OK)
            if op == OP_ABORT:
                self.pending, self.get_headers = None, {}
                return packet(R_OK)
            if op in (OP_SETPATH, OP_SETPATH & 0x7F):
                return self._setpath(data)
            if op & 0x7F == OP_GET:
                return self._get(op, data)
            self.log("pbap_unsupported", op=op)
            return packet(R_NOT_IMPLEMENTED)
        except (ValueError, IndexError, struct.error) as e:
            self.log("pbap_bad_request", op=op, error=str(e))
            self.pending, self.get_headers = None, {}
            return packet(R_BAD_REQUEST)

    def _connect(self, data: bytes) -> bytes:
        version, flags, maxpkt = data[0], data[1], struct.unpack(">H", data[2:4])[0]
        h = parse_headers(data[4:])
        if h.get(H_TARGET) != PBAP_TARGET:
            self.log("pbap_connect_refused", peer=self.peer, target=(h.get(H_TARGET) or b"").hex())
            return packet(R_FORBIDDEN, bytes([0x10, 0x00]) + struct.pack(">H", MY_MAX_PACKET))
        self.peer_max = max(255, min(maxpkt, MY_MAX_PACKET))
        self.connected, self.path = True, []
        self.log("pbap_connect", peer=self.peer, max_packet=maxpkt, version=version)
        return packet(R_OK, bytes([0x10, 0x00]) + struct.pack(">H", MY_MAX_PACKET)
                      + _hdr(H_CONN_ID, self.CONN_ID) + _hdr(H_WHO, PBAP_TARGET))

    def _setpath(self, data: bytes) -> bytes:
        flags = data[0]
        h = parse_headers(data[2:])
        name = h.get(H_NAME)
        new = list(self.path)
        if flags & 0x01:                                # go up
            if not new:
                return packet(R_NOT_FOUND)
            new.pop()
            if name:
                new.append(name)
        elif not name:                                  # empty or missing name: root
            new = []
        else:
            new.append(name)
        if not self._valid_dir(new):
            self.log("pbap_setpath_missing", path="/".join(new))
            return packet(R_NOT_FOUND)
        self.path = new
        self.log("pbap_setpath", path="/".join(new))
        return packet(R_OK)

    @staticmethod
    def _valid_dir(parts: list) -> bool:
        if parts[:1] == ["SIM1"]:
            parts = parts[1:]
        if not parts:
            return True
        if parts[0] != "telecom" or len(parts) > 2:
            return False
        return len(parts) == 1 or parts[1] in FOLDERS

    def _get(self, op: int, data: bytes) -> bytes:
        h = parse_headers(data)
        if self.pending is not None:                    # continuation of the previous response
            if H_TYPE not in h and H_NAME not in h:
                return self._next_chunk()
            self.pending = None                         # new request: the old one is dropped
        for k, v in h.items():
            self.get_headers[k] = v
        if not op & FINAL:                              # the request continues in the next packet
            return packet(R_CONTINUE)
        h, self.get_headers = self.get_headers, {}
        typ = (h.get(H_TYPE) or b"").rstrip(b"\0").decode("ascii", "replace")
        name = h.get(H_NAME) or ""
        ap = parse_app_params(h.get(H_APP) or b"")
        if typ == "x-bt/phonebook":
            code, body, app = self._pull_phonebook(name, ap)
        elif typ == "x-bt/vcard-listing":
            code, body, app = self._listing(name, ap)
        elif typ == "x-bt/vcard":
            code, body, app = self._entry(name, ap)
        else:
            self.log("pbap_unknown_type", type=typ)
            return packet(R_BAD_REQUEST)
        if code != R_OK:
            return packet(code)
        self.pending, self.pending_app = body, app
        return self._next_chunk()

    def _next_chunk(self) -> bytes:
        body, app = self.pending or b"", self.pending_app
        extra = _hdr(H_APP, app) if app else b""
        room = self.peer_max - 3 - len(extra) - 3
        if len(body) <= room:
            self.pending, self.pending_app = None, b""
            return packet(R_OK, extra + _hdr(H_END_BODY, body))
        self.pending, self.pending_app = body[room:], b""
        return packet(R_CONTINUE, extra + _hdr(H_BODY, body[:room]))

    def _folder_of(self, name: str, absolute: bool) -> Optional[str]:
        name = name[:-4] if name.endswith(".vcf") else name
        parts = [p for p in name.split("/") if p]
        if not absolute:
            parts = self.path + parts
        if parts[:1] == ["SIM1"]:
            return "" if len(parts) == 3 and parts[1] == "telecom" and parts[2] in FOLDERS else None
        if len(parts) == 2 and parts[0] == "telecom" and parts[1] in FOLDERS:
            return parts[1]
        if absolute and len(parts) == 1 and parts[0] in FOLDERS:       # relative name used as absolute
            return parts[0]
        return None

    def _window(self, items: list, ap: dict) -> list:
        off = ap.get(A_OFFSET, 0)
        n = ap.get(A_MAX_COUNT, 0xFFFF)
        return items[off:off + n]

    def _extra(self, folder: str, size: Optional[int] = None) -> dict:
        kw = {}
        if size is not None:
            kw["size"] = size
        if folder in ("mch", "cch"):
            kw["new_missed"] = 0
        return kw

    def _pull_phonebook(self, name: str, ap: dict):
        folder = self._folder_of(name, absolute=True)
        if folder is None:
            self.log("pbap_pull_missing", name=name)
            return R_NOT_FOUND, b"", b""
        cards = self.book.folders.get(folder, []) if folder else []
        if ap.get(A_MAX_COUNT, 0xFFFF) == 0:
            self.log("pbap_size", folder=folder or "SIM1", size=len(cards))
            return R_OK, b"", app_params(**self._extra(folder, len(cards)))
        fmt = ap.get(A_FORMAT, 0)
        sel = self._window(cards, ap)
        body = "".join(render_card(c, fmt) for c in sel).encode("utf-8")
        self.log("pbap_pull", folder=folder or "SIM1", cards=len(sel), bytes=len(body), format="3.0" if fmt == 1 else "2.1")
        return R_OK, body, app_params(**self._extra(folder))

    def _listing(self, name: str, ap: dict):
        folder = self._folder_of(name, absolute=False)
        if folder is None:
            self.log("pbap_listing_missing", name=name, path="/".join(self.path))
            return R_NOT_FOUND, b"", b""
        items = self.book.search(folder, ap.get(A_SEARCH_ATTR, 0), ap.get(A_SEARCH_VALUE, "")) if folder else []
        if ap.get(A_MAX_COUNT, 0xFFFF) == 0:
            self.log("pbap_listing_size", folder=folder or "SIM1", size=len(items))
            return R_OK, b"", app_params(**self._extra(folder, len(items)))
        if ap.get(A_ORDER, 0) == 1:                     # alphabetical
            items = sorted(items, key=lambda hc: (hc[1].get("n") or hc[1].get("fn") or "").lower())
        sel = self._window(items, ap)
        rows = "".join(f'<card handle="{h}.vcf" name="{_xml_attr(c.get("n") or c.get("fn") or "")}"/>\r\n' for h, c in sel)
        body = ('<?xml version="1.0"?>\r\n<!DOCTYPE vcard-listing SYSTEM "vcard-listing.dtd">\r\n'
                f'<vCard-listing version="1.0">\r\n{rows}</vCard-listing>\r\n').encode("utf-8")
        self.log("pbap_listing", folder=folder or "SIM1", cards=len(sel), search=bool(ap.get(A_SEARCH_VALUE)))
        return R_OK, body, app_params(**self._extra(folder))

    def _entry(self, name: str, ap: dict):
        m = re.fullmatch(r"(\d+)\.vcf", name or "")
        folder = self._folder_of("", absolute=False)
        if not m or not folder:
            self.log("pbap_entry_missing", name=name, path="/".join(self.path))
            return R_NOT_FOUND, b"", b""
        idx = int(m.group(1)) - self.book.base(folder)
        cards = self.book.folders.get(folder, [])
        if not 0 <= idx < len(cards):
            self.log("pbap_entry_missing", name=name, path="/".join(self.path))
            return R_NOT_FOUND, b"", b""
        self.log("pbap_entry", folder=folder)
        return R_OK, render_card(cards[idx], ap.get(A_FORMAT, 0)).encode("utf-8"), b""


# --------------------------------------------------------------------------- BlueZ
async def serve_bluez(book: Phonebook, log) -> None:
    """Registers the PBAP server with BlueZ and serves every connection (bike). Pairing required: the phonebook
    must not be readable by anyone passing by."""
    from mtspi.bluez import RfcommServer
    server = RfcommServer(log, path="/ducati/pbap", uuid=PBAP_PSE_UUID, name="Phonebook", channel=PBAP_CHANNEL,
                          record=SDP_RECORD, require_auth=True, event="pbap")
    while True:
        device, reader, writer = await server.accept_device()
        peer = device.rsplit("/", 1)[-1].replace("dev_", "").replace("_", ":")
        spawn(ObexPbapServer(book, log, peer).serve(reader, writer))


async def serve_tcp(book: Phonebook, log, port: int, host: str = "127.0.0.1"):
    """On the bench: the same server over TCP (the fake client plays the bike)."""
    async def on_conn(r, w):
        await ObexPbapServer(book, log, "tcp").serve(r, w)
    return await asyncio.start_server(on_conn, host, port)
