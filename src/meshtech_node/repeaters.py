"""Unidentified-repeater tracker (Brett, 2026-09-21).

PROJECT.md rule 3's accountability layer: every repeater tag in every
heard packet's path is counted, even when the packet's payload is
unreadable - ESPECIALLY then. An emergency node that starts repeating
traffic shows up here within seconds of its first relay, as an
"unknown" entry, and is promoted to a named node the moment its own
advert is heard.

Matching is exact, not guessed: a path tag IS the first hash_size
bytes (1-3) of the repeater's pubkey (openhop_core dispatcher/_is_own
packet truth: "our_pubkey[0]" is the 1-byte tag), and adverts carry
the full 32-byte pubkey in the clear (packets.parse_advert now keeps
it). So advert pubkey[:hash_size] == tag identifies the node with no
fuzzy matching and no fabrication.

Alias hazard, handled honestly: 1-byte tags collide across nodes
(256 values). A tag is promoted only through a pubkey captured from
THAT node's advert; counts recorded under a 1-byte tag before the
advert arrives may include other nodes' traffic - the entry's
"unknown" label says exactly that. 2- and 3-byte tags are far less
alias-prone; hash_size rides on the entry so the UI can say so.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

log = logging.getLogger("meshtech-node.repeaters")


@dataclass
class RepeaterEntry:
    """One distinct repeater tag heard on the air."""

    tag: bytes                      # the path bytes as heard (1-3 B)
    hash_size: int                  # 1, 2, or 3 - how wide the tag is
    relay_count: int = 0            # packets this tag has carried
    first_heard: float = 0.0
    last_heard: float = 0.0
    # Filled the moment a matching advert (pubkey prefix == tag) is
    # heard: promotion to a named, placed node. Before that: unknown.
    pubkey: Optional[bytes] = None
    name: Optional[str] = None
    prefix: Optional[int] = None    # first byte of the pubkey
    node_class: int = 0             # 0 = unknown until advert says

    @property
    def identified(self) -> bool:
        return self.pubkey is not None


@dataclass
class RepeaterTable:
    """Every distinct repeater tag heard, with expiry.

    Pure state machine (clock injectable for tests); the rawsource
    feeds it, the store/UI read it. Expired entries are DROPPED, not
    kept forever: an unheard tag ages out (30 days default, matching
    the store's FORGET_AFTER_S posture) - the counts stay honest to
    the recent mesh, not to history.

    DISK MEMORY (2026-09-21, Brett: the plugin's database adopted):
    `sink`, when set, is the NodeStore. Every change is written
    through the moment it happens (no accumulate-and-flush buffer, so
    RAM cannot bloat); at boot the RAM table is refilled from the
    store so a restart forgets nobody. sink=None = pure RAM (tests).
    """

    expire_after_seconds: float = 30 * 86400.0
    entries: Dict[bytes, RepeaterEntry] = field(default_factory=dict)
    sink: Optional[object] = field(default=None, repr=False,
                                   compare=False)

    def observe_tag(self, tag: bytes, *, now: Optional[float] = None,
                    ) -> RepeaterEntry:
        """Count one relay by this tag (packet-level truth)."""
        now = time.time() if now is None else now
        entry = self.entries.get(tag)
        if entry is None:
            entry = RepeaterEntry(tag=tag, hash_size=len(tag))
            self.entries[tag] = entry
        entry.relay_count += 1
        entry.last_heard = now
        if entry.first_heard == 0.0:
            entry.first_heard = now
        self._sink_entry(entry)
        return entry

    def _sink_entry(self, entry: RepeaterEntry) -> None:
        """Write one entry through to disk (best-effort: a database
        hiccup must never take the RX path down - the RAM table stays
        the working truth; logged loudly, retried on the next change)."""
        if self.sink is None:
            return
        try:
            self.sink.upsert_repeater(
                entry.tag, entry.hash_size, entry.relay_count,
                entry.first_heard, entry.last_heard,
                pubkey=entry.pubkey.hex() if entry.pubkey else None,
                name=entry.name, prefix=entry.prefix,
                node_class=entry.node_class)
        except Exception:
            log.exception("repeater write-through failed (tag %s) - "
                          "RAM keeps the truth; retried on next change",
                          entry.tag.hex())

    def observe_advert(self, pubkey: bytes, name: Optional[str],
                       node_class: int, *, now: Optional[float] = None,
                       ) -> List[RepeaterEntry]:
        """Promote every tag this pubkey matches.

        A 32-byte pubkey matches tags of ANY width (1-3 bytes): the
        first byte, the first two, the first three. Returns the
        entries promoted (for logging/tests).
        """
        now = time.time() if now is None else now
        promoted: List[RepeaterEntry] = []
        for width in (1, 2, 3):
            tag = pubkey[:width]
            entry = self.entries.get(tag)
            if entry is not None and not entry.identified:
                entry.pubkey = pubkey
                entry.name = name
                entry.prefix = pubkey[0]
                entry.node_class = int(node_class) & 0x3
                self._sink_entry(entry)     # promotion reaches disk too
                promoted.append(entry)
        return promoted

    def refill_from(self, rows: List[dict]) -> int:
        """Boot refill: load the disk table back into RAM.

        Rows come from NodeStore.repeater_rows(). A RAM entry never
        loses to a disk row (disk is the older copy): existing RAM
        entries keep their live counts; only tags RAM has forgotten
        (or never saw) are restored. Returns rows restored.
        """
        restored = 0
        for row in rows:
            tag = row.get("tag_bytes")
            if not tag:
                continue
            if tag in self.entries:
                continue
            try:
                pubkey_hex = row.get("pubkey")
                entry = RepeaterEntry(
                    tag=tag,
                    hash_size=int(row.get("hash_size") or len(tag)),
                    relay_count=int(row.get("relay_count") or 0),
                    first_heard=float(row.get("first_heard") or 0.0),
                    last_heard=float(row.get("last_heard") or 0.0),
                    pubkey=bytes.fromhex(pubkey_hex) if pubkey_hex else None,
                    name=row.get("name"),
                    prefix=row.get("prefix"),
                    node_class=int(row.get("node_class") or 0),
                )
            except (TypeError, ValueError):
                continue          # malformed row: skip, never crash boot
            self.entries[tag] = entry
            restored += 1
        if restored:
            log.info("repeater table refilled from disk: %d tag(s) "
                     "restored (RAM table now %d)", restored,
                     len(self.entries))
        return restored

    def unknown_repeaters(self) -> List[RepeaterEntry]:
        """Tags with no matching advert yet - the accountability list."""
        return sorted((e for e in self.entries.values()
                       if not e.identified),
                      key=lambda e: -e.relay_count)

    def known_repeaters(self) -> List[RepeaterEntry]:
        return sorted((e for e in self.entries.values()
                       if e.identified),
                      key=lambda e: -e.relay_count)

    def prune(self, *, now: Optional[float] = None) -> int:
        """Drop silent entries; returns how many (honest logging).
        Disk copy pruned with the same rule (the sink's mirror), so
        RAM and disk never disagree about who is forgotten."""
        now = time.time() if now is None else now
        dead = [tag for tag, e in self.entries.items()
                if now - e.last_heard > self.expire_after_seconds]
        for tag in dead:
            del self.entries[tag]
            if self.sink is not None:
                try:
                    self.sink.forget_repeaters_older_than(
                        now - self.expire_after_seconds)
                except Exception:
                    log.exception("repeater disk prune failed - "
                                  "RAM stays the truth")
        return len(dead)

    def __len__(self) -> int:
        return len(self.entries)
