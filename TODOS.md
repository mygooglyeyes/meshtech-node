# meshtech-node - TODOS (order matters, top first)

## BUILT (2026-09-24, Brett's law "TCP is not the mesh"): DOOR-BORNE
## ASKS - 287 tests green, version 00.000.043
An ask through the TCP data door is answered THROUGH THE DOOR: no
radio TX, no airtime budget, no per-phone limiter, no burst gaps.
The wire costs nothing to speak; the RADIO path keeps every limit
(dedupe/rate/budget untouched there - test_radio_path_keeps_every
_limit pins it). Service: on_packet(via_door=) -> the door branch
taps every packet straight to the WS clients (would_tx=False,
honest); dedupe STILL applies to door asks (same ask twice =
answered once - unlimited is not a hammer). WebServe: the
per-client verdict + global map-budget pools no longer gate door
asks (machinery stays, dormant, for the radio path). Tests: 5 new
(test_door_borne_asks.py) + 5 old door-limit pins rewritten to pin
the new law. MOTIVATION: the bench hit "40km map refresh refused -
global budget spent" while testing the phone app - the phone
could not fill its map at all. The old web app rides the same door
and gets the same lift.


## BOX UPDATED (2026-09-24): VECTORED SYNC LIVE ON HILLTOP + VERSION
## RULE (Brett) NOW ENFORCED ON GITHUB
Brett ran `sudo ./manage.sh update`: dbadcee code installed, service
restarted, RADIO LINK UP listen-only. Report said 00.000.041 -> 041
because the vectored-sync commit missed the version bump; the
commit-compare (not the version) correctly drove the copy. Label
fixed + rule shipped in 7de41f4: version 00.000.042, and a GitHub
Actions check (check-version-rise) now FAILS any main push whose
pyproject version did not rise - first live run: success. NO new
box update needed just for the label (same code); the next real
code push rides its own bump.

## QUEUED (Brett, 2026-09-24, "one todo later"): STALE NODES PACKET
(For the meshtech-app phone, tracked here too because the wire
changes.) A packet naming the nodes the server has not heard from in
14 days (STALE_AFTER_S - the fact already exists in the store) ->
the phone renders those dots YELLOW. Design when Brett pulls it.

