# meshtech-node - PHONE CONNECT SEQUENCE (design 2026-09-20, for Brett's review)

How the phone app joins, shows data age, and re-requests gaps WITHOUT
wasting the strict 5-refreshes/hour limit (PLAN.md, schedule section).

Design principles, in priority order:
1. BACKGROUND ROTATION is the default freshness engine. With the new
   schedule every section updates every ~45 min, INTRO batches sweep
   all names every ~35-40 min, LAYOUT every 10 min. A healthy mesh
   keeps the map fresh for ZERO refreshes.
2. REFRESHES ARE GAP-FILLS AND USER ACTIONS ONLY. The app never burns
   tokens on a timer.
3. CACHE-FIRST: the app keeps the last decoded LAYOUT/sections/names
   on disk and renders them instantly on open, each stamped with its
   age. Only first-ever opens start blank.
4. HONEST AGES: every tile says how long ago THIS APP heard the data
   ("heard 12 min ago"). No fake freshness.

## The 5/hour ledger (both sides, already approved)

- Strict 5 refreshes per rolling 60 min per phone.
- Node side: counts per requester pubkey, honest silence over limit.
- App side: same count, locks the refresh button, shows
  "2 of 5 left this hour - next at 4:15 PM".
- DIRECT mode: exact - the node's `state` message carries the real
  remaining count, so the app shows node truth.
- RADIO mode: the app's ledger is best-effort local (there is no way
  to ask the node over the air). If they disagree, the node's silence
  wins and the app shows "no answer - rate limited". v1 limitation,
  accepted.

## Spend rules (what a refresh buys)

- WHOLE-AREA (kind=1 target=0): 11 packets - LAYOUT + all 9 sections
  + 1 INTRO batch. The efficient hammer.
- SECTION (kind=1 target=N): ~4-6 packets - that section + up to 3
  route packets + 1 INTRO batch. NEVER spend one on a single missing
  section: two section requests cost about one whole-area refresh.

## Connect sequence

COLD FIRST OPEN (radio mode):
1. BLE connect, then listen silently ~30 s (costs nothing; whatever
   arrives populates).
2. Still no LAYOUT (no grid)? -> whole-area refresh. 1 token.
3. LAYOUT present but >= 2 sections missing? -> whole-area refresh
   (1 token beats 2+ section requests). Only 1 section missing? ->
   wait for the background rotation (<= 45 min) and spend nothing.
4. Names build passively from the INTRO rotation; the map header
   shows "names: 12/63". Max spend on a cold open: 1 token.

RETURNING OPEN (radio or direct):
1. Render instantly from the local cache with ages shown.
2. Run the gap rule (below). Typically 0 tokens: background kept it
   fresh. Only genuinely stale data triggers a spend.

DIRECT MODE (any open):
1. Node serves LAYOUT at `hello` - grid draws in seconds, 0 tokens.
2. Background feed arrives over the wire continuously (zero airtime,
   zero tokens); the ledger only moves when the USER taps refresh.
3. Ledger display is exact (from `state`).

RECONNECT after sleep/lost link:
- Same as returning open: cache renders, gap rule runs. The 60-min
  ledger window keeps rolling (node side is authoritative).

## Data-age display

- Per-section chip on each tile: fresh (heard < 45 min), aging (45-90),
  stale (> 90), never ("-", hollow). Wording is always "heard Xm ago" -
  time since THIS app decoded it, app-local clock, no sync needed.
- Map header: LAYOUT age (grid), last PULSE age (feed liveness; the
  feed goes quiet-flag after ~15 min without a pulse).
- Names counter: "names 34/63" while the INTRO rotation builds.

## Gap rule (the single anti-waste rule)

Re-request a thing only when it is OLDER than twice its expected
rotation:
- a section: age > 90 min -> eligible
- the layout: age > 20 min -> eligible
- anything younger: NEVER re-asked, by button or by gap rule.

Then choose the cheapest sufficient spend: >= 3 stale sections ->
whole-area; 2 stale -> whole-area if the ledger has >= 3 tokens left,
else wait (background fixes it within 45 min); 1 stale -> always wait.
The manual refresh button always spends whole-area and counts the
same, so the app's worst case is bounded: 5 whole-area answers/hour,
55 packets, still under the node's 60/h budget by itself.

## Typical first hour

- Returning user, healthy mesh: 0-1 tokens.
- Cold open: 1 token.
- Messy air (25% loss modeled): 2 tokens max (connect + one gap fill).
- The other tokens stay free for the user's own taps.
