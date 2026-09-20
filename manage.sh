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
    # listing goes to stderr: $() captures ONLY the chosen tag
    for i in "${!tags[@]}"; do
      printf "  %2d) %-12s %s\n" $((i+1)) "${tags[$i]}" "${labels[$i]}" >&2
    done
    # accept a number, or the word quit/q; anything else re-asks
    while true; do
      read -rp "number (or quit): " n
      [[ "${n,,}" == q || "${n,,}" == quit ]] && { echo "quit"; return; }
      if [[ $n =~ ^[0-9]+$ ]] && (( n >= 1 && n <= ${#tags[@]} )); then
        echo "${tags[$((n-1))]}"; return
      fi
      echo "please type a number from 1 to ${#tags[@]}, or quit" >&2
    done
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
  if [[ ! -x "$APPDIR/.venv/bin/python" ]]; then
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
  local f="$APPDIR/modem.conf"
  [[ -f $f ]] || f=deploy/modem.conf
  echo
  echo "-- Radio settings -------------------------------------------------"
  echo "These set how the radio listens. The defaults are the standard"
  echo "US 915 MHz mesh band - press Enter to accept each one."
  echo
  set_conf frequency_hz      "$(ask_default "Frequency in Hz (915 MHz band)" "$(grep -E '^frequency_hz *=' $f | awk '{print $3}')")" "$f"
  set_conf spreading_factor  "$(ask_default "Spreading factor 5-12 (7 = fast + standard)" "$(grep -E '^spreading_factor *=' $f | awk '{print $3}')")" "$f"
  set_conf bandwidth_hz      "$(ask_default "Bandwidth in Hz (62500 = standard)" "$(grep -E '^bandwidth_hz *=' $f | awk '{print $3}')")" "$f"
  set_conf sync_word         "$(ask_default "Sync word in hex (0x12 = standard)" "$(grep -E '^sync_word *=' $f | awk '{print $3}')")" "$f"
  set_conf preamble_length   "$(ask_default "Preamble length (32 = standard)" "$(grep -E '^preamble_length *=' $f | awk '{print $3}')")" "$f"
  echo
  echo "-- Radio board (the hardware the radio chip sits on) ----------------"
  echo "The pin wiring differs per board. Enter accepts the standard one."
  echo "  1) pimesh-1w-v2   - the 1-watt PiMesh board v2 (standard)"
  echo "  2) pimesh-v2-draft- a PiMesh v2 with external LNA + RF switch"
  local prof
  while true; do
    read -rp "Board [1]: " b
    case "${b:-1}" in
      1) prof=pimesh-1w-v2 ;;
      2) prof=pimesh-v2-draft ;;
      *) echo "please type 1 or 2"; continue ;;
    esac
    break
  done
  set_conf pin_profile "$prof" "$f"
  echo
  echo "Radio settings saved to $f."
}

# sync_to_appdir - copy the program source into $APPDIR (the run home).
# Preserves the installed venv, secrets, and live configs across runs.
sync_to_appdir() {
  mkdir -p "$APPDIR"
  rsync -a --delete \
    --exclude '.venv' --exclude 'secrets' --exclude '.git' \
    --exclude 'modem.conf' --exclude 'config.json' \
    src app cleanmodem deploy tests pyproject.toml "$APPDIR"/
}

