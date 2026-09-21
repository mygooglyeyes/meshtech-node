"""Configuration - JSON edition.

Settings live in $OPENHOP_PLUGIN_DATA/config.json, edited through the
repeater's Plugins settings dialog (which seeds it from the manifest
defaults).  This module parses that file into typed Settings objects
and reports problems in plain English so a non-programmer can fix the
JSON themselves - same pattern as the answerbot's config module.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


class ConfigError(Exception):
    """Raised when config.json cannot be used; message is human readable."""


@dataclass
class RepeaterApiCfg:
    """Read-only access to the repeater's REST API."""
    # SOURCE TRUTH (2026-09-18): all repeater API paths live under
    # /api/... (bare paths serve the web-frontend plugin UI - live
    # bench finding A1).
    base_url: str = "http://127.0.0.1:8000/api"
    # Rows per poll (recent_packets allows limit=1..1000, default 100;
    # adverts default 500). 10 s poll at 200 keeps a busy SF7/BW250
    # mesh covered without giant responses.
    limit: int = 200
    # A token NEVER lives in config.json - token_file points at a file
    # whose first line is the API token. Empty = packet source disabled.
    token_file: str = ""
    timeout_seconds: float = 8.0


@dataclass
class ChannelCfg:
    name: str = "#scope"
    # Empty = the standard hashtag derivation sha256("#scope")[:16].
    secret_hex: str = ""
    # Companion slot to use (0 = first free slot >= 1).
    companion_slot: int = 0


@dataclass
class AreaCfg:
    name: str = "Local area"
    # Default centre: 94945 / Novato CA, 38.1074 -122.5697 (Brett's
    # area, 2026-09-17; CORRECTED same day - an earlier 37.4358,
    # -122.2975 was ~85 km off, near San Bruno, not Novato). Any
    # config.json area block overrides these.
    center_lat: float = 38.1074
    center_lon: float = -122.5697
    span_km: float = 40.0
    grid: int = 3


@dataclass
class FeedCfg:
    pulse_interval_seconds: float = 300.0
    layout_interval_seconds: float = 3600.0
    max_packets_per_hour: int = 26
    max_duty_percent: float = 1.0
    refresh_cooldown_seconds: float = 30.0
    refresh_hourly_cap: int = 10
    # Empty list = any client may request refreshes.
    allowed_prefixes: List[str] = field(default_factory=list)
    # Optional transport key (32 hex chars = 16 bytes) for scoped flooding:
    # when set, feed packets are tagged so they never leave the area.
    flood_scope_key_hex: str = ""
    # Gap between packets of one refresh burst (the DM-saga lesson).
    burst_gap_seconds: float = 0.8
    # This host's 2-byte origin id (v1.1 multi-host). "ab" or "0xab" or
    # 4 hex digits. Empty = derived from the companion pubkey at link-up.
    origin_hex: str = ""
    # LAYOUT discovery-beacon cadence in multi-host mode (v1.1): the
    # beacon announces this host to peers; peers expire after 3 misses.
    layout_beacon_seconds: float = 600.0
    # Multi-host mode: only the elected owner of a section answers a
    # refresh for it; off = answer everything (single-host default).
    multi_host: bool = False
    # Gate 1 master switch (C2, 2026-09-20 review): the ONE tx_enabled
    # source. False = listen-only; the sender refuses every transmit
    # loudly. Toggled via manage.sh's radio on/off (which edits this
    # value and restarts the service). Nothing hardcodes it anymore.
    tx_enabled: bool = False
    # How old an advert row (seconds, from last_seen) may be and still
    # count as an active node. Adverts are periodic (hours apart);
    # 24 h default keeps a slow-advertising mesh visible without
    # publishing long-silent nodes as active.
    advert_fresh_seconds: float = 86400.0


@dataclass
class RadioCfg:
    """Radio settings used ONLY for estimated-airtime figures.

    These must match what the repeater is using on air; the repeater
    owns the actual radio configuration.  Scope design point: SF7/BW250.
    """
    spreading_factor: int = 7
    bandwidth_khz: float = 250.0
    coding_rate_index: int = 1
    preamble_symbols: int = 32


@dataclass
class StorageCfg:
    db_path: str = "data/scope.db"


@dataclass
class LoggingCfg:
    level: str = "INFO"


@dataclass
class WebServeCfg:
    """NODE: the direct-mode feed server (WEBSERVE-PROTOCOL.md).

    Loopback bind by default; a non-loopback host REQUIRES token_file
    (first line = password) or the server refuses to start - fail
    closed, the cleanmodem posture. static_dir points at the scope-app
    dist/ so the app and its WebSocket share one origin (the app's CSP
    is connect-src 'self')."""
    host: str = "127.0.0.1"
    port: int = 8710
    token_file: str = ""
    static_dir: str = ""


