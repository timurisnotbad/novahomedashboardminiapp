"""Web Push for the CRM (phones and computers): RFC 8291 payload encryption and
RFC 8292 VAPID, built on `cryptography` only — no extra wheels to build on Windows.

Subscriptions are per CRM user (crm_push_subs); the VAPID key pair is created
once in data/vapid.pem. iPhone: the CRM must be added to the Home Screen first.
"""
import base64
import json
import logging
import os
import struct
import time
from datetime import datetime
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import config, database

logger = logging.getLogger("nova.push")

SCHEMA = """
CREATE TABLE IF NOT EXISTS crm_push_subs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid INTEGER NOT NULL,
    endpoint TEXT UNIQUE,
    p256dh TEXT,
    auth TEXT,
    ua TEXT,
    created_at TEXT,
    last_ok TEXT,
    fails INTEGER DEFAULT 0
);
"""
_key = None


def init_db() -> None:
    with database.get_conn() as conn:
        conn.executescript(SCHEMA)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    s = s.strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _private_key():
    global _key
    if _key is not None:
        return _key
    path = config.DATA_DIR / "vapid.pem"
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    else:
        _key = ec.generate_private_key(ec.SECP256R1())
        path.write_bytes(_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return _key


def public_key() -> str:
    """Base64url uncompressed P-256 point — what the browser needs as applicationServerKey."""
    pub = _private_key().public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return _b64(pub)


def _vapid_header(endpoint: str) -> str:
    u = urlparse(endpoint)
    aud = f"{u.scheme}://{u.netloc}"
    header = _b64(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
    claims = _b64(json.dumps({"aud": aud, "exp": int(time.time()) + 12 * 3600, "sub": f"mailto:{config.SMTP_USER or 'admin@novahome.uz'}"}).encode())
    signing = f"{header}.{claims}".encode()
    der = _private_key().sign(signing, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    sig = _b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={header}.{claims}.{sig}, k={public_key()}"


def _encrypt(payload: bytes, p256dh: str, auth: str) -> bytes:
    """RFC 8291 aes128gcm: one record, the delimiter 0x02 marks the last record."""
    ua_pub_raw = _unb64(p256dh)
    ua_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub_raw)
    as_key = ec.generate_private_key(ec.SECP256R1())
    as_pub_raw = as_key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = as_key.exchange(ec.ECDH(), ua_pub)
    auth_secret = _unb64(auth)
    ikm = HKDF(hashes.SHA256(), 32, auth_secret, b"WebPush: info\x00" + ua_pub_raw + as_pub_raw).derive(shared)
    salt = os.urandom(16)
    cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack("!I", 4096) + bytes([len(as_pub_raw)]) + as_pub_raw + body


def send_one(sub: dict, data: dict, ttl: int = 3600) -> bool:
    """Deliver one notification; a dead subscription (404/410) is removed."""
    try:
        body = _encrypt(json.dumps(data, ensure_ascii=False).encode(), sub["p256dh"], sub["auth"])
        resp = requests.post(sub["endpoint"], data=body, timeout=15, headers={
            "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream", "TTL": str(ttl),
            "Urgency": "high", "Authorization": _vapid_header(sub["endpoint"])})
        ok = resp.status_code in (200, 201, 202)
        with database.get_conn() as conn:
            if resp.status_code in (404, 410):
                conn.execute("DELETE FROM crm_push_subs WHERE id = ?", (sub["id"],))
            elif ok:
                conn.execute("UPDATE crm_push_subs SET last_ok = ?, fails = 0 WHERE id = ?", (datetime.now().isoformat(timespec="seconds"), sub["id"]))
            else:
                conn.execute("UPDATE crm_push_subs SET fails = fails + 1 WHERE id = ?", (sub["id"],))
                logger.warning("push %s → %s %s", sub["endpoint"][:60], resp.status_code, resp.text[:120])
        return ok
    except Exception as exc:  # noqa: BLE001
        logger.warning("push failed: %s", exc)
        return False


def subscribe(uid: int, sub: dict, ua: str = "") -> int:
    keys = sub.get("keys") or {}
    if not sub.get("endpoint") or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("Подписка браузера неполная")
    with database.get_conn() as conn:
        conn.execute("INSERT INTO crm_push_subs (uid, endpoint, p256dh, auth, ua, created_at) VALUES (?, ?, ?, ?, ?, ?) "
                     "ON CONFLICT(endpoint) DO UPDATE SET uid = excluded.uid, p256dh = excluded.p256dh, auth = excluded.auth, ua = excluded.ua, fails = 0",
                     (uid, sub["endpoint"], keys["p256dh"], keys["auth"], (ua or "")[:200], datetime.now().isoformat(timespec="seconds")))
        return conn.execute("SELECT COUNT(*) FROM crm_push_subs WHERE uid = ?", (uid,)).fetchone()[0]


def unsubscribe(uid: int, endpoint: str) -> None:
    with database.get_conn() as conn:
        conn.execute("DELETE FROM crm_push_subs WHERE uid = ? AND endpoint = ?", (uid, endpoint))


def subs_for(uids: list[int] | None = None) -> list[dict]:
    with database.get_conn() as conn:
        if uids is None:
            rows = conn.execute("SELECT * FROM crm_push_subs").fetchall()
        elif not uids:
            return []
        else:
            rows = conn.execute(f"SELECT * FROM crm_push_subs WHERE uid IN ({','.join('?' * len(uids))})", uids).fetchall()
    return [dict(r) for r in rows]


def send_to(uids: list[int] | None, title: str, body: str, url: str = "/crm/", tag: str = "") -> int:
    n = 0
    for s in subs_for(uids):
        if send_one(s, {"title": title, "body": body, "url": url, "tag": tag}):
            n += 1
    return n


def status(uid: int) -> dict:
    with database.get_conn() as conn:
        mine = conn.execute("SELECT COUNT(*) FROM crm_push_subs WHERE uid = ?", (uid,)).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM crm_push_subs").fetchone()[0]
    return {"key": public_key(), "mine": mine, "total": total}
