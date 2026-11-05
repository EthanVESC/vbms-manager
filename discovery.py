"""Find boards on the local network.

Two ways, results merged:
  - listen for VESC Express WiFi discovery broadcasts (UDP 65109, "name::ip::port")
  - scan this computer's local networks (up to a /24 each) for VESC's TCP port (65102) and ask anything that answers for its
    firmware version (COMM_FW_VERSION, read-only). Broadcasts often don't arrive (VESC Tool or another
    copy of this tool can hold the UDP port), so the scan is what normally finds boards.

The scan skips boards that are already added, connects to each candidate for well under a second and
disconnects again. A board only joins WiFi while it's awake (for example on the charger).
"""

import ipaddress
import re
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from vesc.client import PORT, BoardBusy, VescClient, VescError

DISCOVERY_PORT = 65109
SCAN_INTERVAL_S = 120.0
FOUND_TTL_S = 600.0       # forget a found board this long after it was last seen
CONNECT_TIMEOUT_S = 0.5
WORKERS = 64


def _usable(ip):
    a = ipaddress.ip_address(ip)
    return a.is_private and not a.is_loopback and not a.is_link_local


def lan_networks():
    """This computer's local networks as IPv4Interface objects (address + prefix).

    Only broadcast-capable interfaces count, so VPN tunnels (point-to-point, often holding the default
    route) aren't scanned. Read from `ifconfig` or `ip addr` where available; otherwise every address
    this computer's name resolves to, assumed to be a /24.
    """
    nets = []
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    for block in re.split(r"\n(?=\S)", out):
        head = block.split("\n", 1)[0]
        if "BROADCAST" not in head.upper() or "POINTOPOINT" in head.upper():
            continue
        for m in re.finditer(r"inet (?:addr:)?(\d+\.\d+\.\d+\.\d+).*?(?:netmask|Mask:)\s*(0x[0-9a-f]{8}|[\d.]+)", block):
            mask = m.group(2)
            if mask.startswith("0x"):
                mask = str(ipaddress.ip_address(int(mask, 16)))
            nets.append(ipaddress.ip_interface("%s/%s" % (m.group(1), mask)))
    if not out:
        try:
            out = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        nets += [ipaddress.ip_interface(m.group(1)) for m in re.finditer(r"inet (\S+/\d+) brd ", out)]
    if not out:
        try:
            ips = {ai[4][0] for ai in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
        except OSError:
            ips = set()
        nets += [ipaddress.ip_interface(ip + "/24") for ip in sorted(ips)]
    seen, res = set(), []
    for n in nets:
        if _usable(str(n.ip)) and n.ip not in seen:
            seen.add(n.ip)
            res.append(n)
    return res[:4]


def scan_targets():
    """[(ip, port)] to probe, and a label for the UI. Networks bigger than a /24 are only scanned in the
    /24 around this computer's address."""
    targets, labels = [], []
    for n in lan_networks():
        net = n.network if n.network.prefixlen >= 24 else ipaddress.ip_network("%s/24" % n.ip, strict=False)
        labels.append(str(net))
        targets += [(str(h), PORT) for h in net.hosts() if h != n.ip]
    return targets, ", ".join(labels) or None


def probe(ip, port):
    """Identify a VESC device at ip:port, or None if nothing answers there."""
    # One connection only: the firmware serves a single TCP client and is slow to free the slot, so a
    # quick "is the port open" connect first would make the real one look busy.
    c = VescClient(ip, port, timeout=2.0, connect_timeout=CONNECT_TIMEOUT_S)
    try:
        fw = c.connect()
        return {"hw_name": fw["hw_name"], "mac": fw["mac"], "version": fw["version"], "busy": False}
    except BoardBusy:
        return {"hw_name": None, "mac": None, "version": None, "busy": True}
    except (VescError, ValueError, IndexError):
        return None
    finally:
        c.close()


class Discovery:
    def __init__(self, known=lambda: (set(), set()), names=lambda mac: None, targets=scan_targets):
        """known() -> (hosts already added, their MACs); names(mac) -> nickname from an earlier setup."""
        self.known = known
        self.names = names
        self.targets = targets
        self.found = {}  # host -> entry
        self.error = ""
        self.scanning = False
        self.subnet = None
        self.last_scan = None
        self._lock = threading.Lock()
        self._wake = threading.Event()

    def start(self):
        threading.Thread(target=self._listen, name="discovery-udp", daemon=True).start()
        threading.Thread(target=self._scan_loop, name="discovery-scan", daemon=True).start()

    def scan_now(self):
        self._wake.set()

    # -- UDP broadcasts

    def _listen(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            s.bind(("", DISCOVERY_PORT))
        except OSError:
            return  # the scan still finds boards
        while True:
            try:
                data, addr = s.recvfrom(512)
            except OSError:
                time.sleep(1)
                continue
            parts = data.split(b"\x00")[0].decode("utf-8", errors="replace").split("::")
            if len(parts) != 3:
                continue
            name, ip, port = parts
            try:
                port = int(port)
            except ValueError:
                continue
            host = ip if port == PORT else "%s:%d" % (ip, port)
            with self._lock:
                e = self.found.setdefault(host, {"host": host, "ip": ip, "port": port, "mac": None,
                                                 "hw_name": None, "busy": False})
                e.update(name=name, last_seen=time.time())

    # -- TCP scan

    def _scan_loop(self):
        while True:
            try:
                self.scan()
            except Exception as e:  # never let a scan error stop discovery
                self.error = "Network search failed (%s: %s). Add boards by IP instead." % (type(e).__name__, e)
            self._wake.wait(SCAN_INTERVAL_S)
            self._wake.clear()

    def scan(self):
        targets, self.subnet = self.targets()
        if not targets:
            self.error = "Not connected to a local network, so boards can't be searched for. Add them by IP."
            return
        hosts, _ = self.known()
        todo = [(ip, port) for ip, port in targets if (ip if port == PORT else "%s:%d" % (ip, port)) not in hosts]
        self.scanning = True
        try:
            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                results = list(ex.map(lambda t: (t, probe(*t)), todo))
        finally:
            self.scanning = False
        now = time.time()
        with self._lock:
            for (ip, port), r in results:
                if r is None:
                    continue
                host = ip if port == PORT else "%s:%d" % (ip, port)
                e = self.found.setdefault(host, {"host": host, "ip": ip, "port": port, "name": None})
                if r["busy"] and e.get("mac"):
                    r = dict(r, hw_name=e["hw_name"], mac=e["mac"], version=e.get("version"))
                e.update(r, last_seen=now)
        self.last_scan = now
        self.error = ""

    def list(self):
        hosts, macs = self.known()
        now = time.time()
        out = []
        with self._lock:
            for e in self.found.values():
                if now - e["last_seen"] > FOUND_TTL_S or e["host"] in hosts or (e.get("mac") and e["mac"] in macs):
                    continue
                d = dict(e, age=round(now - e["last_seen"], 1))
                d["nickname"] = self.names(e["mac"]) if e.get("mac") else None
                out.append(d)
        return sorted(out, key=lambda d: (0, int(ipaddress.ip_address(d["ip"])), d["port"]) if _is_ip(d["ip"]) else (1, d["ip"], d["port"]))

    def status(self):
        return {"scanning": self.scanning, "subnet": self.subnet,
                "last_scan_age": round(time.time() - self.last_scan) if self.last_scan else None}


def _is_ip(s):
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False
