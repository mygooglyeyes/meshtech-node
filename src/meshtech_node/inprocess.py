"""In-process radio mode - ONE process owns the radio AND the feed.

Brett's call (2026-09-20): the user must control ONE thing. So instead
of two services (cleanmodem + node), the node's own startup embeds
cleanmodem's ModemServer + SX126xRadio in the SAME process, binds the
loopback port, and then connects to it through the EXISTING proven
ModemClient path (modemlink.py) - the radio code is untouched, the
client code is untouched; only the process boundary is gone.

Config: settings.radio_block = path to the modem.conf (cleanmodem's
native format, same file format the standalone server would use).
The service runs as root on the box (radio pins need root); the node
code itself is unchanged.

Failure honesty: if the radio fails to start, the whole node refuses
to start - a scope feed with no radio is not this program.
"""
from __future__ import annotations

import logging

log = logging.getLogger("meshtech-node.inprocess")


class InProcessRadio:
    """Owns the embedded cleanmodem server for the node's lifetime."""

    def __init__(self, modem_conf_path: str):
        self.path = modem_conf_path
        self._server = None          # cleanmodem.server.ModemServer
        self._hal = None

    async def start(self) -> int:
        """Boot the radio + loopback server. Returns the bound port.

        Raises (loudly, refusing to half-run) when the radio or the
        server cannot start - the caller's failure path is a full
        node shutdown, never a silent radio-less feed."""
        # Lazy imports: bench machines without spidev/gpiod import the
        # node fine; the radio modules only load on the box.
        from cleanmodem.config import load_config, build_config, \
            load_token, ConfigError
        from cleanmodem.server import ModemServer

        cfg = build_config(load_config(self.path))
        import logging as _logmod
        _clog = _logmod.getLogger("cleanmodem")
        _clog.setLevel(logging.getLogger("meshtech-node").getEffectiveLevel())
        observer_token = load_token(cfg.token_file) \
            if cfg.token_file else ""
        controller_token = load_token(cfg.controller_file) \
            if cfg.controller_file else ""

        # The radio HAL: the proven SX126x driver, as cleanmodem's own
        # entrypoint would construct it.
        from cleanmodem.sx126x import SX126xRadio
        self._hal = SX126xRadio(
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

        self._server = ModemServer(cfg, self._hal,
                                   observer_token=observer_token,
                                   controller_token=controller_token)
        if not await self._server.start():
            raise RuntimeError(
                f"radio/loopback server failed to start (modem.conf="
                f"{self.path}) - the node refuses to run radio-less")
        log.info("embedded radio server UP on %s:%d (radio parameters: "
                 "%d Hz, SF%d, BW%d, sync 0x%02x) - the node owns the "
                 "radio in-process", cfg.host, cfg.port,
                 cfg.frequency_hz, cfg.spreading_factor,
                 cfg.bandwidth_hz, cfg.sync_word)
        return cfg.port

    async def stop(self) -> None:
        if self._server is not None:
            try:
                await self._server.stop()
            except Exception:
                log.exception("embedded radio server stop raised")
            self._server = None