## FLUTTER CHAPTER (2026-09-24, Brett's go): NEW app at
## C:\projects\meshtech-app - design doc awaiting Brett's "correct"
Brett's calls this hour: web app chapter CLOSED (his words: "done
trying to fix the web app"); new Flutter app lives in its own repo
C:\projects\meshtech-app (created: skeleton, Android-only, org
com.meshtech); PC web-app server 8616 already stopped (nothing to
kill - hilltop's data door untouched). DESIGN RULE: nothing built
until Brett verifies C:\projects\meshtech-app\DESIGN.md line by
line; REDESIGNED same day to v2 (Brett's three changes): OTA-FIRST
(companion radio -> BLE -> phone is the live path), TCP demoted to a
DOWNLOAD of map + node database for offline use (never a live link),
map size picked BEFORE connect and size changes redraw ONLY (never
ask hilltop, never auto-refresh; the app translates data heard at
any size to the size chosen, packets carry their true span). Phone
keeps its OWN store and always draws from it. Open: build order
(OTA-first-tested-by-download vs BLE-first) = Brett's call; hilltop
TX stays OFF until his explicit go. The app gets its own TODOS.md
(C:\projects\meshtech-app\TODOS.md) - future work tracked THERE.

## CHAPTER CLOSED (Brett's call, 2026-09-24 ~10:00): "done trying to
## fix the web app" - NEXT: a real phone app (Flutter), design first
Brett's words: "it clearly can not handle what I am trying to do".
Tonight's live facts before the close (honest record, not an argue):
the v12 page DID deliver honest refusals + a size-correct map on the
phone; the PC's 60 km view + shared budget (phone took the 40 km slot
at 09:31:14, PC refused in words) and the restart-wiped counts/routes
were the friction. UNRESOLVED when closed: doubled dots in section 8
(no diagnosis yet), per-device budget (offered, never approved).
Flutter chapter NOT started: prerequisites DONE (flutter doctor
green 2026-09-24: Flutter 3.47.5 + Dart 3.13.4 at C:\Users\Brett\flutter,
Android SDK 36 + licenses accepted; VS Code NOT needed - agent edits
files directly; Visual Studio C++ workload NOT needed - that is
Windows-desktop only). Design doc BEFORE any code (project rule),
nothing built without Brett's "correct".

## PUSHED (2026-09-24, Brett's word "commit & push"): THE CLIENT
## CHAPTER - routes on connect, route taps fixed, honest refusals,
## silent-drop watchdog = phone v00.000.012 (served copy in node app/)
Brett's four live reports, all fixed:
(1) ROUTE TAPS DID NOTHING: webserve's _client_msg had NO "route"
kind - every tap fell into the unknown-kind return and died silently.
NEW branch parses the target (0..0xFFFF) and runs the same refresh
path.
(2) ROUTES MISSING ON CONNECT: the connect burst now carries LAYOUT ->
9 section summaries (each with its route stubs, e.g. 4198) -> PULSE ->
intro roster, so a fresh connect + section tap lists routes at once.
(3) SILENT REFUSALS: a sized refresh the budget refuses now ACKs
honestly (accepted:false + reason + retry_after_s; reasons cooldown /
hourly_cap / not_allowed) instead of the server sending NOTHING; the
phone logs refusals in plain words. Wiring: webserve refresh_verdict
ctor param <- node.py serve.refresh_verdict <- budget.py verdict()
(allowed() kept as a wrapper).
(4) 233-MIN PULSE AGE (phone slept, TCP died silently, app never
told): directclient watchdog - no packet AND no pong for 11 min (2x
pulse cadence; any packet/pong refreshes it) -> link closed, chip
reads "link lost (no feed)". NO auto-reconnect - the v11 rule holds.
ALSO size-aware auto-ask: after connect the app asks for a map ONLY
if the held map is not already at the chosen size (3 s grace; a burst
inside the grace cancels the ask) - so a chosen-size answer applies.
Suites: node 271 passed + 6 skipped; phone 45 passed. This push also
carries 779c455 (the advert verdict + DB sift notes).
NEXT (live test): hilltop sudo ./manage.sh update + Ctrl+F5 -> fresh
connect lists routes on a section tap; 20/40 km resizes show honest
plain-words refusals when limited; phone asleep 15+ min comes back to
a "link lost (no feed)" chip.

## VERDICT (2026-09-24, forensic): the remaining advert rejects are
## INVALID-AS-SENT adverts from a MIXED fleet - the gate is right
First captured advert decoded (110 of 112 bytes, Tracy-area coords,
flags 0x92): verifies under NO recipe - reference, capped-96,
header-included, sig-first, sig-at-end, BE-ts, no-ts, relay-edited
prefix/suffix, 2-byte reconstructions, every single-bit flip + tail
unknowns (38.8M verifies), and RX framing add/drop families - ALL
negative. Not noise, not our bug, not a recipe variant we canname: these adverts were never validly signed as transmitted. Reject
sample: 7 DISTINCT senders in 30 min, 1 advert each (a population,
not a spammer). Meanwhile 4 clean nodes stored (Janet, Clover Patch,
9E5F-Band + 1) - standard senders DO pass. Conclusion: mixed fleet;
some devices broadcast adverts that fail Ed25519 under the MeshCore
recipe (custom/broken/older firmware - the old mojibake-twin source).
Pre-gate they became phantom dots; now counted + refused. NODE SIDE
HAS NOTHING TO FIX (gate stays exactly as-is, capture stays at 1/min
for evidence). Possible next: pubkey prefixes (abab91c0..., 8deb749b...)
could be posted to the MeshCore community to identify the broken
firmware - Brett's call.
CAPTURE 2 (132 B sender 1903b6c0, +110 capture minutes): same
verdict - no recipe, no relay-edit, no timestamp-rewrite in a -60d..
+90d window (13M tries) verifies. NEW FINGERPRINTS: BOTH captures
carry broken-clock timestamps (+12 h, +83.6 h - MeshCore needs sane
time for dedupe/order) AND flags 0x92 = advertises AS A REPEATER
(2/2 samples). Leading theory: DIY/alternate repeater firmware that
signs wrong (possibly with a key other than the transmitted pubkey -
unverifiable by design from the air) and keeps bad clocks. A clean
community identification needs the pubkey prefixes + the repeater-
flag observation, not just one payload.
DB HEALTH SIFT (2026-09-24, read-only, run on the box): 98 rows -
ZERO twins (98 distinct names, no name on 2 radio IDs), zero
mojibake (emoji in operator names is normal), zero far-out/zero-half
positions, zero stale, 6 honest no-position rows. ONE oddity: prefix
88 name carries two control bytes (\x03\x00N6UGX-Room2) - sender's
own firmware put them inside its SIGNED name; stored as sent, shown
harmlessly. Verdict: wipe + gate have produced the healthiest map
hilltop has had.

## DONE (2026-09-23 evening): one-time node-table wipe RUN (Brett's
## hands, live output verified)
245 rows before -> 0 after, service active. Every stored corrupt twin
is gone; the signature gate (live on the box since 21:43) stops new
ones. The map refills as real nodes advertise (hours, not minutes).
Phone side stores NOTHING (dots are page memory) - a reload forgets
all; per Brett's call, NO wipe-per-connect in the phone (overkill
with a clean table). WATCH: if duplicate/offset dots ever grow again
-> that is evidence of an unclosed twin path; chase it with
bench_demo.py + the box table dump. Open threads parked:
logo/branding integration; meshtech-phone PARKED list.

