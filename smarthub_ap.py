#!/usr/bin/env python3
"""IRIV SmartHub CM5 - AP-mode network dashboard.

Hold USER button (GPIO4) 5 s -> AP "smarthub" / "smarthub1" on 192.168.4.1 (1 long beep).
Hold again 5 s            -> AP off (2 short beeps). Error = 5 fast beeps.
LED0 (GPIO20) flashes while the AP is on.
Browse http://192.168.4.1:<SMARTHUB_PORT> while connected to the AP (default port 80).

Install/update: see README.md (one-line install.sh).   Self-test:  python3 smarthub_ap.py test
Needs Raspberry Pi OS Bookworm+ (NetworkManager/nmcli, gpiozero preinstalled).
"""
import ipaddress, json, os, re, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AP, WIFI, ETH = "smarthub-ap", "smarthub-wifi", "smarthub-eth"  # NetworkManager profile names
PORT = int(os.environ.get("SMARTHUB_PORT", 80))  # 80 is taken if Beaver-IoT/docker runs on the box
LOGO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.webp")
LOGO = open(LOGO_FILE, "rb").read() if os.path.exists(LOGO_FILE) else b""  # served locally: AP has no internet
buzzer = None
scan_cache = []  # wlan0 can't scan while it is the AP, so we scan just before starting it


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


def wlan0_is_ap():  # no subprocess, so it is cheap to poll: AP mode <=> wlan0 holds 192.168.4.1
    import fcntl, socket, struct
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:  # 0x8915 = SIOCGIFADDR
            return fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", b"wlan0"))[20:24] == socket.inet_aton("192.168.4.1")
        except OSError:  # wlan0 has no IPv4 address
            return False


def button_loop(on_hold, hold=5, line=None):
    # Kernel GPIO edge events: the thread sleeps with zero CPU until the button moves.
    # (gpiozero's Button costs ~1% CPU forever because lgpio polls for edges.)
    if line is None:
        import gpiod
        line = gpiod.Chip("gpiochip0").get_line(4)
        line.request(consumer="smarthub", type=gpiod.LINE_REQ_EV_BOTH_EDGES, flags=gpiod.LINE_REQ_FLAG_BIAS_PULL_UP)

    def wait(t):  # block until an edge or timeout; drop queued edges, we only read the level
        if line.event_wait(sec=int(t), nsec=int(t % 1 * 1e9)):
            line.event_read_multiple()

    while True:
        wait(3600)
        if line.get_value():  # high = not pressed
            continue
        end = time.monotonic() + hold
        while line.get_value() == 0 and end - time.monotonic() > 0:
            wait(end - time.monotonic())
        if line.get_value() == 0:  # still held after 5 s
            on_hold()
            while line.get_value() == 0:  # wait for release before re-arming
                wait(3600)


def led_loop(led):  # LED0 mirrors real AP state, whatever changed it (button, dashboard, WiFi connect)
    blinking = False
    while True:
        on = wlan0_is_ap()
        if on != blinking:
            led.blink(0.5, 0.5) if on else led.off()
            blinking = on
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


def replace_profile(name, args):
    nm("con", "delete", name, check=False)
    nm("con", "add", "con-name", name, *args)


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
    return {"ap": ap_active(), "ap_ssid": ssid, "devices": devs, "containers": ctrs, "docker_error": derr}


