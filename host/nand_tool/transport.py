"""Byte transport to the device: USB CDC via pyserial, with port auto-detection.

The Transport interface is also implemented by tests/fake_device.py, so everything above it runs without hardware.
"""

from __future__ import annotations

from typing import Protocol

USB_VID = 0x2E8A
USB_PID = 0x000A
USB_PRODUCT = "Pico NAND Tool"

POLL_S = 0.02  # one read() call waits at most this long


class Transport(Protocol):
    def write(self, data: bytes) -> None: ...

    def read(self, n: int) -> bytes:
        """Return 0..n bytes, waiting at most about POLL_S. b"" means nothing arrived."""
        ...

    def reset_input(self) -> None: ...

    def close(self) -> None: ...


class SerialTransport:
    def __init__(self, port: str):
        import serial  # imported here so the pure modules don't need pyserial

        # Raw binary port: pyserial configures POSIX ttys in raw mode and does no newline translation. DTR is
        # asserted on open; the firmware only sends while DTR is set (docs/PROTOCOL.md "Transport").
        self._ser = serial.Serial(port, baudrate=115200, timeout=POLL_S, write_timeout=5.0)
        self.port = port

    def write(self, data: bytes) -> None:
        self._ser.write(data)
        self._ser.flush()

    def read(self, n: int) -> bytes:
        return self._ser.read(n)

    def reset_input(self) -> None:
        self._ser.reset_input_buffer()

    def close(self) -> None:
        self._ser.close()


def find_ports() -> list[str]:
    """Serial ports that look like a Pico NAND Tool (VID:PID plus product string when the OS reports it)."""
    from serial.tools import list_ports

    found = []
    for p in list_ports.comports():
        if p.vid == USB_VID and p.pid == USB_PID and (p.product in (None, USB_PRODUCT)):
            found.append(p.device)
    return sorted(found)


def open_transport(port: str | None) -> SerialTransport:
    if port is None:
        ports = find_ports()
        if not ports:
            raise RuntimeError("no Pico NAND Tool found (VID:PID 2E8A:000A); pass --port")
        if len(ports) > 1:
            raise RuntimeError(f"several candidate ports {ports}; pass --port")
        port = ports[0]
    return SerialTransport(port)
