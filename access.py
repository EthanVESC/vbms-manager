"""Who is asking: the computer running the tool, a paired device, or a LAN viewer.

  local    - request from this computer (loopback). Full control, no pairing.
  paired   - another device that entered the pairing code once. Full control.
  viewer   - another device on the LAN. Can watch, can't change anything.
  internet - a client outside your networks (a forwarded port). Gets nothing but the
             key-authenticated API (/api/v1) unless Preferences allows viewing from the
             internet; then it's treated like a LAN device.
  reject   - Host header isn't something this server answers to (DNS rebinding).

A request that came through a proxy (X-Forwarded-For, Forwarded, X-Real-IP) is judged by
the address the proxy reports and is never "local": a reverse proxy on this computer would
otherwise hand everyone on the internet full control.

API keys (ApiKeys) are separate from all of this: /api/v1 always needs one, even from here.

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
PAIR_ROTATE = 10        # wrong codes (from anyone) before the code is replaced with a new one
MAX_TRACKED = 5000      # rate-limit table size; oldest addresses are dropped past this


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


_CGNAT = ipaddress.ip_network("100.64.0.0/10")   # carrier-grade NAT, also Tailscale


def is_lan(ip):
    """True for addresses that can only be on your own networks (or this computer)."""
    if ip is None:
        return False
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local or
                (ip.version == 4 and ip in _CGNAT))


def forwarded_for(headers):
    """The client address a proxy reports, or None if the request didn't come through one.
    headers: anything with .get(). The proxy nearest us appends last, so the last entry is used."""
    xff = headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[-1].strip() or "?"
    fwd = headers.get("Forwarded")
    if fwd:
        last = fwd.split(",")[-1]
        for part in last.split(";"):
            k, _, v = part.strip().partition("=")
            if k.lower() == "for":
                v = v.strip().strip('"')
                if v.startswith("["):
                    v = v[1:v.find("]")] if "]" in v else v
                elif v.count(":") == 1:
                    v = v.split(":")[0]
                return v or "?"
        return "?"
    real = headers.get("X-Real-IP")
    if real:
        return real.strip() or "?"
    return None


def client_ip(peer_ip, headers):
    """(address to judge the request by, came_through_proxy)."""
    fwd = forwarded_for(headers) if headers is not None else None
    if fwd is None:
        return peer_ip, False
    if not is_lan(_ip(peer_ip)):
        return peer_ip, True     # forwarded headers from an internet client prove nothing; judge the peer
    return fwd, True


def classify(peer_ip, host_header, cookie_token, sessions, names=None, headers=None, internet_ui=False,
             lan_untrusted=False):
    """sessions: object with .valid(token) -> bool. headers: the request headers (proxy detection).
    lan_untrusted: treat every non-loopback client as "internet" (Preferences; for Docker port mapping or a
    router that rewrites source addresses, where internet clients arrive with a private address)."""
    names = own_names() if names is None else names
    host = _strip_port(host_header)
    addr, proxied = client_ip(peer_ip, headers)
    peer = _ip(addr)
    host_ip = _ip(host)
    if peer is not None and peer.is_loopback and not proxied:
        if host in ("localhost",) or (host_ip is not None and host_ip.is_loopback):
            return "local"
        return "reject"
    if (lan_untrusted or not is_lan(peer)) and not internet_ui:
        return "internet"
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


def _prune(table, now, window=60):
    """Drop attempts older than the window, and the oldest addresses if the table grows too big."""
    for k in list(table):
        table[k] = [t for t in table[k] if now - t < window]
        if not table[k]:
            del table[k]
    if len(table) > MAX_TRACKED:
        for k in sorted(table, key=lambda k: table[k][-1])[:len(table) - MAX_TRACKED]:
            del table[k]


def _hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Pairing:
    """Per-run pairing code plus the persistent list of paired devices (hashed tokens)."""

    def __init__(self, store):
        self.store = store
        self.code = self._new_code()
        self._tries = {}
        self._wrong = 0
        self._lock = threading.Lock()

    @staticmethod
    def _new_code():
        return "%06d" % secrets.randbelow(10 ** 6)

    def _devices(self):
        return [dict(d) for d in self.store.settings().get("paired_devices") or []]

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
            _prune(self._tries, now)
            mine = self._tries.setdefault(ip, [])
            if len(mine) >= PAIR_TRIES:
                return False
            mine.append(now)
            return True

    def pair(self, ip, code, label):
        """Returns a new device token, or raises ValueError."""
        if not self._rate_ok(ip):
            raise ValueError("too many attempts; wait a minute and try again")
        if not secrets.compare_digest(str(code or "").strip(), self.code):
            with self._lock:
                self._wrong += 1
                if self._wrong >= PAIR_ROTATE:   # guessing from many addresses: the old code is worthless now
                    self.code, self._wrong = self._new_code(), 0
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


# -- API keys -------------------------------------------------------------------

SCOPES = ("read", "control", "settings")
KEY_FAILS = 10          # failed key attempts per client IP per minute (no global cap: keys are 256-bit, and a
                        # global cap would let one client lock everyone out)
KEY_PREFIX = "vbms_"


class ApiKeys:
    """Keys for /api/v1. Only a SHA-256 of each key is stored (settings["api_keys"]); the key itself is
    shown once, when it's made. Each key has scopes (read / control / settings) and an extra "danger" tick."""

    def __init__(self, store):
        self.store = store
        self._fails = {}
        self._used = {}        # id -> last time last_used was saved (saved at most once a minute)
        self._lock = threading.Lock()

    def _keys(self):
        return [dict(k) for k in self.store.settings().get("api_keys") or []]   # copies: never edit the store's own

    @staticmethod
    def public(k):
        return {"id": k["id"], "label": k.get("label") or "Key", "scopes": list(k.get("scopes") or []),
                "danger": bool(k.get("danger")), "created": k.get("created"), "last_used": k.get("last_used"),
                "last_ip": k.get("last_ip")}

    def list(self):
        return [self.public(k) for k in self._keys()]

    def create(self, label, scopes, danger=False):
        scopes = [s for s in SCOPES if s in (scopes or [])]
        if not scopes:
            raise ValueError("pick at least one scope")
        if danger and "settings" not in scopes:
            raise ValueError("danger settings need the settings scope too")
        label = str(label or "").strip()[:40] or "Key"
        token = KEY_PREFIX + secrets.token_urlsafe(32)
        rec = {"id": secrets.token_hex(4), "hash": _hash(token), "label": label, "scopes": scopes,
               "danger": bool(danger), "created": time.strftime("%Y-%m-%d %H:%M")}
        self.store.update_settings(api_keys=self._keys() + [rec])
        return token, self.public(rec)

    def revoke(self, key_id):
        keys = self._keys()
        keep = [k for k in keys if k["id"] != key_id]
        if len(keep) == len(keys):
            raise ValueError("unknown key")
        self.store.update_settings(api_keys=keep)

    def blocked(self, ip):
        now = time.time()
        with self._lock:
            _prune(self._fails, now)
            return len(self._fails.get(ip, ())) >= KEY_FAILS

    def check(self, token, ip):
        """The key's record (without its hash) if the token is valid, else None (and counts a failure)."""
        hit = None
        if isinstance(token, str) and token:
            h = _hash(token)
            for k in self._keys():
                if secrets.compare_digest(k["hash"], h):
                    hit = k
        if hit is None:
            with self._lock:
                self._fails.setdefault(ip, []).append(time.time())
            return None
        now = time.time()
        if now - self._used.get(hit["id"], 0) > 60 or hit.get("last_ip") != ip:
            self._used[hit["id"]] = now
            keys = self._keys()
            for k in keys:
                if k["id"] == hit["id"]:
                    k.update(last_used=time.strftime("%Y-%m-%d %H:%M:%S"), last_ip=ip)
            self.store.update_settings(api_keys=keys)
        return self.public(hit)
