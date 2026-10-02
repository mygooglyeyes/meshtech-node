"""RSSI / noise-floor math - the board's LNA lift is a per-board fact.

noisefloor_2.md (Brett, 2026-09-30): on PiMesh hardware the frontend
LNA lifts everything the chip hears by 14 dB, so the modem backs that
lift out of every RSSI and noise figure; any OTHER hardware keeps the
raw chip math (never a guessed constant bolted onto every radio).
AGENTS rule: the gain register and its values below are the reference
driver's, verbatim from openhop_core's LoRaRF/SX126x.py.
"""
from cleanmodem import sx126x
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