do_install() {
  need_root install
  echo "== meshtech-node install =="
  echo "This copies the program to $APPDIR, asks you a few questions,"
  echo "and can start the service at the end. Nothing is put on the air"
  echo "by this install: transmit stays off."
  echo
  echo "- copying program to $APPDIR ..."
  sync_to_appdir
  echo "- installing python dependencies (this can take a few minutes)..."
  [[ -d "$APPDIR/.venv" ]] || python3 -m venv "$APPDIR/.venv"
  "$APPDIR/.venv/bin/pip" install --quiet --upgrade pip
  "$APPDIR/.venv/bin/pip" install --quiet -e "$APPDIR" aiohttp pycryptodome
  "$APPDIR/.venv/bin/python" -c "import spidev" 2>/dev/null \
    || "$APPDIR/.venv/bin/pip" install --quiet spidev \
    || echo "WARNING: spidev unavailable - radio will not start"
  "$APPDIR/.venv/bin/python" -c "import gpiod" 2>/dev/null \
    || "$APPDIR/.venv/bin/pip" install --quiet gpiod \
    || echo "WARNING: gpiod unavailable - radio will not start"
  mkdir -p "$APPDIR/secrets" && chmod 700 "$APPDIR/secrets"
  if [[ -f "$APPDIR/secrets/modem.token" ]]; then
    echo "- internal radio secret already exists - keeping it"
  else
    umask 077
    python3 -c 'import secrets; print(secrets.token_urlsafe(24))' \
      > "$APPDIR/secrets/modem.token"
    chmod 600 "$APPDIR/secrets/modem.token"
    umask 022
    echo "- internal radio secret generated (stored root-only; the user"
    echo "  never needs it - rotate any time with: sudo ./manage.sh passwords)"
  fi
  [[ -f "$APPDIR/modem.conf" ]] || cp deploy/modem.conf "$APPDIR/modem.conf"
  [[ -f "$APPDIR/config.json" ]] || cp deploy/config.json "$APPDIR/config.json"
  guided_radio_questions
  sed -i "s|^token_file *=.*|token_file = $APPDIR/secrets/modem.token|; s|^controller_file *=.*|controller_file = $APPDIR/secrets/modem.token|" "$APPDIR/modem.conf"
  sed -i "s|\"modem_conf\": *\"[^\"]*\"|\"modem_conf\": \"$APPDIR/modem.conf\"|; s|\"modem_token_file\": *\"[^\"]*\"|\"modem_token_file\": \"$APPDIR/secrets/modem.token\"|; s|\"static_dir\": *\"[^\"]*\"|\"static_dir\": \"$APPDIR/app\"|" "$APPDIR/config.json"
  echo "- writing the service file (runs from $APPDIR)"
  sed "s|@APPDIR@|$APPDIR|g" deploy/meshtech-node.service > /etc/systemd/system/${SERVICE}.service
  systemctl daemon-reload
  systemctl enable "$SERVICE" >/dev/null
  echo "- service registered (starts on boot when you start it)"
  echo
  echo "-- Channel key check ----------------------------------------------"
  do_verify
  echo "Compare the channel hash with the one your handheld radio shows"
  echo "for its #scope channel. They must match or the radios will not"
  echo "understand each other."
  echo
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
  echo "Install complete. The program runs from $APPDIR;"
  echo "this folder stays as your git source (updates: git pull, then"
  echo "sudo ./manage.sh install again, then restart)."
}

do_passwords() {
  need_root passwords
  mkdir -p "$APPDIR/secrets" && chmod 700 "$APPDIR/secrets"
  umask 077
  python3 -c 'import secrets; print(secrets.token_urlsafe(24))' \
    > "$APPDIR/secrets/modem.token"
  chmod 600 "$APPDIR/secrets/modem.token"
  umask 022
  echo "Internal radio secret rotated ($APPDIR/secrets/modem.token, mode 600)."
  echo
  echo "If the service is running, restart it to use the new password:"
  echo "  sudo ./manage.sh restart"
}

do_configure() {
  need_root configure
  require_installed
  local MC="$APPDIR/modem.conf" CJ="$APPDIR/config.json"
  while true; do
    local cur
    cur=$(grep -E "^frequency_hz *=" "$MC" | awk '{print $3}')
    local pick
    pick=$(menu "configure meshtech-node" \
      "frequency"  "radio frequency Hz        (cur: ${cur:-?})" \
      "sf"         "spreading factor           (cur: $(grep -E '^spreading_factor *=' "$MC" | awk '{print $3}'))" \
      "bandwidth"  "bandwidth Hz               (cur: $(grep -E '^bandwidth_hz *=' "$MC" | awk '{print $3}'))" \
      "syncword"   "sync word (hex)            (cur: $(grep -E '^sync_word *=' "$MC" | awk '{print $3}'))" \
      "preamble"   "preamble length            (cur: $(grep -E '^preamble_length *=' "$MC" | awk '{print $3}'))" \
      "power"      "TX power dBm (UNUSED while TX off)" \
      "webserve"   "web app port               (cur: $(grep -oE '"port": [0-9]+' "$CJ" | grep -oE '[0-9]+'))" \
      "back"       "save nothing and go back")
    case "$pick" in
      frequency) set_conf frequency_hz "$(inputbox "frequency_hz" "$cur")" "$MC" ;;
      sf)        set_conf spreading_factor "$(inputbox "spreading_factor (5-12)" "$(grep -E '^spreading_factor *=' "$MC" | awk '{print $3}')")" "$MC" ;;
      bandwidth) set_conf bandwidth_hz "$(inputbox "bandwidth_hz (62500 = 62.5 kHz)" "$(grep -E '^bandwidth_hz *=' "$MC" | awk '{print $3}')")" "$MC" ;;
      syncword)  set_conf sync_word "$(inputbox "sync_word (hex, e.g. 0x12)" "$(grep -E '^sync_word *=' "$MC" | awk '{print $3}')")" "$MC" ;;
      preamble)  set_conf preamble_length "$(inputbox "preamble_length" "$(grep -E '^preamble_length *=' "$MC" | awk '{print $3}')")" "$MC" ;;
      power)     set_conf tx_power_dbm "$(inputbox "tx_power_dbm" "$(grep -E '^tx_power_dbm *=' "$MC" | awk '{print $3}')")" "$MC" ;;
      webserve)
        local port
        port=$(inputbox "web app port" "$(grep -oE '"port": [0-9]+' "$CJ" | grep -oE '[0-9]+')")
        sed -i "s|\"port\": [0-9]*,|\"port\": ${port},|" "$CJ" ;;
      back) return ;;
    esac
  done
}

