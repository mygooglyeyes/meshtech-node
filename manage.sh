#!/usr/bin/env bash
# meshtech-node manager - ONE entry point for the box operator.
#
#   ./manage.sh setup      first-time install (venv, deps, secrets,
#                          config, systemd unit)  - needs sudo
#   ./manage.sh start      systemctl start meshtech-node
#   ./manage.sh stop       systemctl stop meshtech-node
#   ./manage.sh restart    systemctl restart meshtech-node
#   ./manage.sh status     is it running + last log lines
#   ./manage.sh logs       follow the log (journalctl -f)
#   ./manage.sh verify     print the #scope key fingerprint the node
#                          derives + the radio parameters it will use
#                          (compare against your desk radio / app)
#   ./manage.sh bench      run WITHOUT the radio in the foreground
#                          (feed + web app only, TX impossible)
#
# Secrets NEVER leave this machine: `setup` generates the modem token
# under secrets/ (mode 600) and prints it ONCE - save it where you
# keep passwords. Nothing secret is committed to git.
set -euo pipefail
cd "$(dirname "$0")"

INSTALL_DIR=/opt/meshtech-node
SERVICE=meshtech-node
SECRETS=secrets
VENV=.venv
PY="$VENV/bin/python"

need_root() {
  if [[ $EUID -ne 0 ]]; then
    echo "this command needs sudo (re-run: sudo ./manage.sh $1)"; exit 1
  fi
}

cmd="${1:-help}"
case "$cmd" in
  setup)
    need_root setup
    echo "== meshtech-node setup =="
    # 1. venv + dependencies (aiohttp, pycryptodome; the radio libs
    #    spidev/gpiod come from the box's python3-spidev/gpiod packages
    #    or pip - we try pip and warn if the radio libs are missing).
    test -d "$VENV" || python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip
    "$VENV/bin/pip" install --quiet -e . aiohttp pycryptodome
    "$PY" -c "import spidev" 2>/dev/null \
      || "$VENV/bin/pip" install --quiet spidev \
      || echo "WARNING: spidev unavailable - radio will not start"
    "$PY" -c "import gpiod" 2>/dev/null \
      || "$VENV/bin/pip" install --quiet gpiod \
      || echo "WARNING: gpiod unavailable - radio will not start"
    # 2. secrets: modem token (generated once, mode 600, printed ONCE)
    mkdir -p "$SECRETS" && chmod 700 "$SECRETS"
    if [[ -f "$SECRETS/modem.token" ]]; then
      echo "secrets/modem.token already exists - keeping it"
    else
      TOKEN=$("$PY" -c "import secrets; print(secrets.token_urlsafe(24))")
      umask 077; printf '%s\n' "$TOKEN" > "$SECRETS/modem.token"
      echo "====================================================="
      echo "Modem token (generated once, stored mode-600):"
      echo "  $TOKEN"
      echo "Save this where you keep passwords. It is NOT in git."
      echo "====================================================="
    fi
    # 3. real config from the committed template (this folder IS the
    #    install dir on the box; paths inside point at $INSTALL_DIR)
    cp -n deploy/config.json config.json 2>/dev/null || true
    cp -n deploy/modem.conf  modem.conf  2>/dev/null || true
    # 4. systemd unit + enable (root service: it owns the radio pins)
    cp deploy/meshtech-node.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable "$SERVICE" >/dev/null
    echo "setup complete. Next:"
    echo "  1. EDIT modem.conf: radio settings must match the old"
    echo "     repeater's radio block line by line (never assume)."
    echo "  2. ./manage.sh verify    (check the key + radio params)"
    echo "  3. ./manage.sh start"
    ;;

  start)
    need_root start; systemctl start "$SERVICE"; sleep 1
    systemctl --no-pager --lines 5 status "$SERVICE" || true
    ;;
  stop)
    need_root stop; systemctl stop "$SERVICE"; echo "stopped"
    ;;
  restart)
    need_root restart; systemctl restart "$SERVICE"; sleep 1
    systemctl --no-pager --lines 5 status "$SERVICE" || true
    ;;
  status)
    systemctl --no-pager --lines 15 status "$SERVICE" || true
    ;;
  logs)
    journalctl -u "$SERVICE" -f --no-pager
    ;;

  verify)
    echo "== key fingerprint (what the node derives) =="
    "$PY" - <<'PYEOF'
import sys
sys.path.insert(0, "src")
import hashlib
from meshtech_node.packets import derive_channel_keys
name = "#scope"
ch, aes, hmac_key = derive_channel_keys(name)
print(f"channel      : {name} (hashtag rule: sha256('{name}')[:16])")
print(f"channel hash : 0x{ch:02x}   <- desk radio must show the same")
print(f"aes key      : {aes.hex()}  (secret's first 16 bytes, zero-padded)")
print(f"hmac key     : {hmac_key.hex()}")
PYEOF
    echo
    echo "== radio parameters (from modem.conf) =="
    grep -E "^(frequency_hz|spreading_factor|coding_rate|bandwidth_hz|sync_word|preamble_length|tx_power_dbm)" modem.conf \
      || echo "modem.conf not found - run sudo ./manage.sh setup first"
    echo
    echo "Compare against: your desk radio's #scope channel info in the"
    echo "meshcore app (secret must MATCH #scope), and the old repeater's"
    echo "/etc/openhop_repeater/config.yaml radio block."
    ;;

  bench)
    echo "== BENCH mode: NO radio, feed + web app only, TX impossible =="
    test -f config.json || cp deploy/config.json config.json
    PYTHONPATH=src:"$PWD" "$PY" -m meshtech_node \
      --config config.json --bench-no-radio
    ;;

  *)
    sed -n '2,25p' "$0"
    ;;
esac
