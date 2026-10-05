import struct
import time

import pytest
from fake_device import FakeDevice, Fault, page_data
from golden_param import GOLDEN_PARAM_X3

from nand_tool.client import Client, DeviceError, ProtocolMismatch, TransportError
from nand_tool.errors import FrameError
from nand_tool.protocol import Cmd, Status, Timing, TimingMode, encode_request, encode_response


def test_ping(client, dev):
    info = client.ping()
    assert info.proto_version == 2
    assert info.fw_version == (0, 2, 0)
    assert info.clk_hz == 125_000_000
    assert info.version_string.startswith("pico-nand-tool ")
    assert len(dev.requests) == 1


def test_ping_protocol_mismatch():
    c = Client(FakeDevice(proto_version=1), timeout=0.05, quiet_s=0.005)
    with pytest.raises(ProtocolMismatch):
        c.ping()
    assert c.ping(check_version=False).proto_version == 1


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


def test_unimplemented_command_at_m4(client):
    with pytest.raises(DeviceError) as e:
        client.call(Cmd.BUS_TEST)
    assert e.value.status == Status.ERR_UNKNOWN_CMD


def test_reset_and_status(client, dev):
    assert client.reset() == 3200
    assert client.read_status() == 0x60  # §3.12, WP# low


def test_reset_rb_timeout():
    c = Client(FakeDevice(rb_stuck_low=True), timeout=0.05, quiet_s=0.005)
    with pytest.raises(DeviceError) as e:
        c.reset()
    assert e.value.status == Status.ERR_RB_TIMEOUT


def test_read_id(client, dev):
    assert client.read_id() == bytes.fromhex("01DA909544")
    assert dev.requests[-1][2] == b""  # default form: no args (00h, 5)
    assert client.read_id(0x20, 4) == b"ONFI"
    assert dev.requests[-1][2] == b"\x20\x04"
    assert client.read_id(0x00, 2) == b"\x01\xda"


@pytest.mark.parametrize("args", [b"\x10\x05", b"\x00\x00", b"\x00\x09", b"\x00", b"\x00\x05\x00"])
def test_read_id_bad_args(client, args):
    with pytest.raises(DeviceError) as e:
        client.call(Cmd.READ_ID, args)
    assert e.value.status == Status.ERR_BAD_ARGS


def test_read_param(client, dev):
    assert client.read_param() == GOLDEN_PARAM_X3
    assert client.read_status() == 0x60  # the firmware issued FFh first


def test_read_param_rb_timeout():
    c = Client(FakeDevice(rb_stuck_low=True), timeout=0.05, quiet_s=0.005)
    with pytest.raises(DeviceError) as e:
        c.read_param()
    assert e.value.status == Status.ERR_RB_TIMEOUT


def test_read_param_wrong_length_is_frame_error(client, dev):
    dev.param = GOLDEN_PARAM_X3[:512]
    with pytest.raises(FrameError, match="READ_PARAM"):
        client.read_param()


@pytest.mark.parametrize("cmd", [Cmd.RESET, Cmd.READ_STATUS, Cmd.READ_PARAM])
def test_nand_commands_take_no_args(client, cmd):
    with pytest.raises(DeviceError) as e:
        client.call(cmd, b"\x00")
    assert e.value.status == Status.ERR_BAD_ARGS


def test_read_id_wrong_length_is_frame_error(client, dev, monkeypatch):
    monkeypatch.setattr(dev, "_execute", lambda cmd, seq, args: encode_response(cmd, seq, Status.OK, payload=b"\x01"))
    with pytest.raises(FrameError, match="READ_ID"):
        client.read_id()


def test_abort_without_stream(client):
    assert client.call(Cmd.ABORT) == b""


@pytest.mark.parametrize("fault", list(Fault))
def test_recovers_from_single_transport_fault(client, dev, fault):
    dev.inject(fault)
    assert client.ping().fw_version == (0, 2, 0)
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


# ---- READ_PAGES stream ----------------------------------------------------------------------------------------


def _read_all(client, start, count):
    return list(client.read_pages(start, count))


def _stream_requests(dev):
    return [struct.unpack("<II", a) for c, _, a in dev.requests if c == Cmd.READ_PAGES]


def test_read_pages(client, dev):
    got = _read_all(client, 100, 10)
    assert [p for p, _ in got] == list(range(100, 110))
    assert all(data == page_data(p) for p, data in got)
    assert _stream_requests(dev) == [(100, 10)]
    assert client.ping()  # nothing left on the line


