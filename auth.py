"""Password hashing and signed session tokens for InCityJobs.

Standard library only (no Streamlit), so it can be unit-tested and reused by
scripts/hash_password.py.

- Passwords: salted PBKDF2-HMAC-SHA256, stored as
  "pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>".
- Session tokens: "<claims_b64>.<hmac_sha256_b64>", where claims carry the
  subject, role and expiry. A token is accepted only if the signature, role and
  expiry all check out.
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
from functools import lru_cache

HASH_SCHEME = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 600_000  # OWASP guidance for PBKDF2-HMAC-SHA256
MIN_PASSWORD_LENGTH = 12

ROLE_MANAGER = "manager"
ROLE_EMPLOYER = "employer"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# --------------------------------------------------------------------------- passwords
def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS, salt: bytes = None) -> str:
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{HASH_SCHEME}${iterations}${_b64e(salt)}${_b64e(digest)}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Constant-time check of a password against a stored hash. Malformed hashes fail."""
    try:
        scheme, iterations, salt_b64, digest_b64 = (stored_hash or "").split("$")
        if scheme != HASH_SCHEME:
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), _b64d(salt_b64), int(iterations))
        return hmac.compare_digest(candidate, _b64d(digest_b64))
    except (ValueError, TypeError):
        return False


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return hash_password(secrets.token_urlsafe(16))


def burn_verification_time(password: str) -> None:
    """Spend the same time as a real check, so response timing doesn't reveal
    whether an account exists."""
    verify_password(password, _dummy_hash())


def password_problem(password: str, confirm: str) -> str:
    """Return a user-facing problem with a new password, or '' if it is acceptable."""
    if len(password or "") < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if password != confirm:
        return "The two passwords don't match."
    return ""


# --------------------------------------------------------------------------- tokens
def issue_token(key: bytes, subject: str, role: str, ttl_seconds: int) -> str:
    claims = {
        "sub": str(subject),
        "role": role,
        "iat": int(time.time()),
        "exp": int(time.time()) + int(ttl_seconds),
        "nonce": secrets.token_hex(8),
    }
    body = _b64e(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = _b64e(hmac.new(key, body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{signature}"


def verify_token(key: bytes, token: str, role: str):
    """Return the claims dict if the token is authentic, unexpired and for `role`; else None."""
    if not token or not isinstance(token, str) or token.count(".") != 1:
        return None
    body, signature = token.split(".")
    expected = _b64e(hmac.new(key, body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        claims = json.loads(_b64d(body))
    except (ValueError, TypeError):
        return None
    if claims.get("role") != role or int(claims.get("exp", 0)) <= time.time():
        return None
    return claims


# --------------------------------------------------------------------------- setup codes
SETUP_CODE_TTL_HOURS = 48


def new_setup_code() -> str:
    """A random one-time code a manager hands to an existing employer (~72 bits)."""
    return secrets.token_urlsafe(9)


def hash_setup_code(code: str) -> str:
    # Codes are long and random, so a fast hash is sufficient here
    return hashlib.sha256((code or "").strip().encode("utf-8")).hexdigest()
