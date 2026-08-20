from pathlib import Path

from PIL import Image

from peripage_a6.cli import main
from peripage_a6.discover import _parse_devices


def test_parse_bluetoothctl_devices():
    output = """
Device 00:15:83:15:BC:5F PeriPage+BC5F
Device AA:BB:CC:DD:EE:FF Headphones
Device not-a-mac junk
"""
    devices = _parse_devices(output)
    assert devices[0].address == "00:15:83:15:BC:5F"
    assert devices[0].likely_peripage
    assert devices[1].name == "Headphones"
    assert not devices[1].likely_peripage


def test_cli_dump_print_writes_gs_v0(tmp_path: Path):
    image_path = tmp_path / "black.png"
    Image.new("RGB", (576, 2), (0, 0, 0)).save(image_path)
    dump_path = tmp_path / "job.bin"
    rc = main(
        [
            "print",
            "00:11:22:33:44:55",
            str(image_path),
            "--dump",
            str(dump_path),
            "--dither",
            "threshold",
            "--concentration",
            "dark",
            "--feed",
            "32",
        ]
    )
    assert rc == 0
    data = dump_path.read_bytes()
    assert bytes.fromhex("10fffe01") in data
    assert bytes.fromhex("1d76300048000200") in data
    assert bytes.fromhex("10ff100002") in data
    assert bytes.fromhex("1b4a20") in data


def test_cli_help_exits_zero(capsys):
    try:
        main(["--help"])
    except SystemExit as exc:
        assert exc.code == 0
    assert "304dpi" in capsys.readouterr().out