## PUSHED (2026-09-24): PYNACL WAS MISSING ON HILLTOP + REJECT
## CAPTURE + RECIPE PROBE (the every-advert-rejects hunt)
The morning after the signature gate shipped, hilltop's log showed
EVERY advert rejected. Diagnosis, proven with Brett's hands: the box
venv had NO pynacl (TODOS' earlier "pynacl was already in the venv"
was WRONG - manage.sh never installed it, pyproject declares no
deps), and the gate's degraded mode refuses everything while logging
"signature invalid" - a dead gate dressed up as corrupt radio.
FIXED LIVE (Brett's hands, output verified): pip install pynacl into
the box venv (1.6.2), service restarted active. Map refilling: 4
nodes stored within the hour (Janet, Clover Patch, 9E5F-Band + one
nameless advert - clean names, no mojibake).
BUILT (this push): (1) manage.sh install_python_deps now installs
pynacl every time; (2) the gate's missing-library case logs "pynacl
MISSING" (log.error) instead of the lying "signature invalid";
(3) REJECT CAPTURE: at most ONE rejected advert per minute logs its
length + first 110 bytes + a 4-recipe Ed25519 verdict battery (rate-
limited - the log can never fill the disk; the gate itself is
untouched); (4) tools/advert_probe.py decodes a captured line
offline and reports which recipe (if any) verifies - name the
senders' real recipe, never guess it. Suite: 270 passed, 6 skipped.
OPEN: rejects continue after the pynacl fix (gate accepts SOME
senders, refuses most - per-sender recipe difference is the lead).
Next capture line goes through advert_probe.py; recipe battery may
need more variants once real bytes are in hand.

## FIXED (2026-09-23 evening, built on Brett's order "fix the two
## pre-existing webserve test flakes"): TEST DB POLLUTION, NOT CODE
The 2 webserve test failures were never a code bug. FakeSettings
left storage.db_path at the RELATIVE default (data/scope.db), so a
run from the repo root opened the checkout's REAL database; leftover
bench-demo rows there (tools/bench_demo.py, the bench chapter) made
the connect burst stream a full intro roster that drowned the tests'
fixed drain windows. Fresh git worktrees had no such file -> passed.
Fix: every webserve test now gets its own throwaway DB (pytest
tmp_path); the two drain windows are bounded + count only tagged
packets. Main checkout: 266 passed, 6 skipped (was 264+2 failing).
Side effect: test runs no longer touch data/scope.db at all.

## PUSHED (2026-09-23, Brett's word "commit"): ADVERT SIGNATURE GATE
## (01a27c4) - the duplicate-dots root cause closed
ROOT CAUSE (Brett's box table dump, read-only): dozens of bit-corrupt
advert twins stored beside their clean originals - `ARE[(&(CERT`
twins at identical coords, `ES\\7 Gilroy` beside `ESP6 Gilroy`, a
mojibake Novato twin ~200 km off. One flipped bit = a "new" node row
(mojibake name, nonsense position, "new" radio ID); the connect
roster then delivered them all as extra dots. They only SHOW on
reconnect because connect fires the whole roster at once.
FIX (Brett picked the signature check): MeshCore adverts are
Ed25519-signed. packets.verify_advert_signature() verifies
pubkey+timestamp(4 LE)+appdata with the advert's own pubkey (recipe
proven against the openhop_core REFERENCE AdvertHandler - read-only,
rule honored); pynacl was already in the venv. rawsource._from_advert
gates on it BEFORE parse/dedupe/promotion: corrupt -> counted
(stats.corrupt, logged, never stored, never promoted). A failed
nacl import refuses everything (degrade loud, never wave through).
TESTS: tests/test_advert_signature.py (7, reference-signed: intact
passes, flipped name/position/pubkey/timestamp/signature all fail,
truncated fails); test_rawsource +2 (corrupt counted-not-stored;
signed advert still flows); scripted fixtures converted to REAL
signed adverts (deterministic zero-seed keypair); repeaters fixture
now uses a real signed advert too. Suite: 264 passed + 6 skipped;
2 webserve failures are PRE-EXISTING (verified via git stash - fail
identically without this change; timing-sensitive, separate matter).
NOTE: the ~40 corrupt rows ALREADY in hilltop's table stay until a
one-time cleanup (proposed: a small prune command or SQL Brett runs
by hand) - the gate only stops NEW ones. Awaiting his word on that
too. NEXT: commit -> push (node v42) -> hilltop update -> the roster
rebuilds clean as nodes re-advertise; phone needs nothing.

## PUSHED (2026-09-23, Brett's word): update gates on the COPIED
## COMMIT, not the version (the app-only-push blind spot closed)
Flaw Brett's box exposed: update compared only the program version
(040==040 -> skip) even when the served web app (app/) had changed,
so his phone kept the old page after a legend-only push and he had to
run install (full config Q&A - he correctly called that out: update
is the flow, install is NOT the bridge). Fix: do_update stamps the
commit it copied into /opt/meshtech-node/.update-commit after a
successful copy, and gates on stamp vs source HEAD - ANY difference
(version bump or not) re-copies; no stamp (first update / pre-stamp
install) = copy, the safe default. Recent-changes list now scopes to
run_commit..src_commit when a stamp exists. Gate logic dry-run
verified in a sandbox: no-stamp copies, same-commit skips,
different-commit copies. manage.sh only - no runtime code touched.
NOTE for Brett's box: its next update has NO stamp yet -> it copies
once (self-seeding), then behaves as designed.

## PUSHED (2026-09-23, Brett's call): NO AUTO-RECONNECT = phone
## v00.000.011 (served copy pushed; node program unchanged)
The 1006-storm diagnosis (box log): the app's background re-dials
hammered the node with a stale password and tripped the anti-guessing
lockout (5 refusals/min) - locking out even the right password.
Brett's verdict: a drop ENDS the session. Now: any link close ->
state disabled with the close code in the chip, ZERO background
dials, Connect is a human press (host + password stay saved).
test rewritten to pin one-dial-only. Chip -> 00.000.011.
ALSO OPEN (flaw I found in my update gate): update skips the copy
when the PROGRAM version matches even if only the served app changed
(Brett's box served the old page after a bundle-only push; the
commit-stamp fix is proposed, awaiting his go). Bridge that worked:
one manual `sudo ./manage.sh install` copies everything.

## PUSHED (2026-09-23, Brett's word "commit & push"): dot legend +
## version chip = phone v00.000.010 (served copy in node repo app/)
Brett's asks, both shipped: a tiny legend line above every map (color
dot + word: repeater / companion / class unknown - same CSS vars the
dots use) and the app VERSION always visible in the header (version.ts;
"is my page current?" is now a glance, not a guess).
BENCH REPRODUCTION of the reconnect-dots report (tools/bench_demo.py,
new): a demo-fed node + WebServe, drove the REAL app in the preview -
40 demo dots, three full disconnect/reconnect cycles, 40 dots every
time on the v9 app code. No growth reproduced. Brett's live result is
consistent with the page he tested still running the pre-update app
code (the cache question) - NOT proven, and the version chip makes
this checkable from now on (chip must read 00.000.010). If duplicates
EVER return: note the chip version + whether the mapped-nodes count
grows, and bench_demo.py re-runs the scene in minutes.

