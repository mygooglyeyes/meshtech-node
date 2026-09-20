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

# ask_default <question> <default> -> the answer (Enter = default)
ask_default() {
  read -rp "$1 [$2]: " a
  echo "${a:-$2}"
}

# guided radio questions -> modem.conf. Fully self-contained: the
# defaults ARE the shipped US-band settings; nothing is read from any
# other software on the box.
guided_radio_questions() {
  local f=deploy/modem.conf
  echo
  echo "-- Radio settings -------------------------------------------------"
  echo "These set how the radio listens. The defaults are the standard"
  echo "US 915 MHz mesh band - press Enter to accept each one."
  echo
  set_conf frequency_hz      "$(ask_default "Frequency in Hz (915 MHz band)" "$(grep -E '^frequency_hz *=' $f | awk '{print $3}')")" modem.conf
  set_conf spreading_factor  "$(ask_default "Spreading factor 5-12 (7 = fast + standard)" "$(grep -E '^spreading_factor *=' $f | awk '{print $3}')")" modem.conf
  set_conf bandwidth_hz      "$(ask_default "Bandwidth in Hz (62500 = standard)" "$(grep -E '^bandwidth_hz *=' $f | awk '{print $3}')")" modem.conf
  set_conf sync_word         "$(ask_default "Sync word in hex (0x12 = standard)" "$(grep -E '^sync_word *=' $f | awk '{print $3}')")" modem.conf
  set_conf preamble_length   "$(ask_default "Preamble length (32 = standard)" "$(grep -E '^preamble_length *=' $f | awk '{print $3}')")" modem.conf
  echo
  echo "Radio settings saved to modem.conf."
}

# show the modem password once and WAIT until the user confirms saved
password_gate() {
  echo
  echo "==================================================================="
  echo "  MODEM PASSWORD - shown this one time only"
  echo
  echo "  $1"
  echo
  echo "  Save it in your password manager NOW. It is stored on this"
  echo "  machine in secrets/modem.token and never shown again."
  echo "==================================================================="
  while true; do
    read -rp "Type saved and press Enter to continue: " a
    [[ "${a,,}" == saved ]] && break
    echo "please type: saved"
  done
}

do_install() {
  need_root install
  echo "== meshtech-node install =="
  echo "This installs the program, asks you a few questions, and can"
  echo "start the service at the end. Nothing is put on the air by this"
  echo "install: transmit stays off."
  echo
  test -d "$VENV" || python3 -m venv "$VENV"
  echo "- installing python dependencies (this can take a few minutes)..."
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet -e . aiohttp pycryptodome
  "$PY" -c "import spidev" 2>/dev/null \
    || "$VENV/bin/pip" install --quiet spidev \
    || echo "WARNING: spidev unavailable - radio will not start"
  "$PY" -c "import gpiod" 2>/dev/null \
    || "$VENV/bin/pip" install --quiet gpiod \
    || echo "WARNING: gpiod unavailable - radio will not start"
  cp -n deploy/config.json config.json 2>/dev/null || true
  cp -n deploy/modem.conf  modem.conf  2>/dev/null || true
  # password: generate once, show once, gate on confirmation
  if [[ -f "$SECRETS/modem.token" ]]; then
    echo "- modem password already exists (secrets/modem.token) - keeping it"
  else
    mkdir -p "$SECRETS" && chmod 700 "$SECRETS"
    local TOKEN
    TOKEN=$("$PY" -c "import secrets; print(secrets.token_urlsafe(24))")
    umask 077; printf '%s\n' "$TOKEN" > "$SECRETS/modem.token"
    chmod 600 "$SECRETS/modem.token"
    password_gate "$TOKEN"
  fi
  # guided configuration (self-contained)
  guided_radio_questions
  # systemd registration
  cp deploy/meshtech-node.service /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable "$SERVICE" >/dev/null
  echo "- service registered (starts on boot when you start it)"
  # key check, in context
  echo
  echo "-- Channel key check ----------------------------------------------"
  do_verify
  echo "Compare the channel hash with the one your handheld radio shows"
  echo "for its #scope channel. They must match or the radios will not"
  echo "understand each other."
  echo
  # start now? one question, real words
  if yesno "Start the meshtech-node service now"; then
    systemctl start "$SERVICE"
    sleep 2
    systemctl --no-pager --lines 10 status "$SERVICE" || true
    echo
    echo "Watch it live any time with:  sudo ./manage.sh logs"
  else
    echo "Not started. When you are ready:  sudo ./manage.sh start"
  fi
  echo
  echo "Install complete."
}

do_passwords() {
  need_root passwords
  mkdir -p "$SECRETS" && chmod 700 "$SECRETS"
  local TOKEN
  TOKEN=$("$PY" -c "import secrets; print(secrets.token_urlsafe(24))")
  umask 077; printf '%s\n' "$TOKEN" > "$SECRETS/modem.token"
  chmod 600 "$SECRETS/modem.token"
  echo "New modem password generated (secrets/modem.token, mode 600)."
  password_gate "$TOKEN"
  echo
  echo "If the service is running, restart it to use the new password:"
  echo "  sudo ./manage.sh restart"
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
  echo "== channel key check (what this machine derives) =="
  PYTHONPATH=src:"$PWD" "$PY" - <<'PYEOF'
from meshtech_node.packets import derive_channel_keys
name = "#scope"
ch, aes, hmac_key = derive_channel_keys(name)
print(f"channel      : {name} (hashtag rule: sha256('{name}')[:16])")
print(f"channel hash : 0x{ch:02x}   <- your handheld must show the same")
print(f"aes key      : {aes.hex()}")
print(f"hmac key     : {hmac_key.hex()}")
PYEOF
  echo
  echo "== radio settings in use (modem.conf) =="
  grep -E "^(frequency_hz|spreading_factor|coding_rate|bandwidth_hz|sync_word|preamble_length)" modem.conf \
    || echo "modem.conf not found - run sudo ./manage.sh install first"
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
