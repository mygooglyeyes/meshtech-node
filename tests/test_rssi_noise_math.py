"""RSSI / noise-floor math - the board's LNA lift is a per-board fact.

noisefloor_2.md (Brett, 2026-09-30): on PiMesh hardware the frontend
LNA lifts everything the chip hears by 14 dB, so the modem backs that
lift out of every RSSI and noise figure; any OTHER hardware keeps the
raw chip math (never a guessed constant bolted onto every radio).
AGENTS rule: the gain register and its values below are the reference
driver's, verbatim from openhop_core's LoRaRF/SX126x.py.
"""
import struct
import time

from cleanmodem import frames, sx126x
from cleanmodem.config import PIN_PRESETS

# Verbatim from the reference (LoRaRF/SX126x.py:
# REG_RX_GAIN = 0x08AC, POWER_SAVING_GAIN = 0x94).
REF_REG_RX_GAIN = 0x08AC
REF_RX_GAIN_POWER_SAVING = 0x94


def test_gain_constants_match_the_reference() -> None:
    assert sx126x.REG_RX_GAIN == REF_REG_RX_GAIN
    assert sx126x.VAL_RX_GAIN_STANDARD == REF_RX_GAIN_POWER_SAVING


def test_default_math_is_raw_chip_values() -> None:
    # Non-PiMesh hardware: no offset, plain -0.5 dBm per LSB.
    assert sx126x._rssi_dbm(196) == -98
    assert sx126x._rssi_dbm(0) == 0
    assert sx126x._rssi_dbm(224) == -112


def test_pimesh_offset_backs_out_the_lift() -> None:
    # The doc's worked example: a raw -98 reports -112 on PiMesh.
    assert sx126x._rssi_dbm(196, 14.0) == -112
    assert sx126x._rssi_dbm(224, 14.0) == -126


def test_pimesh_presets_carry_the_board_offset() -> None:
    for name, preset in PIN_PRESETS.items():
        if name.startswith("pimesh"):
            assert preset.get("rssi_lna_offset_db") == 14.0, name


def test_radio_picks_the_offset_up_from_the_board() -> None:
    # The offset rides in the pin preset: PiMesh boards 14.0,
    # anything else (missing key) stays at raw chip math.
    radio = sx126x.SX126xRadio(dict(PIN_PRESETS["pimesh-1w-v2"]),
                               force_null_hw=True)
    assert radio._lna_offset_db == 14.0
    plain = sx126x.SX126xRadio({"busy": 5}, force_null_hw=True)
    assert plain._lna_offset_db == 0.0


# ─── the rolling noise sampler (v0.0.067) ──────────────────────────
# Mirrors openhop_core sx1262_wrapper._sample_noise_floor - the
# method behind the steady numbers the old stack showed. Verbatim
# reference values: NUM_NOISE_FLOOR_SAMPLES = 20,
# SAMPLING_THRESHOLD = 10, quiet time 0.5 s after packet activity.

REF_NOISE_SAMPLES = 20
REF_NOISE_SPIKE_DB = 10.0
REF_NOISE_QUIET_S = 0.5


def _sampler(offset: float = 0.0):
    """A radio whose hardware reads queue up in `reads`."""
    radio = sx126x.SX126xRadio({"busy": 5, "rssi_lna_offset_db": offset},
                               force_null_hw=True)
    radio._spi = object()              # "hardware is up" for the guard
    radio._in_rx = True
    reads: list[int] = []

    def _read(_op: int, _size: int) -> bytes:
        return bytes([reads.pop(0)])

    radio._read_cmd = _read
    return radio, reads


def test_sampler_constants_match_the_reference() -> None:
    assert sx126x.SX126xRadio.NOISE_SAMPLES == REF_NOISE_SAMPLES
    assert sx126x.SX126xRadio.NOISE_SPIKE_DB == REF_NOISE_SPIKE_DB
    assert sx126x.SX126xRadio.NOISE_QUIET_S == REF_NOISE_QUIET_S


def test_status_sentinel_matches_the_wire_sentinel() -> None:
    assert (sx126x.NOISE_X10_NO_VALUE
            == struct.unpack("<h", frames.NOISE_NO_VALUE)[0])


def test_noise_is_a_gap_until_the_first_batch() -> None:
    radio, reads = _sampler()
    reads.extend([200] * 19)             # raw -100 each, one short
    for _ in range(19):
        radio._sample_noise_floor()
    assert radio._hw_noise() is None     # honest gap, never a constant
    assert radio._hw_status().noise_x10 == sx126x.NOISE_X10_NO_VALUE


def test_noise_averages_the_batch_and_applies_the_board_offset() -> None:
    radio, reads = _sampler(offset=14.0)
    reads.extend([200] * 20)             # raw -100 each
    for _ in range(20):
        radio._sample_noise_floor()
    assert radio._hw_noise() == -114.0   # -100 raw, LNA lift backed out


def test_noise_rejects_spikes_like_the_reference() -> None:
    radio, reads = _sampler()
    reads.extend([200] * 20)             # seed the floor at -100
    for _ in range(20):
        radio._sample_noise_floor()
    assert radio._hw_noise() == -100.0
    reads.extend([100, 200] * 10)        # a -50 chirp before each quiet
    for _ in range(20):
        radio._sample_noise_floor()
    # the -50 reads were rejected as spikes: only 10 quiet samples
    # counted, no new batch average landed, the floor stands.
    assert radio._hw_noise() == -100.0
    assert radio._noise_count == 10


def test_noise_skips_while_the_radio_is_busy() -> None:
    radio, reads = _sampler()
    reads.append(200)
    radio._last_packet_activity = time.monotonic()   # packet just landed
    radio._sample_noise_floor()
    assert radio._noise_count == 0       # nothing consumed
    assert reads == [200]
