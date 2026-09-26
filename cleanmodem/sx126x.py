"""Clean-room SX1262 radio driver (SPI) behind the RadioHal interface.

Written from the SX126x datasheet's command set; the opcode table and
register conventions below are hardware facts, and the signal-metric
formulas / sync-word nibble rule were verified against the reference
driver's behavior before shipping (RSSI = raw/-2.0, SNR = signed/4.0,
sync word <= 0xFF maps to nibbles |0x04 at register 0x0740).

Design rules (the reasons this file looks the way it does):

- ALL hardware I/O runs on the worker thread inherited from ThreadedHal.
  The asyncio loop never touches SPI or GPIO (a blocking SPI transfer
  on the loop would stall every connected client).
- Work items are executed strictly FIFO by the worker, which serializes
  TX: two concurrent senders can never interleave SPI ops mid-frame.
- DIO1 (packet done / CAD done) is consumed as a GPIO edge event with a
  short blocking wait on the worker thread - no spin-polling, no extra
  threads; wake-up latency is single-digit milliseconds when idle.
- The BUSY line is short-polled with a bounded timeout: hardware
  handshakes may not hang the modem.
- The worker also runs a periodic liveness probe (from the base class)
  and re-initializes the chip after repeated failures, so a wedged SPI
  transaction cannot silence the modem forever.
- Front-end pins (txen/rxen/lna) are optional: unset (-1) means the
  board switches in silicon (DIO2 as RF switch) or has no LNA.
"""
from __future__ import annotations

import logging
import queue as _queue
import struct
import threading
import time
from typing import Any, Callable, Optional, Protocol

from .hal import RadioStatus, RxPacket, ThreadedHal, TxResult

log = logging.getLogger("cleanmodem.sx126x")

# ─── SX126x command opcodes (datasheet §13) ─────────────────────────
# Opcode table (datasheet §13.1; cross-checked against LoRaRF-Python,
# the proven driver vendored by openhop_core for this exact E22
# module). v0.0.167 fixes four entries that were misnumbered and made
# every clocked command EXEC_FAIL on the hilltop trace:
#   SetTxParams        0x8D -> 0x8E  (old: nonexistent command)
#   SetBufferBaseAddr  0x8E -> 0x8F  (old: SetTxParams was never sent)
#   SetDIO3AsTcxoCtrl  0xD4 -> 0x97  (old: unknown opcode)
#   SetSyncWord        0x8F (cmd) -> none: register write to 0x0740
# The old block was shifted by one, and the "sync word" command was
# actually a stray buffer-base write.
OP_SET_STANDBY = 0x80
OP_SET_TX_PARAMS = 0x8E
OP_SET_CAD = 0xC5
OP_SET_RF_FREQUENCY = 0x86
OP_SET_PACKET_TYPE = 0x8A
OP_SET_MODULATION_PARAMS = 0x8B
OP_SET_PACKET_PARAMS = 0x8C
OP_SET_CAD_PARAMS = 0x88
OP_SET_BUFFER_BASE_ADDRESS = 0x8F
OP_SET_DIO_IRQ_PARAMS = 0x08
OP_CLEAR_IRQ_STATUS = 0x02
OP_GET_IRQ_STATUS = 0x12
OP_GET_RX_BUFFER_STATUS = 0x13
OP_GET_PACKET_STATUS = 0x14
OP_GET_RSSI_INST = 0x15
OP_WRITE_BUFFER = 0x0E
OP_READ_BUFFER = 0x1E
OP_SET_TX = 0x83
OP_SET_RX = 0x82
OP_SET_TX_CONTINUOUS_WAVE = 0x81
OP_SET_PA_CONFIG = 0x95
OP_SET_REGULATOR_MODE = 0x96
OP_WRITE_REGISTER = 0x0D        # register write (sync word lives there)
OP_SET_DIO2_AS_RF_SWITCH = 0x9D
OP_SET_DIO3_AS_TCXO_CTRL = 0x97
OP_CALIBRATE = 0x89
OP_CALIBRATE_IMAGE = 0x98

# IRQ flag bits (GetIrqStatus / ClearIrqStatus)
IRQ_TX_DONE = 0x0001
IRQ_RX_DONE = 0x0002
IRQ_PREAMBLE_DETECTED = 0x0004
IRQ_SYNCWORD_VALID = 0x0008
IRQ_HEADER_VALID = 0x0010
IRQ_HEADER_ERR = 0x0020
IRQ_CRC_ERR = 0x0040
# v0.0.054 (2026-09-26): the CAD pair was MISTRANSLATED - DONE sat
# on 0x0100 (the DETECTED bit) and DETECTED on 0x0200 (the TIMEOUT
# bit), so a QUIET channel never matched "finished" and every probe
# died with 'CAD timed out' (hilltop 2026-09-25/26). Numbers verified
# against the reference driver: openhop_core
# src/pymc_core/hardware/lora/LoRaRF/SX126x.py (IRQ_CAD_DONE =
# 0x0080, IRQ_CAD_DETECTED = 0x0100, IRQ_TIMEOUT = 0x0200).
IRQ_CAD_DONE = 0x0080
IRQ_CAD_DETECTED = 0x0100
IRQ_TIMEOUT = 0x0200
IRQ_ALL = 0x03FF

