"""
RFC 6238 TOTP on the standard library.

Deliberately dependency-free: pulling in pyotp for ~60 lines of HMAC would
break the project's "single service, no extra deps" rule for no benefit.

Compatible with Google Authenticator, Aegis, 1Password and Authy defaults:
SHA-1, 6 digits, 30-second step.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from typing import List
from urllib.parse import quote

DIGITS = 6
PERIOD = 30
# Accept the neighbouring steps so a phone clock that drifts by a few
# seconds still works. One step each way = up to ±30s of tolerance.
DEFAULT_WINDOW = 1

RECOVERY_CODE_COUNT = 8
RECOVERY_GROUP = 5


def generate_secret(length: int = 20) -> str:
    """A base32 secret, the format authenticator apps expect."""
    return base64.b32encode(secrets.token_bytes(length)).decode("ascii").rstrip("=")


def _hotp(secret_b32: str, counter: int) -> str:
    # Authenticator apps strip padding from displayed secrets, so restore it
    # before decoding or b32decode raises on anything the user pasted back.
    padded = secret_b32.strip().replace(" ", "").upper()
    padded += "=" * (-len(padded) % 8)
    key = base64.b32decode(padded, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** DIGITS)).zfill(DIGITS)


def generate_code(secret_b32: str, at: float = None) -> str:
    return _hotp(secret_b32, int((at if at is not None else time.time()) // PERIOD))


def verify_code(secret_b32: str, code: str, at: float = None,
                window: int = DEFAULT_WINDOW) -> bool:
    """Constant-time check of `code` against the steps around `at`."""
    if not secret_b32 or not code:
        return False
    code = str(code).strip().replace(" ", "").replace("-", "")
    if not code.isdigit() or len(code) != DIGITS:
        return False
    counter = int((at if at is not None else time.time()) // PERIOD)
    ok = False
    for drift in range(-window, window + 1):
        try:
            candidate = _hotp(secret_b32, counter + drift)
        except Exception:
            return False
        # Compare every candidate rather than breaking early, so the
        # response time doesn't leak which step matched.
        ok |= secrets.compare_digest(candidate, code)
    return ok


def provisioning_uri(secret_b32: str, account: str, issuer: str) -> str:
    """otpauth:// URI for the enrolment QR code."""
    label = quote(f"{issuer}:{account}", safe="")
    return (
        f"otpauth://totp/{label}"
        f"?secret={secret_b32}&issuer={quote(issuer, safe='')}"
        f"&algorithm=SHA1&digits={DIGITS}&period={PERIOD}"
    )


# ---------------------------------------------------------------- recovery
def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> List[str]:
    """Human-transcribable one-time codes, e.g. 'k4m9x-7qp2d'.

    Excludes characters that get misread when written down (0/O, 1/l/I).
    """
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    codes = []
    for _ in range(count):
        raw = "".join(secrets.choice(alphabet) for _ in range(RECOVERY_GROUP * 2))
        codes.append(f"{raw[:RECOVERY_GROUP]}-{raw[RECOVERY_GROUP:]}")
    return codes


def hash_recovery_code(code: str) -> str:
    """Recovery codes are stored hashed, never in the clear."""
    normalised = code.strip().lower().replace(" ", "").replace("-", "")
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def consume_recovery_code(code: str, hashed_codes: List[str]):
    """Return the hash that matched so the caller can burn it, else None."""
    candidate = hash_recovery_code(code)
    for stored in hashed_codes:
        if secrets.compare_digest(stored, candidate):
            return stored
    return None
