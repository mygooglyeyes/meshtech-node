"""C3 - node-table lifecycle (2026-09-20 review, Brett's rule):
stale after 14 days silent, forgotten after 30. The table never grows
forever; stale nodes leave the maps before they leave memory.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from meshtech_node.observations import Observation, RollingStore  # noqa: E402


DAY = 86400.0


def _store_with_node(last_advert_age_s: float) -> RollingStore:
    store = RollingStore(window_seconds=3600.0)
    now = time.time()
    store.add_position(0xAA, 38.0, -122.0, "old-timer",
                       now=now - last_advert_age_s)
    return store


def test_fresh_node_is_neither_stale_nor_forgotten():
    store = _store_with_node(last_advert_age_s=1 * DAY)
    counts = store.prune_nodes()
    assert counts == {"stale": 0, "forgotten": 0}
    assert not store.node_is_stale(0xAA)
    assert 0xAA in store.known_nodes()


def test_14day_silent_node_goes_stale_but_stays_in_table():
    store = _store_with_node(last_advert_age_s=15 * DAY)
    counts = store.prune_nodes()
    assert counts["stale"] == 1 and counts["forgotten"] == 0
    assert store.node_is_stale(0xAA)
    assert 0xAA in store.known_nodes()          # kept, but...
    assert store.map_nodes() == []              # ...off the maps


def test_30day_silent_node_is_forgotten():
    store = _store_with_node(last_advert_age_s=31 * DAY)
    counts = store.prune_nodes()
    assert counts["stale"] == 0 and counts["forgotten"] == 1
    assert 0xAA not in store.known_nodes()


def test_boundary_just_under_thresholds_stays_fresh():
    store = _store_with_node(last_advert_age_s=14 * DAY - 60)
    counts = store.prune_nodes()
    assert counts == {"stale": 0, "forgotten": 0}
    assert not store.node_is_stale(0xAA)


def test_stale_node_heard_again_becomes_fresh():
    store = _store_with_node(last_advert_age_s=20 * DAY)
    store.prune_nodes()
    assert store.node_is_stale(0xAA)
    now = time.time()
    store.add_position(0xAA, 38.0, -122.0, now=now)   # heard again
    assert not store.node_is_stale(0xAA)
    assert len(store.map_nodes()) == 1
