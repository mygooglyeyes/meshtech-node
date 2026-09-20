"""Entry point: python -m cleanmodem [--config modem.conf] [options].

Precedence: command line > config file > built-in defaults.
The modem passwords live in their own protected files, never in the
config file (an old token line in the config is ignored with a warning,
so a leaked value dies with the upgrade).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from .config import (ConfigError, ModemConfig, build_config, load_config,
                     load_token)
from .server import ModemServer


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="cleanmodem LoRa modem")
    parser.add_argument("--config", default="modem.conf",
                        help="config file (default: modem.conf)")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("cleanmodem")

    raw = load_config(args.config)
    try:
        cfg = build_config(raw)
    except ConfigError as exc:
        log.error("config error: %s", exc)
        return 2

    # Command-line overrides.
    if args.host is not None:
        cfg.host = args.host
        cfg.bind_lan = args.host not in ("127.0.0.1", "localhost", "::1")
    if args.port is not None:
        cfg.port = args.port

    if "token" in raw or "feed_token" in raw:
        log.warning("token keys in %s are IGNORED for security - passwords "
                    "live in their own protected token files", args.config)

    try:
        observer_token = load_token(cfg.token_file) if cfg.token_file else ""
        controller_token = (load_token(cfg.controller_file)
                            if cfg.controller_file else "")
    except ConfigError as exc:
        log.error("%s - refusing to start with a loose token file", exc)
        return 2

    if not observer_token and not controller_token:
        log.warning("no token files configured - clients can authenticate "
                    "as neither role; set token_file / controller_file")

    # Import lazily so a dry config check never touches hardware libs.
    from .sx126x import SX126xRadio
    radio = SX126xRadio(
        cfg.pins,
        frequency_hz=cfg.frequency_hz,
        tx_power_dbm=cfg.tx_power_dbm,
        spreading_factor=cfg.spreading_factor,
        coding_rate=cfg.coding_rate,
        bandwidth_hz=cfg.bandwidth_hz,
        sync_word=cfg.sync_word,
        preamble_length=cfg.preamble_length,
        spi_bus=cfg.pins.get("spi_bus", 0),
        spi_device=cfg.pins.get("cs_device", 0),
        spi_speed_hz=cfg.spi_speed_hz,
        cad_peak=cfg.cad_peak, cad_min=cfg.cad_min,
        irq_poll_mode=cfg.irq_poll,
        gpio_backend=cfg.gpio_backend)

    server = ModemServer(cfg, radio,
                         observer_token=observer_token,
                         controller_token=controller_token)

    async def _run() -> int:
        if not await server.start():
            return 1
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig_name in ("SIGINT", "SIGTERM"):
            try:
                loop.add_signal_handler(getattr(signal, sig_name), stop.set)
            except (NotImplementedError, AttributeError):
                pass
        await stop.wait()
        log.info("shutting down")
        await server.stop()
        return 0

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
