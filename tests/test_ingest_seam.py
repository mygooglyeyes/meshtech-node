"""The Adapter-A ingest seam - the queue contract between the packet
source and the brain's ingest_loop.

The 2026-09-20 hilltop silent death: the source puts one Observation
per heard packet, but ingest_loop unpacked a 3-tuple. The first real
packet raised ValueError, ingest died, its finally-cancel killed the
source, and the node went deaf while the radio stayed healthy (the
log said "RawPacketSource stopped" and nothing else). These tests pin
the contract at the boundary so a shape drift can never again kill
the listener silently.
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402

from meshtech_node.observations import Observation  # noqa: E402
from meshtech_node.rawsource import RawPacketSource, RxPacket  # noqa: E402
from meshtech_node.service import ScopeService, _normalize_ingest  # noqa: E402


def _obs(prefix: int = 0xAA) -> Observation:
    return Observation(recv_ts=time.time(), origin_ts=time.time(),
                       prefix=prefix, lat=38.0, lon=-122.0,
                       path_prefixes=[], channel_name=None)


class _FakeStore:
    def __init__(self):
        self.added = []

    def add(self, obs):
        self.added.append(obs)


class _FakeSource:
    """Stands in for RawPacketSource: puts given items, then parks."""

    def __init__(self, items):
        self._items = list(items)

    async def run(self, out_queue: asyncio.Queue, stop: asyncio.Event):
        for item in self._items:
            await out_queue.put(item)
        while not stop.is_set():
            await asyncio.sleep(3600)


def _service_with(items) -> ScopeService:
    svc = ScopeService.__new__(ScopeService)
    svc.store = _FakeStore()
    svc.use_demo = False
    svc.external_source = _FakeSource(items)
    svc._stop = asyncio.Event()
    svc._ingest_node_rows = lambda nodes: None
    svc._ingest_advert_rows = lambda obs: None
    svc._ingest_extras = lambda extras: None
    return svc


# ------------------------------------------------------- normalizer ----

def test_normalize_single_observation():
    obs = _obs()
    got, nodes, extras = _normalize_ingest(obs)
    assert got == [obs] and nodes is None and extras is None


def test_normalize_triple_still_accepted():
    nodes, extras = [object()], {"k": "v"}
    got, n, e = _normalize_ingest(([ _obs() ], nodes, extras))
    assert len(got) == 1 and n is nodes and e is extras


def test_normalize_garbage_is_skipped_not_fatal():
    got, nodes, extras = _normalize_ingest(42)
    assert got == [] and nodes is None and extras is None


# ------------------------------------------------- the hilltop death ---

def test_single_observation_flows_through_ingest():
    """THE regression: one Observation from the source must reach the
    store - not kill ingest_loop with a ValueError unpack."""
    async def scenario():
        svc = _service_with([_obs(0x11), _obs(0x22)])
        task = asyncio.create_task(svc.ingest_loop())
        await asyncio.sleep(0.2)
        assert not task.done(), f"ingest died: {task.exception()!r}"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert [o.prefix for o in svc.store.added] == [0x11, 0x22]

    asyncio.run(scenario())


def test_ingest_survives_garbage_in_the_stream():
    async def scenario():
        svc = _service_with([_obs(0x33), "garbage", 42, None, _obs(0x44)])
        task = asyncio.create_task(svc.ingest_loop())
        await asyncio.sleep(0.3)
        assert not task.done(), f"ingest died: {task.exception()!r}"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert [o.prefix for o in svc.store.added] == [0x33, 0x44]

    asyncio.run(scenario())


# The producer side (RawPacketSource puts ONE Observation per heard
# packet) is visible at rawsource.py:119 and proven by test_rawsource
# and test_rxshim end-to-end; the consumer side is pinned above.