def test_read_last_page(client):
    assert _read_all(client, 131071, 1)[0][0] == 131071


@pytest.mark.parametrize("start,count", [(0, 0), (-1, 1), (131071, 2)])
def test_read_pages_range_checked_on_host(client, start, count):
    with pytest.raises(ValueError):
        _read_all(client, start, count)


@pytest.mark.parametrize("args", [struct.pack("<II", 0, 0), struct.pack("<II", 131071, 2), struct.pack("<I", 0)])
def test_read_pages_bad_args_on_device(client, args):
    with pytest.raises(DeviceError) as e:
        client.call(Cmd.READ_PAGES, args)
    assert e.value.status == Status.ERR_BAD_ARGS


def test_rb_timeout_page_is_yielded_as_none(client, dev):
    dev.rb_timeouts = {3: 1}
    got = _read_all(client, 0, 6)
    assert [p for p, _ in got] == list(range(6))
    assert got[3][1] is None
    assert all(d == page_data(p) for p, d in got if p != 3)
    assert _stream_requests(dev) == [(0, 6)]  # an R/B# timeout is not a framing error: no restart


@pytest.mark.parametrize("fault", [f for f in Fault if f is not Fault.CORRUPT_REQUEST])  # that one is request-side
def test_stream_recovers_from_transport_fault(client, dev, fault):
    dev.page_faults = {5: fault}
    got = _read_all(client, 0, 10)
    assert [p for p, _ in got] == list(range(10))  # every page once, in order
    assert all(d == page_data(p) for p, d in got)
    if fault is Fault.STALE_FRAME:
        assert _stream_requests(dev) == [(0, 10)]  # skipped by seq matching
    else:
        assert _stream_requests(dev) == [(0, 10), (5, 5)]  # re-issued from the first page not accepted
    assert client.ping()


@pytest.mark.parametrize("fault", list(Fault))
def test_request_fault_on_stream_start(client, dev, fault):
    dev.inject(fault)  # applies to the READ_PAGES request / its first frame
    got = _read_all(client, 7, 3)
    assert [(p, d) for p, d in got] == [(p, page_data(p)) for p in range(7, 10)]


def test_err_busy_frames_are_skipped(client, dev):
    gen = client.read_pages(0, 5)
    assert next(gen)[0] == 0
    dev.write(encode_request(Cmd.PING, 0x77))  # someone else's request while the stream runs
    rest = list(gen)
    assert [p for p, _ in rest] == [1, 2, 3, 4]
    busy = [r for r in dev.requests if r[0] == Cmd.PING]
    assert busy == [(Cmd.PING, 0x77, b"")]


def test_stopping_early_aborts_the_stream(client, dev):
    for p, _ in client.read_pages(0, 1000):
        if p == 2:
            break
    assert any(c == Cmd.ABORT for c, _, _ in dev.requests)
    assert dev._stream is None
    assert client.ping()
    assert dev.page_reads.get(10) is None  # the device stopped right after the ABORT


class AlwaysDrop(dict):
    def pop(self, key, default=None):
        return Fault.DROP if key == 5 else default


def test_stream_gives_up_without_progress(client, dev):
    dev.page_faults = AlwaysDrop()
    got = []
    with pytest.raises(TransportError, match="no progress at page 5 after 4 attempts"):
        for item in client.read_pages(0, 10):
            got.append(item[0])
    assert got == [0, 1, 2, 3, 4]
    assert dev._stream is None  # aborted, nothing left running


def test_flaky_bits_alternate(client, dev):
    dev.flaky_bits = {0: [(17, 0x08)]}
    a = _read_all(client, 0, 1)[0][1]
    b = _read_all(client, 0, 1)[0][1]
    assert a == page_data(0) and b != a
    assert [i for i in range(len(a)) if a[i] != b[i]] == [17]


def test_busy_device_is_resynced_and_retried(client, dev):
    dev._start_stream(0x99, struct.pack("<II", 500, 1000), None)  # a stream someone else left running
    got = _read_all(client, 0, 3)
    assert [p for p, _ in got] == [0, 1, 2]
    assert any(c == Cmd.ABORT for c, _, _ in dev.requests)
    assert _stream_requests(dev) == [(0, 3), (0, 3)]
