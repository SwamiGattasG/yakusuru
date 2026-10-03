"""Fast reachability check before a big download, so a dead connection fails in seconds with a
clear reason instead of hanging for minutes inside a library's TCP connect.

Also works around the most common silent hang: a network that advertises IPv6 but doesn't route it.
Each IPv6 address then waits for the OS TCP timeout (≈75 s on macOS) before IPv4 is tried. When
IPv4 works and IPv6 doesn't, the worker is switched to IPv4-only for the rest of its life."""
from __future__ import annotations

import logging
import socket
import time
from urllib.parse import urlparse

log = logging.getLogger(__name__)

_ipv4_only = False
_orig_getaddrinfo = socket.getaddrinfo


def _v4_getaddrinfo(host, port, family=0, *args, **kw):
    res = _orig_getaddrinfo(host, port, family, *args, **kw)
    v4 = [r for r in res if r[0] == socket.AF_INET]
    return v4 or res


def prefer_ipv4() -> None:
    global _ipv4_only
    if not _ipv4_only:
        socket.getaddrinfo = _v4_getaddrinfo
        _ipv4_only = True


def _try(addr, timeout: float) -> tuple[bool, str]:
    fam, typ, proto, _, sa = addr
    s = socket.socket(fam, typ, proto)
    s.settimeout(timeout)
    t0 = time.time()
    try:
        s.connect(sa)
        return True, f"{sa[0]} ok ({(time.time() - t0) * 1000:.0f} ms)"
    except OSError as e:
        return False, f"{sa[0]}: {e.strerror or type(e).__name__}"
    finally:
        s.close()


def check(url_or_host: str, port: int = 443, timeout: float = 6.0) -> tuple[bool, str]:
    """(reachable, explanation). Tries IPv4 and IPv6 separately with a short timeout each."""
    import os
    host = urlparse(url_or_host).hostname if "://" in url_or_host else url_or_host
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or os.environ.get("ALL_PROXY")
    if proxy and "://" in proxy:                 # traffic goes via the proxy: check that instead
        pu = urlparse(proxy)
        host, port = pu.hostname or host, pu.port or port
    try:
        addrs = _orig_getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except socket.gaierror as e:
        return False, (f"Can't look up {host} ({e.strerror or e}). You seem to be offline, or DNS is blocked "
                       "(VPN, firewall, captive Wi-Fi portal).")
    v4 = [a for a in addrs if a[0] == socket.AF_INET][:2]
    v6 = [a for a in addrs if a[0] == socket.AF_INET6][:2]
    ok4, ok6, notes = False, False, []
    for a in v4:
        ok, note = _try(a, timeout)
        notes.append(note)
        if ok:
            ok4 = True
            break
    for a in v6:
        ok, note = _try(a, timeout / 2)
        notes.append(note)
        if ok:
            ok6 = True
            break
    detail = "; ".join(notes)
    if ok4 and v6 and not ok6:
        prefer_ipv4()
        log.info("IPv6 to %s doesn't work on this network — using IPv4 (%s).", host, detail)
        return True, detail
    if ok4 or ok6:
        return True, detail
    return False, (f"Can't connect to {host} — nothing answered within {timeout:.0f} s ({detail}). "
                   "Check your internet connection. A firewall app (Little Snitch, LuLu…), VPN or proxy may be "
                   "blocking Yakusuru's Python process — allow it and try again.")
