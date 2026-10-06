"""Shared pytest fixtures. A test that names `dev` or `client` as a parameter gets a fresh one per test.

`dev` is a FakeDevice (fake_device.py): an in-process simulation of the firmware that speaks the real wire protocol,
holds a simulated NAND, and can inject faults. `client` is the real nand_tool Client talking to it, so every test
exercises the production client code with no hardware.
"""

import pytest
from fake_device import FakeDevice

from nand_tool.client import Client


@pytest.fixture
def dev() -> FakeDevice:
    return FakeDevice()


@pytest.fixture
def client(dev: FakeDevice) -> Client:
    # Short timeouts: the fake answers instantly, so any wait here is a lost or corrupted frame.
    return Client(dev, timeout=0.05, retries=3, quiet_s=0.005)
