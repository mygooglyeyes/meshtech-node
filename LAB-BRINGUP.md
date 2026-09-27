# LAB BRING-UP - openhop repeats through meshtech-node's radio

One box, one radio chip, two programs (lab plan 2026-09-26, Ch1-Ch3).

- **meshtech-node** owns the SX1262 chip on the PiMesh HAT and serves
  the radio on TCP port 5055. It is the radio master.
- **openhop** (the repeater) dials that port with its OWN token, gets
  the "repeater" door (hear + transmit, NO config authority), repeats
  packets for real, and keeps its MQTT work unchanged.
- **Three doors, three tokens.** The bot (controller), openhop
  (repeater), companions (observer). Never share one token between
  two doors - the stronger door wins if you do.

Everything below runs on hilltop. One command per block.

## BRING-UP

### 1. Put the new code on the box

```
cd ~/meshtech-node && sudo ./manage.sh update
```

### 2. Make openhop's own token (its password - shown once)

```
sudo sh -c 'umask 077; python3 -c "import secrets; print(secrets.token_urlsafe(24))" > /opt/meshtech-node/secrets/repeater.token'
```

```
sudo sh -c 'chmod 600 /opt/meshtech-node/secrets/repeater.token && cat /opt/meshtech-node/secrets/repeater.token'
```

Copy the token it prints - openhop's config needs it.

### 3. Open the repeater door in modem.conf

```
sudo sh -c 'grep -q "^repeater_file" /opt/meshtech-node/modem.conf || echo "repeater_file = /opt/meshtech-node/secrets/repeater.token" >> /opt/meshtech-node/modem.conf'
```

### 4. Restart the radio service

```
cd ~/meshtech-node && sudo ./manage.sh restart
```

### 5. Point openhop's radio at the modem

Edit `/etc/openhop_repeater/config.yaml` - THREE small changes:

- set `radio_type: modem_tcp` (older builds call it `pymc_tcp`;
  both names work, `modem_tcp` is the modern one)
- in the EXISTING `modem_tcp:` block (host 127.0.0.1, port 5055),
  replace the `token:` value with openhop's token from step 2
- check the `repeater:` section has `mode: forward` (a REAL repeater:
  forward = repeat on; monitor = hear only; no_tx = everything off)

THE LESSON (learned live on hilltop, 2026-09-26): openhop reads the
`modem_tcp:` block - the one already in the file. A separate
`pymc_tcp:` block is the OLD spelling and is IGNORED when `modem_tcp`
exists (the modern key wins), so putting the token there gets it sent
nowhere. Password in the `modem_tcp:` block, or nothing works.

To pull the token in without typing it, this replaces the token line
in the modem_tcp block with the secret from step 2:

```
sudo bash -c 'sed -i "/^modem_tcp:/,/^  token:/s|^  token:.*|  token: $(cat /opt/meshtech-node/secrets/repeater.token)|" /etc/openhop_repeater/config.yaml'
```

Then restart openhop:

```
sudo systemctl restart openhop-repeater
```

### 6. See it working

```
sudo journalctl -u meshtech-node -n 50 --no-pager | grep -iE "repeater|auth accepted"
```

The proof is `auth accepted: 127.0.0.1:... as repeater` - openhop
coming through its own door. A healthy link then shows its config
proposal echoed with the same radio numbers we keep. If instead you
see `auth rejected`, the password in openhop's `modem_tcp:` block is
not the one in the secret file (step 5's lesson).

## IF IT GOES WRONG - close the door

Delete the `repeater_file` line from
`/opt/meshtech-node/modem.conf`, then:

```
cd ~/meshtech-node && sudo ./manage.sh restart
```

openhop's token then matches no door and it cannot get in (this is
the safe failure: no token file, no access, ever).

## MQTT INPUT (the coverage collector)

The node can LISTEN to a mesh broker and record which observer heard
which packet - packets heard by MORE THAN ONE observer are your
coverage map (where to place the next repeater). The node never
sends anything. OFF until you switch it on.

READ THE HONEST LABEL AT THE END OF THIS SECTION FIRST.

### Turn it on

Put the new code on the box (this also installs the MQTT library):

```
cd ~/meshtech-node && sudo ./manage.sh update
```

Open the config for editing:

```
sudo nano /opt/meshtech-node/config.json
```

