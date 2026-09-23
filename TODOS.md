# meshtech-node - TODOS (order matters, top first)

## PUSHED (2026-09-23): configurable map box 20/40/60 km = v00.000.034
The map box size is now an install-menu question (after the web port
question): 1) 20 km sharpest detail, 2) 40 km standard (recommended,
the default), 3) 60 km biggest view with the honest cost line (~2x
map packets over the air, more data per app update). Enter keeps the
existing value on re-installs. Server side: config.py snaps any
off-menu span_km to the NEAREST choice as a WARNING (never an error -
an existing working install must still boot; 900 km -> 60). The app
needs NO change (the size travels in the LAYOUT packet). Tests:
3 new config tests; suite 239 passed. NEXT: Brett's hilltop install
(git pull && sudo ./manage.sh install) is the live test - question
should appear after the port question, Enter should keep 40 km.

## DESIGN DECIDED (Brett, 2026-09-23): full 60x60 home area + per-phone refresh size
Brett's verdict on coordination vs differing home areas: EVERY scope
server watches its ENTIRE 60x60 km home area (no more 20/40/60
install choice for the server). The PHONE remembers the size the user
chose (20/40/60, client setting); a MANUAL MAP REFRESH carries the
user's size, and the server sends the data for THAT size around the
same center - we already store every node's position, filtering a
window is cheap on the server. Budget stays guarded: manual refreshes
are CAPPED (the existing S2 refresh budget: 2 whole-area per 30 min
global; per-section unchanged; cap now also gates size-trimmed
requests). Automatic pulses keep reporting the full 60x60 sections;
the manual refresh is what trims the picture to the user's window.
BRETT CONFIRMED 2026-09-23 ("correct"). Design revision WRITTEN +
VERIFIED: MAP-SIZE-DESIGN.md (pushed). BUILD PROGRESS:
- step 2 DONE, PUSHED as v00.000.035 (7285b98): server span 60,
  install asks center lat/lon instead of size, old configs boot
  with a snap warning. Suite 240 passed. NOT yet installed on
  hilltop - hilltop still runs v34 until Brett pulls + installs.
- step 3 DONE, committed BOTH repos (node 967a0b4, phone b063420),
  NOT pushed: REFRESH_REQ carries span_km, proto v1.3 (0x04), 0 =
  host decides; golden vectors regenerated + matched across codecs;
  node suite 240 green, phone codec 22 green.
- step 4 DONE, uncommitted: the server answers a sized refresh with
  a size-trimmed LAYOUT/sections/INTRO (same center, window span in
  the LAYOUT, strict-overlap section filter, intro offsets relative
  to the window - zero-dots lesson held). Suite 243 passed.
- LIMITS REVISED (Brett 2026-09-23, load-balanced): 60 km = 1/hour,
  40 km = 2/hour, 20 km = 3/hour. Packet math: each level lands at
  ~12-14 packets/hour (~1.3-1.5 s airtime), level with each other and
  inside the ~34/h headroom. MAP-SIZE-DESIGN.md section 5 updated.
- NEXT: step 5 = phone setting + button sends spanKm + per-size caps
  wired (service limiter picks cap by snapped size; global budget
  pools per size). Then push, hilltop install, Brett's live test.
NOT pushed yet - push rides the next version bump with Brett's OK.

