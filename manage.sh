#!/usr/bin/env bash
# meshtech-node manager - ONE entry point for the box operator.
#
#   sudo ./manage.sh              menu (whiptail when available,
#                                 plain fallback otherwise)
#   sudo ./manage.sh install      first-time install (venv, deps,
#                                 secrets, config, systemd unit)
#   sudo ./manage.sh configure    edit radio + feed settings (menu of
#                                 the values that matter)
#   sudo ./manage.sh passwords    rotate / regenerate the secrets
#   sudo ./manage.sh verify       #scope key fingerprint + radio params
#   sudo ./manage.sh start|stop|restart|status|logs
#   sudo ./manage.sh uninstall    remove the service + (ask) the files
#
# Secrets NEVER leave this machine: generated under secrets/ (mode
# 600), never committed. whiptail is optional; everything works in a
# plain terminal over ssh.
set -euo pipefail
cd "$(dirname "$0")"

SERVICE=meshtech-node
SECRETS=secrets
VENV=.venv
PY="$VENV/bin/python"
APPDIR=/opt/meshtech-node

# ---------------------------------------------------------------- ui --
HAVE_WHIP=0
command -v whiptail >/dev/null 2>&1 && HAVE_WHIP=1

# msg <title> <text>            (info box / plain echo)
msg() {
  local title="$1" text="$2"
  if [[ $HAVE_WHIP -eq 1 ]]; then
    whiptail --title "$title" --msgbox "$text" 20 78
  else
    echo "== $title =="; echo "$text"
  fi
}

