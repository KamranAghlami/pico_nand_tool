"""Byte transport to the device: USB CDC via pyserial, with port auto-detection.

The Transport interface is also implemented by tests/fake_device.py, so everything above it runs without hardware.
"""

from __future__ import annotations

import os
from typing import Protocol

from .errors import TransportError

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
    """pyserial port. Every pyserial/OS error is re-raised as TransportError."""

    def __init__(self, port: str):
        import serial  # imported here so the pure modules don't need pyserial

        self._serial_errors = (serial.SerialException, OSError)
        try:
            # Raw binary port: pyserial configures POSIX ttys in raw mode, with no newline translation. DTR is
            # asserted on open; the firmware only sends while DTR is set, and it drops the previous session's
            # leftovers when DTR changes (docs/PROTOCOL.md "Transport"). exclusive: a second nandtool can't share
            # the port and steal bytes (POSIX flock; Windows COM ports are exclusive anyway).
            kwargs = {"exclusive": True} if os.name == "posix" else {}
            self._ser = serial.Serial(port, baudrate=115200, timeout=POLL_S, write_timeout=5.0, **kwargs)
            self._ser.reset_input_buffer()  # nothing read before our first request can be ours
        except self._serial_errors as e:
            raise TransportError(f"cannot open {port}: {e}") from e
        self.port = port

    def write(self, data: bytes) -> None:
        try:
            self._ser.write(data)
            self._ser.flush()
        except self._serial_errors as e:
            raise TransportError(f"{self.port}: write failed: {e}") from e

    def read(self, n: int) -> bytes:
        try:
            return self._ser.read(n)
        except self._serial_errors as e:
            raise TransportError(f"{self.port}: read failed (device unplugged or rebooted?): {e}") from e

    def reset_input(self) -> None:
        try:
            self._ser.reset_input_buffer()
        except self._serial_errors as e:
            raise TransportError(f"{self.port}: {e}") from e

    def close(self) -> None:
        try:
            self._ser.close()
        except self._serial_errors:
            pass


def find_ports() -> tuple[list[str], list[str]]:
    """Return (confirmed, unverified) ports with VID:PID 2E8A:000A.

    Confirmed ports also report our product string. Unverified ones report no product string, as on Windows, where
    pyserial never provides it. They could just as well be any Pico running stock SDK USB-serial firmware, which
    uses the same VID:PID.
    """
    from serial.tools import list_ports

    confirmed, unverified = [], []
    for p in list_ports.comports():
        if p.vid == USB_VID and p.pid == USB_PID:
            if p.product == USB_PRODUCT:
                confirmed.append(p.device)
            elif p.product is None:
                unverified.append(p.device)
    return sorted(confirmed), sorted(unverified)


def select_port(confirmed: list[str], unverified: list[str]) -> str:
    if len(confirmed) == 1:
        return confirmed[0]
    if len(confirmed) > 1:
        raise TransportError(f"several Pico NAND Tools found {confirmed}; pass --port")
    if unverified:
        raise TransportError(
            f"found 2E8A:000A device(s) {unverified}, but the OS does not report the product name, so they may be "
            f"ordinary Picos; if one is the Pico NAND Tool, pass --port"
        )
    raise TransportError("no Pico NAND Tool found (VID:PID 2E8A:000A, product 'Pico NAND Tool'); pass --port")


def open_transport(port: str | None) -> SerialTransport:
    if port is None:
        port = select_port(*find_ports())
    return SerialTransport(port)
