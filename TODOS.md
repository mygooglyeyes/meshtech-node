# meshtech-node - TODOS (order matters, top first)

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