@dataclass
class Settings:
    companion_host: str = "127.0.0.1"
    # LoganScope - the dedicated companion radio on the box (2026-09-18,
    # Brett: listening on 0.0.0.0:5052). The plugin connects over
    # loopback and speaks the feed through it.
    companion_port: int = 5052
    repeater_api: RepeaterApiCfg = field(default_factory=RepeaterApiCfg)
    channel: ChannelCfg = field(default_factory=ChannelCfg)
    area: AreaCfg = field(default_factory=AreaCfg)
    feed: FeedCfg = field(default_factory=FeedCfg)
    radio: RadioCfg = field(default_factory=RadioCfg)
    storage: StorageCfg = field(default_factory=StorageCfg)
    logging: LoggingCfg = field(default_factory=LoggingCfg)
    webserve: WebServeCfg = field(default_factory=WebServeCfg)
    # NODE: SINGLE-PROCESS radio ownership (Brett 2026-09-20 - the
    # user controls ONE service). When modem_conf names a cleanmodem
    # modem.conf, the node embeds the radio server in-process (root
    # on the box) instead of connecting to a standalone cleanmodem.
    # companion_host/port remain the loopback endpoint it dials -
    # itself. modem_token_file: the token file for that link.
    modem_token_file: str = ""
    modem_conf: str = ""
    config_path: str = "config.json"
    warnings: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