## PUSHED (2026-09-23, Brett's word "commit & push"): INTRO span on
## the wire (proto v1.5) + reconnect URL fix = node v00.000.041 /
## phone v00.000.009
Committed (node 16d2212 + d6af983 + 7ff2c0f served copy; phone
3bf9139 + f8b0b13), tagged, pushed; phone branches synced
(web/android-twa/apple-web ff + tools merge 1b40f36; android/apple
untouched); node's 4 local mirror branches at 7ff2c0f.
NEXT (Brett's live test - also the FIRST REAL RUN of manage.sh
update): on hilltop run `sudo ./manage.sh update` - expect: pulls,
reports 00.000.040 -> 00.000.041, recent-changes list, copies, deps,
restarts, ZERO questions. Then Ctrl+F5 on the app page + reconnect:
1) connect repeatedly - ONE dot set, no offset sets; 2) let the phone
sleep/wake over the next days - the app should heal itself (reconnect
dials the right URL now) without a page refresh.

## BUILT, UNCOMMITTED (2026-09-23): INTRO carries its own span (proto
## v1.5, 0x05) - the "new set of offset dots on every connect" fix
Brett's report: every Connect press drew a NEW SET of offset node
ROOT CAUSE: INTRO positions are deltas measured against a map span,
but the wire carried NO span (v1.0-1.4) - the client GUESSED the scale
from whatever LAYOUT it held; any mismatch (a sized window, a replayed
LAYOUT on connect) scaled every delta wrong = fresh offset dots per
connect. (The earlier 60 km factor hack assumed held span == packet
span - true only at a fixed 60 km.)
FIX (wire change, both codecs, versions next: node 41 / phone 9):
- INTRO gains span_m (2 LE meters) after the header, BEFORE count.
  encode writes round(span_m) (0 < x <= 65535 enforced); decode uses
  the packet's span as THE TRUTH; a caller-passed span is cross-check
  only (mismatch raises - loud beats silently wrong). Version-gated:
  v1.4 packets decode old-style (caller span / 40 km era default).
  decode_any needs NO caller knowledge for v1.5 packets.
