# meshtech-node - TODOS (order matters, top first)

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
