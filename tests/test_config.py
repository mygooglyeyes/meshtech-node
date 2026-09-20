"""Config tests: validation errors are plain English; defaults are sane."""
import json

import pytest

from meshtech_node.config import ConfigError, Settings, load


def _write(tmp_path, body: dict) -> str:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return str(path)


def test_defaults_load(tmp_path):
    s = load(_write(tmp_path, {}))
    assert s.channel.name == "#scope"
    assert s.radio.spreading_factor == 7
    assert s.radio.bandwidth_khz == 250.0
    assert s.area.grid == 3
    assert s.feed.max_packets_per_hour == 26


def test_grid_bounds(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load(_write(tmp_path, {"area": {"grid": 9}}))
    assert "area.grid" in str(exc.value)


def test_span_bounds(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load(_write(tmp_path, {"area": {"span_km": 900}}))
    assert "area.span_km" in str(exc.value)


def test_secret_hex_validated(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load(_write(tmp_path, {"channel": {"secret_hex": "zz"}}))
    assert "secret_hex" in str(exc.value)


def test_token_in_config_warns(tmp_path):
    s = load(_write(tmp_path, {"repeater_api": {"token": "nope"}}))
    assert any("IGNORED" in w for w in s.warnings)


def test_channel_name_gets_hash(tmp_path):
    s = load(_write(tmp_path, {"channel": {"name": "scope"}}))
    assert s.channel.name == "#scope"


def test_flood_scope_key_validated(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load(_write(tmp_path, {"feed": {"flood_scope_key_hex": "1234"}}))
    assert "flood_scope_key_hex" in str(exc.value)


def test_pulse_interval_floor(tmp_path):
    s = load(_write(tmp_path, {"feed": {"pulse_interval_seconds": 5}}))
    assert s.feed.pulse_interval_seconds >= 30.0


def test_missing_file_message(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load(str(tmp_path / "nope.json"))
    assert "not found" in str(exc.value)


def test_settings_object_direct():
    """Direct Settings() (tests/tooling) works with defaults."""
    s = Settings()
    # LoganScope: the dedicated companion radio on the box (5052).
    assert s.companion_port == 5052
