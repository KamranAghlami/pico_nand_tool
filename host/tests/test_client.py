import struct
import time

import pytest
from fake_device import FakeDevice, Fault

from nand_tool.client import Client, DeviceError, ProtocolMismatch, TransportError
from nand_tool.errors import FrameError
from nand_tool.protocol import Cmd, Status, Timing, TimingMode, encode_response


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
    pings = [r for r in dev.requests if r[0] == Cmd.PING]
    sent = len(pings) + (1 if fault is Fault.CORRUPT_REQUEST else 0)
    assert sent == expected_attempts
    # each recovery follows the host recovery rule: ABORT first (docs/PROTOCOL.md)
    aborts = [r for r in dev.requests if r[0] == Cmd.ABORT]
    assert len(aborts) == (0 if fault in (Fault.STALE_FRAME, Fault.CORRUPT_REQUEST) else 1)


def test_gives_up_after_retries(client, dev):
    dev.inject(*[Fault.DROP] * 4)  # retries=3 -> 4 attempts, all lost
    with pytest.raises(TransportError, match="4 attempts"):
        client.ping()
    assert client.ping()


def test_request_bytes_survive_garbage_on_the_line(client, dev):
    # Junk written by some other program before our request: the firmware rescans for the magic.
    dev.write(b"\x00\xa5\x00\x00\xff\x13")
    assert client.ping()


class Babbler:
    """A port that never stops sending text, like a stock Pico printing in a loop."""

    def __init__(self):
        self.written = bytearray()

    def write(self, data):
        self.written += data

    def read(self, n):
        time.sleep(0.001)
        return b"hello from stdio_usb\r\n"[:n]

    def reset_input(self):
        pass

    def close(self):
        pass


def test_resync_gives_up_when_line_never_goes_quiet():
    c = Client(Babbler(), timeout=0.05, retries=3, quiet_s=0.02, resync_max_s=0.2)
    t0 = time.monotonic()
    with pytest.raises(TransportError, match="never went quiet"):
        c.ping()
    assert time.monotonic() - t0 < 1.0


class StaleStream(FakeDevice):
    """After the real reply is suppressed, keeps delivering valid frames with a foreign seq (abandoned stream)."""

    def read(self, n):
        self._tx += encode_response(Cmd.PING, 0xEE, Status.OK, 0, bytes(64))
        return super().read(n)


def test_deadline_holds_while_stale_frames_keep_arriving():
    dev = StaleStream()
    dev.inject(Fault.DROP)  # the real reply is lost; only foreign frames keep coming
    c = Client(dev, timeout=0.05, retries=0, quiet_s=0.02, resync_max_s=0.2)
    t0 = time.monotonic()
    with pytest.raises(TransportError):
        # Without a deadline check the stale-frame skip loop would spin forever. With it: timeout, then resync
        # (which gives up too, because the stream never stops).
        c.request(Cmd.SET_TIMING, b"\x00")
    assert time.monotonic() - t0 < 1.0


def test_seq_start_is_random():
    starts = {Client(FakeDevice())._seq for _ in range(50)}
    assert len(starts) > 5


def test_malformed_payloads_are_frame_errors(client, dev, monkeypatch):
    def short_reply(cmd, seq, args):
        return encode_response(cmd, seq, Status.OK, payload=b"\x01\x02")

    monkeypatch.setattr(dev, "_execute", short_reply)
    with pytest.raises(FrameError, match="SET_TIMING"):
        client.get_timing()

    def zero_clock(cmd, seq, args):
        return encode_response(cmd, seq, Status.OK, payload=struct.pack("<BBBBI", 1, 0, 1, 0, 0) + b"x")

    monkeypatch.setattr(dev, "_execute", zero_clock)
    with pytest.raises(FrameError, match="clk_sys"):
        client.ping()
