#!/bin/bash
# IRIV SmartHub CM5 - AP dashboard installer / updater. Same command installs or updates:
#   curl -fsSL https://raw.githubusercontent.com/dineshpalsingh1989/smarthub-cm5-dashboard/main/install.sh | sudo bash
# A failed board check asks "Skip this check?". Non-interactive: ... | sudo bash -s -- --force
set -euo pipefail
REPO=https://raw.githubusercontent.com/dineshpalsingh1989/smarthub-cm5-dashboard/main
DIR=/opt/smarthub

ok()   { echo "  [OK]   $*"; }
info() { echo "  [INFO] $*"; }
fail() { echo "  [FAIL] $*"; echo "Aborted - nothing was changed."; exit 1; }  # only used before step 3
FORCE=0; SKIPPED=0

ask_skip() {  # hardware/board check failed: let the user decide (or --force) instead of hard abort
  echo "  [FAIL] $*"
  local a=""
  if [ "$FORCE" = 1 ]; then
    a=y; echo "  [SKIP] --force given"
  elif (exec </dev/tty) 2>/dev/null; then  # works even when the script itself is piped from curl
    read -r -p "         Skip this check and install anyway? [y/N] " a </dev/tty || a=""
  fi
  [[ $a == [yY]* ]] || { echo "Aborted - nothing was changed. (To skip failed checks: add --force)"; exit 1; }
  SKIPPED=1
}

main() {  # wrapped so a half-downloaded script never runs
  [ "${1:-}" = "--force" ] && FORCE=1
  [ "$(id -u)" = 0 ] || { echo "Run with sudo:  curl -fsSL $REPO/install.sh | sudo bash"; exit 1; }

  echo "== 1/3 Checking device (nothing is changed yet) =="
  local model
  model=$(tr -d '\0' </proc/device-tree/model 2>/dev/null || true)
  [[ $model == *"Compute Module 5"* ]] && ok "$model" || ask_skip "Not a Compute Module 5 ($model)"
  # SmartHub has no ID EEPROM; its onboard USB 2.0 hub (QinHeng 1a86:8091) is the best board fingerprint
  lsusb -d 1a86:8091 >/dev/null 2>&1 && ok "SmartHub onboard USB hub (1a86:8091)" \
    || ask_skip "SmartHub USB hub not found - not an IRIV SmartHub CM5? (if it is: add 'dtoverlay=dwc2,dr_mode=host' under [cm5] in /boot/firmware/config.txt and reboot)"
  # software the dashboard cannot run without: never skippable
  command -v nmcli >/dev/null && systemctl is-active -q NetworkManager \
    && ok "NetworkManager running" || fail "NetworkManager not running (needs Raspberry Pi OS Bookworm or newer)"
  python3 -c 'import gpiozero' 2>/dev/null && ok "python3-gpiozero" || fail "python3-gpiozero missing (sudo apt install python3-gpiozero)"
  python3 -c 'import gpiod' 2>/dev/null && ok "python3-libgpiod" || fail "python3-libgpiod missing (sudo apt install python3-libgpiod)"
  # set the internal pull-up first (runtime only, gone after reboot): a fresh boot may leave GPIO4 without it
  local g4=""
  command -v pinctrl >/dev/null && { pinctrl set 4 ip pu; sleep 0.1; g4=$(pinctrl get 4); }
  [[ $g4 == *"| hi"* ]] && ok "USER button GPIO4 reads high (not pressed)" \
    || ask_skip "GPIO4 is not high - USER button held down, GPIO4 used by something else (e.g. 1-wire), or not a SmartHub. pinctrl: ${g4:-not available}"
  [ "$(nmcli -g WIFI-PROPERTIES.AP dev show wlan0 2>/dev/null)" = yes ] \
    && ok "wlan0 present and supports AP mode" || ask_skip "wlan0 missing or has no AP support (hotspot will not work)"
  if [ "$SKIPPED" = 0 ]; then
    pinctrl set 19 op dh; pinctrl set 20 op dh; sleep 0.3; pinctrl set 19 dl; pinctrl set 20 dl
    ok "Buzzer GPIO19 + LED0 GPIO20 pulsed (you should hear a beep and see LED0 blink)"
  else  # unknown board: don't drive its pins
    info "Buzzer/LED test skipped because a board check was skipped"
  fi
  command -v docker >/dev/null && ok "Docker found" || info "Docker not installed (optional) - dashboard will say so"

  local tmp; tmp=$(mktemp -d); trap "rm -rf '$tmp'" EXIT  # expand now: $tmp is local, gone when trap fires
  local src; src=$(dirname "${BASH_SOURCE[0]:-}")  # empty when piped from curl
  if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "$src/smarthub_ap.py" ] && [ -f "$src/logo.webp" ]; then
    echo "== 2/3 Using files from downloaded package ($src) =="
    cp "$src/smarthub_ap.py" "$src/logo.webp" "$tmp/"
  else
    echo "== 2/3 Downloading =="
    for f in smarthub_ap.py logo.webp; do
      curl -fsSL "$REPO/$f" -o "$tmp/$f" || fail "Download of $f failed - check internet"
    done
  fi
  python3 "$tmp/smarthub_ap.py" test >/dev/null || fail "Downloaded code failed its self-test"
  ok "$(grep -o 'Dashboard V[0-9.]*' "$tmp/smarthub_ap.py") self-tested"

  echo "== 3/3 Installing =="
  systemctl stop smarthub-ap 2>/dev/null || true  # so our own old copy doesn't count as "port 80 busy"
  local port=80
  [ -n "$(ss -Hltn 'sport = :80')" ] && { port=8080; info "Port 80 is used by another app (e.g. Beaver-IoT) - using 8080"; }
  mkdir -p "$DIR"
  install -m 644 "$tmp/smarthub_ap.py" "$tmp/logo.webp" "$DIR/"
  cat >/etc/systemd/system/smarthub-ap.service <<EOF
[Unit]
Description=SmartHub CM5 AP button + network dashboard
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
Environment=SMARTHUB_PORT=$port
ExecStart=/usr/bin/python3 $DIR/smarthub_ap.py
Restart=always

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable -q smarthub-ap
  systemctl restart smarthub-ap
  sleep 2
  systemctl is-active -q smarthub-ap || { journalctl -u smarthub-ap -n 20 --no-pager; echo "Service failed to start (log above)"; exit 1; }
  local url="http://192.168.4.1"; [ "$port" = 80 ] || url="$url:$port"
  ok "Service running"
  [ "$SKIPPED" = 0 ] || echo "  [WARN] Installed with skipped checks - button, LED, buzzer or hotspot may not work on this board"
  echo
  echo "Done. Hold USER button 5 s -> join WiFi 'smarthub' (password smarthub1) -> open $url"
}

main "$@"
