"""Bandwidth config-name fix (hilltop 2026-09-21).

The trap this pins: cleanmodem only read `bandwidth_khz`, while the
shipped modem.conf and manage.sh write `bandwidth_hz` - so the value
everyone was prompted for was silently ignored. BOTH names must work
now (Hz wins if both appear), and an invalid value must fail loudly.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cleanmodem"))

from cleanmodem.config import build_config, load_config  # noqa: E402


def _conf(tmp_path, lines):
    p = tmp_path / "modem.conf"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return build_config(load_config(str(p)))


def test_bandwidth_hz_is_honored(tmp_path):
    cfg = _conf(tmp_path, ["bandwidth_hz = 62500"])
    assert cfg.bandwidth_hz == 62500


def test_bandwidth_khz_still_works(tmp_path):
    cfg = _conf(tmp_path, ["bandwidth_khz = 62.5"])
    assert cfg.bandwidth_hz == 62500


def test_missing_bandwidth_uses_default(tmp_path):
    cfg = _conf(tmp_path, ["frequency_hz = 910525000"])
    assert cfg.bandwidth_hz == 62500


def test_hz_wins_when_both_present(tmp_path):
    cfg = _conf(tmp_path, ["bandwidth_khz = 125", "bandwidth_hz = 250000"])
    assert cfg.bandwidth_hz == 250000


def test_out_of_range_bandwidth_fails_loudly(tmp_path):
    import pytest

    from cleanmodem.config import ConfigError

    with pytest.raises(ConfigError):
        _conf(tmp_path, ["bandwidth_hz = 5"])
