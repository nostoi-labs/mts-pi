#!/usr/bin/env python3
"""Flow control for the Pi's small serial port, when Bluetooth uses it (dtoverlay=miniuart-bt in config.txt).

Why Bluetooth is on the small port at all: on the usual one (PL011) the radio chip of the Pi 3 A+ stops with
"hci0: hardware error 0x00" while a voice link is open, at random, about 1.5 times every hundred calls or Siri
openings (bench, 22 Nov 2026). On the small port (mini UART) it did not.

Why this program: the mini UART has an 8-byte buffer and the Linux driver leaves its flow control off, so when the
processor is a little late bytes from the radio are lost ("Frame reassembly failed", overruns counted as "oe" in
/proc/tty/driver/serial: 2-4 a minute on the bench, 13 in one burst while the phone book came in). The port can
tell the radio to wait by itself (automatic RTS): one bit in its control register. With it on: no overruns.

The register goes back to the driver's value when the port is opened again (a restart of Bluetooth), so this
program looks every few seconds and puts the bit back. It does nothing on a Pi where Bluetooth is not on the
mini UART, and nothing on a model other than the Pi 3. Runs as root (it writes to /dev/mem).  --once: one look."""
import fcntl
import mmap
import os
import socket
import struct
import sys
import time

AUX_BLOCK = 0x215000             # the "aux" peripherals, inside the peripheral space; the mini UART lives here
CNTL = 0x60                      # its control register
DRIVER = 0x03                    # receiver and transmitter on: what the Linux driver writes
WANTED = 0x77                    # the same, plus automatic RTS (bit 2) that stops the radio with 4 places left in
                                 # the buffer (bits 4-5), RTS level inverted (bit 6). Without bit 6 the radio is held
                                 # off for good: tried on 22 Nov 2026, Bluetooth stopped answering at once.
HCIGETDEVINFO = 0x800448D3


def wanted_value(now):
    """What to write for the value read, or None to leave the register alone."""
    return WANTED if now == DRIVER else None


def is_pi3(model):
    return model.startswith("Raspberry Pi 3")


def on_mini_uart(path):
    """True if this is the device path of a Bluetooth radio on the mini UART (serial port at ...215040)."""
    return "215040.serial" in path


def bluetooth_up(dev=0):
    """True if hci<dev> exists and is switched on."""
    try:
        with socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI) as s:
            buf = bytearray(struct.pack("<H", dev) + bytes(90))
            fcntl.ioctl(s.fileno(), HCIGETDEVINFO, buf)
            return bool(struct.unpack_from("<I", buf, 16)[0] & 1)       # flags, bit 0 = up
    except OSError:
        return False


def peripherals_base():
    """Where the processor sees its peripherals (Pi 3: 0x3F000000), from the device tree."""
    with open("/proc/device-tree/soc/ranges", "rb") as f:
        return struct.unpack(">I", f.read(12)[4:8])[0]


def look(mem):
    if not on_mini_uart(os.path.realpath("/sys/class/bluetooth/hci0/device")) or not bluetooth_up():
        return None
    now = struct.unpack("<I", mem[CNTL:CNTL + 4])[0]
    new = wanted_value(now)
    if new is not None:
        mem[CNTL:CNTL + 4] = struct.pack("<I", new)
        print(f"uart-flow: mini UART control register 0x{now:02x} -> 0x{new:02x} (automatic RTS on)", flush=True)
    return now


def main():
    try:
        model = open("/proc/device-tree/model").read().strip("\0")
    except OSError:
        model = ""
    if not is_pi3(model):
        print(f"uart-flow: not a Raspberry Pi 3 ({model or 'unknown model'}): nothing to do", flush=True)
        return 0
    try:
        fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        mem = mmap.mmap(fd, 4096, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=peripherals_base() + AUX_BLOCK)
    except (OSError, ValueError, struct.error) as e:
        # with Bluetooth on the mini UART this means no flow control: say it plainly and let the unit show as failed
        print(f"uart-flow: the port's registers cannot be reached ({type(e).__name__}: {e}): flow control NOT switched on", flush=True)
        return 1
    if "--once" in sys.argv:
        now = look(mem)
        print("uart-flow: Bluetooth is not on the mini UART, or is off" if now is None else f"uart-flow: register was 0x{now:02x}")
        return 0
    odd = None
    while True:
        now = look(mem)
        if now is not None and now not in (DRIVER, WANTED) and now != odd:
            odd = now
            print(f"uart-flow: register is 0x{now:02x}, not a value this program knows: left alone", flush=True)
        time.sleep(3)


if __name__ == "__main__":
    sys.exit(main())