- PROTO_VERSION 0x04 -> 0x05 (golden vectors regenerated, all six).
- Phone: decodeIntro mirrors the same rules; applyIntro drops the
  factor hack - decode is now exact (same scale both sides), so it
  just adds the held LAYOUT center. (Center itself stays off-wire,
  v1.0 decision, unchanged.)
- Wire cost: +2 bytes per INTRO (~30-35 packets/h) = ~70 B/h.
- tests: node codec/feedbuilder/roster/golden suites green (257 + 6
  skip; new regression pin "intro carries its own span"); phone codec
  23 + state 7 green; served copy synced into node repo app/.
NOTE: cross-version window: an OLD phone (v0x04 client) against a NEW
host gets a decode error on INTROs until it updates (honest refusal,
not wrong dots); a NEW phone against an OLD host decodes old-style.
NEXT: Brett reviews -> commit BOTH -> push (node v41 / phone v9) ->
hilltop: git pull, sudo ./manage.sh install (or the new update cmd),
Ctrl+F5 -> live test: connect repeatedly, ONE dot set, no offsets.
CONNECTION DROPS (second bug) - ROOT CAUSE FOUND + FIXED, same build:
Brett's log line: "link closed: error=1006 clean=false" (the TCP path
died - phone sleep / Wi-Fi blip; the node did not end it). The bug was
NOT the radio or the node: directclient.ts scheduleReconnect dialled
this.lastUrl - which was NEVER ASSIGNED. Every retry dialled null,
failed, retried null forever: "retrying and not connecting" until a
page refresh re-ran connect() with the real URL. Fix (phone only):
connect() remembers the URL (lastUrl = url); open() refuses to dial a
blank one (schedules a retry instead - belt and braces).
NEW TEST: lib/directclient.test.ts (1 test) - stubbed WebSocket,
simulates a 1006 close, awaits the backoff timer, pins the second dial
= the original URL. testrunner.ts extended to await async test fns;
registered in build.py tests tuple. All phone suites green.

## ALSO IN THIS BUILD (phone): testrunner awaits async tests (the
reconnect test needs the ~1 s backoff timer); build.py runs the new
test file.