# ---------- Web server ----------
ROUTES = {"/api/eth": set_eth, "/api/wifi": set_wifi, "/api/ap": set_ap, "/api/docker": set_docker,
          "/api/ap/off": lambda d: threading.Timer(1, ap_off).start()}


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def allowed(self):  # only devices on the SmartHub AP, or localhost (SSH tunnel)
        if self.client_address[0].startswith(("192.168.4.", "127.0.0.1")):
            return True
        self.send(403, {"error": "Connect to the SmartHub AP"})

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
        else:
            self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.allowed():
            return
        fn = ROUTES.get(self.path)
        if not fn:
            return self.send(404, {"error": "not found"})
        try:
            fn(json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}"))
            self.send(200, {"ok": True})
        except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            self.send(400, {"error": str(e)})


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>SmartHub Network</title>
<style>
body{font-family:system-ui,sans-serif;max-width:520px;margin:0 auto;padding:16px;background:#f2f4f7;color:#1d2330}
h1{font-size:1.3em;margin:4px 0}header{display:flex;align-items:center;gap:12px}header img{flex:none}section{background:#fff;border-radius:10px;padding:14px;margin:12px 0;box-shadow:0 1px 3px #0002}
h2{font-size:1.05em;margin:0 0 8px}label{display:block;margin:10px 0 3px;font-size:.9em;color:#555}
input,select,button{width:100%;padding:10px;font-size:1em;box-sizing:border-box;border:1px solid #c5cad3;border-radius:6px}
button{background:#0a84c6;color:#fff;border:0;margin-top:12px;cursor:pointer}button.off{background:#c0392b}
.static{display:none}.row{font-family:monospace;font-size:.9em;margin:3px 0}.note{font-size:.8em;color:#777}
.ctr{padding:8px 0;border-bottom:1px solid #eee}.ctr:last-child{border:0}
.badge{font-size:.75em;padding:2px 8px;border-radius:10px;color:#fff;margin-left:6px;text-transform:uppercase}
.badge.run{background:#2e9e5b}.badge.stop{background:#8a919c}.badge.err{background:#c0392b}
.btns button{width:auto;padding:6px 14px;margin:6px 6px 0 0}
#msg{position:sticky;top:0;padding:10px;border-radius:6px;display:none}#msg.ok{display:block;background:#d8f3dc}#msg.err{display:block;background:#fde2e1}
</style></head><body>
<header><img src="/logo.webp" alt="Cytron Technologies" width="140" height="70"><h1>SmartHub CM5 Network</h1></header><div id="msg"></div>
<section><h2>Status</h2><div id="status">Loading...</div></section>
<section><h2>Docker</h2><div id="docker">Loading...</div></section>
<section id="eth"><h2>Ethernet (eth0)</h2><div class="ipf"></div><button onclick="send('eth')">Apply</button></section>
<section id="wifi"><h2>WiFi</h2><button onclick="scan()">Search WiFi</button>
<label>Network</label><select name="ssid"><option value="">- press Search -</option></select>
<label>Password (blank for open network)</label><input name="password" type="password">
<div class="ipf"></div><button onclick="send('wifi')">Connect</button>
<p class="note">Connecting turns the AP off (single radio). If it fails, the AP comes back.</p></section>
<section id="ap"><h2>Access Point</h2><label>Name (SSID)</label><input name="ssid">
<label>Password (8-63 chars)</label><input name="password" type="password">
<button onclick="send('ap')">Save</button><button class="off" onclick="post('ap/off',{})">Turn off AP</button></section>
<footer class="note" style="text-align:center">SmartHub Network Dashboard V1.4</footer>
<script>
const $=s=>document.querySelector(s);
document.querySelectorAll('.ipf').forEach(e=>e.innerHTML=`<label>IP mode</label>
<select name="mode" onchange="this.parentNode.querySelector('.static').style.display=this.value=='static'?'block':'none'">
<option value="dhcp">Dynamic (DHCP)</option><option value="static">Static</option></select>
<div class="static"><label>IP address / prefix</label><input name="ip" placeholder="192.168.1.50/24">
<label>Gateway</label><input name="gateway" placeholder="192.168.1.1">
<label>DNS</label><input name="dns" placeholder="8.8.8.8, 1.1.1.1"></div>`);
function msg(t,err){const m=$('#msg');m.textContent=t;m.className=err?'err':'ok'}
async function post(path,data){
  try{const r=await fetch('/api/'+path,{method:'POST',body:JSON.stringify(data)});const j=await r.json();
    if(r.ok){msg('Applied');setTimeout(load,2000)}else msg(j.error,1)}
  catch(e){msg('Connection lost - SmartHub is switching network',1)}}
function send(id){const d={};document.querySelectorAll('#'+id+' [name]').forEach(e=>d[e.name]=e.value);post(id,d)}
async function scan(){msg('Searching...');
  const l=await (await fetch('/api/scan')).json();
  $('#wifi [name=ssid]').replaceChildren(...l.map(w=>new Option(w.ssid+'  ('+w.signal+'%, '+(w.security||'open')+')',w.ssid)));
  msg(l.length+' networks found')}
async function load(){const s=await (await fetch('/api/status')).json();
  $('#status').replaceChildren(...s.devices.map(d=>{const p=document.createElement('div');p.className='row';
    p.textContent=d.device+': '+d.state+' '+d.connection+' '+d.ip;return p}));
  if(!$('#ap [name=ssid]').value)$('#ap [name=ssid]').value=s.ap_ssid;
  const note=t=>{const p=document.createElement('div');p.className='note';p.textContent=t;return p};
  $('#docker').replaceChildren(...(s.docker_error?[note(s.docker_error)]:s.containers.length?s.containers.map(crow):[note('No containers')]))}
function crow(c){  // running = green, clean exit = grey, crash/restart loop/dead = red
  const r=document.createElement('div');r.className='ctr';
  const cls=c.state=='running'?'run':(c.state=='created'||/Exited \\(0\\)/.test(c.status))?'stop':'err';
  r.innerHTML=`<b></b><span class="badge ${cls}"></span><div class="note"></div><div class="btns"></div>`;
  r.querySelector('b').textContent=c.name;r.querySelector('.badge').textContent=cls=='err'?'error':c.state;
  r.querySelector('.note').textContent=c.status;
  for(const a of c.state=='running'?['restart','stop']:['start','restart']){
    const b=document.createElement('button');b.textContent=a[0].toUpperCase()+a.slice(1);
    if(a=='stop')b.className='off';b.onclick=()=>dock(c.name,a);r.querySelector('.btns').append(b)}
  return r}
async function dock(name,action){
  if(action=='stop'&&!confirm('Stop '+name+'?'))return;
  msg(action+' '+name+'... please wait');await post('docker',{name,action})}
load();setInterval(load,10000);
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
    for bad in ({"action": "rm", "name": "x"}, {"action": "exec"}):  # only start/stop/restart reach docker
        try:
            set_docker(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    print("selftest ok")


def main():
    global buzzer
    from gpiozero import LED, Buzzer
    buzzer = Buzzer(19)                    # active buzzer, beeps when high
    threading.Thread(target=led_loop, args=(LED(20),), daemon=True).start()  # LED0 flashes while AP on
    threading.Thread(target=button_loop, args=(toggle,), daemon=True).start()  # USER button pulls GPIO4 to GND
    print("SmartHub AP dashboard running - hold USER button 5 s to toggle AP", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    selftest() if sys.argv[1:] == ["test"] else main()
