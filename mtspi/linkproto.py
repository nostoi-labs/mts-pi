"""Emulated links of the test bench (they replace USB and Bluetooth audio, which are not available on the Mac).

Two links, same format:
- "carlink": emulated phone (iPhone + Carlinkit) <-> Pi. On the real Pi this will be the Carlinkit over USB.
- "bikeaudio": Pi <-> emulated dashboard. On the real bike this will be Bluetooth: A2DP + AVRCP (music) and HFP (calls).

Frame: u8 type + u32 length (little-endian) + data.
"""
from __future__ import annotations

import asyncio
import json
import struct

JSON = 1        # control message (JSON dict)
VIDEO = 2       # H.264 Annex-B, one access unit (video frame) per link frame
MUSIC = 3       # PCM s16le 44100 Hz stereo
VOICE = 4       # PCM s16le 16000 Hz mono, to the helmet (caller's voice)
MIC = 5         # PCM s16le 16000 Hz mono, from the helmet (microphone)

MUSIC_RATE, MUSIC_CH = 44100, 2
VOICE_RATE = 16000


class Link:
    MUSIC_BACKLOG = MUSIC_RATE * MUSIC_CH * 2 // 2     # 0.5 s of music not yet sent: anything beyond is dropped

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, name: str = ""):
        self.reader, self.writer, self.name = reader, writer, name
        self._lock = asyncio.Lock()
        self.music_dropped = 0

    async def send(self, kind: int, data: bytes) -> None:
        frame = struct.pack("<BI", kind, len(data)) + data
        if kind == MUSIC:
            # music never waits (it is sent by the Carlinkit loop, with ringing and video queued behind it) and never
            # makes anything wait: if the other end is slow it is dropped instead of queued
            t = self.writer.transport
            if t.is_closing() or t.get_write_buffer_size() > self.MUSIC_BACKLOG:
                self.music_dropped += 1
            else:
                self.writer.write(frame)
            return
        if kind == JSON:
            # commands (ring, callsetup…) go straight into the buffer, not queued on the lock behind other frames:
            # write is synchronous and writes the whole frame, so it does not interleave with the others
            self.writer.write(frame)
            frame = None
        async with self._lock:
            if frame is not None:
                self.writer.write(frame)
            await self.writer.drain()

    async def send_json(self, obj: dict) -> None:
        await self.send(JSON, json.dumps(obj, separators=(",", ":")).encode())

    async def recv(self):
        """(type, data), or (None, None) if the connection is closed. JSON messages are returned already as dicts."""
        try:
            head = await self.reader.readexactly(5)
        except (asyncio.IncompleteReadError, ConnectionError):
            return None, None
        kind, length = struct.unpack("<BI", head)
        data = await self.reader.readexactly(length) if length else b""
        return kind, (json.loads(data) if kind == JSON else data)

    def close(self) -> None:
        try:
            self.writer.close()
        except Exception:
            pass


async def connect(host: str, port: int, name: str, retry_s: float = 60.0) -> Link:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + retry_s
    while True:
        try:
            r, w = await asyncio.open_connection(host, port)
            return Link(r, w, name)
        except OSError:
            if loop.time() > deadline:
                raise
            await asyncio.sleep(0.3)


# Timestamp marker in the image: 24 black/white blocks holding the milliseconds (mod 2^24) of the moment
# the phone draws the frame. The dashboard reads it back and computes the phone -> screen latency
# (same clock: works only with all programs on the same computer).
STAMP_BITS, STAMP_BLOCK = 24, 6


def draw_stamp(img, ms: int) -> None:
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    v = ms % (1 << STAMP_BITS)
    for i in range(STAMP_BITS):
        bit = (v >> (STAMP_BITS - 1 - i)) & 1
        x = i * STAMP_BLOCK
        d.rectangle([x, 0, x + STAMP_BLOCK - 1, STAMP_BLOCK - 1], fill=(255, 255, 255) if bit else (0, 0, 0))


def read_stamp(img):
    """Reads the marker back from an image at the same scale (None if unreadable)."""
    px = img.convert("L").load()
    v = 0
    for i in range(STAMP_BITS):
        x = i * STAMP_BLOCK + STAMP_BLOCK // 2
        v = (v << 1) | (1 if px[x, STAMP_BLOCK // 2] > 127 else 0)
    return v


def stamp_age_ms(stamp: int, now_ms: int) -> int:
    return (now_ms - stamp) % (1 << STAMP_BITS)
