# VECTORED SYNC - SERVER SIDE (2026-09-24, design for Brett's OK)

The small server design behind DESIGN.md section 10 (meshtech-app):
the air carries CHANGES, not re-sent rosters. Brett's design, with
the three review fixes worked in. NOTHING here is built until Brett
says "correct" line by line.

---

## 1. The one-sentence version

Every node fact gets a number that only goes up; the phone says
"I have everything up to number N", and the server sends full
records only for nodes whose number is now higher than N.

```
phone:  "my marker is 1417"                (the ask, over the air)
server: nodes 1418..1432 changed since    (full records for THOSE)
        + "node a1b2 is gone"             (one line, see 5)
        + everything else: just new packets / routes on tap
```

## 2. The change counter (Brett's marker, fix 1)

- ONE counter for the whole node table, saved ON DISK, starting
  where the database is today (100, say - the start value does not
  matter, only that it only ever goes up).
- EVERY change to a node's stored facts bumps it by 1, and the node
  row remembers the number it was changed at:
  `nodes.change_seq INTEGER` (new column, migration 2).
- A "change" = name, position, class, or pubkey actually CHANGED -
  not "heard again". A node heard ten times a day with no new facts
  costs the marker nothing. (Honesty note: hearing facts stay where
  they live today - the one-hour RAM window - and the phone's
  freshness ages for "last heard" keep coming from live packets.)
- WHERE the bump happens: `_disk_node()` in observations.py - the
  single choke point EVERY node write passes through on its way to
  disk (name, position, class, supersede inheritance - all of it).
  One bump point = no writer can forget. RAM rows keep a mirrored
  copy of their number so the server can answer without touching
  the database.
- WHY A COUNTER, NEVER CLOCK TIME: hilltop restarted twice today;
  clock jumps (NTP, power) would silently skip changes forever.
  A number that only goes up is immune. This is fix 1 from the
  review Brett asked for.

## 3. The ask and the answer (the wire, version-gated)

- The phone's ask rides the EXISTING REFRESH_REQ packet (0x5311,
  client -> host). Its unused fields carry the request; the marker
  travels in a NEW 2-byte field. Wire rule from the INTRO-span
  chapter: a NEW packet shape gets a protocol version bump (0x05 ->
  0x06) and OLD decoders reject it LOUDLY rather than misread - the
  old web app keeps working on its own shapes and never sends the
  new one.
- Proposed v1.6 REFRESH_REQ layout (adds 2 bytes):

```
kind(1) target(2) host(2) nonce(2) span_km(2) [existing v1.3]
sync_marker(2 LE)                                 [NEW in v1.6]
marker = 0 means "not vectored" - today's behavior, byte-identical
```

- The answer is the packets hilltop already speaks - LAYOUT, INTRO
  batches, PULSE - but the INTRO roster is FILTERED: only nodes
  whose change number is above the phone's marker ride along. An
  old client (marker 0) gets the full roster, exactly like today.
- Why INTRO and not a new packet type: the app must translate data
  heard at any size anyway (DESIGN.md section 3), INTRO already
  carries its own span (v1.5), and reusing the type means ZERO new
  decode code on the phone - the vectored part is the FILTER, not
  a new format.

## 4. What does NOT ride the air

- Routes: on demand only (the tap = the ask; Brett's pick). The
  initial TCP download carries the route database once (the caveat
  Brett added), so the air never bulk-ships routes.
- Raw packets: never stored, never re-sent (scope rule).
- The one-hour RAM observation window: not shipped historically;
  the phone builds its own live picture from what it hears.

## 5. "Node is gone" (fix 2)

- When the server RETIRES a node it must be able to tell the
  phone. Retirement happens in exactly two places today:
  name-supersede (fresh advert proves the identity moved) and the
  30-day prune (FORGET_AFTER_S). Both already call forget_node() -
  the SAME choke point gets the same treatment: the bump.
- The message: a one-byte "gone" list riding the NEXT vectored
  answer (gone prefixes, up to 8 per packet). The phone REMOVES
  those dots. An honest deletion, not a stale fade - the phone's
  map cannot disagree with the server's table.
- A superseded node is "gone" for its OLD prefix; its facts were
  already re-issued as a changed node (section 2), so the phone
  ends with one dot at the new identity, never two.

## 6. Restart and disk facts

- The counter lives in the database (new 1-row table `sync_state`),
  so a restart CONTINUES the count - the box restarted twice today;
  a counter that resets to zero would tell every phone "everything
  changed" once (harmless - one full roster - but wasteful), and
  worse, could REUSE old numbers if it reset lower than a phone's
  marker (silently skipping changes - the exact bug class this
  design exists to kill). Monotonic across restarts is the law.
- Migration 2 in node_store.py (the numbered-migration pattern is
  already there): add `nodes.change_seq`, add `sync_state`, backfill
  every existing node with the current counter value (they all
  become "changed at N" once - the first sync after this ships is
  a full roster, then it pays).

## 7. Tests (written before the code, same as always)

- counter bumps ONLY on real fact changes (name/position/class),
  not on re-heard silence.
- counter survives a restart (new NodeStore on the same file).
- marker 0 ask = full roster (old-client behavior, byte-compat).
- marker N ask = only nodes with change_seq > N.
- gone list: superseded prefix arrives as gone; pruned prefix
  arrives as gone; the phone's view is provably equal to the
  server's table after a sync.
- wire: old-version client asks stay byte-identical; new-version
  ask decodes the marker; golden vector added for the v1.6
  REFRESH_REQ (tools/gen_golden.py + both codecs' test files).

## 8. What this costs hilltop

- One integer column, one 1-row table, one bump call in a path that
  already runs on every write, one filter on the INTRO builder, and
  a version gate on REFRESH_REQ decode. No new service, no new
  packet type, no raw storage. The web app changes NOTHING.
