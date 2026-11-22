"""Board sign-in: one admin password (scrypt), or a trusted header set by an auth proxy."""

import base64
import hashlib
import hmac
import secrets
import time

_N, _R, _P = 2**14, 8, 1


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def check_password(password, stored):
    try:
        scheme, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=_N, r=_R, p=_P)
    return hmac.compare_digest(got, base64.b64decode(digest))


IDLE_TTL = 12 * 3600
ABSOLUTE_TTL = 7 * 86400


class Sessions:
    """Signed-in browsers: cookie value -> CSRF token, with idle and absolute expiry.

    Kept in memory: after a restart everyone signs in again.
    """

    def __init__(self):
        self._by_sid = {}

    def create(self):
        sid = secrets.token_urlsafe(32)
        now = time.time()
        self._by_sid[sid] = {"csrf": secrets.token_urlsafe(24), "created": now, "seen": now}
        return sid

    def csrf(self, sid, now=None, touch=True):
        entry = self._by_sid.get(sid) if sid else None
        if entry is None:
            return None
        now = now or time.time()
        if now - entry["seen"] > IDLE_TTL or now - entry["created"] > ABSOLUTE_TTL:
            self._by_sid.pop(sid, None)
            return None
        if touch:  # an open live stream alone must not keep a session alive
            entry["seen"] = now
        return entry["csrf"]

    def drop(self, sid):
        self._by_sid.pop(sid, None)


def proxy_csrf(secret, user):
    """A stable CSRF token for a user signed in by the auth proxy."""
    return hmac.new(secret, user.encode(), hashlib.sha256).hexdigest()
