"""Passcode auth for What the Bit.

Model:
- The passcode is a numeric PIN (>= 4 digits), stored as a scrypt hash in
  config.json. `auth_enabled` decides whether it is required at all.
- Requests from the host machine itself (loopback / localhost) are treated
  as a **superuser**: they never need the passcode and may reset it or turn
  protection on/off. This is the forgot-passcode escape hatch — you must be
  sitting at (or SSH'd into) the machine running the monitor.
- Remote LAN devices, when protection is on, must log in: a passcode check
  issues an HMAC-signed session token in an HttpOnly SameSite=Strict cookie,
  plus a CSRF token echoed on writes via the X-CSRF header. Login attempts
  are rate-limited per client IP.
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import sys
import time

from . import config

SESSION_HOURS = 24 * 7
MIN_PASSCODE_LEN = 4
_attempts: dict[str, list[float]] = {}  # ip -> recent failure timestamps
MAX_FAILURES = 5
WINDOW_SECONDS = 300


# ---------------------------------------------------------------- passcode

def valid_passcode(pin: object) -> bool:
    """A passcode must be all digits and at least MIN_PASSCODE_LEN long."""
    return isinstance(pin, str) and pin.isdigit() and len(pin) >= MIN_PASSCODE_LEN


def hash_passcode(passcode: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(passcode.encode(), salt=salt, n=2**14, r=8, p=1)
    return base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_passcode(passcode: str, stored: str | None) -> bool:
    if not stored or "$" not in stored or not isinstance(passcode, str):
        return False
    try:
        salt_b64, dk_b64 = stored.split("$", 1)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        dk = hashlib.scrypt(passcode.encode(), salt=salt, n=2**14, r=8, p=1)
        return hmac.compare_digest(dk, expected)
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------- locality

def is_local(request) -> bool:
    """True when the request came in over loopback (the host machine)."""
    client = getattr(request, "client", None)
    host = client.host if client else None
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("localhost",)


# ---------------------------------------------------------------- rate limit

def rate_limited(ip: str) -> bool:
    now = time.time()
    fails = [t for t in _attempts.get(ip, []) if now - t < WINDOW_SECONDS]
    _attempts[ip] = fails
    return len(fails) >= MAX_FAILURES


def record_failure(ip: str) -> None:
    _attempts.setdefault(ip, []).append(time.time())


def clear_failures(ip: str) -> None:
    _attempts.pop(ip, None)


# ---------------------------------------------------------------- sessions

def _sign(payload: str, secret: str) -> str:
    mac = hmac.new(secret.encode(), payload.encode(), hashlib.sha256)
    return base64.urlsafe_b64encode(mac.digest()).decode().rstrip("=")


def issue_session() -> tuple[str, str]:
    """Returns (session_token, csrf_token)."""
    secret = config.load()["session_secret"]
    csrf = secrets.token_urlsafe(24)
    payload = json.dumps({"exp": time.time() + SESSION_HOURS * 3600,
                          "csrf": csrf}, separators=(",", ":"))
    body = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{body}.{_sign(body, secret)}", csrf


def validate_session(token: str | None, csrf_header: str | None,
                     require_csrf: bool = True) -> bool:
    if not token or "." not in token:
        return False
    secret = config.load()["session_secret"]
    body, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(body, secret)):
        return False
    try:
        pad = "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(body + pad))
    except (ValueError, TypeError):
        return False
    if payload.get("exp", 0) < time.time():
        return False
    if require_csrf and not (csrf_header
                             and hmac.compare_digest(csrf_header,
                                                     payload.get("csrf", ""))):
        return False
    return True


# ---------------------------------------------------------------- CLI

def _cli() -> None:
    """python -m app.auth set-passcode [PIN]  — sets and enables the passcode.
    Run on the host machine (e.g. to recover a forgotten passcode)."""
    if len(sys.argv) >= 2 and sys.argv[1] in ("set-passcode", "set-password"):
        import getpass
        pin = sys.argv[2] if len(sys.argv) > 2 else getpass.getpass(
            "New passcode (>= 4 digits): ")
        if not valid_passcode(pin):
            print("Passcode must be at least 4 digits (numbers only).")
            sys.exit(1)
        config.update(password_hash=hash_passcode(pin), auth_enabled=True)
        print("Passcode set and protection enabled.")
    elif len(sys.argv) >= 2 and sys.argv[1] == "disable":
        config.update(auth_enabled=False)
        print("Passcode protection disabled.")
    else:
        print("Usage: python -m app.auth set-passcode [PIN]")
        print("       python -m app.auth disable")
        sys.exit(1)


if __name__ == "__main__":
    _cli()
