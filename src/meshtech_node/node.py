"""meshtech-node service shell - wires the whole machine.

The bot's shell pattern (supervisor + graceful stop), simplified:
everything runs in ONE process. The radio link (cleanmodem's
ModemClient) is supervised alongside the brain's ingest/broadcast
loops and the WebServe HTTP server.

Wiring (SEED-MAP.md):
    RawPacketSource (Adapter A)   -> brain.ingest_loop (queue seam)
    brain (scope core, as-is)     -> RadioSender (Adapter B, TX-off
                                     default) via _send_burst
    brain feed tap                -> WebServe.on_built_packet (direct
                                     mode; works TX-off)
    WebServe refresh requests     -> brain.on_packet (same dedupe +
                                     rate limiter as on-air requests)

Run:  python -m meshtech_node.node --config config.json
Bench (no radio, TX impossible):  --bench-no-radio
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from typing import Optional

from aiohttp import web

from . import config as cfgmod
from . import packets, webserve
from .client import scope_secret
from .radiosender import RadioSender
from .rawsource import RawPacketSource
from .service import ScopeService

log = logging.getLogger("meshtech-node")


def _build(settings, *, bench_no_radio: bool) -> tuple:
    """Assemble source, sender, brain, webserve - one wiring, tested."""
    secret = scope_secret(settings.channel.name,
                          settings.channel.secret_hex)
    channel = packets.ChannelKeys.from_secret(
        settings.channel.name,
        secret.hex())   # derive_channel_keys takes the secret TEXT

    sender: RadioSender = RadioSender(
        None, channel,
        tx_enabled=False,          # Gate 1: listen-only, always at start
        bench_no_radio=bench_no_radio,
    )
    modem = None
    if not bench_no_radio:
        # REAL MODE: the loopback link to the embedded radio server
        # (or a standalone cleanmodem). RX feed only - the sender still
        # refuses to transmit (TX-off is the shell's invariant here,
        # flipped later only by Brett's explicit gate).
        from .modemlink import ModemTransport
        token = _modem_token(settings)
        host, port = _modem_endpoint(settings)
        modem = ModemTransport(host, port, token)
        sender.modem = modem
    source = RawPacketSource(
        transport=modem,           # None in bench mode: source idles
        channels=[channel],        # #scope (config) - the consumed channel
    )
    dedupe = packets.FloodDedupe()
    source.dedupe = dedupe
    sender.dedupe = dedupe         # own-TX echo suppression

    brain = ScopeService(
        settings, client=sender, use_demo=False,
    )
    brain.external_source = source
    brain.tx_enabled = False       # mirror the sender's guard at the brain

    serve = webserve.WebServe(
        settings.webserve.host, settings.webserve.port,
        token=_token(settings),
        feed_info={"tx_enabled": False,
                   "feed": {"channel": settings.channel.name,
                            "bench_no_radio": bench_no_radio}},
        state_provider=lambda: _state_snapshot(brain, source),
    )
    serve.on_refresh = brain.on_packet
    source.on_scope = brain.on_packet   # RX shim: heard #scope -> brain
    brain.feed_tap = serve         # FeedTap: every built packet -> WS
    brain.bench_no_radio = bench_no_radio
    return source, sender, brain, serve


def _state_snapshot(brain, source) -> dict:
    """Honest node state for the protocol's `state` message. Values
    the node genuinely has; None where it does not (never a guess)."""
    st = source.stats
    heard = st.received > 0
    return {
        "listener": {
            "pkts_last_hour": st.received if heard else 0,
            "nodes_active": len(brain.store.active_prefixes(
                lambda obs: brain.geometry.section_for(obs.lat, obs.lon)
                if getattr(obs, "lat", None) is not None else -1)
                ) if heard else 0,
            "last_heard_s": None,   # refined in a later increment
        },
        "feed": {
            "last_pulse_ts": brain.builder._last_pulse or None,
            "next_pulse_ts": (brain.builder._last_pulse +
                              brain.settings.feed.pulse_interval_seconds)
            if brain.builder._last_pulse else None,
            "budget_used_h": brain.budget.tx_seconds_last_hour(),
            "budget_cap_h": int(brain.settings.feed.max_duty_percent
                                / 100.0 * 3600.0),
        },
    }


def _modem_token(settings) -> str:
    """The modem controller/observer token, from a mode-600 file
    (first line = password). Never a config value, never committed."""
    path = getattr(settings, "modem_token_file", "")
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.readline().strip()
    except OSError as exc:
        log.warning("modem token file %s unreadable (%s)", path, exc)
        return ""


def _modem_endpoint(settings) -> tuple:
    """(host, port) the node dials for its own radio link.

    SINGLE-PROCESS truth (the 5052/5055 mismatch, caught on the box
    2026-09-20): when the node EMBEDS cleanmodem (modem_conf set), the
    server binds the endpoint from modem.conf - so that is what the
    node must dial, not the plugin-era companion default. One source
    of truth: read modem.conf, never a second hardcoded port.
    Falls back to the settings' companion endpoint (bench/standalone
    cleanmodem layouts).
    """
    modem_conf = getattr(settings, "modem_conf", "")
    if modem_conf:
        try:
            cfg = _load_modem_config(modem_conf)
            return cfg.host, cfg.port
        except Exception as exc:
            log.warning("could not derive modem endpoint from %s (%s) - "
                        "falling back to companion endpoint", modem_conf, exc)
    return settings.companion_host, settings.companion_port


def _load_modem_config(modem_conf: str):
    """cleanmodem's parsed config (factored out so tests can inject
    failures)."""
    from cleanmodem.config import load_config, build_config
    return build_config(load_config(modem_conf))


def _token(settings) -> Optional[str]:
    """Loopback needs no token; a wider bind requires one (fail closed
    happens in WebServe.start - here we just load the file)."""
    path = getattr(settings.webserve, "token_file", None)
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.readline().strip()
    except OSError as exc:
        log.warning("Token file %s unreadable (%s) - starting WITHOUT "
                    "a token; non-loopback bind will be refused.", path, exc)
        return None


async def _main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="meshtech-node")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--bench-no-radio", action="store_true",
                        help="bench mode: no radio link; feed runs, TX "
                             "impossible, WebServe serves direct mode")
    args = parser.parse_args(argv)

    settings = cfgmod.load(args.config)
    bench = args.bench_no_radio

    # SINGLE-PROCESS MODE (Brett 2026-09-20: the user controls ONE
    # thing): when config names a modem.conf, the node embeds
    # cleanmodem's radio server in-process (root required on the box),
    # then connects to its own loopback through the proven client.
    radio = None
    modem_conf = getattr(settings, "modem_conf", "")
    if modem_conf and not bench:
        from .inprocess import InProcessRadio
        radio = InProcessRadio(modem_conf)
        await radio.start()          # raises loudly on radio failure

    source, sender, brain, serve = _build(settings, bench_no_radio=bench)
    modem = getattr(sender, "modem", None)

    # WebServe security posture: loopback default; a wider bind needs a
    # token or we refuse to start (fail closed, cleanmodem posture).
    if serve.auth is None and not _is_loopback(serve.host):
        log.error("Refusing to bind %s without a token file - "
                  "fail closed (cleanmodem posture).", serve.host)
        return 2

    if settings.webserve.static_dir:
        serve.add_static(settings.webserve.static_dir)
        log.info("Serving scope-app from %s", settings.webserve.static_dir)

    runner = web.AppRunner(serve.app)
    await runner.setup()
    site = web.TCPSite(runner, serve.host, serve.port)
    await site.start()
    log.info("WebServe listening on %s:%d/feed (direct mode ready)",
             serve.host, serve.port)
    if bench:
        log.info("=" * 62)
        log.info("BENCH: TX DISABLED - listen-only direct-mode feed. "
                 "Nothing this node builds reaches the air.")
        log.info("=" * 62)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:      # Windows: KeyboardInterrupt path
            pass

    tasks = [
        asyncio.create_task(sender.run(), name="sender"),
        asyncio.create_task(brain.run(), name="brain"),
        asyncio.create_task(stop.wait(), name="stop"),
    ]
    if modem is not None:
        # Real mode: bring the modem link up BEFORE the brain's cadence
        # seeds (a link that dies at startup must be loud, not silent).
        try:
            await modem.start()
            tasks.insert(0, asyncio.create_task(
                _watch_modem(modem, stop), name="modem-watch"))
            log.info("RADIO LINK UP - the node is listening (TX off, "
                     "listen-only). Feed and app get real RX.")
        except Exception as exc:
            log.error("Modem link FAILED: %s - the node runs WITHOUT "
                      "the radio (honest gap; WebServe still serves)", exc)
    try:
        done, _pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            exc = task.exception()
            if exc and task.get_name() != "stop":
                log.error("%s died: %r", task.get_name(), exc)
    finally:
        brain.stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runner.cleanup()
        if radio is not None:
            await radio.stop()
    return 0


def _is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


async def _watch_modem(modem, stop: asyncio.Event) -> None:
    """Loud modem-link watcher: a dropped radio link must never die
    silently (answerbot rule). ModemClient reconnects on its own; this
    task just records the truth in the log."""
    was = modem.connected
    log.info("modem-watch: link %s", "up" if was else "down (retrying)")
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass
        now = modem.connected
        if now != was:
            log.warning("modem link %s", "UP" if now else
                        "DOWN - cleanmodem is reconnecting")
            was = now


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        return asyncio.run(_main())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
