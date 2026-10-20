"""Verify real Modbus frames and serial ownership using a virtual serial peer."""

from concurrent.futures import ThreadPoolExecutor
import os
import select
import time

import pytest

from gripper_ros2 import binary_42d as binary


def _crc(payload):
    value = 0xFFFF
    for byte in payload:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (0xA001 if value & 1 else 0)
    return value.to_bytes(2, "little")


def test_real_serial_frames_share_handle_address_and_turnaround():
    pytest.importorskip("minimalmodbus")
    serial = pytest.importorskip("serial")
    master, slave_fd = os.openpty()
    port = os.ttyname(slave_fd)
    replies, arrivals, requests = [], [], []

    def peer():
        pending = b""
        for _ in range(4):
            needed = 8
            while True:
                if len(pending) >= 2 and pending[1] == 16:
                    needed = 11
                if len(pending) >= needed:
                    break
                assert select.select([master], [], [], 2)[0], "missing Modbus request"
                pending += os.read(master, 100)
            arrivals.append(time.monotonic())
            request, pending = pending[:needed], pending[needed:]
            assert _crc(request[:-2]) == request[-2:]
            requests.append(request)
            if request[1] == 3:
                assert request[2:6] == b"\x00\x40\x00\x01"
                response = bytes([request[0], 3, 2, 0, 6 if request[0] == 1 else 1])
            else:
                assert request[2:9] in (
                    b"\x00\x36\x00\x01\x02\x00\x01",
                    b"\x00\x37\x00\x01\x02\x00\x01",
                )
                response = request[:6]
            os.write(master, response + _crc(response))
            replies.append(time.monotonic())

    bus = binary.Binary42DBus(port)
    try:
        assert bus.connect()
        with pytest.raises(serial.SerialException):
            serial.Serial(port, exclusive=True)
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(peer)
            assert bus.transaction(1, lambda sdk: sdk.read_register(0x40)) == 6
            assert bus.transaction(2, lambda sdk: sdk.read_register(0x40)) == 1
            bus.transaction(1, lambda sdk: sdk.write_register(0x36, 1))
            bus.transaction(2, lambda sdk: sdk.write_register(0x37, 1))
            future.result(timeout=3)
        assert [r[0] for r in requests] == [1, 2, 1, 2]
        assert [r[1] for r in requests] == [3, 3, 16, 16]
        assert all(arrivals[i] - replies[i - 1] >= 0.095 for i in range(1, 4))
    finally:
        bus.disconnect()
        os.close(master)
        os.close(slave_fd)
