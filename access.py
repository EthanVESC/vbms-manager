"""Who is asking: the computer running the tool, a paired device, or a LAN viewer.

  local   - request from this computer (loopback). Full control, no pairing.
  paired  - another device that entered the pairing code once. Full control.
  viewer  - another device on the LAN. Can watch, can't change anything.
  reject  - Host header isn't something this server answers to (DNS rebinding).

The Host check: a DNS-rebinding attack needs a domain name that resolves to
us, so we only answer to IP literals, localhost, and this machine's own
hostname. Loopback clients must use a localhost name or a loopback IP.
"""

import hashlib
import ipaddress
import secrets
import socket
import threading
import time

COOKIE = "vbms_pair"
PAIR_TRIES = 5          # per client IP per minute
PAIR_TRIES_ALL = 20     # across all clients per minute


def _strip_port(host_header: str) -> str:
    h = (host_header or "").strip().lower()
    if h.startswith("["):
        return h[1:h.find("]")] if "]" in h else h
    if h.count(":") == 1:
        h = h.split(":")[0]
    return h.rstrip(".")


def _ip(s):
    try:
        ip = ipaddress.ip_address(s)
    except ValueError:
        return None
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip


def own_names():
    n = socket.gethostname().lower().rstrip(".")
    short = n.split(".")[0]
    return {"localhost", n, short, short + ".local"}


def lan_ips():
    """Best-effort list of this machine's LAN IPv4 addresses (for printing URLs)."""
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))  # no packet is sent; just picks the outgoing interface
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for ai in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(ai[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def classify(peer_ip, host_header, cookie_token, sessions, names=None):
    """sessions: object with .valid(token) -> bool."""
    names = own_names() if names is None else names
    host = _strip_port(host_header)
    peer = _ip(peer_ip)
    host_ip = _ip(host)
    if peer is not None and peer.is_loopback:
        if host in ("localhost",) or (host_ip is not None and host_ip.is_loopback):
            return "local"
        return "reject"
    if host_ip is None and host not in names:
        return "reject"
    if cookie_token and sessions.valid(cookie_token):
        return "paired"
    return "viewer"


def parse_cookie(header):
    out = {}
    for part in (header or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Pairing:
    """Per-run pairing code plus the persistent list of paired devices (hashed tokens)."""

    def __init__(self, store):
        self.store = store
        self.code = "%06d" % secrets.randbelow(10 ** 6)
        self._tries = {}
        self._lock = threading.Lock()

    def _devices(self):
        return list(self.store.settings().get("paired_devices") or [])

    def valid(self, token):
        h = _hash(token)
        for d in self._devices():
            if secrets.compare_digest(d["hash"], h):
                return True
        return False

    def devices(self):
        return [{"id": d["hash"][:12], "label": d.get("label") or "Device", "created": d.get("created")}
                for d in self._devices()]

    def _rate_ok(self, ip):
        now = time.time()
        with self._lock:
            for k in list(self._tries):
                self._tries[k] = [t for t in self._tries[k] if now - t < 60]
                if not self._tries[k]:
                    del self._tries[k]
            mine = self._tries.setdefault(ip, [])
            total = sum(len(v) for v in self._tries.values())
            if len(mine) >= PAIR_TRIES or total >= PAIR_TRIES_ALL:
                return False
            mine.append(now)
            return True

    def pair(self, ip, code, label):
        """Returns a new device token, or raises ValueError."""
        if not self._rate_ok(ip):
            raise ValueError("too many attempts; wait a minute and try again")
        if not secrets.compare_digest(str(code or "").strip(), self.code):
            raise ValueError("wrong pairing code")
        token = secrets.token_urlsafe(32)
        devs = self._devices()
        devs.append({"hash": _hash(token), "label": str(label or "").strip()[:40] or "Device",
                     "created": time.strftime("%Y-%m-%d %H:%M")})
        self.store.update_settings(paired_devices=devs)
        return token

    def revoke(self, dev_id):
        devs = [d for d in self._devices() if not d["hash"].startswith(str(dev_id or "-"))]
        self.store.update_settings(paired_devices=devs)