Find the line with `"radio_hardware"` and paste this block DIRECTLY
UNDER it (the block's own comma at the end is on purpose):

```json
"mqtt": {
  "enabled": true,
  "host": "YOUR-BROKER-ADDRESS",
  "port": 1883,
  "topic": "meshcore/#",
  "regions": []
},
```

- `host` = the broker address (see the honest label).
- `regions` = leave empty to hear every region; or list the ones you
  want, for example `["SFO", "LAX"]`.

Save the file (Ctrl+O, Enter, Ctrl+X), then check it is still valid:

```
sudo /opt/meshtech-node/.venv/bin/python -c "import json;json.load(open('/opt/meshtech-node/config.json'));print('config.json is valid')"
```

If it says valid, restart:

```
cd ~/meshtech-node && sudo ./manage.sh restart
```

### See it working

```
cd ~/meshtech-node && sudo ./manage.sh logs
```

Look for `MQTT collector subscribed to meshcore/#` - that line means
it is listening. Then look at the coverage table:

```
sudo /opt/meshtech-node/.venv/bin/python -c "import sqlite3;db=sqlite3.connect('/opt/meshtech-node/data/scope.db');print('packets heard by 2+ observers:');[print(r) for r in db.execute('SELECT packet_hash, COUNT(*) FROM heard_by GROUP BY packet_hash HAVING COUNT(*)>1 ORDER BY 2 DESC LIMIT 20')]"
```

### Turn it off

In `config.json` change `"enabled": true` to `false`, save, then:

```
cd ~/meshtech-node && sudo ./manage.sh restart
```

### HONEST LABEL (read before picking a broker)

The four public brokers openhop publishes to (meshcore.ca,
meshmapper, waev, gomesh) do NOT take plain logins: they use secure
websockets plus an identity login (the Ed25519 token openhop mints).
The collector speaks plain MQTT today - against THOSE four the link
will honestly fail until I add that login path (small work, copying
openhop's proven recipe - just ask). Until then, point it at a plain
MQTT broker (one on your own network is the safe pick).


## CHANGE TO ETHERMESH (the radio on the network) - and back

Instead of the PiMesh HAT on this Pi, the radio can be the EtherMesh-1W
box on your network. One config change says which one is in charge.

### Switch to the EtherMesh box

Put the box's own token in a root-only file (paste the token from the
box's web page, then press Enter and Ctrl+D):

```
sudo sh -c 'umask 077; cat > /opt/meshtech-node/secrets/ethermesh.token'
```

Open the config for editing:

```
sudo nano /opt/meshtech-node/config.json
```

Find the `"radio_hardware"` line and change it to `"ethermesh"`,
then paste this block DIRECTLY UNDER it:

```json
"companion_host": "THE-BOX-IP",
"companion_port": 5055,
```

Also find the `"modem_token_file"` line (it already exists) and
change its path to:

```
/opt/meshtech-node/secrets/ethermesh.token
```

Save (Ctrl+O, Enter, Ctrl+X), check it is valid:

```
sudo /opt/meshtech-node/.venv/bin/python -c "import json;json.load(open('/opt/meshtech-node/config.json'));print('config.json is valid')"
```

Then restart:

```
cd ~/meshtech-node && sudo ./manage.sh restart
```

### See it working

```
cd ~/meshtech-node && sudo ./manage.sh logs
```

Look for `radio hardware: ethermesh (network modem at THE-BOX-IP:5055)`
and then `RADIO LINK UP`.

openhop points at the box too: in `/etc/openhop_repeater/config.yaml`
its `modem_tcp:` block's `host:` becomes THE-BOX-IP (was
127.0.0.1), then:

```
sudo systemctl restart openhop-repeater
```

### Back to the PiMesh HAT

In `config.json`: change `"radio_hardware"` back to `"pimesh"`, delete
the `companion_host` / `companion_port` lines you added, and change
`"modem_token_file"` back to its old path
`/opt/meshtech-node/secrets/modem.token`, save, then:

```
cd ~/meshtech-node && sudo ./manage.sh restart
```

### HONEST LABEL

The box's firmware behavior (two programs talking to it at once, its
door rules) is UNVERIFIED - bench-test it on the table before the lab
trusts it. The PiMesh path is the proven one.
