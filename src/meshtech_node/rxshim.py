"""RX shim - completing Adapter A's scope path.

Bridges RawPacketSource's on_scope callback to the brain's
service.on_packet(obj, sender_prefix) - the SAME entry point the
plugin's CompanionClient used (client.py:379-382, live-proven). What
changes in the node is only WHERE the plaintext comes from: instead
of the companion link's CHANNEL_DATA_RECV events, it is decrypted
straight from heard GRP_DATA frames by packets.py.

Why on_packet and not the ingest queue: on_packet is the brain's
RX-side entry (peer layouts, refresh answering) while ingest feeds the
Observation store. A scope packet arriving on air is BOTH - the shim
delivers it to on_packet, and the same frame's Observation (prefix=0,
channel tagged) flows via the normal ingest path.

Sender identity: group packets carry none on the wire (packets.py),
and scope uplinks are anonymous by protocol (service.py's own note),
so sender_prefix is the honest "unknown" - the rate limiter's designed
behavior for orphans (limited).

Full-header capture: last_scope_frame on the source holds the
UNMODIFIED FrameParts of the last scope packet heard - the
BENCH-CHECKLIST full-header proof (the thing openhop destroyed).
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from .rawsource import RawPacketSource

log = logging.getLogger("meshtech-node.rxshim")

ScopeHandler = Callable[[object, str], Optional[Awaitable[None]]]


def wire_scope_rx(source: RawPacketSource,
                  service: object) -> None:
    """Connect source.on_scope -> service.on_packet.

    Fire-and-forget with the answerbot's loud-death rule: the callback
    result is scheduled when it is a coroutine, its task death is
    logged, and a synchronous raise is contained so one bad packet can
    never kill the listener.
    """
    async def _deliver(obj, sender_prefix) -> None:
        try:
            await service.on_packet(obj, sender_prefix)
        except Exception:
            log.exception("service.on_packet failed for %s", type(obj).__name__)

    def _on_scope(obj, sender_prefix):
        try:
            result = _deliver(obj, sender_prefix)
            return result                      # rawsource schedules it
        except Exception:
            log.exception("scope delivery setup failed - packet dropped")
            return None

    source.on_scope = _on_scope
    log.info("RX shim wired: heard scope packets -> service.on_packet")
