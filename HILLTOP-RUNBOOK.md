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

0.1  From your home folder on hilltop:

    git clone <repo-url> meshtech-node && cd meshtech-node

    (until the repo has a remote, copy the working folder up instead:
     `scp -r C:\\projects\\meshtech-node USER@hilltop:~` from Windows,
     then `cd ~/meshtech-node` - same result)

## PHASE 1 - ONE-TIME SETUP

1.1  Run the manager (needs sudo for systemd + secrets):

    sudo ./manage.sh setup

    It does, in order: venv + dependencies -> generates the modem
    token under secrets/ (mode 600, printed ONCE - save it) -> writes
    config.json + modem.conf from the committed templates -> installs
    and enables the single systemd unit.

1.2  VERIFY THE RADIO SETTINGS (never assume defaults are current):
     compare modem.conf's radio block line by line against the OLD
     repeater's settings:

    grep -A9 "^radio:" /etc/openhop_repeater/config.yaml
    grep -E "^(frequency_hz|spreading_factor|coding_rate|bandwidth_hz|sync_word|preamble_length|tx_power_dbm)" modem.conf

     Every value must match (watch units: openhop lists bandwidth in
     kHz - cleanmodem in Hz; 250 kHz = 250000). Fix modem.conf if any
     differ.

1.3  VERIFY THE KEY (your explicit ask): confirm the #scope secret the
     node derives matches what your radios hold:

    ./manage.sh verify

     Expected: channel hash 0x39 for #scope (the hashtag rule
     sha256('#scope')[:16]), aes key 2373636f706500000000000000000000.
     Cross-check on the desk radio: the scope app's log line
     "#scope found in radio slot 5 - secret MATCHES #scope" is the
     radio-side proof. All three (node / committed code / radio) must
     agree before starting.

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

    ./manage.sh start | stop | restart | status | logs | verify

## EXIT / ABORT (back to the old world in one line)

    sudo ./manage.sh stop && sudo systemctl enable --now openhop-repeater

    (the repeater returns exactly as it was; the node wrote nothing)

- The plugin's releases dir on the box stays untouched (rule: never
  delete Brett's stuff without asking).
- TX NEVER HAPPENS in this runbook. A later, separate decision flips
  tx_enabled - never during this window.