# menu <title> <options...>     -> prints the chosen TAG on stdout
menu() {
  local title="$1"; shift
  if [[ $HAVE_WHIP -eq 1 ]]; then
    whiptail --title "$title" --menu "Choose:" 24 78 16 "$@" \
      3>&1 1>&2 2>&3
  else
    local i=1 tags=() labels=()
    while [[ $# -ge 2 ]]; do tags+=("$1"); labels+=("$2"); shift 2; done
    for i in "${!tags[@]}"; do
      printf "  %2d) %-12s %s\n" $((i+1)) "${tags[$i]}" "${labels[$i]}"
    done
    read -rp "# " n
    echo "${tags[$((n-1))]}"
  fi
}

# inputbox <title> <current>    -> prints entered value
inputbox() {
  local title="$1" cur="$2"
  if [[ $HAVE_WHIP -eq 1 ]]; then
    whiptail --title "$title" --inputbox "$title (current: $cur)" \
      12 70 "$cur" 3>&1 1>&2 2>&3
  else
    read -rp "$title [$cur]: " v; echo "${v:-$cur}"
  fi
}

# yesno <title>                 -> 0 yes / 1 no
yesno() {
  if [[ $HAVE_WHIP -eq 1 ]]; then
    whiptail --title "$1" --yesno "$1?" 10 60
  else
    read -rp "$1? [y/N] " a; [[ $a == y || $a == Y ]]
  fi
}

need_root() {
  if [[ $EUID -ne 0 ]]; then
    echo "this command needs sudo (re-run: sudo ./manage.sh $1)"; exit 1
  fi
}

require_installed() {
  if [[ ! -x "$PY" ]]; then
    msg "Not installed" "Run 'sudo ./manage.sh install' first."
    exit 1
  fi
}

# set_conf <key> <value> <file>   (idempotent key = value writer)
set_conf() {
  local key="$1" val="$2" file="$3"
  if grep -qE "^${key} *=" "$file"; then
    sed -i "s|^${key} *=.*|${key} = ${val}|" "$file"
  else
    echo "${key} = ${val}" >> "$file"
  fi
}

# ------------------------------------------------------------- actions --

do_install() {
  need_root install
  echo "== meshtech-node install =="
  test -d "$VENV" || python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet -e . aiohttp pycryptodome
  "$PY" -c "import spidev" 2>/dev/null \
    || "$VENV/bin/pip" install --quiet spidev \
    || echo "WARNING: spidev unavailable - radio will not start"
  "$PY" -c "import gpiod" 2>/dev/null \
    || "$VENV/bin/pip" install --quiet gpiod \
    || echo "WARNING: gpiod unavailable - radio will not start"
  mkdir -p "$SECRETS" && chmod 700 "$SECRETS"
  if [[ -f "$SECRETS/modem.token" ]]; then
    echo "secrets/modem.token already exists - keeping it"
  else
    do_passwords quiet
  fi
  cp -n deploy/config.json config.json 2>/dev/null || true
  cp -n deploy/modem.conf  modem.conf  2>/dev/null || true
  cp deploy/meshtech-node.service /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable "$SERVICE" >/dev/null
  msg "Install complete" "Next:\n  1. sudo ./manage.sh configure  (radio settings - VERIFY against the old repeater config)\n  2. sudo ./manage.sh verify      (key fingerprint check)\n  3. sudo ./manage.sh start"
}

do_passwords() {
  need_root passwords
  local mode="${1:-menu}"
  mkdir -p "$SECRETS" && chmod 700 "$SECRETS"
  local TOKEN
  TOKEN=$("$PY" -c "import secrets; print(secrets.token_urlsafe(24))")
  umask 077; printf '%s\n' "$TOKEN" > "$SECRETS/modem.token"
  chmod 600 "$SECRETS/modem.token"
  if [[ $mode == quiet ]]; then
    echo "====================================================="
    echo "Modem token (generated once, stored mode-600):"
    echo "  $TOKEN"
    echo "Save this where you keep passwords. It is NOT in git."
    echo "====================================================="
  else
    msg "Password rotated" "New modem token generated and stored in\n$SECRETS/modem.token (mode 600):\n\n$TOKEN\n\nSave it now - it is NOT in git.\nRestart the service to apply: sudo ./manage.sh restart"
  fi
}

do_configure() {
  need_root configure
  require_installed
  [[ -f modem.conf ]] || cp deploy/modem.conf modem.conf
  [[ -f config.json ]] || cp deploy/config.json config.json
  while true; do
    local cur
    cur=$(grep -E "^frequency_hz *=" modem.conf | awk '{print $3}')
    local pick
    pick=$(menu "configure meshtech-node" \
      "frequency"  "radio frequency Hz        (cur: ${cur:-?})" \
      "sf"         "spreading factor           (cur: $(grep -E '^spreading_factor *=' modem.conf | awk '{print $3}'))" \
      "bandwidth"  "bandwidth Hz               (cur: $(grep -E '^bandwidth_hz *=' modem.conf | awk '{print $3}'))" \
      "syncword"   "sync word (hex)            (cur: $(grep -E '^sync_word *=' modem.conf | awk '{print $3}'))" \
      "preamble"   "preamble length            (cur: $(grep -E '^preamble_length *=' modem.conf | awk '{print $3}'))" \
      "power"      "TX power dBm (UNUSED while TX off)" \
      "webserve"   "web app port               (cur: $(grep -oE '"port": [0-9]+' config.json | grep -oE '[0-9]+'))" \
      "back"       "save nothing and go back")
    case "$pick" in
      frequency) set_conf frequency_hz "$(inputbox "frequency_hz" "$cur")" modem.conf ;;
      sf)        set_conf spreading_factor "$(inputbox "spreading_factor (5-12)" "$(grep -E '^spreading_factor *=' modem.conf | awk '{print $3}')")" modem.conf ;;
      bandwidth) set_conf bandwidth_hz "$(inputbox "bandwidth_hz (62500 = 62.5 kHz)" "$(grep -E '^bandwidth_hz *=' modem.conf | awk '{print $3}')")" modem.conf ;;
      syncword)  set_conf sync_word "$(inputbox "sync_word (hex, e.g. 0x12)" "$(grep -E '^sync_word *=' modem.conf | awk '{print $3}')")" modem.conf ;;
      preamble)  set_conf preamble_length "$(inputbox "preamble_length" "$(grep -E '^preamble_length *=' modem.conf | awk '{print $3}')")" modem.conf ;;
      power)     set_conf tx_power_dbm "$(inputbox "tx_power_dbm" "$(grep -E '^tx_power_dbm *=' modem.conf | awk '{print $3}')")" modem.conf ;;
      webserve)
        local port
        port=$(inputbox "web app port" "$(grep -oE '"port": [0-9]+' config.json | grep -oE '[0-9]+')")
        sed -i "s|\"port\": [0-9]*,|\"port\": ${port},|" config.json ;;
      back) return ;;
    esac
  done
}

