#!/usr/bin/env python3
"""IRIV SmartHub CM5 - AP-mode network dashboard.

Hold USER button (GPIO4) 5 s -> AP "smarthub" / "smarthub1" on 192.168.4.1 (1 long beep).
Hold again 5 s            -> AP off (2 short beeps). Error = 5 fast beeps.
Keep holding to 15 s      -> network reset (3 beeps): eth0 + WiFi DHCP, hotspot smarthub/smarthub1 and on.
Hotspot turns itself off after N minutes with no one connected (dashboard option, default 15).
LED0 (GPIO20) flashes while the AP is on.
Browse http://192.168.4.1:<SMARTHUB_PORT> while connected to the AP (default port 80).
The web server only exists while the AP is on: AP off = port closed, free for other software.

Install/update: see README.md (one-line install.sh).   Self-test:  python3 smarthub_ap.py test
Needs Raspberry Pi OS Bookworm+ (NetworkManager/nmcli, gpiozero preinstalled).
"""
import glob, ipaddress, json, os, pathlib, re, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AP, WIFI, ETH = "smarthub-ap", "smarthub-wifi", "smarthub-eth"  # NetworkManager profile names
PORT = int(os.environ.get("SMARTHUB_PORT", 80))  # 80 is taken if Beaver-IoT/docker runs on the box
HERE = os.path.dirname(os.path.abspath(__file__))
LOGO_FILE = os.path.join(HERE, "logo.webp")
SETTINGS_FILE = os.path.join(HERE, "settings.json")  # installer only replaces the .py/.webp, so this survives updates
DEFAULTS = {"auto_off": 15, "reset_button": True, "share_internet": False}  # auto_off: minutes, 0 = never
# NetworkManager's hotspot DHCP server (dnsmasq) reads this dir; "dhcp-option=3" = give devices no gateway
NO_INTERNET_FILE = "/etc/NetworkManager/dnsmasq-shared.d/smarthub-no-internet.conf"
AUTO_OFF_CHOICES = (0, 5, 10, 15, 30, 60)
LOGO = pathlib.Path(LOGO_FILE).read_bytes() if os.path.exists(LOGO_FILE) else b""  # served locally: AP has no internet
buzzer = None
server = None    # dashboard web server; only running while the AP is up
scan_cache = []  # wlan0 can't scan while it is the AP, so we scan just before starting it


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip().strip("\0")
    except OSError:
        return default


def load_settings():
    try:
        return {**DEFAULTS, **json.loads(read(SETTINGS_FILE, "{}") or "{}")}
    except ValueError:  # corrupt file: fall back to defaults
        return dict(DEFAULTS)


settings = load_settings()


def set_options(d):
    m, rb, si = d.get("auto_off"), d.get("reset_button"), d.get("share_internet")
    if type(m) is not int or m not in AUTO_OFF_CHOICES or not isinstance(rb, bool) or not isinstance(si, bool):
        raise ValueError("Bad options")
    changed = si != settings["share_internet"]
    settings.update(auto_off=m, reset_button=rb, share_internet=si)
    with open(SETTINGS_FILE + ".tmp", "w") as f:
        json.dump(settings, f)
    os.replace(SETTINGS_FILE + ".tmp", SETTINGS_FILE)  # atomic: power loss never leaves a half-written file
    apply_internet()
    if changed and ap_active():  # dnsmasq only re-reads its config when the hotspot restarts
        threading.Timer(1, nm, ("con", "up", AP)).start()


def apply_internet():
    # shortcut: soft block - a device that sets its own gateway by hand still gets out; nft rule if that matters
    if settings["share_internet"]:
        if os.path.exists(NO_INTERNET_FILE):
            os.remove(NO_INTERNET_FILE)
    else:
        os.makedirs(os.path.dirname(NO_INTERNET_FILE), exist_ok=True)
        with open(NO_INTERNET_FILE, "w") as f:
            f.write("# smarthub-ap: hotspot devices get no gateway = no internet through the SmartHub\ndhcp-option=3\n")


def nm(*args, check=True):
    r = subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=60)
    if check and r.returncode:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return r.stdout


def fields(line):  # nmcli -t separates with ':' and escapes ':' inside values as '\:'
    return [f.replace("\\:", ":") for f in re.split(r"(?<!\\):", line)]


# ---------- Access point ----------
def ap_active():
    return "activated" in nm("-g", "GENERAL.STATE", "con", "show", AP, check=False)


def ensure_ap():
    if nm("-g", "connection.id", "con", "show", AP, check=False).strip():
        return
    nm("con", "add", "type", "wifi", "ifname", "wlan0", "con-name", AP, "autoconnect", "no",
       "ssid", "smarthub", "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
       "ipv4.method", "shared", "ipv4.addresses", "192.168.4.1/24",  # shared = NM runs DHCP for clients
       "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", "smarthub1",
       "wifi-sec.proto", "rsn", "wifi-sec.pairwise", "ccmp", "wifi-sec.group", "ccmp")


def ap_on():
    global scan_cache
    if "enabled" not in nm("radio", "wifi"):
        nm("radio", "wifi", "on")
        time.sleep(3)  # let wlan0 become available before scanning / starting AP
    scan_cache = wifi_list("yes")
    ensure_ap()
    nm("con", "up", AP)


def ap_off():
    nm("con", "down", AP, check=False)  # NM then auto-reconnects the saved WiFi client profile


def toggle():  # called by the 5 s button hold
    try:
        if ap_active():
            buzzer.beep(0.1, 0.1, 2)
            ap_off()
        else:
            buzzer.beep(0.6, 0, 1)
            ap_on()
    except Exception as e:
        print("AP toggle failed:", e, flush=True)
        buzzer.beep(0.05, 0.05, 5)


