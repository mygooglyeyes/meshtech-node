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
  # sync_word (0x12) and preamble (32) are NOT asked: they are fixed
  # mesh standards here and stay at their shipped values.
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

# guided_web_question - the port the web app is served on. The one
# chance to pick it at install; Enter keeps the current value (8710
# on a first install). Re-runs default to what was chosen before.
guided_web_question() {
  local cj="$APPDIR/config.json"
  local cur port
  cur="$(grep -oE '"port": *[0-9]+' "$cj" | grep -oE '[0-9]+' | head -1)"
  while true; do
    read -rp "Web app port (the browser page; Enter = $cur): " port
    port="${port:-$cur}"
    case "$port" in
      ''|*[!0-9]*) echo "please type a number (1-65535)"; continue ;;
    esac
    if (( port < 1 || port > 65535 )); then
      echo "please type a number (1-65535)"; continue
    fi
    break
  done
  sed -i "s|\"port\": *[0-9]*|\"port\": $port|" "$cj"
  echo "Web app will be served on port $port."
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
  # Migrate a pre-2026-09-20 modem.conf that used the LoRa notation
  # (coding_rate 5 = CR 4/5): cleanmodem's parser takes the INDEX
  # (1..4). 5 meant CR 4/5 then; write the index 1 that means the same.
  sed -i 's/^coding_rate *= *5/coding_rate = 1/' "$APPDIR/modem.conf"
  [[ -f "$APPDIR/config.json" ]] || cp deploy/config.json "$APPDIR/config.json"
  guided_radio_questions
  guided_web_question
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
      "txmode"     "radio TRANSMIT on/off      (cur: $(grep -oE '"tx_enabled": *[a-z]+' "$CJ" | grep -oE '[a-z]+$')) - RESTARTS the service" \
      "companions" "let other devices LISTEN to this radio (PC/phone simulator)" \
      "webserve"   "web app port               (cur: $(grep -oE '"port": [0-9]+' "$CJ" | grep -oE '[0-9]+'))" \
      "datadoor"   "feed DATA door for devices (PC/phone app) - open/close" \
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
      datadoor)
        # DATA DOOR (SELF-CONTAINED RULE, Brett 2026-09-21): display
        # devices (the PC/phone app) take FEED DATA over the network;
        # pages never leave this box. The door token is that data
        # link's password - same fail-closed posture as the radio door.
        local WT="$APPDIR/secrets/webserve.token"
        if [[ -f "$WT" ]]; then
          echo "Data door is OPEN to the network (token exists)."
          if yesno "CLOSE the data door (token deleted, loopback-only, restart)"; then
            need_root datadoor
            rm -f "$WT"
            sed -i 's|"host": "[^"]*"|"host": "127.0.0.1"|' "$CJ"
            systemctl restart "$SERVICE"
            sleep 2
            systemctl --no-pager --lines 5 status "$SERVICE" || true
            echo "Data door closed - the feed answers this box only."
            pause
          fi
        else
          echo "Opening the DATA DOOR: display devices on your network may"
          echo "take the feed (password required, TX unchanged). Pages never"
          echo "leave this box - data only."
          if yesno "Create the data-door password and open the door"; then
            need_root datadoor
            umask 077
            python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > "$WT"
            chmod 600 "$WT"
            umask 022
            sed -i 's|"host": "[^"]*"|"host": "0.0.0.0"|' "$CJ"
            sed -i "s|\"token_file\": \"\"|\"token_file\": \"$APPDIR/secrets/webserve.token\"|" "$CJ"
            systemctl restart "$SERVICE"
            sleep 2
            systemctl --no-pager --lines 5 status "$SERVICE" || true
            echo
            echo "DATA-DOOR PASSWORD - shown this one time only:"
            cat "$WT"
            echo
            local lanip
            lanip=$(hostname -I 2>/dev/null | awk '{print $1}')
            echo "On the display device (PC app, Direct mode):"
            echo "  - Host node address: ${lanip:-<this box IP>}"
            echo "  - Password: paste the one above (asked once, remembered)"
            pause
          fi
        fi ;;
      txmode)
        local cur_tx new_tx
        cur_tx=$(grep -oE '"tx_enabled": *[a-z]+' "$CJ" | grep -oE '[a-z]+$')
        if [[ "$cur_tx" == "true" ]]; then
          new_tx=false
          echo "Turning radio TRANSMIT **OFF** - the node becomes listen-only."
        else
          new_tx=true
          echo "Turning radio TRANSMIT **ON** - the node will put feed packets ON THE AIR."
          echo "The airtime budget and duty cap in config.json still apply."
        fi
        if yesno "Change tx_enabled to ${new_tx} and restart the service"; then
          sed -i "s|\"tx_enabled\": *[a-z]*|\"tx_enabled\": ${new_tx}|" "$CJ"
          grep -q '"tx_enabled"' "$CJ" || \
            sed -i "s|\"refresh_hourly_cap\": *[0-9]*|&,\n    \"tx_enabled\": ${new_tx}|" "$CJ"
          need_root txmode
          systemctl restart "$SERVICE"
          sleep 2
          systemctl --no-pager --lines 5 status "$SERVICE" || true
          echo
          echo "tx_enabled is now ${new_tx}."
          pause
        else
          echo "unchanged."
        fi ;;
      companions)
        # COMPANION DEVICES (Brett 2026-09-20, the phone-app
        # simulation): other machines on the LAN connect as OBSERVERS
        # (TX refused server-side - the cleanmodem role system IS the
        # mandate) and receive every frame the radio hears. The token
        # is the one secret a companion PC legitimately needs.
        local OT="$APPDIR/secrets/observer.token"
        if [[ -f "$OT" ]]; then
          echo "Companion access is ON (an observer token exists)."
          if yesno "REVOKE companion access (token deleted, LAN closed, restart)"; then
            need_root companions
            rm -f "$OT"
            sed -i 's|^host *=.*|host = 127.0.0.1|' "$MC"
            systemctl restart "$SERVICE"
            sleep 2
            systemctl --no-pager --lines 5 status "$SERVICE" || true
            echo
            echo "Companion access revoked - the radio server is loopback-only again."
            pause
          fi
        else
          echo "Turning ON companion access: other devices on your network may"
          echo "LISTEN to the radio feed (observer role - transmit is refused"
          echo "server-side). A one-time token is created; give it ONLY to"
          echo "devices you trust."
          if yesno "Create the observer token and open the radio to the LAN"; then
            need_root companions
            umask 077
            python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > "$OT"
            chmod 600 "$OT"
            umask 022
            sed -i 's|^host *=.*|host = 0.0.0.0|' "$MC"
            sed -i "s|^token_file *=.*|token_file = $APPDIR/secrets/observer.token|" "$MC"
            systemctl restart "$SERVICE"
            sleep 2
            systemctl --no-pager --lines 5 status "$SERVICE" || true
            echo
            echo "COMPANION TOKEN - shown this one time only:"
            cat "$OT"
            echo
            local lanip
            lanip=$(hostname -I 2>/dev/null | awk '{print $1}')
            echo "On the companion PC (Windows paths shown):"
            echo "  1. git clone https://github.com/mygooglyeyes/meshtech-node"
            echo "  2. cd meshtech-node && python -m venv .venv"
            echo "  3. .venv\\Scripts\\pip install -e . aiohttp pycryptodome"
            echo "  4. copy deploy\\config.companion.json config.json"
            echo "  5. edit config.json: companion_host = ${lanip:-<hilltop IP>}"
            echo "  6. put the token above in a file named companion.token"
            echo "  7. start it: deploy\\start-companion.cmd"
            echo "     -> the web app opens at http://127.0.0.1:8710/"
            pause
          fi
        fi ;;
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
  # Fail LOUDLY on a config the radio parser would reject (the
  # coding_rate 5-out-of-range crash, caught on the box 2026-09-20).
  if PYTHONPATH="$APPDIR/cleanmodem":"$APPDIR/src" "$APPDIR/.venv/bin/python" -c \
    "import sys; sys.path.insert(0, '$APPDIR/cleanmodem'); from cleanmodem.config import build_config, load_config; build_config(load_config('$APPDIR/modem.conf'))" 2>/tmp/mn-verify.err; then
    rm -f /tmp/mn-verify.err
    echo "config parses clean - the radio will accept it"
  else
    echo "CONFIG ERROR - the radio would REFUSE to start:"
    cat /tmp/mn-verify.err
    rm -f /tmp/mn-verify.err
  fi
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
