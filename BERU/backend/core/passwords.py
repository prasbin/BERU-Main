"""Password hashing (PBKDF2-HMAC-SHA256) with no third-party dependency.

BERU stores per-user passwords as salted PBKDF2 digests, never in plaintext.
The stored representation embeds its own parameters so future strengthening
(iteration count) does not invalidate existing hashes::

    pbkdf2_sha256$<iterations>$<salt_hex>$<digest_hex>

``verify_password`` uses ``secrets.compare_digest`` so failures do not leak
through timing. Unknown/malformed stored values simply fail verification.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_ALG = "pbkdf2_sha256"
_DEFAULT_ITERATIONS = 120_000
_SALT_BYTES = 16
_KEY_LENGTH = 32


def hash_password(password: str, *, iterations: int = _DEFAULT_ITERATIONS) -> str:
    """Hash ``password`` and return the portable ``pbkdf2_sha256$...`` string."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations, dklen=_KEY_LENGTH
    )
    return f"{_ALG}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    """Return True when ``password`` matches the stored PBKDF2 hash."""
    if not stored:
        return False
    try:
        alg, iterations, salt_hex, digest_hex = stored.split("$", 3)
        if alg != _ALG:
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations),
            dklen=_KEY_LENGTH,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual.hex(), digest_hex.lower())