do_verify() {
  require_installed
  echo "== channel key check (what this machine derives) =="
  PYTHONPATH="$APPDIR/src":"$APPDIR" "$APPDIR/.venv/bin/python" - <<'PYEOF'
from meshtech_node.packets import derive_channel_keys
name = "#scope"
ch, aes, hmac_key = derive_channel_keys(name)
print(f"channel      : {name} (hashtag rule: sha256('{name}')[:16])")
print(f"channel hash : 0x{ch:02x}   <- your handheld must show the same")
print(f"aes key      : {aes.hex()}")
print(f"hmac key     : {hmac_key.hex()}")
PYEOF
  echo
  echo "== radio settings in use ($APPDIR/modem.conf) =="
  grep -E "^(frequency_hz|spreading_factor|coding_rate|bandwidth_hz|sync_word|preamble_length|pin_profile)" "$APPDIR/modem.conf" \
    || echo "modem.conf not found - run sudo ./manage.sh install first"
}

do_uninstall() {
  need_root uninstall
  systemctl stop "$SERVICE" 2>/dev/null || true
  systemctl disable "$SERVICE" 2>/dev/null || true
  rm -f /etc/systemd/system/${SERVICE}.service
  systemctl daemon-reload
  echo "service removed."
  if yesno "Also delete the program in $APPDIR (venv, configs, SECRETS)?"; then
    rm -rf "$APPDIR"
    echo "$APPDIR removed. This git clone remains as the source."
  else
    echo "kept $APPDIR (secrets included)."
  fi
  echo "NOTE: the openhop repeater is NOT touched - restore it with:"
  echo "  sudo systemctl enable --now openhop-repeater"
}

# pause <title> - keep output readable in menu mode: wait for Enter
pause() {
  [[ $HAVE_WHIP -eq 1 ]] && read -rp "Press Enter to return to the menu..." _ || true
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
      "uninstall"  "remove the service (and optionally the files)" \
      "quit"       "leave the manager")
    case "$pick" in
      install)   do_install ;;
      configure) do_configure ;;
      verify)    do_verify; [[ $HAVE_WHIP -eq 1 ]] && read -rp "Enter to continue..." _ ;;
      passwords) do_passwords ;;
      start)     need_root start; systemctl start "$SERVICE"; sleep 1; systemctl --no-pager --lines 5 status "$SERVICE" || true; pause ;;
      stop)      need_root stop; systemctl stop "$SERVICE"; echo "stopped"; pause ;;
      restart)   need_root restart; systemctl restart "$SERVICE"; sleep 1; systemctl --no-pager --lines 5 status "$SERVICE" || true; pause ;;
      status)    systemctl --no-pager --lines 15 status "$SERVICE" || true; pause ;;
      logs)      journalctl -u "$SERVICE" -f --no-pager; pause ;;
      bench)     require_installed; PYTHONPATH="$APPDIR/src":"$APPDIR" "$APPDIR/.venv/bin/python" -m meshtech_node --config "$APPDIR/config.json" --bench-no-radio; pause ;;
      uninstall) do_uninstall; return ;;
      quit)      echo "bye"; return ;;
    esac
  done
}

cmd="${1:-menu}"
case "$cmd" in
  menu)      do_menu ;;
  install)   do_install ;;
  configure) do_configure ;;
  passwords) do_passwords ;;
  verify)    do_verify; pause ;;
  uninstall) do_uninstall ;;
  start)     need_root start; systemctl start "$SERVICE"; sleep 1; systemctl --no-pager --lines 5 status "$SERVICE" || true ;;
  stop)      need_root stop; systemctl stop "$SERVICE"; echo "stopped" ;;
  restart)   need_root restart; systemctl restart "$SERVICE"; sleep 1; systemctl --no-pager --lines 5 status "$SERVICE" || true ;;
  status)    systemctl --no-pager --lines 15 status "$SERVICE" || true ;;
  logs)      journalctl -u "$SERVICE" -f --no-pager ;;
  bench)     require_installed; PYTHONPATH="$APPDIR/src":"$APPDIR" "$APPDIR/.venv/bin/python" -m meshtech_node --config "$APPDIR/config.json" --bench-no-radio ;;
  *)         sed -n '2,30p' "$0" ;;
esac