def net_reset():  # USER held 15 s: undo network changes that can lock you out (bad static IP, forgotten hotspot password)
    if not settings["reset_button"]:
        return
    buzzer.beep(0.4, 0.2, 3)
    try:
        set_eth({"mode": "dhcp"})
        if nm("-g", "connection.id", "con", "show", WIFI, check=False).strip():  # keep the saved WiFi, just DHCP
            nm("con", "modify", WIFI, "ipv4.method", "auto", "ipv4.addresses", "", "ipv4.gateway", "", "ipv4.dns", "")
        ensure_ap()
        nm("con", "modify", AP, "ssid", "smarthub", "wifi-sec.psk", "smarthub1")
        nm("con", "up", AP) if ap_active() else ap_on()  # re-up applies the default name/password
        print("Network reset: eth0 + WiFi DHCP, hotspot smarthub/smarthub1", flush=True)
    except Exception as e:
        print("Network reset failed:", e, flush=True)
        buzzer.beep(0.05, 0.05, 5)


def ap_clients():  # phones/laptops joined to the hotspot; None if unknown
    try:
        r = subprocess.run(["iw", "dev", "wlan0", "station", "dump"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return None if r.returncode else r.stdout.count("Station ")


def idle_tick(since, clients, now, minutes):  # -> (when the hotspot became empty, turn it off now?)
    if clients != 0 or not minutes:  # someone connected, unknown (iw missing), or auto-off disabled
        return None, False
    since = now if since is None else since
    return since, now - since >= minutes * 60


def wlan0_is_ap():  # no subprocess, so it is cheap to poll: AP mode <=> wlan0 holds 192.168.4.1
    import fcntl, socket, struct
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:  # 0x8915 = SIOCGIFADDR
            return fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", b"wlan0"))[20:24] == socket.inet_aton("192.168.4.1")
        except OSError:  # wlan0 has no IPv4 address
            return False


def button_loop(on_hold, hold=5, line=None, on_long=None, long=15):
    # Kernel GPIO edge events: the thread sleeps with zero CPU until the button moves.
    # (gpiozero's Button costs ~1% CPU forever because lgpio polls for edges.)
    if line is None:
        import gpiod
        line = gpiod.Chip("gpiochip0").get_line(4)
        line.request(consumer="smarthub", type=gpiod.LINE_REQ_EV_BOTH_EDGES, flags=gpiod.LINE_REQ_FLAG_BIAS_PULL_UP)

    def wait(t):  # block until an edge or timeout; drop queued edges, we only read the level
        if line.event_wait(sec=int(t), nsec=int(t % 1 * 1e9)):
            line.event_read_multiple()

    def held_until(end):  # True if the button is still down at monotonic time `end`
        while line.get_value() == 0 and end - time.monotonic() > 0:
            wait(end - time.monotonic())
        return line.get_value() == 0

    while True:
        wait(3600)
        if line.get_value():  # high = not pressed
            continue
        t0 = time.monotonic()
        if held_until(t0 + hold):  # still held after 5 s
            on_hold()
            if on_long and held_until(t0 + long):  # kept holding to 15 s
                on_long()
            while line.get_value() == 0:  # wait for release before re-arming
                wait(3600)


def web(on):  # open the dashboard port only while the AP is up; close it (free the port) when it goes down
    global server
    if on and server is None:
        try:
            server = ThreadingHTTPServer(("192.168.4.1", PORT), Handler)
        except OSError as e:  # e.g. another app took the port
            print("Dashboard could not open port", PORT, "-", e, flush=True)
            buzzer.beep(0.05, 0.05, 5)
            return
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print("Dashboard open on http://192.168.4.1:%d" % PORT, flush=True)
    elif not on and server is not None:
        server.shutdown()
        server.server_close()
        server = None
        print("Dashboard closed, port %d is free" % PORT, flush=True)


def ap_watch(led):  # LED0 + web server follow the real AP state, whatever changed it (button, dashboard, WiFi)
    up, idle, n = False, None, 0
    while True:
        on = wlan0_is_ap()
        if on != up:
            led.blink(0.5, 0.5) if on else led.off()
            web(on)
            up, idle = on, None
        n += 1
        if on and n % 15 == 0:  # every 30 s: is anyone still on the hotspot?
            idle, off = idle_tick(idle, ap_clients(), time.monotonic(), settings["auto_off"])
            if off:
                print("No one on the hotspot for %d min - turning it off" % settings["auto_off"], flush=True)
                buzzer.beep(0.1, 0.1, 2)
                ap_off()
        time.sleep(2)


def set_ap(d):
    ssid, pw = d.get("ssid", ""), d.get("password", "")
    if not 1 <= len(ssid.encode()) <= 32 or not 8 <= len(pw) <= 63:
        raise ValueError("AP name 1-32 chars, password 8-63 chars")
    ensure_ap()
    nm("con", "modify", AP, "ssid", ssid, "wifi-sec.psk", pw)
    if ap_active():
        threading.Timer(1, nm, ("con", "up", AP)).start()  # restart after the reply is sent


# ---------- Ethernet / WiFi client ----------
def ipv4_args(d):
    if d.get("mode") != "static":
        return ["ipv4.method", "auto"]
    ip = d.get("ip", "").strip()
    args = ["ipv4.method", "manual", "ipv4.addresses",
            str(ipaddress.IPv4Interface(ip if "/" in ip else ip + "/24"))]
    if d.get("gateway", "").strip():
        args += ["ipv4.gateway", str(ipaddress.IPv4Address(d["gateway"].strip()))]
    if d.get("dns", "").strip():
        args += ["ipv4.dns", ",".join(str(ipaddress.IPv4Address(x)) for x in d["dns"].replace(",", " ").split())]
    return args


def replace_profile(name, args):  # add the new profile first: if nmcli refuses it, the old one is still there
    nm("con", "delete", name + "-new", check=False)  # leftover from an interrupted change
    nm("con", "add", "con-name", name + "-new", *args)
    nm("con", "delete", name, check=False)
    nm("con", "modify", name + "-new", "connection.id", name)


def set_eth(d):
    replace_profile(ETH, ["type", "ethernet", "ifname", "eth0",
                          "connection.autoconnect-priority", "10", *ipv4_args(d)])
    nm("con", "up", ETH, check=False)  # no cable -> profile is saved and applies when plugged


def set_wifi(d):
    ssid, pw = d.get("ssid", ""), d.get("password", "")
    if not 1 <= len(ssid.encode()) <= 32:
        raise ValueError("Pick a WiFi network")
    if pw and not 8 <= len(pw) <= 63:
        raise ValueError("WiFi password must be 8-63 chars")
    sec = ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", pw] if pw else []
    replace_profile(WIFI, ["type", "wifi", "ifname", "wlan0", "ssid", ssid,
                           "connection.autoconnect-priority", "10", *sec, *ipv4_args(d)])

    def go():  # wlan0 is one radio: joining WiFi drops the AP; bring the AP back if it fails
        try:
            nm("con", "up", WIFI)
            buzzer.beep(0.1, 0.1, 2)
        except RuntimeError as e:
            print("WiFi connect failed:", e, flush=True)
            nm("con", "up", AP, check=False)
            buzzer.beep(0.05, 0.05, 5)
    threading.Timer(1, go).start()


def wifi_list(rescan="no"):
    best = {}
    for line in nm("-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "--rescan", rescan, check=False).splitlines():
        f = fields(line)
        if len(f) == 3 and f[0] and int(f[1] or 0) > best.get(f[0], {}).get("signal", -1):
            best[f[0]] = {"ssid": f[0], "signal": int(f[1] or 0), "security": f[2]}
    return sorted(best.values(), key=lambda w: -w["signal"])


# ---------- Docker containers ----------
def docker(*args):
    r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=90)
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return r.stdout


def containers():
    out = docker("ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}")
    return [dict(zip(("name", "state", "status"), l.split("\t"))) for l in out.splitlines()]


def set_docker(d):
    if d.get("action") not in ("start", "stop", "restart"):
        raise ValueError("Unknown action")
    if d.get("name") not in {c["name"] for c in containers()}:
        raise ValueError("Unknown container")
    docker(d["action"], d["name"])


def status():
    devs = []
    for line in nm("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "dev").splitlines():
        dev, typ, state, con = (fields(line) + [""] * 4)[:4]
        if typ in ("ethernet", "wifi") and state != "unmanaged":  # skip docker veth
            ip = nm("-g", "IP4.ADDRESS", "dev", "show", dev, check=False).strip()
            devs.append({"device": dev, "state": state, "connection": con, "ip": ip})
    ssid = nm("-g", "802-11-wireless.ssid", "con", "show", AP, check=False).strip() or "smarthub"
    try:
        ctrs, derr = containers(), ""
    except FileNotFoundError:
        ctrs, derr = [], "Docker is not installed on this SmartHub."
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as e:  # e.g. docker daemon stopped
        ctrs, derr = [], "Docker error: " + str(e)
    return {"ap": ap_active(), "ap_ssid": ssid, "devices": devs, "containers": ctrs, "docker_error": derr,
            "usb": usb_devices(), "options": settings}


# ---------- USB ports ----------
def usb_port(name, path):
    """SmartHub port a USB device sits on. name: sysfs name ("1-1", "5-1.3"); path: its real sysfs path."""
    m = re.search(r"xhci-hcd\.(\d+)", path)  # the two blue ports each have their own CM5 xhci controller
    if m:
        return "USB 3.0 port %d" % (int(m.group(1)) + 1)
    # black ports + mini PCIe hang off the onboard USB 2.0 hub on the CM5's dwc2 port
    return "USB 2.0 / mini PCIe (hub port %s)" % name.rsplit(".", 1)[-1] if "." in name else "USB 2.0"


def usb_devices(root="/sys/bus/usb/devices"):
    out = []
    for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        d = os.path.join(root, name)
        if ":" in name or name.startswith("usb") or read(d + "/bDeviceClass") == "09":
            continue  # interfaces, root hubs, hubs (incl. the onboard hub)
        vid = read(d + "/idVendor")
        if vid:
            out.append({"port": usb_port(name, os.path.realpath(d)),
                        "name": read(d + "/product") or "Unknown device", "maker": read(d + "/manufacturer"),
                        "id": vid + ":" + read(d + "/idProduct"), "speed": read(d + "/speed") + " Mb/s"})
    return out


def usb_has(vid_pid, root="/sys/bus/usb/devices"):
    return any(read(os.path.join(root, n, "idVendor")) + ":" + read(os.path.join(root, n, "idProduct")) == vid_pid
               for n in (os.listdir(root) if os.path.isdir(root) else []))


# ---------- Hardware check (IRIV SmartHub CM5 datasheet) ----------
def hardware():
    rows = []
    add = lambda name, ok, detail: rows.append({"name": name, "ok": ok, "detail": detail})  # ok None = info only
    model = read("/proc/device-tree/model")
    add("Compute Module 5", "Compute Module 5" in model, model or "unknown")
    m = re.search(r"MemTotal:\s+(\d+)", read("/proc/meminfo"))
    mem = int(m.group(1)) / 1048576 if m else 0
    add("RAM", mem > 0, "%d GB" % round(mem) if mem else "unknown")
    emmc = int(read("/sys/block/mmcblk0/size", "0") or 0) * 512
    add("eMMC storage", emmc > 0, "%.0f GB" % (emmc / 1e9) if emmc else "not found")
    nvme = sorted(glob.glob("/sys/class/nvme/nvme*"))
    add("M.2 NVMe SSD", True if nvme else None, read(nvme[0] + "/model") if nvme else "not fitted (optional)")
    add("WiFi", os.path.exists("/sys/class/net/wlan0"), "wlan0" if os.path.exists("/sys/class/net/wlan0") else "not found")
    add("Bluetooth", os.path.exists("/sys/class/bluetooth/hci0"), "hci0" if os.path.exists("/sys/class/bluetooth/hci0") else "not found")
    link = read("/sys/class/net/eth0/carrier") == "1"
    add("Gigabit Ethernet", True if link else None,
        "link %s Mb/s" % read("/sys/class/net/eth0/speed") if link else "no cable / no link")
    add("USB 2.0 hub", usb_has("1a86:8091"), "QinHeng 1a86:8091" if usb_has("1a86:8091") else "not found")
    for i in (1, 2):
        st = [read(p) for p in glob.glob("/sys/class/drm/card*-HDMI-A-%d/status" % i)]
        add("HDMI%d" % (i - 1), None, {"connected": "monitor connected", "disconnected": "no monitor"}.get(st[0] if st else "", "unknown"))
    rtc = os.path.exists("/sys/class/rtc/rtc0")
    bv = read("/sys/class/rtc/rtc0/battery_voltage")
    add("RTC + CR2032", rtc and (not bv or int(bv) > 2500000),
        (("battery %.2f V" if int(bv) > 2500000 else "battery %.2f V - low or no CR2032 fitted") % (int(bv) / 1e6)
         if bv else "battery voltage unknown") if rtc else "RTC not found")
    t = read("/sys/class/thermal/thermal_zone0/temp")
    add("CPU temperature", int(t) < 80000 if t else None, "%.1f °C" % (int(t) / 1000) if t else "unknown")
    try:
        thr = int(subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5).stdout.split("=")[1], 16)
        add("Power supply", not thr & 0x1, "under-voltage NOW" if thr & 0x1 else
            "under-voltage happened since boot" if thr & 0x10000 else "OK, no under-voltage")
    except (OSError, IndexError, ValueError, subprocess.TimeoutExpired):
        add("Power supply", None, "unknown (vcgencmd missing)")
    add("USER button / LED0 / buzzer", True, "GPIO4 / GPIO20 / GPIO19 in use by this dashboard")
    return rows


# ---------- OS + software ----------
def first_line(cmd):  # version line of a program, or None if not installed
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = (r.stdout.strip() or r.stderr.strip()).splitlines()
    return lines[0][:70] if lines else None


SOFTWARE = [("Docker", ["docker", "--version"]), ("Docker Compose", ["docker", "compose", "version"]),
            ("NetworkManager", ["nmcli", "--version"]), ("Node.js", ["node", "--version"]),
            ("Mosquitto MQTT", ["mosquitto", "-h"]), ("Nginx", ["nginx", "-v"]), ("Git", ["git", "--version"])]


def system():
    up = float(read("/proc/uptime", "0").split()[0] or 0)
    osr = dict(re.findall(r'^(\w+)="?([^"\n]*)"?$', read("/etc/os-release"), re.M))
    apps = [{"name": "Python", "version": sys.version.split()[0]}]
    apps += [{"name": n, "version": first_line(c)} for n, c in SOFTWARE]
    nr = glob.glob("/usr/lib/node_modules/node-red/package.json") + glob.glob("/usr/local/lib/node_modules/node-red/package.json")
    apps.append({"name": "Node-RED", "version": json.loads(read(nr[0], "{}")).get("version") if nr else None})
    try:
        pkgs = len(subprocess.run(["dpkg-query", "-f", ".\n", "-W"], capture_output=True, text=True, timeout=20).stdout.split())
    except (OSError, subprocess.TimeoutExpired):
        pkgs = None
    return {"os": osr.get("PRETTY_NAME", "unknown"), "kernel": os.uname().release if hasattr(os, "uname") else "",
            "image": read("/etc/rpi-issue").split("\n")[0], "uptime": "%dd %dh %dm" % (up // 86400, up % 86400 // 3600, up % 3600 // 60),
            "packages": pkgs, "hostname": read("/etc/hostname"), "hardware": hardware(), "software": apps}


# ---------- Web server ----------
ROUTES = {"/api/eth": set_eth, "/api/wifi": set_wifi, "/api/ap": set_ap, "/api/docker": set_docker, "/api/options": set_options,
          "/api/ap/off": lambda d: threading.Timer(1, ap_off).start()}


MAX_BODY = 4096  # real requests are ~100 bytes


def request_problem(h, post):  # h: request headers. Blocks other websites and DNS rebinding from using the dashboard
    host = h.get("Host") or ""
    if host.split(":")[0] != "192.168.4.1":
        return 403, "Open the dashboard at http://192.168.4.1"
    if not post:
        return None
    if h.get("Origin") not in (None, "http://" + host):
        return 403, "Request from another website blocked"
    if (h.get("Content-Type") or "").split(";")[0].strip() != "application/json":
        return 415, "JSON only"  # browsers can't send this cross-site without asking first, and we never say yes
    try:
        n = int(h.get("Content-Length") or 0)
    except ValueError:
        n = -1
    if not 0 <= n <= MAX_BODY:
        return 413, "Request too large"
    return None


class Handler(BaseHTTPRequestHandler):
    timeout = 10  # hang up on clients that go silent, so stalled connections can't pile up
    def send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def allowed(self, post=False):  # only devices on the SmartHub AP (server binds 192.168.4.1 only; 2nd guard)
        if not self.client_address[0].startswith("192.168.4."):
            return self.send(403, {"error": "Connect to the SmartHub AP"})
        bad = request_problem(self.headers, post)
        if bad:
            return self.send(bad[0], {"error": bad[1]})
        return True

    def do_GET(self):
        if not self.allowed():
            return
        if self.path == "/":
            self.send(200, PAGE, "text/html; charset=utf-8")
        elif self.path == "/logo.webp":
            self.send(200, LOGO, "image/webp")
        elif self.path == "/api/status":
            self.send(200, status())
        elif self.path == "/api/scan":
            self.send(200, wifi_list() or scan_cache)
        elif self.path == "/api/system":
            self.send(200, system())
        else:
            self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.allowed(post=True):
            return
        fn = ROUTES.get(self.path)
        if not fn:
            return self.send(404, {"error": "not found"})
        try:
            fn(json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}"))
            self.send(200, {"ok": True})
        except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            self.send(400, {"error": str(e)})


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>SmartHub Dashboard</title>
<style>
:root{--bg:#eef2f6;--card:#fff;--ink:#16202c;--mute:#6b7684;--line:#e2e7ed;--blue:#0a84c6;--blue2:#e4f2fb;--green:#1f9d55;--green2:#e2f5e9;--red:#d03a2f;--red2:#fde6e4;--amber:#c27c0e;--amber2:#fff3d9}
@media(prefers-color-scheme:dark){:root{--bg:#0f141a;--card:#18202a;--ink:#e6edf3;--mute:#8d99a8;--line:#273241;--blue:#2ea0e0;--blue2:#11314a;--green:#3cc47a;--green2:#12331f;--red:#f0685d;--red2:#3a1816;--amber:#e3a43a;--amber2:#3a2b0e}}
*{box-sizing:border-box}
body{margin:0;font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--ink)}
header{background:var(--card);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:2}
.top{max-width:760px;margin:0 auto;padding:10px 16px 6px;display:flex;align-items:center;gap:12px}
.top img{height:36px;width:72px}
.top h1{font-size:16px;margin:0;flex:1;line-height:1.2}.top h1 small{display:block;font-weight:400;color:var(--mute);font-size:12px}
.pill{font-size:12px;font-weight:600;padding:4px 10px;border-radius:99px;background:var(--amber2);color:var(--amber);white-space:nowrap}
nav{max-width:760px;margin:0 auto;padding:4px 16px 10px;display:flex;gap:6px}
nav button{flex:1;padding:8px 4px;border:0;border-radius:8px;background:transparent;color:var(--mute);font:inherit;font-weight:600;cursor:pointer}
nav button.on{background:var(--blue2);color:var(--blue)}
main{max-width:760px;margin:0 auto;padding:14px 16px 28px}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin-bottom:14px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:11px 12px;min-width:0}
.tile .k{font-size:12px;color:var(--mute);display:flex;align-items:center;gap:6px}
.tile .v{font-size:16px;font-weight:700;margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dot{width:8px;height:8px;border-radius:50%;background:var(--line);display:inline-block;flex:none}
.dot.g{background:var(--green)}.dot.r{background:var(--red)}.dot.a{background:var(--amber)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:12px}
.card h2{font-size:15px;margin:0 0 8px;display:flex;justify-content:space-between;align-items:center}
label{display:block;margin:10px 0 4px;font-size:13px;color:var(--mute)}
input,select{width:100%;padding:10px;font:inherit;color:var(--ink);background:var(--card);border:1px solid var(--line);border-radius:8px}
input:focus,select:focus,button:focus-visible{outline:2px solid var(--blue);outline-offset:1px}
.btn{width:100%;margin-top:12px;padding:11px;border:0;border-radius:8px;background:var(--blue);color:#fff;font:inherit;font-weight:600;cursor:pointer}
.btn.red{background:var(--red)}.btn.ghost{background:var(--blue2);color:var(--blue)}
.btn.sm{width:auto;padding:6px 12px;margin:8px 6px 0 0;font-size:13px}
.seg{display:flex;background:var(--bg);border-radius:8px;padding:3px}
.seg button{flex:1;border:0;background:transparent;padding:8px;border-radius:6px;font:inherit;color:var(--mute);cursor:pointer}
.seg button.on{background:var(--card);color:var(--ink);font-weight:600;box-shadow:0 1px 2px #0003}
.kv{display:flex;gap:10px;padding:8px 0;border-bottom:1px solid var(--line);font-size:14px;align-items:baseline}.kv:last-child{border:0}
.kv b{flex:0 0 44%;font-weight:600;display:flex;gap:7px;align-items:center}.kv span{flex:1;color:var(--mute);word-break:break-word}
.badge{font-size:11px;font-weight:700;padding:2px 8px;border-radius:99px;text-transform:uppercase;margin-left:6px}
.badge.g{background:var(--green2);color:var(--green)}.badge.r{background:var(--red2);color:var(--red)}.badge.n{background:var(--bg);color:var(--mute)}
.ctr{padding:10px 0;border-bottom:1px solid var(--line)}.ctr:last-child{border:0}.ctr .s{font-size:13px;color:var(--mute)}
.chk{display:flex;gap:10px;align-items:flex-start;color:var(--ink);font-size:14px;margin-top:14px;cursor:pointer}
.chk input{width:18px;height:18px;flex:none;margin:1px 0 0;accent-color:var(--blue)}
.note{font-size:12.5px;color:var(--mute);margin:10px 0 0}
#toast{position:fixed;left:50%;bottom:18px;transform:translate(-50%,30px);opacity:0;transition:.25s;background:var(--ink);color:var(--card);padding:10px 16px;border-radius:10px;font-size:14px;max-width:90%;z-index:3}
#toast.show{opacity:1;transform:translate(-50%,0)}#toast.err{background:var(--red);color:#fff}
footer{text-align:center;font-size:12px;color:var(--mute);padding:4px 16px 20px}
[hidden]{display:none!important}
</style></head><body>
<header><div class="top"><img src="/logo.webp" alt="Cytron Technologies"><h1>SmartHub CM5<small>Network dashboard</small></h1><span class="pill" id="pill">Hotspot on</span></div>
<nav><button class="on" data-go="net">Network</button><button data-go="dev">Devices</button><button data-go="sys">System</button></nav></header>
<main>
<div class="tiles" id="tiles"></div>

<div data-tab="net">
<section class="card"><h2>Connections</h2><div id="status">Loading...</div></section>
<section class="card" id="eth"><h2>Ethernet (eth0)</h2><div class="ipf"></div><button class="btn" onclick="send('eth')">Apply</button>
<p class="note">Changing Ethernet can change the SmartHub's IP address on your wired network.</p></section>
<section class="card" id="wifi"><h2>Connect to WiFi</h2><button class="btn ghost" onclick="scan()">Search WiFi</button>
<label>Network</label><select name="ssid"><option value="">Press Search WiFi first</option></select>
<label>Password (blank for open network)</label><input name="password" type="password" autocomplete="off">
<div class="ipf"></div><button class="btn" onclick="send('wifi')">Connect</button>
<p class="note">Connecting turns the hotspot off (one WiFi radio). If it fails, the hotspot comes back.</p></section>
<section class="card" id="ap"><h2>Hotspot</h2><label>Name (SSID)</label><input name="ssid">
<label>Password (8-63 characters)</label><input name="password" type="password" autocomplete="new-password">
<button class="btn" onclick="send('ap')">Save</button><button class="btn red" onclick="if(confirm('Turn off the hotspot? This page will close.'))post('ap/off',{})">Turn off hotspot</button>
<p class="note">Turning the hotspot off also closes this dashboard and frees its port for other software.</p></section>
<section class="card" id="opt"><h2>Safety options</h2>
<label for="autooff">Turn the hotspot off automatically</label><select id="autooff">
<option value="0">Never</option><option value="5">After 5 min with no one connected</option><option value="10">After 10 min with no one connected</option>
<option value="15">After 15 min with no one connected</option><option value="30">After 30 min with no one connected</option><option value="60">After 60 min with no one connected</option></select>
<label class="chk"><input type="checkbox" id="resetbtn"><span>Network reset button<br><small style="color:var(--mute)">Hold USER for 15 s: Ethernet and WiFi go back to DHCP, hotspot back to smarthub / smarthub1 and on (3 beeps)</small></span></label>
<label class="chk"><input type="checkbox" id="netshare"><span>Share internet with hotspot devices<br><small style="color:var(--mute)">Off: phones on the hotspot can open this dashboard but get no internet through the SmartHub. Changing it restarts the hotspot: rejoin it.</small></span></label>
<button class="btn" onclick="post('options',{auto_off:+$('#autooff').value,reset_button:$('#resetbtn').checked,share_internet:$('#netshare').checked})">Save options</button>
<p class="note">Auto-off frees the dashboard port even if someone forgets to turn the hotspot off. Hotspot devices can never log in to the SmartHub's terminal (SSH is blocked for them).</p></section>
</div>

<div data-tab="dev" hidden>
<section class="card"><h2>USB ports</h2><div id="usb">Loading...</div></section>
<section class="card"><h2>Docker</h2><div id="docker">Loading...</div></section>
</div>

<div data-tab="sys" hidden>
<section class="card"><h2>Hardware check <button class="btn ghost sm" style="margin:0" onclick="sysload()">Check again</button></h2><div id="hw">Loading...</div></section>
<section class="card"><h2>System</h2><div id="sys">Loading...</div></section>
<section class="card"><h2>Software</h2><div id="sw">Loading...</div></section>
</div>
</main>
<footer>SmartHub Network Dashboard V2.0-beta</footer>
<div id="toast" role="status"></div>
<script>
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const el=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e};
let S={},Y=null,tt,optset=0;
$$('nav button').forEach(b=>b.onclick=()=>{$$('nav button').forEach(x=>x.classList.toggle('on',x==b));
  $$('[data-tab]').forEach(t=>t.hidden=t.dataset.tab!=b.dataset.go);history.replaceState(null,'','#'+b.dataset.go)});
($$('nav button').find(b=>'#'+b.dataset.go==location.hash)||{click(){}}).click();  // keep tab on refresh
$$('.ipf').forEach(e=>{e.innerHTML=`<label>IP address</label><div class="seg"><button type="button" class="on" data-m="dhcp">Dynamic (DHCP)</button>
<button type="button" data-m="static">Static</button></div><input type="hidden" name="mode" value="dhcp">
<div class="static" hidden><label>IP address / prefix</label><input name="ip" placeholder="192.168.1.50/24">
<label>Gateway</label><input name="gateway" placeholder="192.168.1.1"><label>DNS</label><input name="dns" placeholder="8.8.8.8, 1.1.1.1"></div>`;
  e.querySelectorAll('.seg button').forEach(b=>b.onclick=()=>{e.querySelectorAll('.seg button').forEach(x=>x.classList.toggle('on',x==b));
    e.querySelector('[name=mode]').value=b.dataset.m;e.querySelector('.static').hidden=b.dataset.m!='static'})});
function toast(t,err){const m=$('#toast');m.textContent=t;m.className='show'+(err?' err':'');clearTimeout(tt);tt=setTimeout(()=>m.className='',4000)}
async function post(path,data){
  try{const r=await fetch('/api/'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});const j=await r.json();
    if(r.ok){toast('Applied');setTimeout(load,2000)}else toast(j.error,1)}
  catch(e){toast('Connection lost - SmartHub is switching network',1)}}
function send(id){const d={};$$('#'+id+' [name]').forEach(e=>d[e.name]=e.value);post(id,d)}
async function scan(){toast('Searching...');
  const l=await (await fetch('/api/scan')).json();
  $('#wifi [name=ssid]').replaceChildren(...l.map(w=>new Option(w.ssid+'  ('+w.signal+'%, '+(w.security||'open')+')',w.ssid)));
  toast(l.length+' networks found')}
function kv(k,v,dot,muted){const r=el('div','kv'),b=el('b');if(dot!==undefined)b.append(el('i','dot '+dot));b.append(k);
  r.append(b,el('span',muted?'':null,v));if(muted)r.lastChild.style.opacity=.6;return r}
function tile(k,v,dot){const t=el('div','tile'),h=el('div','k');h.append(el('i','dot '+(dot||'')),k);t.append(h,el('div','v',v));return t}
const ip=x=>(x&&x.ip||'').split(' | ')[0].split('/')[0];
const ccls=c=>c.state=='running'?'g':(c.state=='created'||/Exited \\(0\\)/.test(c.status))?'n':'r';
function tiles(){const dv=n=>(S.devices||[]).find(x=>x.device==n)||{},e=dv('eth0'),w=dv('wlan0'),cs=S.containers||[];
  const run=cs.filter(c=>c.state=='running').length,bad=cs.filter(c=>ccls(c)=='r').length,hot=w.connection=='smarthub-ap';
  const t=Y&&Y.hardware.find(h=>h.name=='CPU temperature'),iss=Y&&Y.hardware.filter(h=>h.ok===false).length;
  $('#tiles').replaceChildren(
    tile('Ethernet',ip(e)||(e.state?'no cable':'not found'),ip(e)?'g':'r'),
    tile('WiFi',hot?'Hotspot':ip(w)||w.state||'off',hot?'a':ip(w)?'g':''),
    tile('USB devices',String((S.usb||[]).length),(S.usb||[]).length?'g':''),
    tile('Docker',S.docker_error?'not available':run+' of '+cs.length+' running',S.docker_error?'':bad?'r':'g'),
    tile('CPU temperature',t?t.detail:'...',t?(t.ok?'g':'r'):''),
    tile('Hardware',Y?(iss?iss+' issue'+(iss>1?'s':''):'All OK'):'...',Y?(iss?'r':'g'):''))}
async function load(){try{S=await (await fetch('/api/status')).json()}catch(e){return}
  $('#status').replaceChildren(...S.devices.map(d=>kv(d.device,d.state+(d.connection?' · '+d.connection:'')+(ip(d)?' · '+ip(d):''),
    d.state=='connected'?'g':'r')));
  $('#pill').textContent='Hotspot on · '+S.ap_ssid;
  if(!$('#ap [name=ssid]').value)$('#ap [name=ssid]').value=S.ap_ssid;
  if(S.options&&!optset){optset=1;$('#autooff').value=S.options.auto_off;$('#resetbtn').checked=S.options.reset_button;$('#netshare').checked=S.options.share_internet}
  $('#docker').replaceChildren(...(S.docker_error?[el('p','note',S.docker_error)]:S.containers.length?S.containers.map(crow):[el('p','note','No containers')]));
  const p3=['USB 3.0 port 1','USB 3.0 port 2'],u=S.usb,us=d=>d.name+(d.maker?' ('+d.maker+')':'')+' · '+d.speed;
  const rows=[];for(const p of p3){const ds=u.filter(d=>d.port==p);rows.push(...(ds.length?ds.map(d=>kv(p,us(d),'g')):[kv(p,'empty','',1)]))}
  const u2=u.filter(d=>!p3.includes(d.port));rows.push(...(u2.length?u2.map(d=>kv(d.port,us(d),'g')):[kv('USB 2.0 ports','empty','',1)]));
  $('#usb').replaceChildren(...rows);tiles()}
function crow(c){const r=el('div','ctr'),k=ccls(c),h=el('div');
  h.append(el('b',null,c.name),el('span','badge '+k,k=='r'?'error':c.state));r.append(h,el('div','s',c.status));
  for(const a of c.state=='running'?['restart','stop']:['start','restart']){
    const b=el('button','btn sm'+(a=='stop'?' red':' ghost'),a[0].toUpperCase()+a.slice(1));b.onclick=()=>dock(c.name,a);r.append(b)}
  return r}
async function dock(name,action){if(action=='stop'&&!confirm('Stop '+name+'?'))return;toast(action+' '+name+'... please wait');await post('docker',{name,action})}
async function sysload(){$('#hw').textContent='Checking...';
  try{Y=await (await fetch('/api/system')).json()}catch(e){return}
  $('#hw').replaceChildren(...Y.hardware.map(h=>kv(h.name,h.detail,h.ok===true?'g':h.ok===false?'r':'',h.ok===null)));
  $('#sys').replaceChildren(kv('OS',Y.os),kv('Kernel',Y.kernel),kv('OS image',Y.image||'unknown'),kv('Hostname',Y.hostname),
    kv('Uptime',Y.uptime),kv('Packages installed',String(Y.packages??'unknown')));
  $('#sw').replaceChildren(...Y.software.map(a=>kv(a.name,a.version||'not installed',a.version?'g':'',!a.version)));tiles()}
load();sysload();setInterval(()=>{if(!document.hidden)load()},10000);  // no polling while the tab is in the background
</script></body></html>"""


def selftest():
    assert ipv4_args({"mode": "dhcp"}) == ["ipv4.method", "auto"]
    assert ipv4_args({"mode": "static", "ip": "192.168.1.50", "gateway": "192.168.1.1", "dns": "8.8.8.8, 1.1.1.1"}) == \
        ["ipv4.method", "manual", "ipv4.addresses", "192.168.1.50/24", "ipv4.gateway", "192.168.1.1", "ipv4.dns", "8.8.8.8,1.1.1.1"]
    for bad in ("999.1.1.1", "abc", "1.2.3.4/40", ""):
        try:
            ipv4_args({"mode": "static", "ip": bad})
            raise AssertionError(bad)
        except ValueError:
            pass
    assert fields(r"my\:net:80:WPA2") == ["my:net", "80", "WPA2"]
    class FakeLine:  # scripted button: [(seconds until edge, new level)], 0 = pressed
        def __init__(self, script):
            self.v, self.script = 1, list(script)
        def get_value(self):
            return self.v
        def event_read_multiple(self):
            pass
        def event_wait(self, sec, nsec):
            if not self.script:
                raise EOFError  # script finished: stop the loop
            delay, v = self.script[0]
            if delay > sec + nsec / 1e9:
                self.script[0] = (delay - sec - nsec / 1e9, v)
                time.sleep(sec + nsec / 1e9)
                return False
            time.sleep(delay)
            self.v = v
            self.script.pop(0)
            return True

    for script, want in (([(0, 0), (.05, 1)], 0),                          # short press: ignored
                         ([(0, 0), (.3, 1)], 1),                           # held past 0.2 s: toggles once
                         ([(0, 0), (.01, 1), (.01, 0), (.3, 1)], 1),       # bounce, then hold
                         ([(0, 0), (.3, 1), (.05, 0), (.3, 1)], 2)):       # two holds: toggles twice
        calls = []
        try:
            button_loop(lambda: calls.append(1), hold=.2, line=FakeLine(script))
        except EOFError:
            pass
        assert len(calls) == want, (script, calls)
    for script, want in (([(0, 0), (.3, 1)], ["hold"]),                     # 5 s style hold: toggle only
                         ([(0, 0), (.6, 1)], ["hold", "long"])):            # held past 15 s style: toggle, then reset
        calls = []
        try:
            button_loop(lambda: calls.append("hold"), hold=.2, line=FakeLine(script),
                        on_long=lambda: calls.append("long"), long=.5)
        except EOFError:
            pass
        assert calls == want, (script, calls)
    assert idle_tick(None, 0, 100, 15) == (100, False)          # hotspot just became empty: start the clock
    assert idle_tick(100, 0, 100 + 899, 15) == (100, False)
    assert idle_tick(100, 0, 100 + 900, 15) == (100, True)      # empty for 15 min: off
    assert idle_tick(100, 1, 2000, 15) == (None, False)         # someone joined: clock resets
    assert idle_tick(100, None, 2000, 15) == (None, False)      # iw missing: never turn off blindly
    assert idle_tick(100, 0, 2000, 0) == (None, False)          # auto-off disabled
    for bad in ({"auto_off": 7, "reset_button": True, "share_internet": False},
                {"auto_off": 15, "reset_button": "yes", "share_internet": False},
                {"auto_off": True, "reset_button": True, "share_internet": False},
                {"auto_off": 15, "reset_button": True}, {}):
        try:
            set_options(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    assert usb_port("3-1", "/sys/devices/platform/axi/1000120000.pcie/1f00300000.usb/xhci-hcd.1/usb3/3-1") == "USB 3.0 port 2"
    assert usb_port("1-1.4", "/sys/devices/x/xhci-hcd.0/usb1/1-1/1-1.4") == "USB 3.0 port 1"  # behind an external hub
    assert usb_port("5-1.3", "/sys/devices/platform/axi/1000480000.usb/usb5/5-1/5-1.3") == "USB 2.0 / mini PCIe (hub port 3)"
    import tempfile
    with tempfile.TemporaryDirectory() as root:  # fake /sys/bus/usb/devices
        for name, attrs in {"usb5": {"idVendor": "1d6b"}, "5-1": {"idVendor": "1a86", "idProduct": "8091", "bDeviceClass": "09"},
                            "5-1.3": {"idVendor": "0781", "idProduct": "5567", "product": "Cruzer", "manufacturer": "SanDisk",
                                      "speed": "480", "bDeviceClass": "00"}, "5-1.3:1.0": {}}.items():
            if ":" in name and os.name == "nt":
                continue  # Windows file names can't contain ':' (interface entries only exist on Linux anyway)
            os.mkdir(os.path.join(root, name))
            for k, v in attrs.items():
                with open(os.path.join(root, name, k), "w") as f:
                    f.write(v + "\n")
        assert usb_devices(root) == [{"port": "USB 2.0 / mini PCIe (hub port 3)", "name": "Cruzer", "maker": "SanDisk",
                                      "id": "0781:5567", "speed": "480 Mb/s"}], usb_devices(root)
        assert usb_has("1a86:8091", root) and not usb_has("dead:beef", root)
    for bad in ({"action": "rm", "name": "x"}, {"action": "exec"}):  # only start/stop/restart reach docker
        try:
            set_docker(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    ok = {"Host": "192.168.4.1:8080", "Origin": "http://192.168.4.1:8080", "Content-Type": "application/json",
          "Content-Length": "60"}
    assert request_problem(ok, True) is None and request_problem({"Host": "192.168.4.1"}, False) is None
    for change, code in (({"Host": "evil.example:8080"}, 403),           # DNS rebinding
                         ({"Origin": "http://evil.example"}, 403),      # other website (CSRF)
                         ({"Content-Type": "text/plain"}, 415),         # cross-site "simple" request
                         ({"Content-Length": "99999999"}, 413), ({"Content-Length": "x"}, 413)):
        assert request_problem({**ok, **change}, True)[0] == code, change
    print("selftest ok")


def main():
    global buzzer
    from gpiozero import LED, Buzzer
    buzzer = Buzzer(19)                    # active buzzer, beeps when high
    try:
        apply_internet()  # match the saved internet-sharing option (fresh install: no internet for hotspot devices)
    except OSError as e:
        print("Could not apply internet sharing option:", e, flush=True)
    threading.Thread(target=button_loop, args=(toggle,), kwargs={"on_long": net_reset},  # USER pulls GPIO4 to GND
                     daemon=True).start()
    print("SmartHub AP dashboard running - hold USER button 5 s to toggle AP", flush=True)
    ap_watch(LED(20))  # LED0 flashes + dashboard port open only while the AP is on


if __name__ == "__main__":
    selftest() if sys.argv[1:] == ["test"] else main()
