# MAP SIZE - DESIGN REVISION (2026-09-23, for Brett's review)

Brett's decision, restated: the SERVER always watches the full
60x60 km home area. The PHONE holds the size the user wants
(20/40/60). A manual map refresh asks for THAT size, and the server
sends just that window. Refresh limits stay. Nothing here is built
until Brett verifies this document line by line.

---

## 1. The picture

```
THE SERVER (hilltop, every scope server):
  watches + stores everything inside its full 60x60 km home box.
  The box center is the server's own location (user-entered at
  install - the "hardwired Novato" default becomes a question).

THE PHONE (every listener):
  remembers ONE setting: my map size = 20, 40, or 60 km.
  The automatic feed keeps painting whatever the server broadcasts.
  The MANUAL REFRESH button asks for the user's chosen window.

        60x60 server box (what the server always watches)
        +--------------------------------------+
        |                                      |
        |      20x20 phone window (example)    |
        |      +----------------+             |
        |      |   your dots    |             |
        |      +----------------+             |
        |                                      |
        +--------------------------------------+
```

Why the split: the server is ONE box because the radio airtime
budget is ONE budget - every listener shares the same broadcasts.
The window each phone wants costs nothing to store, and filtering
it out on refresh is cheap (the server already holds every node's
position).

## 2. What changes on the server (hilltop)

- **config.json**: `span_km` becomes fixed at **60** for the home
  area. The install menu's Map size question MOVES OUT of the server
  install (it becomes a phone setting, section 3). The install menu
  instead asks for the **home-area center** (lat/lon, plain
  "degrees, e.g. 38.1074" question with Enter-keeps-current), which
  was on the todo list anyway.
- **Everything already works at 60**: sections become 20x20 km
  squares (60 / 3 = 20), the wire carries the size in the LAYOUT,
  and the node store already keeps every positioned node.
- **The refresh budget grows no new rules** - same counters, see
  section 5.

## 3. What changes on the phone (meshtech-phone)

- A new setting: **Map size: 20 / 40 / 60 km** (default 40). It
  lives on the phone only - stored locally, remembered like the
  password is today.
- The **Map refresh button** sends the size along with the request.
- The app keeps drawing whatever the LAYOUT says (it already does
  this); the size choice only decides how much data a manual refresh
  pulls in.
- If the user's size is bigger than their server's data, they simply
  see the whole 60x60 - no error, the server just sends everything.

## 4. The wire change (the only protocol edit)

The refresh request gains ONE field:

```
today:    { type:"refresh", req_id:"r7", kind:"map" }
revised:  { type:"refresh", req_id:"r7", kind:"map",
            span_km: 40 }
```

- `span_km` is OPTIONAL. Missing or invalid = the server's home
  size (today's behavior, so old apps keep working untouched).
- The server snaps any value to the nearest of 20/40/60 (same snap
  rule the config uses - one shared idea, tested once).
- The reply is a normal LAYOUT + sections + INTRO batch, just
  computed over the requested window. The LAYOUT itself announces
  the window it covers, so the phone never guesses.
- DIRECT mode (WiFi) and RADIO mode (over the air) both carry it;
  over the air the field costs 1 byte inside the existing
  REFRESH_REQ payload - measured, not guessed, before ship.

## 5. Limits (the "with limits" Brett asked for)

The existing S2 refresh budget is unchanged and now also gates
size-trimmed requests:

- A whole-window refresh (any size) = the same 2-per-30-minutes
  GLOBAL budget that whole-area refreshes use today. Trimming the
  window does NOT make refreshes cheaper in budget - the packets
  sent are fewer, but the request is still "the map".
- The phone's own 5/hour ledger counts it identically.
- A smaller window DOES cost less airtime per answer (fewer section
  packets), which is the honest reward for asking small.

## 6. The automatic feed keeps its shape

Background rotation, pulses, and INTRO sweeps keep covering the
full 60x60 exactly as today. The phone's chosen size never changes
what the server broadcasts - only what a manual refresh pulls.
So a user who never touches refresh sees the standard feed, same
as the PC app does today.

## 7. The nearest-server future (why this design)

When coordination arrives - "ask for a map update, the nearest
scope server replies" - every server speaks the same shape: full
60x60 home box, same section math, same refresh rules. A phone
roaming between areas changes NOTHING in its behavior: same size
setting, same button, same budget, just a different server
answering. Stitching overlaps between two servers' boxes remains
its own future design task (node dedupe, which box wins) - noted,
not designed here.

## 8. Build order (each step verified before the next)

1. REVISE THE DESIGN (this document) - Brett verifies line by line.
2. Server: span_km fixed 60 + center question at install + tests.
3. Wire: span_km field on refresh (codec version bump, both codecs,
   golden vectors updated) + tests.
4. Server reply: size-trimmed LAYOUT/sections/INTRO + budget gate
   tests.
5. Phone: the size setting + refresh button sends it + tests.
6. Hilltop install + Brett's live test (menu asks center, refresh
   asks 20/40/60, budget shows the wait).

 NOTHING in this document is built yet. Brett's "correct" on each
 section is the go signal; rule 4 of PROJECT.md applies to every
 step of the build order above.
