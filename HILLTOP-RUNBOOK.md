# HILLTOP CUTOVER RUNBOOK - meshtech-node (single service, listen-only)

Rewritten 2026-09-20 for the SINGLE-PROCESS model (Brett: the user
controls ONE thing): the node embeds the radio server in-process, so
there is exactly ONE service - `meshtech-node` - and ONE manager:
`./manage.sh`. Nothing here transmits: TX stays config-off for this
whole window (a separate, explicit later decision turns it on).

Success picture: hilltop's PiMesh 1W v2 radio is owned by the node
service; your desk radio's #scope traffic appears in the scope app
over the local web feed; the openhop repeater stays STOPPED (the
accepted stand-down). Abort at any time = one command (bottom).

---

## PHASE 0 - GET THE CODE ONTO THE BOX (one command, your home folder)

## PHASE 0 - GET THE SOURCE (home = git source, never the run home)

0.1  From your home folder on hilltop:

    git clone <repo-url> meshtech-node && cd meshtech-node

    THE LAYOUT CONTRACT: this clone is the SOURCE only. `install`
    copies the program into /opt/meshtech-node (root-owned) and that
    is where it runs - venv, secrets, configs, service all live there.
    Nothing root-owned is ever created in your home folder. Updates:
    git pull in this folder, run install again, restart the service.

## PHASE 1 - ONE-TIME SETUP (guided, self-contained)

1.1  Run the manager (needs sudo for systemd + secrets):

    sudo ./manage.sh install

    The installer walks you through everything in plain text, one
    question at a time. It: installs the python dependencies ->
    generates the modem password and shows it ONCE (it waits until
    you type "saved") -> asks the radio settings (US 915 MHz band
    defaults; Enter accepts each) -> registers the systemd service ->
    runs the channel key check -> asks "Start the service now?".
    Nothing is read from any other software on the box: the install
    is fully self-contained. Transmit stays OFF.

    To change settings later: sudo ./manage.sh configure

1.2  VERIFY THE KEY: the installer runs this for you, but any time:

    ./manage.sh verify

     Expected: channel hash 0x39 for #scope (the hashtag rule
     sha256('#scope')[:16]), aes key 2373636f706500000000000000000000.
     Cross-check on the handheld: the scope app's log line
     "#scope found in radio slot 5 - secret MATCHES #scope" is the
     radio-side proof. The node and the radios must agree.

     (HILLTOP MIGRATION NOTE, our own step, not the installer's: the
     box's previous openhop repeater ran 910.525 MHz / SF7 / 250 kHz /
     sync 0x12 / preamble 32 - the shipped defaults match it; the
     installer asks each value so a different box can differ.)

## PHASE 2 - THE HANDOVER (the one irreversible moment)

2.1  Your call, out loud, in the moment. Only when YOU say so:

    sudo systemctl stop openhop-repeater && sudo systemctl disable openhop-repeater

     (disable = it will not return on reboot - the stand-down)

2.2  VERIFY the radio is free (nothing else holds it):

    sudo fuser -v /dev/spidev0.0 2>&1 | head -3; ps aux | grep -iE 'openhop|repeater' | grep -v grep | head -3

     Expected: no output from either.

## PHASE 3 - START THE ONE SERVICE

3.1  Start it (this boots the radio in-process and then the feed):

    sudo ./manage.sh start

3.2  VERIFY the boot checklist:

    ./manage.sh status

     Expected log lines, in order:
       'embedded radio server UP on 127.0.0.1:5055 (radio parameters:
       910525000 Hz, SF7, ...)'
       'RADIO LINK UP - the node is listening (TX off, listen-only)'
       'crypto: vendored _NodeCrypto in use' (or reference pymc_core)
       'Scope feed running: channel #scope ... budget 60 pkt/h'
       'WebServe listening on 127.0.0.1:8710/feed'

3.3  VERIFY SILENCE ON THE AIR (the honesty check):

    journalctl -u meshtech-node --since '-10 min' --no-pager | grep -c 'TX '

     Expected: 0.

## PHASE 4 - SEE THE REAL MESH

4.1  From the Windows machine, tunnel the app through:

    ssh -N -L 8710:127.0.0.1:8710 USER@hilltop

     then open http://127.0.0.1:8710/ locally, switch the source to
     Direct, Connect node.

4.2  Key the desk radio / let the mesh breathe. Expected: 'active
     nodes' and 'mesh RX/hour' climb from 0; the log fills with
     [direct] scope lines; sections light up.

4.3  Sanity-check the numbers against the plugin era (tens of nodes,
     RX/hour in the hundreds) - the 37890-style nonsense of 2026-09-18
     must NOT return.

## DAY-TO-DAY (the whole surface)

    sudo ./manage.sh          # interactive menu (whiptail when present)

    # or direct commands:
    sudo ./manage.sh install | configure | passwords | verify
    sudo ./manage.sh start | stop | restart | status | logs
    sudo ./manage.sh uninstall   # removes the service; asks before deleting files

## RADIO TRANSMIT (the txmode toggle - a separate, explicit decision)

TX stays OFF through this whole runbook (listen-only). The one and
only switch is in the configure menu:

    sudo ./manage.sh configure   # pick "txmode"

- Shows the current state and asks before changing anything.
- Flips `tx_enabled` in /opt/meshtech-node/config.json (the single
  source of truth - no other copy of the flag exists), restarts the
  service, and shows the new status.
- Turn ON only when YOU decide the data feed is proven. Turn OFF any
  time the same way; the node returns to listen-only on restart.
- Proof of state, any time: the log line after boot says either
  'TX off, listen-only' or the feed starts TX pulses.

## SECTION NUMBERING (v1.2, 2026-09-20 - for anyone reading old logs)

Sections are numbered **1 to 9** everywhere - on the wire, in the
node's logs, and on the web app's map. Section 1 is the upper-left
(NW) square; 9 is the lower-right (SE); the centre is 5. A target of
**0 means "the whole map"** in a refresh request - never a square.
The web app's "Refresh map" button logs as `kind=map`.

Older captures (before this change) used 0-8 for squares, so an old
log's "section 0" is today's section 1. The wire protocol version
byte moved from 0x02 to 0x03 at the same time; packets from either
era are decode-able but the numbering convention differs - compare
old and new logs with that in mind.

## WEB APP REFRESH LIMITS (what users will see)

The map refresh button is rationed to protect airtime (S2, 2026-09-20):

- Whole-map refreshes: 2 per 30 minutes for the WHOLE SERVER, all
  connected browsers combined. A third attempt is refused with an
  event-log line saying how long to wait (~minutes). Not an error -
  the budget doing its job.
- Per-section refreshes: not drawn from that budget; each connection
  has its own small cooldown/cap, so browsing routes is never blocked
  by someone else's map refresh.
- On-air refresh requests from radios follow the same brain limiter as
  always; the web budget is separate and only guards the wire path.
- With TX off (Gate 1) none of this costs airtime - the budget still
  applies so the behavior is identical when TX turns on.

## EXIT / ABORT (back to the old world in one line)

    sudo ./manage.sh stop && sudo systemctl enable --now openhop-repeater

    (the repeater returns exactly as it was; the node wrote nothing)

- The plugin's releases dir on the box stays untouched (rule: never
  delete Brett's stuff without asking).
- TX NEVER HAPPENS in this runbook. A later, separate decision flips
  tx_enabled - never during this window.
