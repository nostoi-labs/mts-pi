"""uart-flow: the decisions that can be checked without a Pi."""
import importlib.util
import os
import unittest

spec = importlib.util.spec_from_file_location("uart_flow", os.path.join(os.path.dirname(__file__), "..", "system", "uart-flow.py"))
uf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uf)


class UartFlow(unittest.TestCase):
    def test_only_the_drivers_value_is_changed(self):
        self.assertEqual(uf.wanted_value(0x03), 0x77)
        self.assertIsNone(uf.wanted_value(0x77))          # already on
        self.assertIsNone(uf.wanted_value(0x3F))          # somebody else's setting: left alone
        self.assertIsNone(uf.wanted_value(0x00))          # port switched off

    def test_the_value_keeps_receiver_and_transmitter_on(self):
        self.assertEqual(uf.WANTED & 0x03, 0x03)
        self.assertTrue(uf.WANTED & 0x04)                 # automatic RTS
        self.assertTrue(uf.WANTED & 0x40)                 # inverted level: without it the radio is held off for good
        self.assertFalse(uf.WANTED & 0x08)                # automatic CTS stays with the driver

    def test_where_bluetooth_is(self):
        self.assertTrue(uf.on_mini_uart("/sys/devices/platform/soc/3f215040.serial/3f215040.serial:0/3f215040.serial:0.0/serial0/serial0-0"))
        self.assertFalse(uf.on_mini_uart("/sys/devices/platform/soc/3f201000.serial/serial0/serial0-0"))
        self.assertFalse(uf.on_mini_uart("/sys/class/bluetooth/hci0/device"))     # no radio: the link does not resolve

    def test_model(self):
        self.assertTrue(uf.is_pi3("Raspberry Pi 3 Model A Plus Rev 1.0"))
        self.assertFalse(uf.is_pi3("Raspberry Pi 4 Model B Rev 1.4"))
        self.assertFalse(uf.is_pi3(""))


if __name__ == "__main__":
    unittest.main()
