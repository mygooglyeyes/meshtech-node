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
        tx_enabled=settings.feed.tx_enabled,   # C2: config is the ONE
        bench_no_radio=bench_no_radio,         # source (Gate 1 default
    )                                          # False in the template)
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
    brain.tx_enabled = settings.feed.tx_enabled   # C2: one source

    # DISK MEMORY (2026-09-21, Brett: "use the database we made"): the
    # plugin's SQLite store adopted for nodes + repeaters. Every fact
    # is written through as learned; the boot refill below restores
    # what a restart would otherwise forget. Raw packets are never
    # stored (scope rule - hilltop processes them; the RAM window is
    # the only packet memory). Companion mode skips it: a companion's
    # tables would duplicate its host's.
    if not settings.feed.companion_mode:
        from .node_store import NodeStore
        try:
            disk = NodeStore(settings.storage.db_path)
        except Exception:
            log.exception("node database FAILED to open at %s - running "
                          "memory-only (honest gap, logged once)",
                          settings.storage.db_path)
            disk = None
        if disk is not None:
            source.repeaters.sink = disk
            brain.store.disk = disk
            # BOOT REFILL: disk -> RAM before the first packet flows, so
            # a restart forgets nobody (the map is whole in seconds).
            # ROUTE MEMORY (2026-09-24): routes refill too - they were
            # never meant to die with the process.
            n_nodes = brain.store.refill_nodes(disk.node_rows())
            n_tags = source.repeaters.refill_from(disk.repeater_rows())
            n_routes = brain.store.refill_routes(disk.route_rows())
            if n_nodes or n_tags or n_routes:
                log.info("disk memory restored: %d node(s), %d repeater "
                         "tag(s), %d route(s) from %s", n_nodes, n_tags,
                         n_routes, settings.storage.db_path)

    # Connect-time PULSE (Brett 2026-09-21): a new web client gets the
    # Feed-health card filled immediately. HOST feeds only - a companion
    # has no host pulse to give (heard packets are its map).
    # with_layout (Brett, same day): the connect burst ALSO carries the
    # LAYOUT map frame, so the area grid draws the moment the app
    # connects - no refresh press needed.
    connect_pulse = (None if settings.feed.companion_mode
                     else lambda **kw: brain.pulse_now(with_layout=True,
                                                       **kw))
    serve = webserve.WebServe(
        settings.webserve.host, settings.webserve.port,
        token=_token(settings),
        on_client_connected=connect_pulse,
        feed_info={"tx_enabled": settings.feed.tx_enabled,
                   "companion_mode": settings.feed.companion_mode,
                   "feed": {"channel": settings.channel.name,
                            "bench_no_radio": bench_no_radio}},
        state_provider=lambda: _state_snapshot(brain, source),
    )
    serve.on_refresh = brain.on_packet
    # HONEST REFUSALS (2026-09-24): the WS layer pre-checks the same
    # limiter the brain dispatches through, so a refused ask is ACKED
    # with its req_id ("cooldown, retry in Ns") instead of silence.
    serve.refresh_verdict = lambda conn_id, span_km: \
        brain.rate.verdict(conn_id, span_km=span_km)
    source.on_scope = brain.on_packet   # RX shim: heard #scope -> brain
    brain.feed_tap = serve         # FeedTap: every built packet -> WS
    brain.bench_no_radio = bench_no_radio
    if settings.feed.companion_mode:
        # COMPANION MODE: the app is fed by what the companion HEARS.
        # Hilltop's layout/sections/pulse arrive over the air (or via
        # the TCP observer feed); the tap serves them with the SAME
        # packet message shape, snr from the real radio hop when the
        # modem reports one (None stays None - honesty rule).
        def _tap_heard(data_type: int, plaintext: bytes,
                       rx: object) -> None:
            serve.on_heard_packet(
                data_type, plaintext,
                snr=getattr(rx, "snr", None))
        source.on_heard = _tap_heard
    # disk handle travels on the source's repeater table (single owner;
    # main() closes it on shutdown via _close_disk). None in companion
    # mode or when the open failed (memory-only posture).
    return source, sender, brain, serve


