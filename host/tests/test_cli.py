from fake_device import FakeDevice, Fault

from nand_tool.cli import main


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
        raise RuntimeError("no Pico NAND Tool found")

    assert main(["ping"], transport_factory=boom) == 1
    assert "no Pico NAND Tool found" in capsys.readouterr().err
