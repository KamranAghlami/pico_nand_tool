from fake_device import FakeDevice, Fault

from nand_tool.cli import main
from nand_tool.errors import TransportError


def run(dev: FakeDevice, *argv: str) -> int:
    return main(["--timeout", "0.05", *argv], transport_factory=lambda port: dev)


def test_ping(capsys):
    dev = FakeDevice()
    assert run(dev, "ping") == 0
    out = capsys.readouterr().out
    assert "pico-nand-tool 0.1.0" in out
    assert "protocol v1" in out
    assert "125.000 MHz" in out
    assert dev.closed


def test_timing_slow_then_show(capsys):
    dev = FakeDevice()
    assert run(dev, "timing", "--slow") == 0
    assert "SLOW" in capsys.readouterr().out
    assert run(dev, "timing") == 0
    out = capsys.readouterr().out
    assert "SLOW" in out and "1000 ns" in out  # 125 cycles at 8 ns


def test_transport_failure_is_an_error_exit(capsys):
    dev = FakeDevice()
    dev.inject(*[Fault.DROP] * 10)
    assert run(dev, "ping") == 1
    assert "no valid response" in capsys.readouterr().err


def test_port_open_failure(capsys):
    def boom(port):
        raise TransportError("no Pico NAND Tool found")

    assert main(["ping"], transport_factory=boom) == 1
    assert "no Pico NAND Tool found" in capsys.readouterr().err


def test_unplug_mid_command_is_a_clean_error(capsys):
    class Unplugged(FakeDevice):
        def read(self, n):
            raise TransportError("/dev/ttyACM0: read failed (device unplugged or rebooted?)")

    dev = Unplugged()
    assert run(dev, "ping") == 1
    assert "unplugged" in capsys.readouterr().err
    assert dev.closed


def test_id_matches(capsys):
    assert run(FakeDevice(), "id", "--onfi") == 0
    out = capsys.readouterr().out
    assert "01 DA 90 95 44" in out and "match" in out and "MISMATCH" not in out
    assert "2 KB page, 128 KB block, 16 B spare per 512 B, 25 ns serial access, x8" in out
    assert "2 planes x 1 Gb" in out
    assert "'ONFI': match" in out


def test_id_repeat_identical(capsys):
    dev = FakeDevice()
    assert run(dev, "id", "--repeat", "50") == 0
    assert "50/50 reads identical" in capsys.readouterr().out
    assert len(dev.requests) == 50


def test_id_repeat_unstable(capsys):
    dev = FakeDevice()
    dev.id_glitches = [bytes.fromhex("01DA909546")]  # IO1 flaky once
    assert run(dev, "id", "--repeat", "10") == 1
    out = capsys.readouterr().out
    assert "NOT stable" in out and "x9" in out and "x1" in out
    assert "MISMATCH" in out


def test_id_no_chip(capsys):
    assert run(FakeDevice(chip_id=b"\xff" * 5), "id") == 1
    assert "MISMATCH" in capsys.readouterr().out


def test_status_after_reset(capsys):
    assert run(FakeDevice(), "status", "--reset") == 0
    out = capsys.readouterr().out
    assert "R/B# busy 3.20 us" in out
    assert "60h (as expected" in out
    assert "protected (WP# low)" in out and "ready" in out


def test_status_rb_never_low(capsys):
    assert run(FakeDevice(rb_never_low=True), "status", "--reset") == 0
    assert "never seen low" in capsys.readouterr().out


def test_status_wp_high_is_an_error(capsys):
    assert run(FakeDevice(wp_high=True), "status", "--reset") == 1
    cap = capsys.readouterr()
    assert "E0h (expected 60h after reset)" in cap.out
    assert "WP# HIGH" in cap.err


def test_status_rb_timeout(capsys):
    assert run(FakeDevice(rb_stuck_low=True), "status", "--reset") == 1
    assert "ERR_RB_TIMEOUT" in capsys.readouterr().err


def test_status_not_ready_after_reset_is_an_error(capsys, monkeypatch):
    dev = FakeDevice()
    monkeypatch.setattr(dev, "_sr_after_reset", lambda: 0x20)  # busy, WP# low
    assert run(dev, "status", "--reset") == 1
    out = capsys.readouterr().out
    assert "20h (expected 60h after reset)" in out and "busy" in out
    assert run(dev, "status") == 0  # without --reset there is no expectation
