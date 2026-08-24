"""Authentication primitives: password hashing, app session tokens, and the
shared device key the four ESP32 boards authenticate with.

Before this module existed, `/login` was decorative — it checked the password
and returned `ok`, but nothing afterwards ever proved identity again, so
anyone who could reach the backend could `POST /disarm` on an armed alarm
without knowing the password at all. Everything here exists to close that.

Two distinct credentials, because there are two distinct kinds of caller:

- **App session tokens** (`TokenStore`) — minted by `/login` after a correct
  password, sent back as `Authorization: Bearer <token>`. Short-lived and
  revocable, so a stolen phone can be cut off by changing the password.
- **The device key** (`device_key`) — one long-lived shared secret the ESP32
  boards send on every `/esp32/*` call. Boards can't do an interactive login
  (no UI, no way to store a rotating token across a power cycle), so they get
  a static credential instead. It is deliberately *not* the user's password:
  it's burned into four sketches, so it must be independently rotatable.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timedelta

logger = logging.getLogger("security")

# scrypt work factors. N=2**14 with r=8/p=1 is the interactive-login profile
# from the scrypt paper — roughly 100ms and 16MB per hash on a normal
# machine. Deliberately slow: it's what makes a stolen hash expensive to
# brute-force, and it's only paid once per login, not per request (requests
# carry a token instead, which is a cheap dict lookup).
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16

# How long an app session token stays valid. Long enough that the user isn't
# re-typing the password constantly, short enough that a token lifted off a
# device doesn't work forever.
TOKEN_TTL = timedelta(hours=12)


def hash_password(password: str) -> str:
    """Hashes `password` with a fresh random salt, returning a single
    `scrypt$<salt_hex>$<hash_hex>` string suitable for storing.

    The salt is per-password (not a global constant) so two accounts with the
    same password don't produce the same hash, and so a precomputed rainbow
    table can't be reused across installs. The algorithm name is embedded in
    the string so a future migration to a different KDF can recognize old
    hashes instead of having to guess their format.
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
    )
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Checks `password` against a `stored` hash produced by
    [hash_password]. Returns False rather than raising on a malformed or
    unrecognized stored value, so a corrupted state file fails closed (nobody
    can log in) instead of crashing every login request.

    Uses `hmac.compare_digest` for the final comparison specifically to avoid
    a timing side channel: a plain `==` on bytes short-circuits at the first
    differing byte, which leaks how much of a guess was correct.
    """
    try:
        algorithm, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        logger.warning("Stored password hash is malformed — refusing all logins.")
        return False
    if algorithm != "scrypt":
        logger.warning("Unknown password hash algorithm %r — refusing all logins.", algorithm)
        return False

    candidate = hashlib.scrypt(
        password.encode(),
        salt=bytes.fromhex(salt_hex),
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
    )
    return hmac.compare_digest(candidate.hex(), digest_hex)


class TokenStore:
    """In-memory set of live app session tokens, each with an expiry.

    Deliberately not persisted across restarts: a backend restart invalidating
    every session is the safe direction to fail, and it's cheap to recover
    from (the app just logs in again). Contrast with the device key, which
    *is* persisted, because re-flashing four boards is not cheap.
    """

    def __init__(self) -> None:
        self._tokens: dict[str, datetime] = {}

    def issue(self) -> str:
        """Mints a new token valid for [TOKEN_TTL]. `token_urlsafe(32)` is 32
        bytes of `secrets`-grade randomness (~43 chars), which is far beyond
        guessable — the token is the only thing standing between a network
        peer and disarming the alarm, so it must not be derived from anything
        predictable like a counter or a timestamp.
        """
        token = secrets.token_urlsafe(32)
        self._tokens[token] = datetime.now() + TOKEN_TTL
        self._prune()
        return token

    def is_valid(self, token: str | None) -> bool:
        """True if `token` is a live, unexpired session token. Takes
        `str | None` so callers can pass a missing header straight through
        without a separate None check.
        """
        if not token:
            return False
        expiry = self._tokens.get(token)
        if expiry is None:
            return False
        if datetime.now() >= expiry:
            # Expired tokens are dropped lazily, on the request that first
            # notices — there's no background sweep, and none is needed at
            # this scale (one household, a handful of devices).
            del self._tokens[token]
            return False
        return True

    def revoke(self, token: str) -> None:
        """Drops a single token (used by `/logout`). Safe to call with a
        token that was never issued or already expired."""
        self._tokens.pop(token, None)

    def revoke_all(self, except_token: str | None = None) -> None:
        """Drops every live token, optionally sparing one.

        Called on password change: whoever changed the password keeps their
        own session, and every *other* device is signed out. That's the point
        of changing the password on an alarm system — if you're locking
        someone out, leaving their existing session alive would defeat it.
        """
        self._tokens = (
            {except_token: self._tokens[except_token]}
            if except_token and except_token in self._tokens
            else {}
        )

    def _prune(self) -> None:
        """Drops all expired tokens. Called on issue so the dict can't grow
        without bound across a long-running process."""
        now = datetime.now()
        self._tokens = {t: e for t, e in self._tokens.items() if e > now}


def resolve_device_key(persisted: str | None) -> str:
    """Decides which shared key the ESP32 boards must present, preferring an
    explicitly configured one over a previously generated one.

    Order of precedence:
    1. `HOMEGUARD_DEVICE_KEY` from the environment — the intended production
       path, so the key can be managed alongside other deployment config.
    2. `persisted` — a key generated on an earlier run and saved to the state
       file. Reused so the value stays stable across restarts; a key that
       changed on every boot would mean re-flashing four boards every time
       the backend restarted.
    3. A freshly generated key, logged loudly so it can be copied into the
       sketches' `secrets.h`.

    Case 3 is what makes the system work on a first run with no setup, without
    the fallback being a guessable hardcoded constant.
    """
    from_env = os.environ.get("HOMEGUARD_DEVICE_KEY", "").strip()
    if from_env:
        return from_env
    if persisted:
        return persisted

    generated = secrets.token_urlsafe(24)
    logger.warning(
        "HOMEGUARD_DEVICE_KEY not set — generated one for this install: %s\n"
        "    Put it in esp32/secrets.h as HOMEGUARD_DEVICE_KEY on all four boards.\n"
        "    It is saved to the state file, so it stays the same across restarts.",
        generated,
    )
    return generated
