#!/usr/bin/env python3
import struct
import unittest
from unittest.mock import patch

import dps150


class FakeSerial:
    def __init__(self, incoming=b""):
        self.incoming = bytearray(incoming)
        self.writes = []
        self.port = "FAKE"

    def read(self, size):
        if not self.incoming:
            return b""
        out = bytes(self.incoming[:size])
        del self.incoming[:size]
        return out

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def flush(self):
        pass


def reply(type_, payload):
    payload = bytes(payload)
    checksum = (type_ + len(payload) + sum(payload)) % 256
    return bytes([dps150.HDR_IN, dps150.CMD_GET, type_, len(payload)]) + payload + bytes([checksum])


class ProtocolTests(unittest.TestCase):
    def make_device(self, incoming=b""):
        dev = dps150.DPS150.__new__(dps150.DPS150)
        dev.ser = FakeSerial(incoming)
        dev.buf = bytearray()
        dev.state = {}
        dev.usb_serial = None
        return dev

    @patch("dps150.time.sleep", return_value=None)
    def test_set_output_frame(self, _sleep):
        dev = self.make_device()
        dev.set_output(True)
        self.assertEqual(dev.ser.writes, [bytes([0xF1, 0xB1, 219, 1, 1, 221])])

    def test_parse_output_measurements(self):
        payload = struct.pack("<fff", 12.0, 1.5, 18.0)
        dev = self.make_device(reply(dps150.REG_OUT_VIP, payload))
        frames = dev.read_frames(0.05, want_type=dps150.REG_OUT_VIP)
        self.assertEqual(frames, 1)
        self.assertAlmostEqual(dev.state["output_voltage"], 12.0)
        self.assertAlmostEqual(dev.state["output_current"], 1.5)
        self.assertAlmostEqual(dev.state["output_power"], 18.0)

    def test_bad_checksum_is_ignored(self):
        bad = bytearray(reply(dps150.REG_TEMPERATURE, struct.pack("<f", 30.0)))
        bad[-1] ^= 0xFF
        dev = self.make_device(bad)
        self.assertEqual(dev.read_frames(0.01), 0)
        self.assertNotIn("temperature", dev.state)


if __name__ == "__main__":
    unittest.main()
