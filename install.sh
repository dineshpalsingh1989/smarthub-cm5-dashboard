#!/bin/bash
# IRIV SmartHub CM5 - AP dashboard installer / updater. Same command installs or updates:
#   curl -fsSL https://raw.githubusercontent.com/dineshpalsingh1989/smarthub-cm5-dashboard/main/install.sh | sudo bash
set -euo pipefail
REPO=https://raw.githubusercontent.com/dineshpalsingh1989/smarthub-cm5-dashboard/main
DIR=/opt/smarthub

ok()   { echo "  [OK]   $*"; }
info() { echo "  [INFO] $*"; }
fail() { echo "  [FAIL] $*"; echo "Aborted - nothing was changed."; exit 1; }  # only used before step 3

main() {  # wrapped so a half-downloaded script never runs
  [ "$(id -u)" = 0 ] || { echo "Run with sudo:  curl -fsSL $REPO/install.sh | sudo bash"; exit 1; }

  echo "== 1/3 Checking device (nothing is changed yet) =="
  local model
  model=$(tr -d '\0' </proc/device-tree/model 2>/dev/null || true)
  [[ $model == *"Compute Module 5"* ]] && ok "$model" || fail "Not a Compute Module 5 ($model)"
  command -v nmcli >/dev/null && systemctl is-active -q NetworkManager \
    && ok "NetworkManager running" || fail "NetworkManager not running (needs Raspberry Pi OS Bookworm or newer)"
  python3 -c 'import gpiozero' 2>/dev/null && ok "python3-gpiozero" || fail "python3-gpiozero missing (sudo apt install python3-gpiozero)"
  command -v pinctrl >/dev/null || fail "pinctrl missing (update Raspberry Pi OS)"
  [[ $(pinctrl get 4) == *"| hi"* ]] && ok "USER button GPIO4 reads high (not pressed)" \
    || fail "GPIO4 is low - not a SmartHub, or USER button is held down"
  [ "$(nmcli -g WIFI-PROPERTIES.AP dev show wlan0 2>/dev/null)" = yes ] \
    && ok "wlan0 present and supports AP mode" || fail "wlan0 missing or has no AP support"
  pinctrl set 19 op dh; sleep 0.2; pinctrl set 19 dl
  ok "Buzzer GPIO19 pulsed (you should hear a short beep)"
  command -v docker >/dev/null && ok "Docker found" || info "Docker not installed - Docker panel will show that"

  echo "== 2/3 Downloading =="
  local tmp; tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
  for f in smarthub_ap.py logo.webp; do
    curl -fsSL "$REPO/$f" -o "$tmp/$f" || fail "Download of $f failed - check internet"
  done
  python3 "$tmp/smarthub_ap.py" test >/dev/null || fail "Downloaded code failed its self-test"
  ok "$(grep -o 'Dashboard V[0-9.]*' "$tmp/smarthub_ap.py") downloaded and self-tested"

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
  echo
  echo "Done. Hold USER button 5 s -> join WiFi 'smarthub' (password smarthub1) -> open $url"
}

main "$@"
