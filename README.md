# IRIV SmartHub CM5 – AP Network Dashboard

Hold the **USER** button for 5 seconds to start a WiFi hotspot. Join it from your phone or laptop and set up the SmartHub's network in a browser. No keyboard or screen needed.

**Requirements:** IRIV SmartHub CM5 running Raspberry Pi OS Bookworm or newer.

---

## Install

Open a terminal on the SmartHub (keyboard + screen, or SSH), then choose **one** option.

### Option 1 – One line (SmartHub has internet)

```bash
curl -fsSL https://raw.githubusercontent.com/dineshpalsingh1989/smarthub-cm5-dashboard/main/install.sh | sudo bash
```

### Option 2 – Download the package (no internet on the SmartHub)

1. On your PC, open **[Releases](https://github.com/dineshpalsingh1989/smarthub-cm5-dashboard/releases/latest)** and download `smarthub-cm5-dashboard.zip`.
2. Copy the zip to the SmartHub with a USB stick, or over the network:
   ```bash
   scp smarthub-cm5-dashboard.zip pi@<smarthub-ip>:~
   ```
3. On the SmartHub:
   ```bash
   unzip smarthub-cm5-dashboard.zip
   sudo bash smarthub-cm5-dashboard/install.sh
   ```

### What the installer does

1. **Checks the device first and changes nothing** if a check fails:
   - Compute Module 5 with NetworkManager and gpiozero
   - USER button (GPIO4) reads high, meaning it is not pressed
   - Buzzer (GPIO19) and LED0 (GPIO20) are pulsed: **you should hear a beep and see LED0 blink**
   - wlan0 exists and supports AP mode
2. Runs the dashboard's self-test.
3. Installs to `/opt/smarthub`, starts the service and enables it on boot. If port 80 is already in use (for example by Beaver-IoT), it uses **8080**.

### Update

Run the same command again, or download the newest package from Releases. Your AP name, password and network settings are kept.

---

## Use

| Action | Result |
|---|---|
| Hold USER 5 s | AP on: WiFi `smarthub`, password `smarthub1`. 1 long beep, **LED0 flashes** |
| Hold USER 5 s again | AP off. 2 short beeps, LED0 off |
| Something failed | 5 fast beeps |

1. Join WiFi **smarthub** (password **smarthub1**).
2. Open **http://192.168.4.1**, or **http://192.168.4.1:8080** if the installer said it is using port 8080.

### Dashboard

- **Status:** eth0 / wlan0 state and IP
- **Docker:** container state (running / stopped / error) with Start, Stop and Restart
- **Ethernet:** Dynamic (DHCP) or Static IP
- **WiFi:** search for networks and connect, with DHCP or Static IP. Connecting turns the AP off, because the board has one WiFi radio. If the connection fails, the AP comes back on.
- **Access Point:** change the AP name and password, or turn the AP off

> Change the default AP password `smarthub1` on devices in the field. It is published in this README.

---

## Troubleshooting

```bash
sudo systemctl status smarthub-ap      # is it running?
sudo journalctl -u smarthub-ap -f      # live log
```

To uninstall:

```bash
sudo systemctl disable --now smarthub-ap
sudo rm -rf /opt/smarthub /etc/systemd/system/smarthub-ap.service
```
