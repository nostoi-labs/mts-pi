"""Tells systemd that the program is still working (the service's WatchdogSec).

Restart=always only covers a program that exits. One that is still there but stuck (its main loop blocked) is caught
by this: while the loop runs, a short message goes to systemd every few seconds; when the messages stop for
WatchdogSec, systemd ends the program and starts it again. Only the program restarts, never the Pi.
Without WatchdogSec in the unit (or outside systemd) this does nothing.

Two times are involved. WatchdogSec in the unit is generous, because it also has to cover the program's start (on a
slow card, during a busy first boot, loading the libraries takes a while). Once the loop runs, the program itself asks
systemd for the short time (ALIVE_S, default 30 s): from then on, silence for that long means stuck.
"""
import asyncio
import os
import socket

from mtspi.tasks import spawn


def start():
    """Call once from inside the running loop. Returns the task, or None when no watchdog was asked for."""
    addr = os.environ.get("NOTIFY_SOCKET")
    try:
        usec = int(os.environ.get("WATCHDOG_USEC") or 0)
    except ValueError:
        usec = 0
    pid = os.environ.get("WATCHDOG_PID")
    if not addr or usec <= 0 or (pid and pid != str(os.getpid())):
        return None
    if addr[0] == "@":                      # abstract socket
        addr = "\0" + addr[1:]
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.setblocking(False)

    try:
        short = float(os.environ.get("ALIVE_S", "30"))
    except ValueError:
        short = 30.0
    period = min(usec / 1e6, short) if short > 0 else usec / 1e6

    async def beat():
        every = max(1.0, period / 4)        # four chances per period
        try:
            sock.sendto(b"WATCHDOG=1", addr)
            if period < usec / 1e6:         # the loop is running: from now on the short time applies
                sock.sendto(b"WATCHDOG_USEC=%d" % int(period * 1e6), addr)
        except OSError:
            pass
        while True:
            try:
                sock.sendto(b"WATCHDOG=1", addr)
            except OSError:
                pass
            await asyncio.sleep(every)

    return spawn(beat())
