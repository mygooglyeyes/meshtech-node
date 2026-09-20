"""Modem configuration: modem.conf parsing, pin resolution, validation.

modem.conf is a simple key = value file (no dependencies). Values keep
internal whitespace; keys are case-insensitive; blank lines and
# comments are ignored.

The modem password NEVER lives in this file - token files point at
separate mode-600 files whose first line is the password (the same
pattern the bot's dashboard password uses). A missing token file means
that port serves nothing (fail closed).

Pin configuration is fully configurable: a named preset plus explicit
per-pin overrides (explicit values win). The resolved map is validated
at startup - bad pin configs fail loud, never silently.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

# ─── Pin presets ─────────────────────────────────────────────────────
# Each preset describes one known board. `dio2_rf_switch` means the
# chip's DIO2 pin drives the antenna RF switch in silicon (no MCU pin
# needed); `dio3_tcxo` is the TCXO voltage in volts (0 = don't touch).

PIN_PRESETS: Dict[str, dict] = {
    # The working map (proven on air on the PiMesh-1W v2 by openHop):
    # hardware CS on CE0, DIO2 as the RF switch, TCXO powered through
    # DIO3 at 1.8 V - and pin 26 as the radio power-enable ("en").
    # v0.0.166: en was missing here for months; openHop drives it HIGH
    # at init, and the hilltop trace showed why it matters - with the
    # enable line untouched the chip answered SPI from standby but the
    # radio stage never ran.
    "pimesh-1w-v2": {
        "spi_bus": 0,
        "cs": 8,               # CE0, hardware chip select
        "busy": 5,
        "dio1": 6,
        "reset": 18,
        "en": 26,
        "dio2_rf_switch": True,
        "dio3_tcxo": 1.8,
        "txen": -1, "rxen": -1, "lna": -1,
    },
    # The rough-draft map for boards with an external LNA + RF switch
    # front end driven by MCU pins (rxtx switches TX/RX lines, lna gates
    # the low-noise amplifier's power).
    "pimesh-v2-draft": {
        "spi_bus": 0,
        "cs": 8,
        "busy": 18,
        "dio1": 27,
        "reset": 17,
        "dio2_rf_switch": False,
        "dio3_tcxo": 0.0,
        "txen": 22, "rxen": -1, "lna": 23,
    },
}

# Keys a preset/override map may carry, and their valid domains.
_PIN_KEYS = ("spi_bus", "cs", "busy", "dio1", "reset", "en",
             "txen", "rxen", "lna")
_FLAG_KEYS = ("dio2_rf_switch",)
_FLOAT_KEYS = ("dio3_tcxo",)


class ConfigError(ValueError):
    """Invalid configuration (fail loud at startup)."""


@dataclass
class ModemConfig:
    host: str = "127.0.0.1"
    port: int = 5055
    token_file: str = ""            # repeater (observer) token file
    controller_file: str = ""       # bot (controller) token file
    bind_lan: bool = False          # False = loopback only (default, safe)
    demo_feed: bool = False
    demo_interval: float = 10.0
    # Radio parameters (defaults = the live mesh, verified on air).
    frequency_hz: int = 910525000
    tx_power_dbm: int = 20
    spreading_factor: int = 7
    coding_rate: int = 5            # CR 4/5
    bandwidth_hz: int = 62500
    sync_word: int = 0x12
    preamble_length: int = 32
    spi_speed_hz: int = 2_000_000   # known working; 8 MHz is a bench option
    # Listen-before-talk policy (controller TX).
    lbt_enabled: bool = True
    # v0.0.155 diagnostics: poll the IRQ flags instead of waiting on the
    # DIO1 edge (works around GPIO event-detection breakage; costs a
    # little RX latency). Default off - edge mode is the proven path.
    irq_poll: bool = False
    # v0.0.156: force a GPIO backend. "auto" (default) = try gpiod, fall
    # back to RPi.GPIO. "gpiod" = gpiod v2 only, fail loud. "rpi" =
    # RPi.GPIO-compatible only, fail loud. Hilltop 2026-09-14: the
    # rpi-lgpio shim left the radio deaf (BUSY/reset writes not reaching
    # the pins -> blind SPI -> 0xAA00 garbage flags); pinning the backend
    # makes the choice explicit and A/B-testable. v0.0.158: gpiod means
    # the v2 bindings (2.x), which ship cp313 aarch64 wheels and match
    # Debian 13's libgpiod - the v1 1.x pip package is ABI-broken there.
    gpio_backend: str = "auto"
    # RESERVED, not implemented: retries are bounded by time
    # (clear_channel_wait_seconds), not attempt count. Accepted in
    # configs for compatibility; changing it has no effect.
    lbt_max_attempts: int = 5
    clear_channel_wait_seconds: float = 4.0
    politeness_seconds: float = 2.0
    cad_peak: int = 22              # pre-check pair (Semtech SF7 default)
    cad_min: int = 10
    # Resolved pin map (after preset + overrides).
    pins: dict = field(default_factory=lambda: dict(PIN_PRESETS["pimesh-1w-v2"]))


def load_config(path: str) -> dict:
    """Read modem.conf into a raw dict (case-insensitive keys).

    Missing file -> empty dict (defaults apply) with a note from the
    caller. Malformed values are rejected by _parse_* helpers later.
    """
    cfg: dict = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip().lower()
                value = value.strip()
                if not key:
                    raise ConfigError(f"{path}: empty config key")
                for ch in key:
                    if not (ch.isalnum() or ch in "_-"):
                        raise ConfigError(f"{path}: bad key {key!r}")
                cfg[key] = value
    except FileNotFoundError:
        pass
    return cfg


def _as_int(key: str, value: str, lo: int, hi: int) -> int:
    try:
        number = int(value, 0)       # accepts 0x.. hex too
    except ValueError as exc:
        raise ConfigError(f"{key}: {value!r} is not an integer") from exc
    if not lo <= number <= hi:
        raise ConfigError(f"{key}: {number} out of range {lo}..{hi}")
    return number


def _as_float(key: str, value: str, lo: float, hi: float) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise ConfigError(f"{key}: {value!r} is not a number") from exc
    if not lo <= number <= hi:
        raise ConfigError(f"{key}: {number} out of range {lo}..{hi}")
    return number


def _as_bool(key: str, value: str) -> bool:
    low = value.strip().lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{key}: {value!r} is not a boolean")


def _resolve_pins(raw: dict) -> dict:
    """Resolve the pin map: preset + explicit overrides, then validate."""
    profile = raw.get("pin_profile", "pimesh-1w-v2").strip()
    if profile not in PIN_PRESETS:
        known = ", ".join(sorted(PIN_PRESETS))
        raise ConfigError(
            f"pin_profile {profile!r} unknown (known profiles: {known})")
    pins = dict(PIN_PRESETS[profile])

    overrides = 0
    for key in _PIN_KEYS:
        if key in raw:
            pins[key] = _as_int(f"pins.{key}", raw[key], -1, 40)
            overrides += 1
    for key in _FLAG_KEYS:
        if key in raw:
            pins[key] = _as_bool(f"pins.{key}", raw[key])
            overrides += 1
    for key in _FLOAT_KEYS:
        if key in raw:
            pins[key] = _as_float(f"pins.{key}", raw[key], 0.0, 3.3)
            overrides += 1

    _validate_pins(pins, profile, overrides)
    return pins


def _validate_pins(pins: dict, profile: str, overrides: int) -> None:
    """Fail loud on an unusable pin map (no silent fallbacks)."""
    for key in _PIN_KEYS:
        value = pins.get(key, -1)
        if not isinstance(value, int) or not -1 <= value <= 40:
            raise ConfigError(f"pins.{key}: {value!r} is not a valid pin")
    used = {}
    for key in _PIN_KEYS:
        value = pins.get(key, -1)
        if value >= 0:
            if value in used:
                raise ConfigError(
                    f"pins: {used[value]} and {key} share pin {value}")
            used[value] = key
    if pins.get("busy", -1) < 0:
        raise ConfigError("pins.busy is required (the chip's BUSY line)")
    if pins.get("dio1", -1) < 0 and pins.get("txen", -1) < 0 and not overrides:
        raise ConfigError("pins.dio1 is required (packet-done interrupt)")
    if not 0 <= pins.get("spi_bus", 0) <= 10:
        raise ConfigError(f"pins.spi_bus out of range: {pins.get('spi_bus')}")
    if pins.get("dio3_tcxo", 0.0) and pins.get("dio3_tcxo") not in (
            1.6, 1.7, 1.8, 2.2, 2.4, 2.7, 3.0, 3.3):
        raise ConfigError(
            "pins.dio3_tcxo must be one of 1.6/1.7/1.8/2.2/2.4/2.7/3.0/3.3 V")


def build_config(raw: dict) -> ModemConfig:
    """Validate raw config values into a ModemConfig (fails loud)."""
    cfg = ModemConfig()
    if "host" in raw:
        host = raw["host"]
        if not host or any(ch.isspace() for ch in host) or len(host) > 253:
            raise ConfigError("host: invalid listen address")
        cfg.host = host
        cfg.bind_lan = host not in ("127.0.0.1", "localhost", "::1")
    if "port" in raw:
        cfg.port = _as_int("port", raw["port"], 1, 65535)
    if "bind_lan" in raw:
        cfg.bind_lan = _as_bool("bind_lan", raw["bind_lan"])
    if "token_file" in raw:
        cfg.token_file = raw["token_file"]
    if "controller_file" in raw:
        cfg.controller_file = raw["controller_file"]
    if "demo_feed" in raw:
        cfg.demo_feed = _as_bool("demo_feed", raw["demo_feed"])
    if "demo_interval" in raw:
        cfg.demo_interval = _as_float("demo_interval", raw["demo_interval"],
                                      1.0, 3600.0)
    if "frequency_hz" in raw:
        cfg.frequency_hz = _as_int("frequency_hz", raw["frequency_hz"],
                                   150_000_000, 1_050_000_000)
    if "tx_power_dbm" in raw:
        cfg.tx_power_dbm = _as_int("tx_power_dbm", raw["tx_power_dbm"],
                                   -9, 22)
    if "spreading_factor" in raw:
        cfg.spreading_factor = _as_int("spreading_factor",
                                       raw["spreading_factor"], 5, 12)
    if "coding_rate" in raw:
        cr = _as_int("coding_rate", raw["coding_rate"], 1, 4)
        cfg.coding_rate = cr + 4          # config index 1..4 -> CR 5..8
    if "bandwidth_khz" in raw:
        cfg.bandwidth_hz = int(round(_as_float(
            "bandwidth_khz", raw["bandwidth_khz"], 7.8, 500.0) * 1000))
    if "sync_word" in raw:
        cfg.sync_word = _as_int("sync_word", raw["sync_word"], 0, 0xFFFF)
    if "preamble_length" in raw:
        cfg.preamble_length = _as_int("preamble_length",
                                      raw["preamble_length"], 6, 255)
    if "spi_speed_hz" in raw:
        cfg.spi_speed_hz = _as_int("spi_speed_hz", raw["spi_speed_hz"],
                                   100_000, 32_000_000)
    if "lbt_enabled" in raw:
        cfg.lbt_enabled = _as_bool("lbt_enabled", raw["lbt_enabled"])
    if "irq_poll" in raw:
        cfg.irq_poll = _as_bool("irq_poll", raw["irq_poll"])
    if "gpio_backend" in raw:
        cfg.gpio_backend = raw["gpio_backend"].strip().lower()
        if cfg.gpio_backend not in ("auto", "gpiod", "rpi"):
            raise ConfigError(
                f"gpio_backend: {cfg.gpio_backend!r} unknown "
                '(auto | gpiod | rpi)')
    if "lbt_max_attempts" in raw:
        cfg.lbt_max_attempts = _as_int("lbt_max_attempts",
                                       raw["lbt_max_attempts"], 1, 20)
    if "clear_channel_wait_seconds" in raw:
        cfg.clear_channel_wait_seconds = _as_float(
            "clear_channel_wait_seconds", raw["clear_channel_wait_seconds"],
            0.0, 30.0)
    if "politeness_seconds" in raw:
        cfg.politeness_seconds = _as_float(
            "politeness_seconds", raw["politeness_seconds"], 0.0, 60.0)
    if "cad_peak" in raw:
        cfg.cad_peak = _as_int("cad_peak", raw["cad_peak"], 0, 31)
    if "cad_min" in raw:
        cfg.cad_min = _as_int("cad_min", raw["cad_min"], 0, 31)
    cfg.pins = _resolve_pins(raw)
    return cfg


def load_token(path: str) -> str:
    """Read a token file (first line, stripped). Missing/empty = no token.

    The file must not be world-readable (mode 600 or tighter); a loose
    file is refused so a leaked password fails loud instead of quietly.
    """
    if not path:
        return ""
    target = Path(path)
    try:
        if os.name == "posix":
            # POSIX: the file must not be world/group readable. On
            # Windows the POSIX mode bits don't exist (NTFS ACLs are a
            # different model), so the strict gate runs only where it
            # means what it says - the deployment target is Linux.
            mode = stat.S_IMODE(target.stat().st_mode)
            if mode & 0o077:
                raise ConfigError(
                    f"token file {path} is too open (mode {mode:03o}) - "
                    "chmod 600 it; refusing to read a loose token file")
        with open(target, encoding="utf-8") as handle:
            return handle.readline().strip()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise ConfigError(f"token file {path}: {exc}") from exc