do_verify() {
  require_installed
  echo "== key fingerprint (what the node derives) =="
  PYTHONPATH=src:"$PWD" "$PY" - <<'PYEOF'
from meshtech_node.packets import derive_channel_keys
name = "#scope"
ch, aes, hmac_key = derive_channel_keys(name)
print(f"channel      : {name} (hashtag rule: sha256('{name}')[:16])")
print(f"channel hash : 0x{ch:02x}   <- desk radio must show the same")
print(f"aes key      : {aes.hex()}")
print(f"hmac key     : {hmac_key.hex()}")
PYEOF
  echo
  echo "== radio parameters (from modem.conf) =="
  grep -E "^(frequency_hz|spreading_factor|coding_rate|bandwidth_hz|sync_word|preamble_length|tx_power_dbm)" modem.conf \
    || echo "modem.conf not found - run sudo ./manage.sh install first"
  echo
  echo "Compare: desk radio's #scope channel in the meshcore app"
  echo "(secret must MATCH #scope) and the old repeater's"
  echo "/etc/openhop_repeater/config.yaml radio block."
}

do_uninstall() {
  need_root uninstall
  systemctl stop "$SERVICE" 2>/dev/null || true
  systemctl disable "$SERVICE" 2>/dev/null || true
  rm -f /etc/systemd/system/${SERVICE}.service
  systemctl daemon-reload
  echo "service removed."
  if yesno "Also delete the software in this folder (venv, configs, SECRETS)?"; then
    rm -rf "$VENV" "$SECRETS" config.json modem.conf
    echo "software removed. The repo files themselves remain."
  else
    echo "kept everything in this folder (secrets included)."
  fi
  echo "NOTE: the openhop repeater is NOT touched - restore it with:"
  echo "  sudo systemctl enable --now openhop-repeater"
}

do_menu() {
  while true; do
    local pick
    pick=$(menu "meshtech-node manager" \
      "install"    "first-time install (venv, deps, secrets, service)" \
      "configure"  "radio + feed settings (frequency, SF, ...)" \
      "verify"     "#scope key fingerprint + radio params check" \
      "passwords"  "rotate the modem token" \
      "start"      "start the service" \
      "stop"       "stop the service" \
      "restart"    "restart the service" \
      "status"     "is it running + recent log" \
      "logs"       "follow the live log" \
      "bench"      "run without the radio (foreground, TX impossible)" \
      "uninstall"  "remove the service (and optionally the files)")
    case "$pick" in
      install)   do_install ;;
      configure) do_configure ;;
      verify)    do_verify; [[ $HAVE_WHIP -eq 1 ]] && read -rp "Enter to continue..." _ ;;
      passwords) do_passwords ;;
      start)     need_root start; systemctl start "$SERVICE"; sleep 1; systemctl --no-pager --lines 5 status "$SERVICE" || true ;;
      stop)      need_root stop; systemctl stop "$SERVICE"; echo "stopped" ;;
      restart)   need_root restart; systemctl restart "$SERVICE"; sleep 1; systemctl --no-pager --lines 5 status "$SERVICE" || true ;;
      status)    systemctl --no-pager --lines 15 status "$SERVICE" || true ;;
      logs)      journalctl -u "$SERVICE" -f --no-pager ;;
      bench)     test -f config.json || cp deploy/config.json config.json; PYTHONPATH=src:"$PWD" "$PY" -m meshtech_node --config config.json --bench-no-radio ;;
      uninstall) do_uninstall; return ;;
    esac
  done
}

cmd="${1:-menu}"
case "$cmd" in
  menu)      do_menu ;;
  install)   do_install ;;
  configure) do_configure ;;
  passwords) do_passwords ;;
  verify)    do_verify ;;
  uninstall) do_uninstall ;;
  start)     need_root start; systemctl start "$SERVICE"; sleep 1; systemctl --no-pager --lines 5 status "$SERVICE" || true ;;
  stop)      need_root stop; systemctl stop "$SERVICE"; echo "stopped" ;;
  restart)   need_root restart; systemctl restart "$SERVICE"; sleep 1; systemctl --no-pager --lines 5 status "$SERVICE" || true ;;
  status)    systemctl --no-pager --lines 15 status "$SERVICE" || true ;;
  logs)      journalctl -u "$SERVICE" -f --no-pager ;;
  bench)     test -f config.json || cp deploy/config.json config.json; PYTHONPATH=src:"$PWD" "$PY" -m meshtech_node --config config.json --bench-no-radio ;;
  *)         sed -n '2,30p' "$0" ;;
esac
