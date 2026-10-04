"""Auth security primitives (P8): password hashing (stdlib PBKDF2) + JWT (pyjwt).

Password hashing uses hashlib.pbkdf2_hmac (PBKDF2-HMAC-SHA256) with a per-user random salt, so
there is NO passlib/bcrypt dependency. Stored format: pbkdf2_sha256$<iter>$<salt_hex>$<hash_hex>.
JWT is signed HS256 with settings.auth.jwt_secret (from env, never committed).
Generic infrastructure -> committed.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import jwt

_ALGO = "SHA256"
_ITERATIONS = 600_000          # OWASP 2023 guidance for PBKDF2-HMAC-SHA256
_SALT_BYTES = 16


def hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256 + random salt -> 'pbkdf2_sha256$<iter>$<salt_hex>$<hash_hex>'."""
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(_ALGO, password.encode("utf-8"), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time compare a plaintext password against a stored PBKDF2 hash."""
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(_ALGO, password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters))
        return secrets.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError, TypeError):
        return False


def create_access_token(uid: str, secret: str, algorithm: str = "HS256", expire_minutes: int = 720) -> str:
    """Sign a JWT (sub=uid, iat, exp). `secret` MUST be configured (env) -> raise if empty."""
    if not secret:
        raise RuntimeError("AUTH_JWT_SECRET is not configured")
    now = datetime.now(timezone.utc)
    payload = {"sub": uid, "iat": now, "exp": now + timedelta(minutes=expire_minutes)}
    token = jwt.encode(payload, secret, algorithm=algorithm)
    return token.decode("utf-8") if isinstance(token, bytes) else token   # pyjwt 1.x returns bytes


def decode_token(token: str, secret: str, algorithm: str = "HS256") -> str | None:
    """Verify a JWT -> uid (sub); None if invalid/expired/unconfigured (caller turns None into 401)."""
    if not secret or not token:
        return None
    try:
        payload = jwt.decode(token, secret, algorithms=[algorithm])
        return payload.get("sub")
    except jwt.PyJWTError:
        return None
