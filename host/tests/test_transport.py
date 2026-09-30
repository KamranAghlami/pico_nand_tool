from types import SimpleNamespace

import pytest
import serial
from serial.tools import list_ports

from nand_tool import transport as T
from nand_tool.errors import TransportError


def port(device, vid=0x2E8A, pid=0x000A, product="Pico NAND Tool"):
    return SimpleNamespace(device=device, vid=vid, pid=pid, product=product)


def test_find_ports_classifies(monkeypatch):
    monkeypatch.setattr(
        list_ports,
        "comports",
        lambda: [
            port("/dev/ttyACM1"),
            port("/dev/ttyACM0", product="Pico"),  # stock SDK stdio_usb firmware: never ours
            port("COM7", product=None),  # Windows: product unknown
            port("/dev/ttyUSB0", vid=0x0403, pid=0x6001, product="FT232R"),
        ],
    )
    assert T.find_ports() == (["/dev/ttyACM1"], ["COM7"])


def test_select_port():
    assert T.select_port(["/dev/ttyACM1"], ["COM7"]) == "/dev/ttyACM1"
    with pytest.raises(TransportError, match="several"):
        T.select_port(["a", "b"], [])
    with pytest.raises(TransportError, match="does not report the product name"):
        T.select_port([], ["COM7"])  # never auto-pick a device we can't identify
    with pytest.raises(TransportError, match="no Pico NAND Tool found"):
        T.select_port([], [])


def test_serial_errors_become_transport_errors(monkeypatch):
    def refuse(*a, **kw):
        raise serial.SerialException("[Errno 11] Could not exclusively lock port")

    monkeypatch.setattr(serial, "Serial", refuse)
    with pytest.raises(TransportError, match="cannot open /dev/ttyACM0"):
        T.SerialTransport("/dev/ttyACM0")


def test_serial_open_is_exclusive_and_flushes(monkeypatch):
    seen = {}

    class FakeSerial:
        def __init__(self, port, **kw):
            seen.update(kw)
            self.flushed = False

        def reset_input_buffer(self):
            seen["flushed"] = True

        def read(self, n):
            raise serial.SerialException("device reports readiness to read but returned no data")

    monkeypatch.setattr(serial, "Serial", FakeSerial)
    t = T.SerialTransport("/dev/ttyACM0")
    assert seen.get("exclusive") is (True if T.os.name == "posix" else None)
    assert seen["flushed"]
    with pytest.raises(TransportError, match="unplugged"):
        t.read(10)
