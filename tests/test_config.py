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


def test_span_snap_covers_any_value(tmp_path):
    """No span value can break boot: everything snaps into the menu
    (900 km -> 60, the largest honest choice), with a warning."""
    s = load(_write(tmp_path, {"area": {"span_km": 900}}))
    assert s.area.span_km == 60.0
    assert any("area.span_km" in w for w in s.warnings)


def test_span_choices_accepted(tmp_path):
    """20/40/60 all still load clean (old configs must boot) - but the
    SHIPPED default is 60: the server always watches the full home box
    (MAP-SIZE-DESIGN.md, Brett verified 2026-09-23)."""
    for km in (20.0, 40.0, 60.0):
        s = load(_write(tmp_path, {"area": {"span_km": km}}))
        assert s.area.span_km == km
        assert s.warnings == []


def test_span_default_is_sixty(tmp_path):
    """No area block at all -> 60 km home box (server watches it all;
    20/40/60 is now a PHONE refresh choice, not a server size)."""
    s = load(_write(tmp_path, {}))
    assert s.area.span_km == 60.0


def test_span_snaps_to_nearest_with_warning(tmp_path):
    """An off-menu size snaps to the nearest choice - a WARNING, never
    an error (an existing working install must still boot)."""
    s = load(_write(tmp_path, {"area": {"span_km": 45.0}}))
    assert s.area.span_km == 40.0
    assert any("snapped to 40" in w for w in s.warnings)
    s = load(_write(tmp_path, {"area": {"span_km": 55.0}}))
    assert s.area.span_km == 60.0
    s = load(_write(tmp_path, {"area": {"span_km": 12.0}}))
    assert s.area.span_km == 20.0


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


def test_logging_level_switch_applied_to_root():
    """v0.0.050: config's logging.level was parsed and DEAD - DEBUG
    could never be turned on the box (found while tracing an air
    uplink that died in silence). node.apply_log_level must put the
    config value on the root logger; the test restores it after."""
    import logging as _logging
    from meshtech_node.config import LoggingCfg
    from meshtech_node.node import apply_log_level
    root = _logging.getLogger()
    previous = root.level
    try:
        apply_log_level(Settings(logging=LoggingCfg(level="DEBUG")))
        assert root.level == _logging.DEBUG
        apply_log_level(Settings(logging=LoggingCfg(level="INFO")))
        assert root.level == _logging.INFO
    finally:
        root.setLevel(previous)


def test_settings_object_direct():
    """Direct Settings() (tests/tooling) works with defaults."""
    s = Settings()
    # LoganScope: the dedicated companion radio on the box (5052).
    assert s.companion_port == 5052