def _disk_of(source) -> object:
    """The NodeStore handle (None when companion/memory-only)."""
    return getattr(source.repeaters, "sink", None)


def _close_disk(source) -> None:
    """Commit + close the database on shutdown (best-effort)."""
    disk = _disk_of(source)
    if disk is not None:
        try:
            disk.close()
        except Exception:
            log.exception("node database close failed (WAL committed "
                          "up to the last checkpoint)")


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

    # SHUTDOWN_TIMEOUT=1 (2026-09-21, the slow-restart fix): aiohttp's
    # default (60.1 s) waits for OPEN WEBSOCKETS during cleanup, so a
    # single connected browser made every restart hang for a minute
    # (Brett: "disconnect the app and it restarts immediately"). One
    # second is ample for our own graceful WS close below.
    runner = web.AppRunner(serve.app, shutdown_timeout=1.0)
    await runner.setup()
    site = web.TCPSite(runner, serve.host, serve.port)
    await site.start()
    log.info("WebServe listening on %s:%d/feed (direct mode ready)",
             serve.host, serve.port)
    if settings.feed.companion_mode:
        log.info("=" * 62)
        log.info("COMPANION DEVICE: listening to %s:%s - the map builds "
                 "from heard packets. Host feed parked, TX impossible, "
                 "refreshes refused (listen_only). This is the phone-app "
                 "simulation.", settings.companion_host,
                 settings.companion_port)
        log.info("=" * 62)
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
        # COMPANION MODE: the link IS the device's whole purpose (the
        # PC has no radio) - a hilltop reboot must never leave it deaf
        # until a human restarts. The client retries forever on its
        # own; the transport's one-shot start is replaced here by a
        # patient start loop that outlives the outage.
        if settings.feed.companion_mode:
            tasks.insert(0, asyncio.create_task(
                _companion_link(modem, stop), name="companion-link"))
        else:
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
        exit_code = 0
        for task in done:
            exc = task.exception()
            if exc and task.get_name() != "stop":
                log.error("%s died: %r", task.get_name(), exc)
                # C1 (2026-09-20 review): a dead core task must mean a
                # FAILED process, not a clean one - systemd's
                # Restart=on-failure ignores exit 0, so a clean exit
                # left the box dark until a human noticed.
                exit_code = 1
        return exit_code
    finally:
        brain.stop()
        # close_all_clients BEFORE cleanup: the browser learns "node
        # restarting" instantly instead of waiting out the shutdown
        # timeout (the slow-restart bug, Brett 2026-09-21).
        await serve.close_all_clients()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runner.cleanup()
        if radio is not None:
            await radio.stop()
        _close_disk(source)   # commit + close the database last


def _is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


async def _companion_link(modem, stop: asyncio.Event) -> None:
    """COMPANION MODE's patient link.

    Layered retries, each with its own job:
    - ModemClient.run() retries the TCP session forever (its own loop)
      - that covers hilltop reboots and network blips.
    - the transport task dying (only possible while start() was still
      waiting, or a fatal client crash) is covered HERE: the monitor
      restarts the transport with backoff. `alive` guards the race -
      we never start a second client against a live retrying one.
    The log carries every transition, loud (answerbot rule)."""
    delay = 2.0
    announced = False
    while not stop.is_set():
        if not modem.alive:
            try:
                await modem.start()
                delay = 2.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("companion link not up yet (%s) - "
                            "retry in %.0fs", exc, delay)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, 30.0)
                continue
        if modem.connected and not announced:
            log.info("COMPANION LINK UP - hearing %s:%s; the app is fed "
                     "from heard packets", modem.host, modem.port)
            announced = True
        elif not modem.connected and announced:
            log.warning("companion link DOWN - the client is retrying")
            announced = False
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass


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