def load(config_path: str) -> Settings:
    """Load and validate the plugin's config.json. Raises ConfigError."""
    path = Path(config_path)
    if not path.is_file():
        raise ConfigError(
            f"Config file not found: {config_path}\n"
            "The plugin data dir should hold config.json (seeded from the "
            "manifest defaults on first run)."
        )
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config.json is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Could not read {config_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("config.json must contain a JSON object of settings.")

    errors: List[str] = []
    warnings: List[str] = []

    host = _text(raw, "companion_host", "127.0.0.1", errors, "companion_host")
    port = _int(raw, "companion_port", 5052, errors, "companion_port")
    if port < 1 or port > 65535:
        errors.append("companion_port must be between 1 and 65535.")

    api_raw = _dict(raw, "repeater_api")
    repeater_api = RepeaterApiCfg(
        base_url=_text(api_raw, "base_url", "http://127.0.0.1:8000/api",
                       errors, "repeater_api.base_url"),
        token_file=_text(api_raw, "token_file", "", errors,
                         "repeater_api.token_file"),
        timeout_seconds=max(1.0, _float(api_raw, "timeout_seconds", 8.0,
                                        errors, "repeater_api.timeout_seconds")),
        limit=max(1, _int(api_raw, "limit", 200, errors,
                          "repeater_api.limit")),
    )
    if _text(api_raw, "token", "", errors, "repeater_api.token"):
        warnings.append("repeater_api.token inside config.json is IGNORED for "
                        "safety - put the token in repeater_api.token_file "
                        "instead (first line of the file).")

    ch_raw = _dict(raw, "channel")
    name = _text(ch_raw, "name", "#scope", errors, "channel.name")
    if name and not name.startswith("#"):
        name = "#" + name.lstrip("#")
    secret_hex = _text(ch_raw, "secret_hex", "", errors, "channel.secret_hex")
    if secret_hex:
        try:
            if len(bytes.fromhex(secret_hex)) < 16:
                errors.append("channel.secret_hex must decode to at least "
                              "16 bytes (32 hex characters).")
        except ValueError:
            errors.append("channel.secret_hex is not valid hex.")
    channel = ChannelCfg(
        name=name,
        secret_hex=secret_hex,
        companion_slot=max(0, _int(ch_raw, "companion_slot", 0, errors,
                                   "channel.companion_slot")),
    )

    area_raw = _dict(raw, "area")
    grid = _int(area_raw, "grid", 3, errors, "area.grid")
    if grid < 2 or grid > 5:
        errors.append("area.grid must be between 2 and 5 (a grid x grid "
                      "tiling of the area).")
        grid = 3
    span_km = _float(area_raw, "span_km", 40.0, errors, "area.span_km")
    if span_km <= 0 or span_km > 500:
        errors.append("area.span_km must be between 0 and 500 kilometres.")
        span_km = 40.0
    area = AreaCfg(
        name=_text(area_raw, "name", "Local area", errors, "area.name"),
        center_lat=_float(area_raw, "center_lat", 0.0, errors, "area.center_lat"),
        center_lon=_float(area_raw, "center_lon", 0.0, errors, "area.center_lon"),
        span_km=span_km,
        grid=grid,
    )
    if not (-90.0 <= area.center_lat <= 90.0 and -180.0 <= area.center_lon <= 180.0):
        errors.append("area.center_lat / area.center_lon must be valid "
                      "coordinates (lat -90..90, lon -180..180), e.g. the "
                      "94945 default is lat 38.1074, lon -122.5697.")

    feed_raw = _dict(raw, "feed")
    allowed: List[str] = []
    allowed_raw = feed_raw.get("allowed_prefixes", [])
    if isinstance(allowed_raw, list):
        for item in allowed_raw:
            if isinstance(item, str) and item.strip():
                allowed.append(item.strip().lower())
    else:
        errors.append("feed.allowed_prefixes must be a list of hex prefixes.")
    scope_key = _text(feed_raw, "flood_scope_key_hex", "", errors,
                      "feed.flood_scope_key_hex")
    if scope_key:
        try:
            if len(bytes.fromhex(scope_key)) < 16:
                errors.append("feed.flood_scope_key_hex must decode to at "
                              "least 16 bytes (32 hex characters).")
        except ValueError:
            errors.append("feed.flood_scope_key_hex is not valid hex.")
    origin_hex = _text(feed_raw, "origin_hex", "", errors,
                       "feed.origin_hex")
    if origin_hex:
        cleaned = origin_hex.lower().removeprefix("0x")
        if len(cleaned) != 2 or not all(c in "0123456789abcdef" for c in cleaned):
            errors.append("feed.origin_hex must be exactly 2 hex characters "
                          "(one byte, e.g. '3a') when set.")
    feed = FeedCfg(
        pulse_interval_seconds=max(30.0, _float(feed_raw, "pulse_interval_seconds",
                                                300.0, errors,
                                                "feed.pulse_interval_seconds")),
        layout_interval_seconds=max(300.0, _float(feed_raw,
                                                  "layout_interval_seconds",
                                                  3600.0, errors,
                                                  "feed.layout_interval_seconds")),
        max_packets_per_hour=max(1, _int(feed_raw, "max_packets_per_hour", 26,
                                         errors, "feed.max_packets_per_hour")),
        max_duty_percent=max(0.05, _float(feed_raw, "max_duty_percent", 1.0,
                                          errors, "feed.max_duty_percent")),
        refresh_cooldown_seconds=max(5.0, _float(feed_raw,
                                                 "refresh_cooldown_seconds",
                                                 30.0, errors,
                                                 "feed.refresh_cooldown_seconds")),
        refresh_hourly_cap=max(1, _int(feed_raw, "refresh_hourly_cap", 10,
                                       errors, "feed.refresh_hourly_cap")),
        allowed_prefixes=allowed,
        flood_scope_key_hex=scope_key,
        burst_gap_seconds=max(0.2, _float(feed_raw, "burst_gap_seconds", 0.8,
                                          errors, "feed.burst_gap_seconds")),
        origin_hex=_text(feed_raw, "origin_hex", "", errors,
                         "feed.origin_hex"),
        layout_beacon_seconds=max(120.0, _float(feed_raw,
                                                "layout_beacon_seconds",
                                                600.0, errors,
                                                "feed.layout_beacon_seconds")),
        multi_host=bool(feed_raw.get("multi_host", False)),
        advert_fresh_seconds=max(60.0, _float(feed_raw,
                                              "advert_fresh_seconds",
                                              86400.0, errors,
                                              "feed.advert_fresh_seconds")),
        tx_enabled=bool(feed_raw.get("tx_enabled", False)),
    )

    radio_raw = _dict(raw, "radio")
    sf = _int(radio_raw, "spreading_factor", 7, errors, "radio.spreading_factor")
    if sf < 5 or sf > 12:
        errors.append("radio.spreading_factor must be between 5 and 12 "
                      f"(found '{sf}').")
    bw = _float(radio_raw, "bandwidth_khz", 250.0, errors, "radio.bandwidth_khz")
    if bw <= 0:
        errors.append("radio.bandwidth_khz must be a positive number.")
    cr = _int(radio_raw, "coding_rate_index", 1, errors, "radio.coding_rate_index")
    if cr < 1 or cr > 4:
        errors.append("radio.coding_rate_index must be 1 (4/5), 2 (4/6), "
                      "3 (4/7) or 4 (4/8).")
    preamble = _int(radio_raw, "preamble_symbols", 32, errors,
                    "radio.preamble_symbols")
    if preamble < 6:
        errors.append("radio.preamble_symbols must be at least 6.")
    radio = RadioCfg(spreading_factor=sf, bandwidth_khz=bw,
                     coding_rate_index=cr, preamble_symbols=preamble)

    storage_raw = _dict(raw, "storage")
    storage = StorageCfg(
        db_path=_text(storage_raw, "db_path", "data/scope.db", errors,
                      "storage.db_path"),
    )

    log_raw = _dict(raw, "logging")
    log_level = _text(log_raw, "level", "INFO", errors, "logging.level").upper()
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        errors.append("logging.level must be one of DEBUG, INFO, WARNING, "
                      f"ERROR, CRITICAL (found '{log_level}').")
        log_level = "INFO"

    # NODE: direct-mode server (WEBSERVE-PROTOCOL.md). Loopback default;
    # a non-loopback host with no token_file is refused by node.py at
    # startup (fail closed) - the loader only validates the shapes.
    ws_raw = _dict(raw, "webserve")
    ws_port = _int(ws_raw, "port", 8710, errors, "webserve.port")
    if ws_port < 1 or ws_port > 65535:
        errors.append("webserve.port must be between 1 and 65535.")
    webserve_cfg = WebServeCfg(
        host=_text(ws_raw, "host", "127.0.0.1", errors, "webserve.host"),
        port=ws_port,
        token_file=_text(ws_raw, "token_file", "", errors,
                         "webserve.token_file"),
        static_dir=_text(ws_raw, "static_dir", "", errors,
                         "webserve.static_dir"),
    )

    modem_token_file = _text(raw, "modem_token_file", "", errors,
                             "modem_token_file")
    modem_conf = _text(raw, "modem_conf", "", errors, "modem_conf")

    if errors:
        pretty = "\n".join(f"  - {e}" for e in errors)
        raise ConfigError(f"config.json has {len(errors)} problem(s):\n{pretty}")

    return Settings(
        companion_host=host,
        companion_port=port,
        repeater_api=repeater_api,
        channel=channel,
        area=area,
        feed=feed,
        radio=radio,
        storage=storage,
        logging=LoggingCfg(level=log_level),
        webserve=webserve_cfg,
        modem_token_file=modem_token_file,
        modem_conf=modem_conf,
        config_path=config_path,
        warnings=warnings,
        raw=raw,
    )


# --------------------------------------------------------------------------
# Data-dir helpers (the $OPENHOP_PLUGIN_DATA contract)
# --------------------------------------------------------------------------

def _manifest_path() -> Optional[Path]:
    """Locate the shipped openhop-plugin.json (works editable and in a wheel)."""
    try:
        from importlib import resources
        anchor = resources.files("meshtech_scope")
        path = Path(str(anchor)) / "share" / "openhop" / "plugins" / \
            "meshtech.scope" / "openhop-plugin.json"
        if path.is_file():
            return path
    except Exception:
        pass
    return None


def seed_config_if_missing(config_path: str) -> bool:
    """Write config.json from the manifest's config.defaults on first run."""
    path = Path(config_path)
    if path.is_file():
        return False
    defaults: Optional[Dict[str, Any]] = None
    manifest = _manifest_path()
    if manifest is not None:
        try:
            with open(manifest, "r", encoding="utf-8") as handle:
                defaults = json.load(handle).get("config", {}).get("defaults")
        except (OSError, json.JSONDecodeError):
            defaults = None
    if not isinstance(defaults, dict):
        raise ConfigError(
            "First run could not find the shipped plugin manifest, so there "
            "are no defaults to write config.json from. Reinstall the "
            "plugin, or create config.json by hand."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(defaults, handle, indent=2)
        handle.write("\n")
    return True


def data_dir() -> str:
    """The plugin's persistent data directory, from the manager's env var.

    Falls back to ./data for local development runs outside the manager.
    """
    value = os.environ.get("OPENHOP_PLUGIN_DATA", "")
    if value:
        return value
    return str(Path("data").resolve())


# --------------------------------------------------------------------------
# Small helpers (same shape as the answerbot's parser helpers)
# --------------------------------------------------------------------------

def _dict(raw: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = raw.get(key)
    return value if isinstance(value, dict) else {}


def _text(data: Dict[str, Any], key: str, default: str, errors: List[str],
          where: str = "") -> str:
    value = data.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        return default
    return value.strip()


def _int(data: Dict[str, Any], key: str, default: int, errors: List[str],
         where: str = "") -> int:
    value = data.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        errors.append(f"'{where or key}' must be a whole number (found '{value}').")
        return default


def _float(data: Dict[str, Any], key: str, default: float, errors: List[str],
           where: str = "") -> float:
    value = data.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        errors.append(f"'{where or key}' must be a number (found '{value}').")
        return default
