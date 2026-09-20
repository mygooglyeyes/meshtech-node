"""RadioHal - the interface between the network server and the radio.

The server (asyncio) never touches SPI or GPIO. It talks to a RadioHal:

- work items go IN through tx()/cad()/noise()/status()/apply_config(),
  each returning an awaitable result;
- radio events come OUT through on_rx_packet/on_tx_result callbacks.

SX126xRadio runs all hardware I/O on one dedicated worker thread (the
only thread allowed to touch the radio), so two clients can never
interleave SPI operations mid-frame and the asyncio event loop never
blocks on hardware. A FakeRadio with the same interface runs the whole
server in tests without hardware.

Work items are executed strictly one at a time (FIFO) - that is the
TX serialization guarantee.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

log = logging.getLogger("cleanmodem.hal")


@dataclass
class RxPacket:
    """One received radio packet with its signal metadata."""
    rssi: int                 # dBm
    snr: float                # dB
    signal_rssi: int          # dBm (instantaneous, pre-average)
    data: bytes               # raw radio bytes
    mono: float = 0.0         # time.monotonic() when the IRQ was handled
                              # (the IRQ→fan-out latency baseline)


@dataclass
class TxResult:
    """Outcome of one transmission."""
    ok: bool
    airtime_us: int = 0       # measured on-air time when ok
    error: str = ""


@dataclass
class RadioStatus:
    """Everything the STATUS command reports."""
    uptime_s: int = 0
    rx_count: int = 0
    tx_count: int = 0
    crc_errors: int = 0
    last_rssi: int = -100
    last_snr_x10: int = 0
    noise_x10: int = -1050
    radio_state: int = 1      # 1 = RX (the modem's steady state)
    hal_alive: bool = True
    # v0.0.155 RX-deafness diagnostics (hilltop): poll/edge counts and
    # the last raw IRQ flag word make the interrupt path visible
    # through the status probe instead of failing silently.
    irq_polls: int = 0
    irq_edges: int = 0
    last_irq_flags: int = 0


class RadioHal:
    """Abstract radio interface (see SX126xRadio for the real one)."""

    def __init__(self) -> None:
        self.on_rx_packet: Optional[Callable[[RxPacket], None]] = None
        self.on_hal_error: Optional[Callable[[str], None]] = None

    # -- lifecycle (awaitable from asyncio) --------------------------------
    async def start(self, loop: asyncio.AbstractEventLoop) -> bool:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError

    # -- operations (awaitable from asyncio) -------------------------------
    async def tx(self, data: bytes) -> TxResult:
        raise NotImplementedError

    async def cad(self, det_peak: int = 0, det_min: int = 0) -> bool:
        """True = channel busy. 0/0 thresholds = the configured pair."""
        raise NotImplementedError

    async def noise(self) -> float:
        """Instantaneous noise floor in dBm."""
        raise NotImplementedError

    async def status(self) -> RadioStatus:
        raise NotImplementedError

    async def apply_config(self, cfg: Any) -> bool:
        """Push radio parameters (freq/sf/bw/...). False on failure."""
        raise NotImplementedError


class _Work:
    """One queued work item: fn runs on the radio thread, result lands
    on the asyncio loop that submitted it."""
    __slots__ = ("fn", "loop", "future", "event", "value", "exc")

    def __init__(self, fn: Callable[[], Any], loop=None,
                 future: Optional[asyncio.Future] = None):
        self.fn = fn
        self.loop = loop
        self.future = future
        self.event: Optional[threading.Event] = None
        self.value: Any = None
        self.exc: Optional[BaseException] = None

    def complete(self) -> None:
        """Radio thread side: store the outcome and wake the waiter."""
        if self.loop is not None and self.future is not None:
            def _set() -> None:
                if not self.future.done():
                    if self.exc is not None:
                        self.future.set_exception(self.exc)
                    else:
                        self.future.set_result(self.value)
            try:
                self.loop.call_soon_threadsafe(_set)
            except RuntimeError:  # nosec B110 - loop closed during shutdown; result delivery is moot
                pass                    # loop closed during shutdown
        elif self.event is not None:
            self.event.set()

    def wait_sync(self, timeout: float = 10.0) -> Any:
        """Blocking variant for non-asyncio callers (tests, scripts)."""
        if self.event is None:
            raise RuntimeError("not a sync work item")
        if not self.event.wait(timeout):
            raise TimeoutError("radio work item timed out")
        if self.exc is not None:
            raise self.exc
        return self.value


class ThreadedHal(RadioHal):
    """Base class: one worker thread, FIFO work queue, asyncio bridge.

    Subclasses implement _hw_* methods (all hardware I/O, called only on
    the worker thread) and hardware does not leak past this class.
    """

    WATCHDOG_INTERVAL_S = 30.0
    WATCHDOG_FAIL_LIMIT = 3

    def __init__(self) -> None:
        super().__init__()
        self._queue: "queue.SimpleQueue" = queue.SimpleQueue()
        self._thread: Optional[threading.Thread] = None
        self._stop_flag = False
        self._started_at = 0.0
        self._hw_alive = False
        self._watchdog_fails = 0
        self._state = "down"            # down | rx | tx | cad
        # Counters surfaced by status().
        self.rx_count = 0
        self.tx_count = 0
        self.crc_errors = 0
        self.tx_timeouts = 0
        self.hw_reinits = 0
        self.last_rssi = -100
        self.last_snr = 0.0
        # NOTE: deliberately NOT setting self.noise here - RadioHal.noise
        # is the ASYNC METHOD the server calls for every NOISE_REQ, and an
        # instance attribute of the same name shadows it: self.hal.noise()
        # then raises 'float' object is not callable and every read dies
        # into the NO-VALUE sentinel. That shadow sat here from the first
        # cleanmodem commit; the pre-v0.0.183 silent -105.0 fallback made
        # the dashboard show a plausible constant instead of an error -
        # the REAL story of the frozen -105 line. The live value travels
        # in RadioStatus.noise_x10 (see SX126xRadio._hw_status).

    # ── lifecycle ─────────────────────────────────────────────────────
    async def start(self, loop: asyncio.AbstractEventLoop) -> bool:
        """Spawn the worker thread, then bring the hardware up ON it -
        bring-up (reset pulses, TCXO settle) must never run on the
        asyncio loop."""
        self._loop = loop
        self._stop_flag = False
        self._thread = threading.Thread(
            target=self._worker, name="radio-hal", daemon=True)
        self._thread.start()
        try:
            await self._submit(self._hw_begin)
        except Exception as exc:
            log.error("radio bring-up failed: %s", exc)
            self._stop_flag = True
            self._thread.join(timeout=5.0)
            self._thread = None
            return False
        self._started_at = time.monotonic()
        return True

    async def stop(self) -> None:
        # Graceful hardware shutdown on the worker (if it can take one
        # more item), then stop the loop and join.
        if self._thread is not None and self._thread.is_alive():
            try:
                await self._submit(self._hw_shutdown)
            except Exception as exc:
                log.warning("radio shutdown submit failed: %s", exc)
        self._stop_flag = True
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    # ── asyncio operations ────────────────────────────────────────────
    async def tx(self, data: bytes) -> TxResult:
        return await self._submit(lambda: self._hw_tx(data))

    async def cad(self, det_peak: int = 0, det_min: int = 0) -> bool:
        return await self._submit(
            lambda: self._hw_cad(det_peak, det_min))

    async def noise(self) -> float:
        return await self._submit(self._hw_noise)

    async def status(self) -> RadioStatus:
        return await self._submit(self._hw_status)

    async def apply_config(self, cfg: Any) -> bool:
        return await self._submit(lambda: self._hw_apply_config(cfg))

    # ── queue bridge ──────────────────────────────────────────────────
    async def _submit(self, fn: Callable[[], Any]) -> Any:
        if self._thread is None or not self._thread.is_alive():
            raise RuntimeError("radio thread not running")
        future: asyncio.Future = self._loop.create_future()
        self._queue.put(_Work(fn, self._loop, future))
        return await future

    def _submit_sync(self, fn: Callable[[], Any]) -> Any:
        """Submit before/without the worker thread (init + shutdown)."""
        work = _Work(fn)
        work.event = threading.Event()
        self._run_work(work)             # init runs on the calling thread
        if work.exc is not None:
            raise work.exc
        return work.value

    def _run_work(self, work: _Work) -> None:
        try:
            work.value = work.fn()
        except BaseException as exc:     # noqa: BLE001 - report, don't die
            work.exc = exc
            log.error("radio work failed: %s", exc)
            if self.on_hal_error is not None:
                try:
                    self.on_hal_error(str(exc))
                except Exception:  # nosec B110 - error hook must never take down the radio thread
                    pass
        work.complete()

    # ── worker thread ─────────────────────────────────────────────────
    def _worker(self) -> None:
        """Serial work loop + hardware watchdog. The ONLY hardware thread."""
        last_probe = time.monotonic()
        while not self._stop_flag:
            try:
                work = self._queue.get(timeout=1.0)
            except Exception:
                work = None
            if work is not None:
                self._run_work(work)
                last_probe = time.monotonic()
            if (time.monotonic() - last_probe >= self.WATCHDOG_INTERVAL_S
                    and not self._stop_flag):
                last_probe = time.monotonic()
                self._watchdog()
        log.info("radio thread stopped")

    def _watchdog(self) -> None:
        """Periodic liveness probe; repeated failures trigger re-init."""
        try:
            self._hw_probe()
            self._watchdog_fails = 0
            if not self._hw_alive:
                log.info("radio watchdog: hardware is alive again")
            self._hw_alive = True
        except Exception as exc:
            self._watchdog_fails += 1
            self._hw_alive = False
            log.warning("radio watchdog probe failed (%d/%d): %s",
                        self._watchdog_fails, self.WATCHDOG_FAIL_LIMIT, exc)
            if self._watchdog_fails >= self.WATCHDOG_FAIL_LIMIT:
                try:
                    log.warning("radio watchdog: re-initializing hardware")
                    self._hw_begin()
                    self._watchdog_fails = 0
                    self.hw_reinits += 1
                    self._hw_alive = True
                except Exception as exc2:
                    log.error("radio watchdog re-init failed: %s", exc2)

    # ── hardware methods (worker thread only; subclass implements) ────
    def _hw_begin(self) -> bool:
        raise NotImplementedError

    def _hw_shutdown(self) -> None:
        raise NotImplementedError

    def _hw_probe(self) -> None:
        raise NotImplementedError

    def _hw_tx(self, data: bytes) -> TxResult:
        raise NotImplementedError

    def _hw_cad(self, det_peak: int, det_min: int) -> bool:
        raise NotImplementedError

    def _hw_noise(self) -> float:
        raise NotImplementedError

    def _hw_status(self) -> RadioStatus:
        raise NotImplementedError

    def _hw_apply_config(self, cfg: Any) -> bool:
        raise NotImplementedError