## NEW (Brett, 2026-09-22 late): LOGO/BRANDING ASSETS available
Brett has logo images in C:\projects\visuals (logo-draft-1.svg,
logo-draft-2.svg + HTML previews, plus diagram SVGs). TODO (needs
Brett's pick of draft): incorporate the chosen logo into the phone
app (header + app icons replacing the current placeholder four-pane
mark) and possibly the web terminal. Look before designing stage 2
UI work.

## PARKED (Brett, 2026-09-21 ~23:30): protocol evolutions for later
- DYNAMIC GRID: wire already allows 2x2..5x5 (both codecs, config);
  need install menu choices + 4x4/5x5 test vectors. Sweep cost at
  5x5 = 25 sections (2.1 h full sweep); refresh = 26 packets.
- DELTA UPDATES: send only CHANGED section counts per pulse (counts
  rarely change between beats). Fewer/smaller packets = the only real
  airtime win (preamble ~110 ms per packet is the fixed cost; zlib
  GROWS our 26-109 B packets - classic compression is the wrong tool
  here, measured 2026-09-21).
- ZOOM-OUT VIEW + focus-area button + user-configurable area
  center/size (20/40/60 km) - supersedes dynamic grid urgency.
  (SIZE half is DONE above; zoom-out + center-on-config remain.)

## PHONE APP (Brett, 2026-09-21 ~23:45): design doc FIRST
The next chapter. Design document before any code (project rule).
Existing material to plan from: PHONE-CONNECT.md (connect sequence,
5/hour ledger, cache-first, honest ages - all approved), PROJECT.md
end goal, companion mode (the PC sim), scope-app renderer (reusable).
OPEN QUESTIONS for Brett before writing: (1) transport - BLE to a
companion radio, WiFi/TCP like the PC sim, or both? (2) app tech -
PWA (same web app), native, or wrapper? (3) companion radio hardware
- what are we building toward?

## WATCH (Brett, 2026-09-21 ~22:30): are we hearing ALL the packets?
"I know there are more than 49 active nodes... the plugin was getting
far more hits." Research done: our RX chain has NO filters - the chip
serves every frame to cleanmodem (fan-out delivers the same bytes to
every client, no RSSI floor, no channel gate), and rawsource turns
EVERY frame into an observation (headers of foreign/unreadable
traffic included). Plugin-era difference: the bot listened through
the openhop REPEATER's companion feed (repeater-decoded events) vs
our raw chip firehose - not directly comparable. AUDIT PLAN (after
the db ships): compare per hour cleanmodem's rx counter vs
rawsource's heard/decoded/dup/undecodable stats line - every gap gets
a name, so 'just me or an issue' becomes a number.
APPLES-TO-APPLES IDEA (Brett, ~22:40): the openhop repeater built up
a LOT of node history in its own database - that is why identifying
was easier then. Compare: how many of OUR known nodes appear in the
openhop repeater's node db? That measures coverage, not hearing.
Second lever: import CONTACTS from the companion radio (its saved
node list) to fill the db for nodes whose adverts we've never caught.

## PUSHED (2026-09-22): half-fix guard = v00.000.033 (tag v00.000.033)
A torn advert (RF bit errors) can corrupt ONE half of a GPS fix.
Live evidence: KHV Solar stored lat exactly 0.0 with good lon
-121.908836. Guard: a position with ANY exact-zero half is rejected
as no-position (name/hearing kept; self-heals on the next clean
advert). 0.0/0.0 now rejected at the same choke point (was filtered
one layer up - single point of enforcement). 5 regression tests;
suite 237 passed. LIVE ON HILLTOP since 2026-09-22 23:52 install.
The existing bad KHV row self-heals on its next clean advert; a
manual db clean is available if wanted.

## BUILT, AWAITING COMMIT: disk memory (Brett, 2026-09-21 ~22:00)
The plugin's SQLite store adopted (node_store.py): nodes + repeaters
written through to disk as learned (no raw packets - scope rule, and
NONE on a future battery phone), boot refill restores what a restart
forgot (with honest staleness: a 20-day-silent node returns STALE),
RAM stays the working truth (db failure never breaks RX), and the
repeater-table expiry that was NEVER CALLED now runs on the layout
cadence with its disk mirror. 12 new tests; suite 228 green.
NOTE for hilltop: first boot creates /opt/meshtech-node/data/
(scope.db via storage.db_path). manage.sh install already copies the
program; the data dir is created by the store itself.

## TODO: web app reconnect retries (Brett, 2026-09-21 ~21:00)
After a drop the app gave up too fast - Brett had to Ctrl+F5 to get
it back. Give the direct-mode link at least 10 reconnect tries with
a short backoff before surrendering to the Connect button.
(scope-app, not hilltop.)

## DONE: Ctrl+F5 reminder (Brett, 2026-09-21: "just include a Ctrl+F5
reminder when we reboot the server") - every restart path in
manage.sh (install self-restart, start, restart) now prints:
"ON YOUR PC: press Ctrl+F5 on the web app page, then reconnect."

## RESOLVED (2026-09-21 ~20:15): slow restart - CLOSED
Fast restart verified by Brett WITH a client connected (the exact
condition that used to hang ~60 s). Root cause was aiohttp's default
shutdown timeout waiting on open WebSockets; fixed in v29
(shutdown_timeout=1 + explicit client close). Install also
self-restarts the service now (v29, verified live).

## OPEN QUESTION (Brett, 2026-09-21 ~20:00): active nodes 50 -> 1 -> 2
After Ctrl+F5 + reconnect the card showed 1 active node (2 after a
manual refresh) where it had shown 50 before. Likely benign: active =
nodes heard by the CURRENT process in the last hour; hilltop's
in-memory store restarted recently (17:38 boot + tonight's restarts),
so the window refills from zero. The 50 was the OLD page's
long-accumulated view. VERIFY next session: after ~1h uptime, does
active climb back toward the true mesh count? If yes: no bug - but
consider an honest 'uptime' hint on the card so the number's context
is visible. If it stays at 1-2 with live RX flowing: real bug in
identity extraction - investigate then.

## IDEA (Brett, 2026-09-21 ~19:50): actively ASK the mesh to identify
Is there a flood/local packet that asks nodes to advert or identify?
MeshCore answer (docs + openhop_core): YES - PAYLOAD_TYPE_CONTROL
DISCOVER_REQ (control sub_type 0x8) floods, and nodes answer with
DISCOVER_RESP (pubkey + SNR + type). Also: our own node could TX its
own advert on connect. Design first: what we send, how often, airtime
budget, and whether responses give us positions (probably not -
 adverts still needed for lat/lon). Wait for Brett's go to design.

## Feed health: "mapped nodes" stat (Brett: stat FIRST, client-side) - BUILDING
Between "active nodes" and "mesh RX/hour" on the app's Feed health
card: a MAPPED-nodes count - active nodes that have a position (the
dots that should be visible). Brett wants the calculation CLIENT-SIDE
(the app derives it from the node data it already holds) so the number
is always current, never waiting on a server stat refresh.

## DM attribution (sender+dest hashes for direct packets) - ON HOLD
Brett, 2026-09-21: another time. Research + wire facts stay recorded
below for whenever it comes back.

## Feed health: add a "mapped nodes" stat (Brett, 2026-09-21)
Between "active nodes" and "mesh RX/hour" on the app's Feed health
card, add a MAPPED-nodes count - how many of the active nodes actually
have a position (dots that should be visible on the map). That tells
Brett at a glance whether the map is full or whether idents are still
missing. Source: the store's node table (nodes with lat/lon in
window); the app can also compute it from its own INTRO/node state.
Needs: count source decided (host-side pulse field vs app-side
derived), then build + test. Wire change adds a pulse field - version
the codec honestly if we extend the packet.

## DM attribution: record sender+dest hashes + paths for direct packets (APPROVED, next build)
Direct packets (REQ/RESPONSE/TXT/ACK/ANON_REQ) carry a 1-byte dest
hash + 1-byte src hash as the first two PAYLOAD bytes, readable
without keys (proven against openhop_core packet_builder:
_hash_bytes(dest, local) prepends them). Wire fact: a direct-routed
packet's path is the REPEATER chain; sender is the src hash byte.
Build: rawsource records dest_hash + src_hash on those types (bytes
outside the MAC - honest, no crypto), Observation carries them, node
table gains a who-talks-to-whom view, and advert identification lights
up named DM routes. Tests pin the byte positions. Backfill of TRULY
anonymous group traffic is impossible honestly - Brett informed
(2026-09-21 ~19:15) that prefix=0 rows stay unattributed.

## Destination tracking (Brett question, 2026-09-21 ~19:00) - DECIDED: not tracked yet, worth adding
Direct packets (REQ 0x00, RESPONSE 0x01, TXT 0x02, ACK 0x03,
PATH 0x08, ANON_REQ 0x07) carry a 1-byte destination hash + source
hash INSIDE their encrypted payload header (per docs.meshcore.io
packet_format + payloads). Our rawsource records these packets today
as header-only observations: path recorded, dest/src hashes DROPPED
(stats.ignored_type). To track recipients: parse the first 2 payload
bytes for those types (dest=byte0, src=byte1, readable WITHOUT any
keys - they sit outside the MAC), record dest_hash in the Observation,
and surface it in the node table/UI ("packets TO x"). Pairs with the
retroactive-path-backfill feature (Brett approved build next).
Docs read: https://docs.meshcore.io/packet_format/ + /payloads/

## HARD STOP STATE (2026-09-21 ~17:00) - SUPERSEDED by resume (read for context only)
RESUMED 2026-09-21 ~18:10. Both next-features are BUILT and TESTED,
uncommitted: (1) button now reads just "Disconnect" when live;
(2) connect burst = LAYOUT + PULSE, so the map opens on connect.
Suite 213 green. Awaiting Brett's commit & push OK (-> 00.000.026).
The original hard-stop text below is kept for context.
Session paused by Brett. Where everything stands:

- **v00.000.025 is the live version everywhere.** The feed-killer fix
  (background section 1..9 + broadcast-loop seatbelt) is committed,
  pushed, pulled on hilltop, installed, and the service restarted
  (slow restart - investigate why, see below). NOT yet verified live:
  Brett must connect the PC app and watch ~10 min to confirm a SECOND
  cadence pulse fires ~5 min after connect (the old bug killed tick 1).
- **VERSION RULE (Brett):** version = 00.000.NNN where NNN = total
  raw push count. 25 pushes done. ALWAYS ask before committing.
- **NEXT FEATURE - agreed, NOT started (do this on resume):**
  1. Connect button: while connected it must read just "Disconnect"
     (green/pressed look is already built); clicking drops the link
     and it returns to "Connect node". Currently it says
     "Connect node - Disconnect" which Brett rejected.
  2. Map on connect: hilltop's connect-time burst must carry the
     LAYOUT (map frame) + PULSE together, so the area map opens itself
     on connect without pressing refresh. Today only the pulse goes.
     Scope: service.py pulse_now/_send_burst on_connect path (node.py
     wires the callback) + app expectations; tests in
     test_pulse_on_demand.py get a layout-assertion sibling.
  Both live in meshtech-node/src (webserve.py fires the callback,
  service.py builds packets, app/ is the built web page - rebuild
  from scope-app repo if the button changes: `python build.py` then
  copy dist/app.js+app.css to meshtech-node/app/).
- **Uncommitted in scope-app repo** (C:\projects\scope-app): the
  button change so far (src/App.ts + src/style.css modified,
  dist already built+synced into meshtech-node/app and SHIPPED in
  v25). Commit on Brett's OK, or fold into the button redo.
- **meshtech-node repo: clean** except whiptail_manual.txt (Brett's
  reference download, gitignored-not, leave alone).
- **SLOW RESTART mystery:** `sudo systemctl restart meshtech-node`
  took ~forever at 16:5x. Likely the radio/modem handover timeout
  (cleanmodem owns SX1262, node is controller). Get
  `journalctl -u meshtech-node -b --since "-60 min"` around the
  restart before touching anything.
- **Pending hilltop bug batches (unchanged, all wait for Brett's go):**
  six-defect batch (cancel-writes-empty, cancel-kills-script, missing
  key kills configure, port-edit comma-only match, Ctrl-C kills log
  view, door toggle restarts stopped service), install-not-restart bug
  (hit AGAIN this session), install revokes companion access,
  configure-direct dump-to-CLI, door header label "Return to menu"
  wording, menu restart/stop/start feedback text.
- **The PC app is served locally** at http://127.0.0.1:8616/ (static
  server from C:\projects\scope-app\dist, run doc:
  C:\projects\.freebuff\run.md). Data door on hilltop is OPEN with a
  password Brett has. App connect = Direct mode, 192.168.12.145.
- App feed behaved right after v25 install: health card fills on
  connect, log stays live. Map still needs refresh (fix #2 above).

## ACTIVE BUG - feed dies after first PULSE (found+reproduced 2026-09-21 16:20)
Root cause, proven in my sandbox: the background-summary counter
(`_background_section`) starts at 0, but the v1.2 renumbering made 0
RESERVED (whole-area, never a square) and the encoder now refuses 0.
So the FIRST cadence pulse (5 min after every service start) raises
CodecError inside the broadcast loop, which has no seatbelt - the loop
dies silently and the feed goes quiet forever: no more pulses,
layouts, or summaries, while the app stays connected looking healthy.
Matches hilltop exactly: burst OK at 16:13:58, then cadence silence,
though the separate connect-pulse path (16:19:17) still worked.
FIX: seed the counter at 1 and rotate 1..9, plus wrap the loop tick so
one bad packet can never kill the feed again.

## Menu restart/stop/start feel hung (Brett hit, 2026-09-21)

Choosing restart (and start/stop) shows NOTHING while systemctl works
- the screen sits frozen for seconds and looks hung. Add feedback:
print/re-echo "restarting... please wait" BEFORE the systemctl call,
then show the status lines after. Fold into the next rewrite batch
with the other menu fixes.

## Label wording: configure menu "back" entry (Brett, 2026-09-21)

The configure menu's last entry says "save nothing and go back" - but
changes are saved the moment each one is confirmed (there is no
batch/undo), so the label is wrong and confusing. Rewrite it to just
"Return to menu". Brett: fix in the NEXT rewrite batch, not as a
one-line update of its own.

## configure "Return to menu" dumps to the CLI (Brett hit, 2026-09-21)

Choosing "Return to menu" on the configure screen exits to the command
line instead of returning to the main menu. CONFIRMED (Brett tested
2026-09-21): from the MAIN menu the flow works exactly right
(configure -> back -> main menu). The dump-to-CLI happens ONLY when
`sudo ./manage.sh configure` is run DIRECTLY - that command-line path
calls do_configure with no do_menu wrapper, so "return" has no menu to
return to and the script just ends. Fix in the next rewrite batch:
when invoked directly, do_configure's exit should hand over to the
main menu (do_menu) instead of falling off the end of the script - the
runbook sells manage.sh as the ONE entry point, so ending inside it
fits. Check the same treatment for any other sub-command that can
loop (install, passwords, verify).

## datadoor: "show the password again" option (Brett hit, 2026-09-21)

The data-door password prints ONCE when the door is created. If it
scrolls away or is lost, the only recovery is close + reopen (which
revokes and regenerates). Add a datadoor choice that re-prints the
EXISTING password (and the address) without regenerating it - and
print it in plain text that stays on screen (no popup, pause after).

## BUG: install never restarts an already-running service (found 2026-09-21 11:24)

do_install copies the NEW code into /opt, but its final "start the
service?" runs `systemctl start` - a no-op when the service is already
running. Result: the box keeps running the OLD program after every
install until someone remembers to `sudo ./manage.sh restart` by hand
(the 401-from-stale-code confusion, 2026-09-21 11:24). Fix: if the
service is active at the end of install, RESTART it (and say so); the
start question then only applies when it was not running.

## BUG: install silently revokes companion access (found 2026-09-21 11:17)

do_install rewrites modem.conf's token_file/controller_file back to
secrets/modem.token, so after ANY install the radio server's door
reverts to the internal token and every companion device (observer
token) is locked out - hilltop logged `raw-token auth rejected` spam
from the PC companion until it was stopped. Fix: install must PRESERVE
an existing observer.token setting (or re-link it) instead of forcing
modem.token. Related: decide whether `companions` ON should survive
reinstall by design.

## Database maintenance tasks (Brett, 2026-09-20)

The node table currently lives in memory only (RollingStore) and is
pruned on the layout cadence (stale 14d -> off maps, lost 30d ->
forgotten). The moment persistence lands (sqlite or similar), add a
maintenance story with it:

- periodic VACUUM/compact of the database file
- prune of old observations/packets consistent with the node rules
  (stale 14d / lost 30d)
- a `manage.sh` entry (or automatic task) that reports table sizes so
  growth is visible before it becomes a problem

## Add an `update` option to manage.sh (Brett, 2026-09-20 - for later)

One menu/command option that safely updates an installed box:

1. CHECK whether a newer version exists (git: local source clone vs
   origin/main) - no network writes, just a fetch and compare.
2. If a newer version exists: `git pull` the home source clone, then
   run the normal `install` (re-copies to /opt; the contract already
   preserves the venv, secrets, and previous answers).
3. SKIPS `configure` entirely - existing settings stay as they are.
4. Restarts the service ONLY if something was actually updated; says
   "already up to date" and does nothing otherwise.

Design note: reuse do_install as-is rather than duplicating its steps;
the update command is: check -> (pull + install) -> (restart if changed).