# LoRa packet type
PACKET_TYPE_LORA = 0x01

# Modulation params: SX126x bandwidth register codes (datasheet §13.4.4)
BW_7800, BW_10400, BW_15600, BW_20800 = 0x00, 0x08, 0x01, 0x09
BW_31250, BW_41700, BW_62500, BW_125000 = 0x02, 0x0A, 0x03, 0x04
BW_250000, BW_500000 = 0x05, 0x06

# SetDIO3AsTcxoCtrl voltage codes (datasheet §13.1.13)
TCXO_VOLTAGE_CODES = {1.6: 0x00, 1.7: 0x01, 1.8: 0x02, 2.2: 0x03,
                      2.4: 0x04, 2.7: 0x05, 3.0: 0x06, 3.3: 0x07}

# SX1262 PA config for the 22 dBm high-power table
PA_CONFIG_SX1262 = bytes([0x04, 0x07, 0x00, 0x01])

# TCXO needs a settle delay after powering up (datasheet: 5 ms typical)
TCXO_SETTLE_S = 0.005


def _tcxo_ctrl_params(voltage: float) -> list[int]:
    """SetDIO3AsTcxoCtrl payload: voltage code + 3-byte timeout field.

    v0.0.166: the FULL four bytes (the old 3-byte command was
    truncated and rejected). v0.0.167: timeout 0x000560 (LoRaRF's
    proven TCXO_DELAY for a 1.8 V module) - the old zero timeout is
    another documented EXEC_FAIL source, and the hilltop trace still
    failed with the 4-byte form.
    """
    return [TCXO_VOLTAGE_CODES[voltage], 0x00, 0x05, 0x60]


def _calibrate_image_pair(frequency_hz: int) -> list[int]:
    """CalibrateImage band pair (LoRaRF calibrateImagePairs).

    v0.0.167: 902-928 is (0xE1, 0xE9) and 863-870 is (0xD7, 0xDB).
    The old (0x7B, 0x81) was an invalid pair - the chip answered
    EXEC_FAIL (hilltop trace 2026-09-14).
    """
    if 863_000_000 <= frequency_hz <= 870_000_000:
        return [0xD7, 0xDB]
    if 902_000_000 <= frequency_hz <= 928_000_000:
        return [0xE1, 0xE9]
    return []


def _sync_word_bytes(word: int) -> bytes:
    """Sync-word register bytes for register 0x0740.

    Values <= 0xFF map to nibbles |0x04 (0x12 -> 0x1424), the
    MeshCore convention per the reference driver.
    """
    if word <= 0xFF:
        return bytes([(word & 0xF0) | 0x04,
                      ((word << 4) | 0x04) & 0xFF])
    return struct.pack(">H", word)

XTAL_FREQ_HZ = 32_000_000
PLL_STEP_SHIFT = 25               # freq * 2^25 / XTAL

# Sync word: MeshCore convention uses config value 0x12, which maps to
# register nibbles |0x04 (0x12 -> 0x1424) per the reference driver.
SYNC_WORD_REGISTER = 0x0740


class RadioHwError(RuntimeError):
    """Hardware operation failed (busy timeout, SPI error, ...)."""


class GpioLines(Protocol):
    """Minimal GPIO surface the driver needs (test fakes implement this)."""

    def read(self, pin: int) -> int: ...
    def write(self, pin: int, value: int) -> None: ...
    def wait_edge(self, pin: int, timeout: float) -> bool: ...
    def close(self) -> None: ...


class SpiBus(Protocol):
    """Minimal SPI surface (test fakes implement this)."""

    def transfer(self, data: bytes) -> bytes: ...
    def close(self) -> None: ...


class _RpiGpio:
    """GPIO backend using RPi.GPIO (the classic Pi library).

    Edge waits use RPi.GPIO's background event detection; the callback
    only sets a threading.Event that wait_edge() observes, so all real
    work still happens on the radio thread.
    """

    def __init__(self) -> None:
        import RPi.GPIO as GPIO     # noqa: N813 - the library's name
        self._gpio = GPIO
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        self._edge_events: dict[int, threading.Event] = {}
        self._outputs: set[int] = set()

    def setup_out(self, pin: int, initial: int) -> None:
        self._gpio.setup(pin, self._gpio.OUT, initial=initial)
        self._outputs.add(pin)

    def setup_in(self, pin: int) -> None:
        self._gpio.setup(pin, self._gpio.IN, pull_up_down=self._gpio.PUD_DOWN)

    def _arm_edge(self, pin: int) -> threading.Event:
        event = self._edge_events.get(pin)
        if event is None:
            event = threading.Event()
            self._edge_events[pin] = event
            self._gpio.add_event_detect(
                pin, self._gpio.RISING,
                callback=lambda _ch: event.set())
        event.clear()
        return event

    def read(self, pin: int) -> int:
        return int(self._gpio.input(pin))

    def write(self, pin: int, value: int) -> None:
        self._gpio.output(pin, value)

    def wait_edge(self, pin: int, timeout: float) -> bool:
        return self._arm_edge(pin).wait(timeout)

    def close(self) -> None:
        try:
            self._gpio.cleanup()
        except Exception:  # nosec B110 - best-effort GPIO cleanup  # pragma: no cover
            pass


