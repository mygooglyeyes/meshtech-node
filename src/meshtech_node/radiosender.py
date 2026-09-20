"""Adapter B - RadioSender: the scope feed's mouth.

SEED-MAP.md NEW module. Replaces the plugin's CompanionClient
TRANSPORT: the brain calls send_channel_data(data_type, payload) - the
SAME interface CompanionClient satisfied (proven by the seeded
client.py tests) - but instead of a companion link, this sender wraps
the plaintext in an encrypted GRP_DATA packet and hands the RAW frame
to cleanmodem's CMD_TX_REQUEST. cleanmodem owns LBT/CAD/politeness.

WIRE SCHEME (verified 2026-09-20, the decision this module stands on):
the standard firmware GROUP scheme - AES key = zero-padded secret's
first 16 bytes, HMAC key = full zero-padded 32, channel hash =
sha256(secret)[0] with the zero-tail 16-byte rule. Evidence: hilltop's
on-air GRP_DATA pulses were decrypted by the desk radio's FIRMWARE
using exactly this scheme (packets.derive_channel_keys, proven against
live traffic in the bot), and the bot's phone-decoded chat TX used it.
openhop_core's PYTHON create_group_data_packet derives keys as
sha256(secret) instead - that path never transmitted on hilltop, so it
is NOT the on-air truth. The scope-app's own decoder consumes
derive_channel_keys-shaped packets (it decoded hilltop's feed).

Framing: the brain's payload is the FULL plaintext
data_type(2 LE) + data_len(1) + body. The v0.2.2 shipped-frame bug was
a framing mismatch - refused here, never sent (client.py's check
mirrored).

Own-TX loopback: cleanmodem loops the just-sent frame back as RX, so
the sender marks its own payloads in the SAME FloodDedupe the source
uses - the loopback becomes a suppressed duplicate, not a phantom
observation.

TX-off guard: refuses to transmit unless tx_enabled is True (config-
gated; Gate 1 listen-only). Refusals are loud and honest.
"""
from __future__ import annotations

import logging
from typing import Optional

from . import packets
from .packets import (PAYLOAD_TYPE_GRP_DATA, ChannelKeys, FloodDedupe)

log = logging.getLogger("meshtech-node.radiosender")


def build_grp_data_frame(channel: ChannelKeys, plaintext: bytes,
                         *, crypto=None) -> Optional[bytes]:
    """One on-air GRP_DATA frame, firmware-compatible scheme.

    frame = header(1) | path_len(1)=0x00 | ch_hash(1) | mac(2) |
            AES-ECB(plaintext)
    header = (GRP_DATA << 2) | ROUTE_FLOOD, version 0 - the bot's
    proven flood-group layout (mcp.py _build_group_packet), route bit
    per the REFERENCE numbering (1 = flood).

    Returns None when crypto is unavailable - never a half-built frame.
    """
    crypto = crypto or packets._crypto()
    if crypto is None:
        log.error("TX unavailable: channel crypto not importable - "
                  "the gap is honest")
        return None
    if not plaintext:
        return None
    ciphertext = crypto._aes_encrypt(channel.aes_key, plaintext)
    mac = crypto._hmac_sha256(channel.hmac_key, ciphertext)[:2]
    header = (PAYLOAD_TYPE_GRP_DATA << 2) | packets.ROUTE_FLOOD
    return (bytes([header, 0x00])
            + bytes([channel.channel_hash]) + mac + ciphertext)


class RadioSender:
    """The brain's client (Adapter B). Satisfies the interface
    CompanionClient satisfied: send_channel_data / is_connected /
    has_slot / run()."""

    def __init__(self, modem: object, channel: ChannelKeys, *,
                 tx_enabled: bool = False,
                 dedupe: Optional[FloodDedupe] = None,
                 bench_no_radio: bool = False):
        self.modem = modem
        self.channel = channel
        self.tx_enabled = tx_enabled
        # BENCH mode (--bench-no-radio): no radio exists. The link
        # REPORTS READY so the brain's broadcast loop runs and the feed
        # builds packets for WebServe - but tx_enabled stays False, so
        # every send attempt is refused by the guard below. Never
        # transmitted, honestly labeled.
        self.bench_no_radio = bench_no_radio
        # shared with RawPacketSource so our own loopback is suppressed
        self.dedupe = dedupe

    # -- the brain's link-state probes -------------------------------
    @property
    def is_connected(self) -> bool:
        if self.bench_no_radio:
            return True
        if self.modem is None:
            return False
        # ModemTransport (real mode) or raw ModemClient: both expose
        # `connected` truthfully.
        return bool(getattr(self.modem, "connected", False))

    @property
    def has_slot(self) -> bool:
        return self.channel is not None

    async def run(self) -> None:
        # ModemClient.run() is supervised by the shell (cleanmodem is a
        # separate process; the client maintains the TCP link). The
        # sender just parks until cancelled.
        import asyncio
        if self.bench_no_radio:
            log.info("BENCH MODE: no radio wired - the feed builds "
                     "packets for WebServe only, TX is IMPOSSIBLE "
                     "(tx_enabled=False). Nothing reaches the air.")
        await asyncio.Event().wait()

    # -- TX -----------------------------------------------------------
    async def send_channel_data(self, data_type: int, payload: bytes) -> bool:
        """The brain's send seam. payload = FULL scope plaintext."""
        if not self.tx_enabled:
            log.warning("TX refused: TX DISABLED (listen-only) - "
                        "type=%04x not sent. The gap is honest.", data_type)
            return False
        payload = bytes(payload)
        if not isinstance(data_type, int) or not 0 < data_type <= 0xFFFF:
            log.warning("TX refused: bad data_type %r", data_type)
            return False
        # v0.2.2 framing check: the plaintext must name its own type and
        # length coherently, or the packet is refused, never sent.
        if len(payload) < 3 or payload[2] != len(payload) - 3:
            log.warning("TX refused: framing mismatch (type=%04x, "
                        "%dB) - possible encoder bug", data_type, len(payload))
            return False
        if payload[0] | (payload[1] << 8) != data_type:
            log.warning("TX refused: embedded type %04x != requested "
                        "%04x", payload[0] | (payload[1] << 8), data_type)
            return False
        frame = build_grp_data_frame(self.channel, payload)
        if frame is None:
            return False
        if len(frame) > 255:
            log.warning("TX refused: frame %dB exceeds the LoRa MTU",
                        len(frame))
            return False
        ok = await self.modem.send(frame)
        if ok and self.dedupe is not None:
            # our own TX will loop back as RX - mark it so the source's
            # dedupe suppresses the echo (120 s window)
            self.dedupe.mark_own_tx(PAYLOAD_TYPE_GRP_DATA,
                                    frame[2:])
        return ok
