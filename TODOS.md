# meshtech-node - TODOS (order matters, top first)

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
