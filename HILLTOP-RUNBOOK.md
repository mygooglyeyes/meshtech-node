# HILLTOP CUTOVER RUNBOOK - meshtech-node takes the radio (LISTEN-ONLY)

Written 2026-09-20. One step at a time, every step a single command or
single decision, Brett's hands on every irreversible moment (the
Confirmation Rule). Nothing here transmits: the node runs LISTEN-ONLY
for this whole window. TX stays config-off until a separate, explicit
later decision.

Success picture at the end: hilltop's PiMesh 1W v2 radio is owned by
cleanmodem, the node is connected to it listen-only, and your desk
radio's #scope traffic appears in the scope app over the local web
feed. The openhop repeater stays STOPPED - that's the accepted
stand-down.

---

## PHASE 0 - PREP (agent machine, before Brett touches hilltop)

0.1  Agent: verify the package builds and the suite is green
     (`.venv/Scripts/python.exe -m pytest tests/ -q` -> 143 pass +
     6 skip). DONE 2026-09-20.

0.2  Agent: bundle the deploy package (node src + cleanmodem + config
     template + runbook) into `meshtech-node-deploy-YYYYMMDD.tar.gz`.
     No secrets inside: the #scope key is the public hashtag rule, and
     the modem token file is created ON the box by Brett.

0.3  Agent: hand Brett the scp command (one line, copy-button safe).

## PHASE 1 - DEPLOY (Brett's hands, one command per step)

NOTE: the runbook shows the generic username `USER@hilltop` - it is
NOT committed as a real hostname/account pair (rule zero: no
machine-specific details in git). Brett knows his own.

1.1  Brett: copy the package up:
     scp meshtech-node-deploy-*.tar.gz USER@hilltop:/tmp/

1.2  Brett: unpack it:
     ssh USER@hilltop "sudo mkdir -p /opt/meshtech-node && sudo tar -xzf /tmp/meshtech-node-deploy-*.tar.gz -C /opt/meshtech-node && sudo chown -R USER:USER /opt/meshtech-node"

1.3  Brett: create the python venv on the box and install the node:
     ssh USER@hilltop "cd /opt/meshtech-node && python3 -m venv .venv && .venv/bin/pip install -e . aiohttp pycryptodome pytest"
     (deps are stdlib+3; no pymc_core needed - vendored crypto,
     byte-proven identical 2026-09-20)

1.4  Brett: create the modem token file (mode 600, never committed):
     ssh USER@hilltop "umask 077 && printf 'pick-a-password\n' > /opt/meshtech-node/modem.token"
     then set cleanmodem's config to the SAME password (step 1.6).

## PHASE 2 - THE HANDOVER (the one irreversible moment)

2.1  Brett's call, out loud, in the moment: STOP the openhop repeater.
     Only when you say so:
     ssh k6bps@hilltop "sudo systemctl stop openhop-repeater && sudo systemctl disable openhop-repeater"
     (disable = it will not come back on reboot - the stand-down)

2.2  VERIFY the radio is now free (no other process holds it):
     ssh k6bps@hilltop "sudo systemctl stop openhop-repeater 2>/dev/null; sudo fuser -v /dev/spidev0.0 2>&1 | head -3; echo '---'; ps aux | grep -iE 'openhop|repeater' | grep -v grep | head -3"
     Expected: no processes, no fuser output.

## PHASE 3 - CLEANMODEM TAKES THE RADIO

3.1  Agent hands over cleanmodem's config (modem.conf on the box):
     radio settings MUST match the repeater's old block line by line
     (frequency, SF, BW, sync word, preamble, power) - BENCH-CHECKLIST
     rule: verify against the box's own /etc/openhop_repeater/config.yaml
     radio block BEFORE first start, never assume defaults.

3.2  Brett: install cleanmodem as a boot service and start it:
     ssh k6bps@hilltop "cd /opt/meshtech-node && sudo cp cleanmodem/cleanmodem.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now cleanmodem"

3.3  VERIFY the modem server is up and the radio answered:
     ssh k6bps@hilltop "systemctl is-active cleanmodem && journalctl -u cleanmodem -n 12 --no-pager"
     Expected: 'listening on 127.0.0.1:5055', radio init OK.

## PHASE 4 - NODE, LISTEN-ONLY

4.1  Brett: write the node's config from the template (agent provides
     exact content at this step), then start the node service:
     ssh k6bps@hilltop "cd /opt/meshtech-node && sudo cp meshtech-node.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now meshtech-node"

4.2  VERIFY the whole chain in one log line set:
     ssh k6bps@hilltop "journalctl -u meshtech-node -n 20 --no-pager"
     Expected, in order:
       'BENCH...' NO - expect: 'RADIO LINK UP - the node is listening
       (TX off, listen-only)'
       'crypto: vendored _NodeCrypto in use'
       'Scope feed running: channel #scope ... budget 60 pkt/h'
       WebServe listening on 127.0.0.1:8710/feed

4.3  VERIFY silence on the air (the honesty check): the node log must
     show ZERO TX lines, and the desk radio hears nothing new.
     ssh k6bps@hilltop "journalctl -u meshtech-node --since '-10 min' --no-pager | grep -c 'TX '"
     Expected: 0.

## PHASE 5 - SEE THE REAL MESH

5.1  Browser on the LAN (or SSH tunnel): open the node's served app.
     Direct mode is unnecessary here - the app is served BY the node
     already; just connect. From the Windows machine:
     ssh -N -L 8710:127.0.0.1:8710 k6bps@hilltop
     then open http://127.0.0.1:8710/ locally and switch source to
     Direct, Connect node.

5.2  Brett: key the desk radio, send any #scope traffic (or just let
     the mesh breathe). Expected: 'active nodes' and 'mesh RX/hour'
     climb from 0; the log fills with [direct] scope lines; sections
     light up as the brain ingests.

5.3  Agent + Brett: eyeball the numbers against the plugin era
     (sanity, not precision): tens of nodes, RX/hour in the hundreds -
     the 37890-style nonsense numbers of 2026-09-18 must NOT return.

## EXIT / ABORT

- ABORT ANY TIME: restart the old world in 2 commands:
  ssh k6bps@hilltop "sudo systemctl stop meshtech-node cleanmodem && sudo systemctl enable --now openhop-repeater"
  (the repeater returns exactly as it was; nothing was written to it)
- TX NEVER HAPPENS in this runbook. A later, separate decision turns
  tx_enabled on - never during this window.
- The plugin is now moot on the box; its releases dir stays untouched
  (rule: never delete Brett's stuff without asking).
