"""The ONE non-rotating origin key + the periodic collision check.

Brett, 2026-10-02: "stop with the rotating key ... a single
non-rotating key" (a fresh random 16-bit id every boot made three
boots look like three different boxes) and "a periodic check to see
if it collides would be good".

- minted ONCE, saved beside the data dir, reused every boot
- config feed.origin_hex wins (explicit beats minted, no file needed)
- a LAYOUT wearing OUR key but describing a DIFFERENT box is caught
  LOUDLY - two boxes' facts must never silently look like one box.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from meshtech_node import codec  # noqa: E402
from meshtech_node.config import FeedCfg, Settings  # noqa: E402
from meshtech_node.service import ScopeService  # noqa: E402

_LOG = "meshtech-node.service"


def _key_file(tmp_path):
    return tmp_path / "origin.hex"


def test_key_is_minted_once_then_reused(tmp_path):
    first = ScopeService(Settings(), use_demo=True).origin
    assert _key_file(tmp_path).read_text(encoding="utf-8").strip() \
        == f"{first:04x}"
    second = ScopeService(Settings(), use_demo=True).origin
    assert second == first              # the SAME key every boot


def test_config_key_wins_and_writes_no_file(tmp_path):
    svc = ScopeService(Settings(feed=FeedCfg(origin_hex="ab12")),
                       use_demo=True)
    assert svc.origin == 0xAB12
    assert not _key_file(tmp_path).exists()


def test_corrupt_key_file_mints_fresh_loudly(tmp_path, caplog):
    _key_file(tmp_path).write_text("not-a-key", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=_LOG):
        svc = ScopeService(Settings(), use_demo=True)
    assert _key_file(tmp_path).read_text(encoding="utf-8").strip() \
        == f"{svc.origin:04x}"          # replaced by a real key
    assert any("minting a fresh key" in r.getMessage()
               for r in caplog.records)


def _layout(svc, **kw):
    """A LAYOUT describing hilltop's own area (the echo shape)."""
    base = dict(seq=1, grid=3, center_lat=38.1074, center_lon=-122.5697,
                span_m=60000, origin=svc.origin, name="Local area",
                rows=3)
    base.update(kw)
    return codec.Layout(**base)


def test_own_echo_is_silent(tmp_path, caplog):
    svc = ScopeService(Settings(), use_demo=True)
    with caplog.at_level(logging.ERROR, logger=_LOG):
        svc._on_peer_layout(_layout(svc))
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_foreign_box_wearing_our_key_is_caught_loudly(tmp_path, caplog):
    svc = ScopeService(Settings(), use_demo=True)
    with caplog.at_level(logging.ERROR, logger=_LOG):
        svc._on_peer_layout(_layout(svc, name="Some Other Box"))
    errors = [r.getMessage() for r in caplog.records
              if r.levelno >= logging.ERROR]
    assert any("ORIGIN COLLISION" in m for m in errors)
    assert svc.peers.count() == 0       # dropped, never trusted