class _GpiodGpio:
    """GPIO backend using the gpiod v2 Python bindings.

    v0.0.158: targets the v2 API (gpiod.Chip + request_lines +
    LineSettings). The earlier v1 attempt died twice on hilltop: first
    the v2-only name in a v1 body (fixed in v0.0.157), then the v1 pip
    bindings themselves crash on Debian 13's v2 C library ('iter()
    returned non-iterator'). gpiod 2.5.0 ships cp313 aarch64 wheels and
    links the v2 libgpiod Debian 13 already installs, so this backend
    finally runs where it was always meant to.

    Lines are owned via one request per pin, reconfigured in place for
    edge waits - the SX126x BUSY line is input most of the time and
    only needs an edge request while waiting on DIO1.
    """

    def __init__(self, path: str = "/dev/gpiochip0") -> None:
        import gpiod                    # lazy: optional dependency
        self._gpiod = gpiod
        self._chip = gpiod.Chip(path)
        self._reqs: dict[int, Any] = {}

    def _request(self, pin: int, settings: Any) -> Any:
        req = self._reqs.get(pin)
        if req is not None:
            req.release()
        req = self._chip.request_lines(
            config={pin: settings}, consumer="cleanmodem")
        self._reqs[pin] = req
        return req

    def setup_out(self, pin: int, initial: int) -> None:
        g = self._gpiod
        self._request(pin, g.LineSettings(
            direction=g.line.Direction.OUTPUT,
            output_value=(g.line.Value.ACTIVE if initial
                          else g.line.Value.INACTIVE)))

    def setup_in(self, pin: int) -> None:
        self._request(pin, self._gpiod.LineSettings(
            direction=self._gpiod.line.Direction.INPUT,
            bias=self._gpiod.line.Bias.PULL_DOWN))

    def read(self, pin: int) -> int:
        return int(self._reqs[pin].get_value(pin)
                   == self._gpiod.line.Value.ACTIVE)

    def write(self, pin: int, value: int) -> None:
        self._reqs[pin].set_value(
            pin, self._gpiod.line.Value.ACTIVE if value
            else self._gpiod.line.Value.INACTIVE)

    def wait_edge(self, pin: int, timeout: float) -> bool:
        g = self._gpiod
        req = self._reqs.get(pin)
        if req is None:
            req = self._request(pin, g.LineSettings(
                direction=g.line.Direction.INPUT,
                edge_detection=g.line.Edge.RISING))
        else:
            req.reconfigure_lines(config={pin: g.LineSettings(
                direction=g.line.Direction.INPUT,
                edge_detection=g.line.Edge.RISING)})
        fired = req.wait_edge_events(timeout)
        if fired:
            req.read_edge_events()
        return fired

    def close(self) -> None:
        for req in self._reqs.values():
            try:
                req.release()
            except Exception:  # nosec B110 - best-effort release  # pragma: no cover
                pass
        self._reqs.clear()
        try:
            self._chip.close()
        except Exception:  # nosec B110 - best-effort chip release  # pragma: no cover
            pass


class _NullGpio:
    """GPIO backend for tests / dry runs (no hardware attached)."""

    def __init__(self, busy_always_clear: bool = True) -> None:
        self.busy_always_clear = busy_always_clear

    def setup_out(self, pin: int, initial: int) -> None:
        pass

    def setup_in(self, pin: int) -> None:
        pass

    def read(self, pin: int) -> int:
        return 0

    def write(self, pin: int, value: int) -> None:
        pass

    def wait_edge(self, pin: int, timeout: float) -> bool:
        time.sleep(min(timeout, 0.05))
        return False

    def close(self) -> None:
        pass


class _SpidevBus:
    """SPI backend using spidev (the classic Pi library)."""

    def __init__(self, bus: int, device: int, speed_hz: int) -> None:
        import spidev                   # lazy: optional dependency
        self._spi = spidev.SpiDev()
        self._spi.open(bus, device)
        self._spi.max_speed_hz = speed_hz
        self._spi.mode = 0b00
        self._spi.bits_per_word = 8

    def transfer(self, data: bytes) -> bytes:
        return bytes(self._spi.xfer2(list(data)))

    def close(self) -> None:
        self._spi.close()


class _NullSpi:
    """SPI backend for tests / dry runs: reads come back as zeroes."""

    def transfer(self, data: bytes) -> bytes:
        return bytes(len(data))

    def close(self) -> None:
        pass


