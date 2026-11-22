"""Per-user dashboard keys.

The API has no accounts: the Discord token *is* the login. Before this module any
caller could PATCH `/api/user/<any id>` or POST a token for somebody else's id, so
the only protection was obscurity. A dashboard key is a signed, expiring token
bound to one discord_id, issued at login and sent back as `X-Dashboard-Key`.

Stdlib only (hmac + sha256), no new dependency.
"""
import base64
import hashlib
import hmac
import os
import time


def _read_secret(name: str) -> bytes:
    """Read a required secret, refusing the placeholder that used to be here.

    The old "fallback_secret_123" default meant an unset ENGINE_SECRET still
    authenticated every engine call, so it is gone rather than merely deprecated.
    """
    value = (os.environ.get(name) or "").strip()
    if not value or value == "fallback_secret_123":
        raise RuntimeError(f"{name} must be set to a real secret")
    return value.encode()


#: the engine's own secret, accepted in place of a dashboard key on server-to-server calls
_ENGINE_SECRET = _read_secret("ENGINE_SECRET")

#: falls back to ENGINE_SECRET so a deployment only has to set one secret
_SECRET = (
    _read_secret("DASHBOARD_SECRET")
    if (os.environ.get("DASHBOARD_SECRET") or "").strip()
    else _ENGINE_SECRET
)

#: 30 days; a dashboard that has not been opened in a month logs in again
TTL = int(os.environ.get("DASHBOARD_KEY_TTL") or 60 * 60 * 24 * 30)


def _sign(body: bytes) -> str:
    return hmac.new(_SECRET, body, hashlib.sha256).hexdigest()


def issue(discord_id: str, ttl: int = None) -> str:
    """Return a key binding ``discord_id`` until ``ttl`` seconds from now."""
    expiry = int(time.time()) + int(TTL if ttl is None else ttl)
    body = f"{discord_id}:{expiry}".encode()
    encoded = base64.urlsafe_b64encode(body).decode().rstrip("=")
    return f"{encoded}.{_sign(body)}"


def matches_engine_secret(value: str) -> bool:
    """True when ``value`` is the shared engine secret. Cogs save through this."""
    if not value:
        return False
    return hmac.compare_digest(str(value).encode(), _ENGINE_SECRET)


def verify(key: str, discord_id: str) -> bool:
    """True when ``key`` was issued for this ``discord_id`` and has not expired."""
    if not key or not discord_id or "." not in key:
        return False

    encoded, _, signature = key.rpartition(".")
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        body = base64.urlsafe_b64decode(padded.encode())
        body_id, _, expiry = body.decode().rpartition(":")
        expiry = int(expiry)
    except (ValueError, UnicodeDecodeError):
        return False

    if not hmac.compare_digest(signature, _sign(body)):
        return False
    if not hmac.compare_digest(body_id, str(discord_id)):
        return False
    return expiry >= int(time.time())
