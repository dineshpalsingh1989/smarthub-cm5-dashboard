# IRIV SmartHub CM5 – AP Network Dashboard

Hold the **USER** button for 5 s to start a WiFi access point, then configure the network from your phone.

## Install / update (one line, on the SmartHub)

```bash
curl -fsSL https://raw.githubusercontent.com/dineshpalsingh1989/smarthub-cm5-dashboard/main/install.sh | sudo bash
```

Run the same line again to update. Before changing anything, the installer checks that:

- the device is a Compute Module 5 running NetworkManager (Raspberry Pi OS Bookworm or newer)
- the USER button (GPIO4) reads high and the buzzer (GPIO19) works (you hear a short beep)
- wlan0 exists and supports AP mode
- the downloaded code passes its self-test

## Use

| Action | Result |
|---|---|
| Hold USER 5 s | AP on: SSID `smarthub`, password `smarthub1`, IP 192.168.4.1 (1 long beep) |
| Hold USER 5 s again | AP off (2 short beeps) |
| Error | 5 fast beeps |

Open `http://192.168.4.1` (or `http://192.168.4.1:8080` if port 80 is already used, for example by Beaver-IoT).

Dashboard: status, Ethernet DHCP/static, WiFi search + connect (DHCP/static), AP name/password, Docker containers (start/stop/restart).

Logs: `sudo journalctl -u smarthub-ap -f`