# Backend factory hooks (tests may monkeypatch these).
_make_gpio: Optional[Callable[[bool], Any]] = None
_make_spi: Optional[Callable[[int, int, int], Any]] = None


def _default_gpio(force_null: bool, backend: str = "auto") -> Any:
    """GPIO backend factory. backend: auto | gpiod | rpi (v0.0.156).

    "auto" keeps the historical order (gpiod, then RPi.GPIO). Explicit
    selections fail loud instead of falling back - a silent backend
    switch is exactly what made the hilltop RX deafness hard to see.
    Tests may monkeypatch _make_gpio.
    """
    if force_null:
        return _NullGpio()
    if backend == "gpiod":
        return _GpiodGpio()          # no fallback - fail loud
    if backend == "rpi":
        return _RpiGpio()            # no fallback - fail loud
    if _make_gpio is not None:
        return _make_gpio(False)
    try:
        return _GpiodGpio()
    except Exception:
        return _RpiGpio()


def _default_spi(bus: int, device: int, speed_hz: int) -> Any:
    if _make_spi is not None:
        return _make_spi(bus, device, speed_hz)
    return _SpidevBus(bus, device, speed_hz)


def _snr_signed(raw: int) -> float:
    """SX126x SNR register: signed byte in 0.25 dB steps."""
    value = raw & 0xFF
    return (value - 256 if value > 127 else value) / 4.0


def _rssi_dbm(raw: int) -> int:
    """SX126x packet RSSI: -0.5 dBm per LSB."""
    return int(round(raw / -2.0))


def _bw_register(bandwidth_hz: int) -> int:
    """Bandwidth Hz -> SetModulationParams register code."""
    if bandwidth_hz < 9100:
        return BW_7800
    if bandwidth_hz < 13000:
        return BW_10400
    if bandwidth_hz < 18200:
        return BW_15600
    if bandwidth_hz < 26000:
        return BW_20800
    if bandwidth_hz < 36500:
        return BW_31250
    if bandwidth_hz < 52100:
        return BW_41700
    if bandwidth_hz < 93800:
        return BW_62500
    if bandwidth_hz < 187500:
        return BW_125000
    if bandwidth_hz < 375000:
        return BW_250000
    return BW_500000


def _timeout_steps(seconds: float) -> bytes:
    """SetTx/SetRx timeout: 15.625 us steps, 24-bit (3 bytes, big-endian).
    0xFFFFFFFF means single-shot off; we clamp to the field's max."""
    steps = int(seconds / 15.625e-6)
    steps = max(1, min(steps, 0xFFFFFF))
    return bytes([(steps >> 16) & 0xFF, (steps >> 8) & 0xFF, steps & 0xFF])


def _rf_frequency(freq_hz: int) -> bytes:
    """SetRfFrequency payload: freq * 2^25 / 32 MHz, 32-bit big-endian."""
    value = int(freq_hz * (1 << PLL_STEP_SHIFT) / XTAL_FREQ_HZ)
    return struct.pack(">I", value)


