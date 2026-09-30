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