## PUSHED (2026-09-23, Brett's word "commit & push"): manage.sh update
## command (fd0c491 on main; no version bump - runtime code unchanged)
Brett's flow fix: no more full config Q&A after every git pull.
'sudo ./manage.sh update' (+ menu item) = pull --ff-only as the clone
owner (fail-closed: pull failure changes NOTHING), version report
(installed /opt vs pulled source - a manual pull first still lands),
last-5 change notes, copy + deps + service file, restart-if-running /
start-if-stopped, ZERO config questions, settings + secrets untouched.
install now shares the install_python_deps helper (behavior identical).
NOTE: box still on v40; first update run will honestly say "already at
the newest code (version 40)" and skip the restart - that IS the safe
live test. Box needs ONE more manual git pull to receive this manage.sh.
NEXT: Brett pulls once + runs sudo ./manage.sh update as the test.
LIVE-TESTED (2026-09-23 16:52, Brett on hilltop): manual pull, ran
update from the menu -> "already at the newest code (version
00.000.040; the installed copy matches). Nothing to do." - the no-op
path works on the box. His manual restart was harmless; service came
back clean (radio up, 246 nodes refilled). REAL TEST = the next
release: update should pull + copy + restart with zero questions.

## PUSHED (2026-09-23, Brett's word "commit and push"): name-supersede
## replaces the twin merge = node v00.000.040 / phone v00.000.008
Brett's corrected rule built, tested, committed, pushed: node main
767b254 + tag v00.000.040 (3cf2515 supersede, 1305902 version,
767b254 served phone v8 bundle); phone main 5b20b0c + tag
v00.000.008 (c45300c dotNodes collapse, 5b20b0c version). Branch
syncs: phone web/android-twa/apple-web ff + tools merge a9c91d8
(android/apple untouched); node's 4 local mirror branches at 767b254.
NEXT: hilltop install (git pull && sudo ./manage.sh install), then
Ctrl+F5 + reconnect - live test: ONE dot per node (KN6OBW DT was the
doubled one). Boot log should show the two old same-name rows
collapse to one at refill.

## BUILT, UNCOMMITTED (2026-09-23): name-supersede replaces the twin
## merge (Brett's corrected design; awaiting his commit word)
Brett's live test: dots STILL doubled after v39 (connection drops,
reconnects, refresh). His corrected rule, approved with one amendment:
THE NAME IS THE IDENTITY and identities MOVE - a fresh advert carrying
a name that matches an existing DIFFERENT prefix retires that old row
(RAM + disk), freshest advert wins, NO distance test, no plausibility
judgment. AMENDMENT (his): implausible location data is still filtered
before saving - satisfied by the existing planet-range + half-fix
guards, which run BEFORE any supersede (a torn advert can neither
move a dot nor delete a good one).
- SERVER: observations.py same_place/TWIN_MERGE_* GONE ->
  node_with_name(); add_position supersedes by name; boot refill
  collapses same-name disk rows fresher-wins in EITHER arrival order
  (older row forgotten from disk both ways; honest restored count).
  node_store.forget_node docstring updated. tests/test_twin_merge.py
  rewritten (9 tests - the far-apart test now PINS supersede).
- PHONE: state.ts dotNodes() = one dot per name, freshest lastIntroTs
  wins (ties -> latest arrival); filteredNodes() collapses too, so the
  mapped-nodes count equals drawn dots. App.ts/demo.ts all map+count
  call sites use it. state.test.ts +3 tests (7 total). Nameless rows
  never collapse (nothing to collapse BY) - server + client agree.
TESTS: node 257 passed (6 skipped); phone all suites green + bundle
rebuilt; served copy synced into node repo app/ (8 files).
NEXT: Brett reviews -> commit BOTH repos -> push (node v00.000.040 /
phone v00.000.008) -> hilltop install -> live test: one dot per node.

## PUSHED (2026-09-23, Brett's word): twin merge + startup saved view +
## auto sized refresh = node v00.000.039 / phone v00.000.007
Committed (node 8d24f2c + f7126ee; phone 0fbbcdd + 1d1408d), tagged,
pushed: node main + tag on GitHub (remote is main+tags only - three
stray branch names I created by pushing the phone scheme there were
deleted seconds later, nothing else touched); phone main + tag +
web/android-twa/apple-web fast-forwarded + tools merged in the
established pattern; node's four local mirror branches fast-forwarded.
NEXT: hilltop install (git pull && sudo ./manage.sh install), then
Ctrl+F5 + connect - expect: saved view card on load, auto "asking for
a 60 km map refresh" in the event log on connect, ONE dot per node.