class SX126xRadio(ThreadedHal):
    """Clean-room SX126x driver: worker thread + FIFO work + IRQ edge."""

    BUSY_TIMEOUT_S = 1.0
    BUSY_POLL_S = 0.0002
    TX_TIMEOUT_S = 2.0               # SetTx hardware timeout (SF7 mesh)
    CAD_TIMEOUT_S = 0.15
    IRQ_WAIT_S = 0.05                # idle edge-wait slice
    POLL_IRQ_S = 0.05                # polling-mode flag-poll interval

    def __init__(self, pins: dict, *, frequency_hz: int = 910525000,
                 tx_power_dbm: int = 20, spreading_factor: int = 7,
                 coding_rate: int = 5, bandwidth_hz: int = 62500,
                 sync_word: int = 0x12, preamble_length: int = 32,
                 spi_bus: int = 0, spi_device: int = 0,
                 spi_speed_hz: int = 2_000_000,
                 cad_peak: int = 22, cad_min: int = 10,
                 force_null_hw: bool = False,
                 irq_poll_mode: bool = False,
                 gpio_backend: str = "auto") -> None:
        super().__init__()
        self._pins = dict(pins)
        self._radio = {
            "frequency_hz": frequency_hz,
            "tx_power_dbm": tx_power_dbm,
            "spreading_factor": spreading_factor,
            "coding_rate": coding_rate,
            "bandwidth_hz": bandwidth_hz,
            "sync_word": sync_word,
            "preamble_length": preamble_length,
        }
        self._spi_bus = spi_bus
        self._spi_device = spi_device
        self._spi_speed_hz = spi_speed_hz
        self._cad_peak = cad_peak
        self._cad_min = cad_min
        self._force_null = force_null_hw
        # v0.0.155 diagnostics: hilltop RX deafness (2026-09-14) - the
        # IRQ edge event never fired under the rpi-lgpio shim, so RX
        # packets sat unread in the chip with no error logged. irq_poll
        # mode replaces the edge wait with periodic flag polls (costs
        # latency, proves the RF path); the counters make the failure
        # mode VISIBLE through the status probe.
        self.irq_poll_mode = irq_poll_mode
        self.gpio_backend = gpio_backend
        self.irq_polls = 0
        self.irq_edges = 0
        self.last_irq_flags = 0
        self._spi: Optional[SpiBus] = None
        self._gpio: Optional[Any] = None
        self._in_rx = False
        self._rx_mode = False           # SetRx armed (continuous listen)
        self._loop = None

    # ── hardware bring-up (worker thread) ─────────────────────────────
    def _hw_begin(self) -> bool:
        self._gpio = _default_gpio(self._force_null, self.gpio_backend)
        self._spi = (_NullSpi() if self._force_null
                     else _default_spi(self._spi_bus, self._spi_device,
                                       self._spi_speed_hz))
        pins = self._pins
        if pins.get("en", -1) >= 0:
            # Radio power-enable (openHop's proven PiMesh-1W v2 map
            # drives this HIGH before the reset pulse).
            self._gpio.setup_out(pins["en"], 1)
            time.sleep(0.05)
        self._gpio.setup_out(pins["reset"], 1)
        self._gpio.setup_in(pins["busy"])
        if pins.get("dio1", -1) >= 0:
            self._gpio.setup_in(pins["dio1"])
        for key in ("txen", "rxen", "lna"):
            pin = pins.get(key, -1)
            if pin >= 0:
                self._gpio.setup_out(pin, 0)

        # Hardware reset pulse, then the chip answers commands.
        self._gpio.write(pins["reset"], 0)
        time.sleep(0.002)
        self._gpio.write(pins["reset"], 1)
        time.sleep(0.01)

        self._cmd(OP_SET_STANDBY, [0x00])           # STDBY_RC: required before TCXO ctrl
        tcxo = self._pins.get("dio3_tcxo", 0.0)
        if tcxo:
            # Four bytes, from STDBY_RC (see _tcxo_ctrl_params).
            self._cmd(OP_SET_DIO3_AS_TCXO_CTRL, _tcxo_ctrl_params(tcxo))
            time.sleep(TCXO_SETTLE_S)
        if self._pins.get("dio2_rf_switch"):
            self._cmd(OP_SET_DIO2_AS_RF_SWITCH, [0x01])
        # Full calibration: RC64k/RC13M/PLL/ADC (image cal separately).
        self._cmd(OP_CALIBRATE, [0x7F])
        pair = _calibrate_image_pair(self._radio["frequency_hz"])
        if pair:
            self._cmd(OP_CALIBRATE_IMAGE, pair)

        self._cmd(OP_SET_PACKET_TYPE, [PACKET_TYPE_LORA])
        # v0.0.167: the SX126x has NO SetSyncWord command - the sync
        # word lives in register 0x0740 (LoRaRF's setSyncWord writes it
        # there via WriteRegister 0x0D). The old code sent opcode 0x8F,
        # which is actually SetBufferBaseAddress: the "sync word" was a
        # stray buffer-pointer write.
        self._write_register(SYNC_WORD_REGISTER,
                             _sync_word_bytes(self._radio["sync_word"]))
        self._cmd(OP_SET_REGULATOR_MODE, [0x01])    # DC-DC converter
        self._cmd(OP_SET_PA_CONFIG, list(PA_CONFIG_SX1262))
        self._cmd(OP_SET_BUFFER_BASE_ADDRESS, [0x00, 0x00])

        self._apply_radio_params()

        # DIO1 raises on TX done + RX done (+ CAD done for probing).
        mask = IRQ_TX_DONE | IRQ_RX_DONE | IRQ_CAD_DONE | IRQ_CAD_DETECTED
        self._cmd(OP_SET_DIO_IRQ_PARAMS,
                  [mask >> 8, mask & 0xFF, mask >> 8, mask & 0xFF,
                   0x00, 0x00, 0x00, 0x00])
        self._clear_irq(IRQ_ALL)

        # Front-end idle state: LNA powered (RX path), TX line low.
        if self._pins.get("lna", -1) >= 0:
            self._gpio.write(self._pins["lna"], 1)

        self._hw_enter_rx()
        log.info("SX1262 up: %s", self._describe())
        return True

    def _apply_radio_params(self) -> None:
        r = self._radio
        self._cmd(OP_SET_RF_FREQUENCY, list(_rf_frequency(r["frequency_hz"])))
        # Modulation: SF, BW register, CR (index = 4/x - 4), LDRO off at
        # this bandwidth/symbol rate.
        ldro = 1 if (r["bandwidth_hz"] < 100_000
                     and r["spreading_factor"] >= 11) else 0
        self._cmd(OP_SET_MODULATION_PARAMS,
                  [r["spreading_factor"], _bw_register(r["bandwidth_hz"]),
                   r["coding_rate"] - 4, ldro])
        # Packet: preamble (16-bit BE), explicit header, dynamic length,
        # CRC on, IQ standard. (TX power is set per-transmission in
        # _hw_tx via SetTxParams.)
        pre = r["preamble_length"]
        self._cmd(OP_SET_PACKET_PARAMS,
                  [(pre >> 8) & 0xFF, pre & 0xFF, 0x00, 0x00, 0x01, 0x00])

    def _cmd(self, opcode: int, params: list[int]) -> None:
        """One command write (opcode + params), gated on BUSY."""
        self._wait_busy()
        self._spi.transfer(bytes([opcode]) + bytes(params))

    def _write_register(self, addr: int, data: bytes) -> None:
        """WriteRegister (0x0D): 16-bit BE address + data, gated on BUSY."""
        self._wait_busy()
        self._spi.transfer(bytes([OP_WRITE_REGISTER,
                                  (addr >> 8) & 0xFF, addr & 0xFF])
                           + bytes(data))

    def _read_cmd(self, opcode: int, size: int) -> bytes:
        """One command read (opcode + NOPs), gated on BUSY.

        Datasheet layout: MISO = [garbage, status, data...]. v0.0.163
        sliced [2:] (right); v0.0.165 shifted to [3:] because a probe
        'aa aa 00 00 03' looked like flags at 3-4 - but the GetStatus
        decode later proved the chip was stuck in STANDBY (TCXO bug,
        v0.0.166) and that GetStatus repeats its status byte across
        the data window, which is what fooled that decode. Back to
        [2:2+size] - the layout the Semtech driver and the bench both
        use.
        """
        self._wait_busy()
        result = self._spi.transfer(bytes([opcode]) + bytes(size + 2))
        return bytes(result[2:2 + size])

    def _wait_busy(self) -> None:
        deadline = time.monotonic() + self.BUSY_TIMEOUT_S
        while self._gpio.read(self._pins["busy"]):
            if time.monotonic() >= deadline:
                raise RadioHwError("BUSY stuck high for %.1fs" %
                                   self.BUSY_TIMEOUT_S)
            time.sleep(self.BUSY_POLL_S)

    def _clear_irq(self, mask: int) -> None:
        self._cmd(OP_CLEAR_IRQ_STATUS, [mask >> 8, mask & 0xFF])

    def _hw_enter_rx(self) -> None:
        """Arm continuous RX; front-end lines to the receive path."""
        pins = self._pins
        if pins.get("rxen", -1) >= 0:
            self._gpio.write(pins["rxen"], 1)
        if pins.get("lna", -1) >= 0:
            self._gpio.write(pins["lna"], 1)
        if pins.get("txen", -1) >= 0:
            self._gpio.write(pins["txen"], 0)
        # Packet params payload length is dynamic in RX (max 255).
        pre = self._radio["preamble_length"]
        self._cmd(OP_SET_PACKET_PARAMS,
                  [(pre >> 8) & 0xFF, pre & 0xFF, 0x00, 0xFF, 0x01, 0x00])
        self._cmd(OP_SET_RX, [0xFF, 0xFF, 0xFF])    # continuous, no timeout
        self._clear_irq(IRQ_ALL)
        self._in_rx = True
        self._rx_mode = True

    def _hw_tx(self, data: bytes) -> TxResult:
        if len(data) > 255:
            return TxResult(ok=False, error="payload too big")
        pins = self._pins
        # Front-end to the transmit path BEFORE the packet goes out.
        if pins.get("lna", -1) >= 0:
            self._gpio.write(pins["lna"], 0)
        if pins.get("rxen", -1) >= 0:
            self._gpio.write(pins["rxen"], 0)
        if pins.get("txen", -1) >= 0:
            self._gpio.write(pins["txen"], 1)
        self._in_rx = False
        try:
            # Payload length is fixed at TX (explicit header).
            pre = self._radio["preamble_length"]
            self._cmd(OP_SET_PACKET_PARAMS,
                      [(pre >> 8) & 0xFF, pre & 0xFF, 0x00, len(data),
                       0x01, 0x00])
            self._cmd(OP_WRITE_BUFFER, [0x00] + list(data))
            # SetTxParams: power (dBm, SX1262 range -9..22) + ramp 200 us.
            self._cmd(OP_SET_TX_PARAMS,
                      [max(-9, min(22, self._radio["tx_power_dbm"])), 0x04])
            self._clear_irq(IRQ_ALL)
            started = time.monotonic()
            self._cmd(OP_SET_TX, list(_timeout_steps(self.TX_TIMEOUT_S)))
            # Wait for TX_DONE on DIO1 (bounded by the hardware timeout).
            deadline = started + self.TX_TIMEOUT_S + 0.5
            irq = 0
            while time.monotonic() < deadline:
                status = self._read_cmd(OP_GET_IRQ_STATUS, 2)
                irq = (status[0] << 8) | status[1]
                if irq & IRQ_TX_DONE:
                    break
                time.sleep(0.001)
            self._clear_irq(IRQ_ALL)
            airtime_us = int((time.monotonic() - started) * 1_000_000)
            if not irq & IRQ_TX_DONE:
                self.tx_timeouts += 1
                return TxResult(ok=False, airtime_us=airtime_us,
                                error="TX timeout")
            self.tx_count += 1
            return TxResult(ok=True, airtime_us=airtime_us)
        finally:
            # Straight back to listening; front-end back to RX path.
            try:
                self._hw_enter_rx()
            except RadioHwError as exc:
                log.error("re-entering RX after TX failed: %s", exc)

    def _hw_cad(self, det_peak: int, det_min: int) -> bool:
        """One CAD probe. True = channel busy (activity detected).

        v0.0.162: SetCAD is only valid from STANDBY - issued while the
        chip sits in continuous RX it is silently IGNORED, so CAD_DONE
        never comes ('CAD timed out' at every probe, then a wedged
        chip / 'BUSY stuck' on the next command - hilltop 2026-09-14
        first modem-mode TX). Dance: RX -> STDBY -> CAD -> back to RX.
        """
        self._cmd(OP_SET_STANDBY, [0x00])       # STDBY_RC: exit RX
        try:
            peak = det_peak or self._cad_peak
            smallest = det_min or self._cad_min
            # v0.0.164: SetCadParams takes SEVEN param bytes
            # (numSymbols, detPeak, detMin, exitMode, timeout[3]). The
            # old 4-byte command was truncated -> rejected by the chip
            # -> SetCad ran with undefined params and CAD_DONE never
            # came (the second half of the CAD-timeout bug). Timeout
            # field = 0: CAD is one-shot, exit mode 0 = back to STANDBY.
            self._cmd(OP_SET_CAD_PARAMS,
                      [0x04, peak, smallest, 0x00, 0x00, 0x00, 0x00])
            self._clear_irq(IRQ_ALL)
            self._cmd(OP_SET_CAD, [])           # 0xC5: enter CAD
            deadline = time.monotonic() + self.CAD_TIMEOUT_S
            while time.monotonic() < deadline:
                status = self._read_cmd(OP_GET_IRQ_STATUS, 2)
                irq = (status[0] << 8) | status[1]
                if irq & IRQ_CAD_DONE:
                    self._clear_irq(IRQ_ALL)
                    return bool(irq & IRQ_CAD_DETECTED)
                time.sleep(0.001)
            self._clear_irq(IRQ_ALL)
            raise RadioHwError("CAD timed out")
        finally:
            # CAD leaves the chip in STDBY (exit mode 0x00) - arm RX
            # again whatever happened, or the modem goes deaf.
            self._in_rx = False
            self._hw_enter_rx()

    def _hw_noise(self) -> float:
        """Instantaneous RSSI of the channel (dBm) as the noise floor."""
        result = self._read_cmd(OP_GET_RSSI_INST, 1)
        return result[0] / -2.0

    def _hw_status(self) -> RadioStatus:
        # v0.0.183: the noise field was read from self.noise, which no
        # code path ever assigned (ThreadedHal's -105.0 default masked
        # it) - the FIRST STATUS request would have crashed the work
        # item. The live read replaces the never-updated attribute.
        return RadioStatus(
            uptime_s=int(time.monotonic() - self._started_at),
            rx_count=self.rx_count,
            tx_count=self.tx_count,
            crc_errors=self.crc_errors,
            last_rssi=self.last_rssi,
            last_snr_x10=int(round(self.last_snr * 10)),
            noise_x10=int(round(self._hw_noise() * 10)),
            radio_state=1 if self._rx_mode else 0,
            hal_alive=True,
            irq_polls=self.irq_polls,
            irq_edges=self.irq_edges,
            last_irq_flags=self.last_irq_flags)

    def _hw_apply_config(self, cfg: dict) -> bool:
        """Reprogram frequency/modulation/packet parameters."""
        self._radio.update(cfg)
        self._apply_radio_params()
        self._hw_enter_rx()
        return True

    def _hw_probe(self) -> None:
        """Watchdog liveness probe: check the chip actually obeys.

        A plain flags read succeeds even on a wedged chip. v0.0.166:
        also enter STANDBY and back out - a mode change is a clocked
        operation, so this catches the 'chip answers SPI but the radio
        never runs' failure class (the TCXO bug sat here for hours:
        flags read fine, chip in standby, deaf RX). Re-arms RX after.
        """
        self._read_cmd(OP_GET_IRQ_STATUS, 2)
        self._cmd(OP_SET_STANDBY, [0x00])
        self._hw_enter_rx()

    def _hw_shutdown(self) -> None:
        try:
            self._cmd(OP_SET_STANDBY, [0x00])
        except Exception:  # nosec B110 - best-effort: the radio may already be gone
            pass
        pins = self._pins
        if pins.get("en", -1) >= 0 and self._gpio is not None:
            try:
                self._gpio.write(pins["en"], 0)
            except Exception:  # nosec B110 - best-effort power-down
                pass
        if self._spi is not None:
            self._spi.close()
            self._spi = None
        if self._gpio is not None:
            self._gpio.close()
            self._gpio = None

    # ── worker thread: FIFO work + IRQ edges + watchdog ───────────────
    def _worker(self) -> None:
        last_probe = time.monotonic()
        while not self._stop_flag:
            ran_work = False
            while True:
                try:
                    work = self._queue.get_nowait()
                except _queue.Empty:
                    break
                self._run_work(work)
                ran_work = True
                last_probe = time.monotonic()
            if self._stop_flag:
                break
            # Block briefly on the DIO1 edge (instant wake on a packet,
            # no spin); after processing work, poll without waiting so a
            # burst of packets is not delayed. irq_poll mode replaces the
            # edge wait with a straight flag poll (v0.0.155 hilltop RX
            # deafness diagnostics).
            dio1 = self._pins.get("dio1", -1)
            if self.irq_poll_mode:
                time.sleep(0.0 if ran_work else self.POLL_IRQ_S)
                # v0.0.160: bring-up runs as a work item on this same
                # thread - before it completes, _gpio/_spi are still
                # None and a poll here can only crash (the harmless
                # 'NoneType' object has no attribute read' at every
                # start). Skip until the hardware is up.
                if self._gpio is None or self._spi is None:
                    continue
                self.irq_polls += 1
                try:
                    status = self._read_cmd(OP_GET_IRQ_STATUS, 2)
                    self.last_irq_flags = (status[0] << 8) | status[1]
                    if self.last_irq_flags & (IRQ_RX_DONE | IRQ_CRC_ERR |
                                              IRQ_TIMEOUT | IRQ_HEADER_ERR |
                                              IRQ_TX_DONE | IRQ_CAD_DONE |
                                              IRQ_CAD_DETECTED):
                        self._handle_irq()
                except RadioHwError as exc:
                    log.error("IRQ poll failed: %s", exc)
                except Exception as exc:            # noqa: BLE001
                    log.error("radio thread error: %s", exc)
            else:
                try:
                    if dio1 >= 0 and self._gpio is not None and \
                            self._gpio.wait_edge(dio1,
                                                 0.0 if ran_work else self.IRQ_WAIT_S):
                        self.irq_edges += 1
                        self._handle_irq()
                except RadioHwError as exc:
                    log.error("IRQ handling failed: %s", exc)
                except Exception as exc:            # noqa: BLE001
                    log.error("radio thread error: %s", exc)
            if time.monotonic() - last_probe >= self.WATCHDOG_INTERVAL_S:
                last_probe = time.monotonic()
                self._watchdog()
        log.info("radio thread stopped")

    def _handle_irq(self) -> None:
        """DIO1 fired: fetch IRQ flags, drain a packet if one arrived."""
        status = self._read_cmd(OP_GET_IRQ_STATUS, 2)
        irq = (status[0] << 8) | status[1]
        if not irq & (IRQ_RX_DONE | IRQ_CRC_ERR | IRQ_TIMEOUT | IRQ_HEADER_ERR):
            if not irq & IRQ_TX_DONE:
                self._clear_irq(irq if irq else IRQ_ALL)
            return
        self._clear_irq(IRQ_ALL)
        if irq & (IRQ_CRC_ERR | IRQ_HEADER_ERR):
            self.crc_errors += 1
            return
        if not irq & IRQ_RX_DONE:
            return
        buf_status = self._read_cmd(OP_GET_RX_BUFFER_STATUS, 2)
        length = buf_status[0]
        start = buf_status[1]
        if not 1 <= length <= 255:
            return
        # ReadBuffer: opcode + offset + one status byte, then the payload.
        self._wait_busy()
        raw = self._spi.transfer(
            bytes([OP_READ_BUFFER, start & 0xFF, 0x00]) + bytes(length))
        data = bytes(raw[3:3 + length])
        pkt_status = self._read_cmd(OP_GET_PACKET_STATUS, 3)
        rssi = _rssi_dbm(pkt_status[0])
        snr = _snr_signed(pkt_status[1])
        signal_rssi = _rssi_dbm(pkt_status[2])
        self.rx_count += 1
        self.last_rssi = rssi
        self.last_snr = snr
        packet = RxPacket(rssi=rssi, snr=snr, signal_rssi=signal_rssi,
                          data=data, mono=time.monotonic())
        self._in_rx = True
        self._rx_mode = True
        if self.on_rx_packet is not None and self._loop is not None:
            def _deliver(pkt=packet) -> None:
                try:
                    self.on_rx_packet(pkt)
                except Exception:           # noqa: BLE001 - never kill RX
                    log.exception("RX callback failed")
            self._loop.call_soon_threadsafe(_deliver)

    def _describe(self) -> str:
        r = self._radio
        return (f"{r['frequency_hz'] / 1e6:.3f}MHz "
                f"SF{r['spreading_factor']} "
                f"BW{r['bandwidth_hz'] / 1000:g}kHz "
                f"CR4/{r['coding_rate']} "
                f"{r['tx_power_dbm']}dBm "
                f"sync=0x{r['sync_word']:02X} "
                f"pre={r['preamble_length']}")
