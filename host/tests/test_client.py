import pytest
from fake_device import FakeDevice, Fault

from nand_tool.client import Client, DeviceError, ProtocolMismatch, TransportError
from nand_tool.protocol import Cmd, Status, Timing, TimingMode


def test_ping(client, dev):
    info = client.ping()
    assert info.proto_version == 1
    assert info.fw_version == (0, 1, 0)
    assert info.clk_hz == 125_000_000
    assert info.version_string.startswith("pico-nand-tool ")
    assert len(dev.requests) == 1


def test_ping_protocol_mismatch():
    c = Client(FakeDevice(proto_version=2), timeout=0.05, quiet_s=0.005)
    with pytest.raises(ProtocolMismatch):
        c.ping()
    assert c.ping(check_version=False).proto_version == 2


def test_timing_presets(client, dev):
    assert client.get_timing() == (TimingMode.DEFAULT, Timing.default())
    assert client.set_timing(TimingMode.SLOW) == (TimingMode.SLOW, Timing.slow())
    assert dev.timing == Timing.slow()
    assert client.get_timing() == (TimingMode.SLOW, Timing.slow())
    assert client.set_timing(TimingMode.DEFAULT) == (TimingMode.DEFAULT, Timing.default())


def test_timing_custom_and_floor(client, dev):
    t = Timing.default()
    t.t_rea = 10
    assert client.set_timing(TimingMode.CUSTOM, t) == (TimingMode.CUSTOM, t)
    too_fast = Timing.default()
    too_fast.t_rea = 4  # 32 ns: below tREA 20 ns + 2-cycle input sync
    with pytest.raises(DeviceError) as e:
        client.set_timing(TimingMode.CUSTOM, too_fast)
    assert e.value.status == Status.ERR_TIMING_FLOOR
    assert dev.timing == t  # rejected, not clamped: nothing changed


def test_bad_args(client):
    with pytest.raises(DeviceError) as e:
        client.call(Cmd.SET_TIMING, bytes((9,)))
    assert e.value.status == Status.ERR_BAD_ARGS
    with pytest.raises(DeviceError) as e:
        client.call(Cmd.PING, b"\x00")
    assert e.value.status == Status.ERR_BAD_ARGS


def test_unimplemented_command_at_m0(client):
    with pytest.raises(DeviceError) as e:
        client.call(Cmd.READ_ID)
    assert e.value.status == Status.ERR_UNKNOWN_CMD


def test_abort_without_stream(client):
    assert client.call(Cmd.ABORT) == b""


@pytest.mark.parametrize("fault", list(Fault))
def test_recovers_from_single_transport_fault(client, dev, fault):
    dev.inject(fault)
    assert client.ping().fw_version == (0, 1, 0)
    # every fault except STALE_FRAME (skipped by seq matching) costs exactly one retry
    expected_attempts = 1 if fault is Fault.STALE_FRAME else 2
    sent = len(dev.requests) + (1 if fault is Fault.CORRUPT_REQUEST else 0)
    assert sent == expected_attempts


def test_gives_up_after_retries(client, dev):
    dev.inject(*[Fault.DROP] * 4)  # retries=3 -> 4 attempts, all lost
    with pytest.raises(TransportError, match="4 attempts"):
        client.ping()
    assert client.ping()


def test_request_bytes_survive_garbage_on_the_line(client, dev):
    # Junk written by some other program before our request: the firmware rescans for the magic.
    dev.write(b"\x00\xa5\x00\x00\xff\x13")
    assert client.ping()
