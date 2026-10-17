"""Outbound HTTP that cannot be pointed at ourselves.

Webhooks hand us URLs chosen by the other side (Wazzup media, Instagram
attachments, browser push endpoints). Before fetching we insist on https, a
public IP (every hop — no redirects) and, where the provider is known, its own
CDN hosts; the body is read with a hard cap. Anything else is refused."""
import ipaddress
import logging
import socket
from fnmatch import fnmatch
from urllib.parse import urlparse

import requests

logger = logging.getLogger("nova.netsafe")

PUSH_HOSTS = ["fcm.googleapis.com", "*.push.apple.com", "updates.push.services.mozilla.com", "*.notify.windows.com",
              "android.googleapis.com", "*.push.samsungosp.com", "push.opera.com"]
META_HOSTS = ["*.fbcdn.net", "*.cdninstagram.com", "lookaside.fbsbx.com", "*.facebook.com", "*.whatsapp.net", "scontent*.xx.fbcdn.net"]
WAZZUP_HOSTS = ["*.wazzup24.com", "*.wazzup24.ru", "store.wazzup24.com", "*.amazonaws.com", "*.storage.yandexcloud.net", "*.selstorage.ru", "*.cloudfront.net"]


class UnsafeURL(ValueError):
    pass


def _public_ip(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_reserved or a.is_unspecified)


def check_url(url: str, allowed_hosts: list[str] | None = None) -> str:
    """Return the hostname when the URL may be fetched, raise UnsafeURL otherwise."""
    p = urlparse(url or "")
    if p.scheme != "https":
        raise UnsafeURL("only https")
    host = (p.hostname or "").lower()
    if not host or p.username or p.password:
        raise UnsafeURL("bad host")
    if allowed_hosts and not any(fnmatch(host, pat) for pat in allowed_hosts):
        raise UnsafeURL(f"host {host} not allowed")
    try:
        infos = socket.getaddrinfo(host, p.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeURL(f"dns: {exc}") from exc
    ips = {i[4][0] for i in infos}
    if not ips or not all(_public_ip(ip) for ip in ips):
        raise UnsafeURL(f"{host} resolves to a private address")
    return host


def safe_get(url: str, allowed_hosts: list[str] | None = None, max_bytes: int = 30 * 1024 * 1024,
             timeout: int = 60, headers: dict | None = None) -> tuple[bytes, str]:
    """GET with the checks above. Returns (body, content-type). Redirects are
    not followed (the target would skip the checks)."""
    check_url(url, allowed_hosts)
    with requests.get(url, timeout=timeout, stream=True, allow_redirects=False, headers=headers or {}) as r:
        if r.status_code in (301, 302, 303, 307, 308):
            raise UnsafeURL("redirects are not followed")
        if r.status_code != 200:
            raise UnsafeURL(f"http {r.status_code}")
        if int(r.headers.get("content-length") or 0) > max_bytes:
            raise UnsafeURL("too large")
        buf = bytearray()
        for chunk in r.iter_content(64 * 1024):
            buf += chunk
            if len(buf) > max_bytes:
                raise UnsafeURL("too large")
        return bytes(buf), (r.headers.get("Content-Type") or "").split(";")[0].strip()
