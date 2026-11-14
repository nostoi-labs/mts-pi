"""USB transport for the real Carlinkit (CPC200-CCPA, 1314:1520 / 1314:1521): same bytes as the TCP bench.

Ported from node-carplay (rhysmorgan134/node-CarPlay, MIT license, src/modules/DongleDriver.ts,
DongleDriver.initialise): configuration 1, interface 0, one bulk IN endpoint and one OUT;
messages are a 16-byte header + data. Here one thread reads from the IN endpoint and fills an asyncio.StreamReader,
another writes to OUT: CarlinkitHost (mtspi/carlinkit.py) does not know whether the bench network or USB is underneath.

Requires pyusb (pip install pyusb) and permission on the device (system/conf/60-mtspi-carlinkit.rules).
Test without the bridge: python -m mtspi.carlinkit_usb   (opens, sends Open and heartbeat, prints the messages for 30 s)
"""
import asyncio
import functools
import queue
import threading
import time

VENDOR = 0x1314
PRODUCTS = (0x1520, 0x1521)
READ_SIZE = 16384


def find_device():
    import usb.core
    for pid in PRODUCTS:
        dev = usb.core.find(idVendor=VENDOR, idProduct=pid)
        if dev is not None:
            return dev
    return None


class _UsbWriter:
    def __init__(self, link):
        self.link = link

    def write(self, data):
        self.link.out.put(bytes(data))

    async def drain(self):
        # no more than ~1 s of data accumulates if the dongle is not reading
        while self.link.out.qsize() > 64 and not self.link.closed:
            await asyncio.sleep(0.005)

    def close(self):
        self.link.close()

    def get_extra_info(self, name, default=None):
        return default


class UsbLink:
    """Open link: .reader (asyncio.StreamReader) and .writer (write/drain/close)."""

    def __init__(self, dev, log, reset=False):
        import usb.core
        import usb.util
        self.dev, self.log = dev, log
        self.closed = False
        if reset:
            # only after failed opens (as in carlinkit-cxx): the reset reboots the dongle (~10 s each time)
            try:
                dev.reset()
            except Exception as e:
                log("carlinkit_usb_reset_error", error=str(e))
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except Exception:
            pass
        # the dongle also exposes a mass storage interface (1) claimed by usb-storage: setting the
        # configuration again would give "Resource busy", so only if it is not already 1
        try:
            active = dev.get_active_configuration().bConfigurationValue
        except usb.core.USBError:
            active = None
        if active != 1:
            dev.set_configuration(1)
        intf = dev.get_active_configuration()[(0, 0)]
        usb.util.claim_interface(dev, intf.bInterfaceNumber)
        self.ep_in = usb.util.find_descriptor(intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
        self.ep_out = usb.util.find_descriptor(intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
        if self.ep_in is None or self.ep_out is None:
            raise RuntimeError("Carlinkit bulk endpoints not found")
        self.loop = asyncio.get_running_loop()
        self.reader = asyncio.StreamReader(limit=1 << 22)
        self.writer = _UsbWriter(self)
        self.out = queue.Queue()
        threading.Thread(target=self._rx, name="carlinkit-rx", daemon=True).start()
        threading.Thread(target=self._tx, name="carlinkit-tx", daemon=True).start()
        log("carlinkit_usb_open", product=hex(dev.idProduct), ep_in=hex(self.ep_in.bEndpointAddress),
            ep_out=hex(self.ep_out.bEndpointAddress))

    def _rx(self):
        import usb.core
        while not self.closed:
            try:
                data = self.dev.read(self.ep_in.bEndpointAddress, READ_SIZE, timeout=1000)
            except usb.core.USBTimeoutError:
                continue
            except Exception as e:                   # unplugged or rebooted
                self.loop.call_soon_threadsafe(functools.partial(self.log, "carlinkit_usb_rx_error", error=str(e)))
                break
            if data:
                self.loop.call_soon_threadsafe(self.reader.feed_data, bytes(data))
        self.closed = True
        self.loop.call_soon_threadsafe(self.reader.feed_eof)

    def _tx(self):
        while not self.closed:
            try:
                data = self.out.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.dev.write(self.ep_out.bEndpointAddress, data, timeout=4000)
            except Exception as e:
                self.loop.call_soon_threadsafe(functools.partial(self.log, "carlinkit_usb_tx_error", error=str(e)))
                self.closed = True
                self.loop.call_soon_threadsafe(self.reader.feed_eof)
                break

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                import usb.util
                usb.util.dispose_resources(self.dev)
            except Exception:
                pass


async def connect(log, retry_s=3600):
    """Waits for the Carlinkit on USB (the dongle reboots and reappears during startup) and opens it."""
    t0 = time.monotonic()
    announced = False
    fails = 0
    while True:
        dev = find_device()
        if dev is not None:
            try:
                return UsbLink(dev, log, reset=fails >= 3)
            except Exception as e:
                fails += 1
                log("carlinkit_usb_open_error", error=f"{type(e).__name__}: {e}", fails=fails)
        elif not announced:
            log("carlinkit_usb_waiting")
            announced = True
        if time.monotonic() - t0 > retry_s:
            raise TimeoutError("Carlinkit not found on USB")
        await asyncio.sleep(2)


async def _probe():
    """Live test: opens the dongle, sends the startup sequence and the heartbeat, prints the message types."""
    import sys
    from mtspi import carlinkit as ck

    def log(ev, **kw):
        print(ev, kw, flush=True)
    link = await connect(log, retry_s=30)
    host = ck.CarlinkitHost(link.reader, link.writer, log, 848, 304, 30)
    seen = {}

    async def on_msg(mtype, data):
        seen[mtype] = seen.get(mtype, 0) + 1
        if seen[mtype] <= 3:
            print(f"message {hex(mtype)} ({len(data)} B): {data[:48]!r}", flush=True)
    host.on_message = on_msg
    runner = asyncio.ensure_future(host.run())
    await host.start()
    await asyncio.sleep(float(sys.argv[1]) if len(sys.argv) > 1 else 30)
    print("type summary:", {hex(k): v for k, v in seen.items()}, flush=True)
    runner.cancel()
    link.close()


if __name__ == "__main__":
    asyncio.run(_probe())