## BUILT, UNCOMMITTED (2026-09-23): startup saved view + auto sized
refresh + the twin-dots fix (awaiting Brett's commit word)
Brett's live test found the second-dot artifact and the 60 km startup
gap; his approved plan ("correct"): 1) startup shows what was saved,
2) connection asks for the saved size. Built on top:
- TWIN MERGE (the double-dots bug): one physical node under two radio
  identities (hilltop's own table: KN6OBW DT = prefix 3b AND a8, ~40 m
  apart) drew two dots. Same name + same place (<=100 m) merges on the
  server (observations.py same_place/add_position; NEWEST identity
  wins, older retired in RAM + disk via node_store.forget_node - new
  method); boot refill collapses disk twins too (fresher identity
  kept, older row deleted). tests/test_twin_merge.py (5 tests).
- INTRO PROJECTION FIX (phone, state.ts): the wire carries NO span,
  the live decode assumed the old 40 km map - on the 60 km box dots
  landed at 2/3 offset and a 20 km window's INTRO decoded at 2x; the
  same node across two decode eras landed tens of meters apart, which
  FED the twins. Now the client rescales by the held LAYOUT's real
  span before adding the center (40 km = factor 1 = old behavior).
  app/src/lib/state.test.ts (4 tests) registered in build.py --test.
- STARTUP SAVED VIEW (phone): the last LAYOUT frame is stored
  (scope.mapFrame) and drawn on load - "saved view: 3x3, ~60 km" -
  with the saved pulse counts; the waiting card only shows with
  nothing saved.
- AUTO SIZED REFRESH (phone): a FRESH view (no geometry, no pulse)
  asks for a map at the stored size the moment the link is up - the
  same ask the button makes, spends one slot from the size's pool; a
  link bounce never re-asks (map already drawn, pool is small).
TESTS: node suite 253 passed (6 skipped); phone all suites green;
served copy synced into node repo app/ (8 files). NOTE: test_service
fixture rows renamed (Chat Node/Mystery Node) - the twin-merge
correctly collapses the old same-name-same-place synthetic rows.
NEXT: Brett reviews -> commit -> push (node v00.000.039, phone
v00.000.007) -> hilltop install -> live test.

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
- step 5 DONE, committed phone repo e5ec3e3: Map size selector on
  the map card (20/40/60, remembered, caps shown on the options),
  whole-map refresh sends spanKm on wire + direct message. All phone
  lib tests green, build clean.
ALL 5 STEPS BUILT + PUSHED (2026-09-23, Brett's word): node =
v00.000.036 (tag pushed, all 5 local branch mirrors fast-forwarded
to main), phone = v00.000.004 (tag pushed; all 7 branches synced -
tools was repaired to its remote merge-lineage then merged with
main). v36 INSTALLED on hilltop 2026-09-23 08:22 (server half verified:
home-area question, Enter kept center, box bumped to 60, service
clean). LIVE TEST FOUND A BUG: the 1s housekeeping timer re-rendered
the whole page, snapping the Map size dropdown shut (Brett: whole
page flashes ~1/s). FIXED (tick-only clock: timer updates just the
pulse-age text) -> node v00.000.037 / phone v00.000.005, pushed,
served copy refreshed via repaired sync_to_node (it still pointed at
the pre-restructure web/ layout). android/apple phone branches left
alone (unique stage-2 work, self-consistent with remotes). v37 INSTALLED on hilltop 2026-09-23 09:19 - dropdown live test
PASSED ("functioning as intended"; no more 1s page flash).
SECOND live bug: section-map node dots rendered as black specks -
the dot rim was drawn in map units, so any zoom-in fattened it past
the fill (Brett's call 2026-09-23: dots need NO border at all).
FIXED rimless (style.css .dot stroke:none) -> node v00.000.038 /
phone v00.000.006 pushed, branches synced, served copy refreshed.
Remaining: hilltop install of v38 + Ctrl+F5 + reconnect; dots
should be bright on the main map AND section detail maps.

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
