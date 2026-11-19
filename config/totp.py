"""Time-based one-time codes (TOTP, RFC 6238) with the standard library, for the second factor of /admin/.

HMAC-SHA1, 6 digits, steps of 30 seconds from the Unix epoch, a secret of 160 random bits written in base32 without
padding: what every authenticator app reads from an `otpauth://totp/` link or QR code. The time is the server's clock:
the machine must keep it right (NTP), or the codes of the phones fall outside the window.

`accepted_step` says which step a code belongs to, within one step either side of now, and refuses a step that is not
later than the last one accepted for the account: the same code, or an older one, never works twice. Storing that step
is the caller's job (config/second_factor.py), atomically. The functions that hold a secret or a code are marked
`sensitive_variables`: an error report shows none of their values.
"""
import base64
import binascii
import hashlib
import hmac
import secrets
import struct
from urllib.parse import quote, urlencode

from django.views.decorators.debug import sensitive_variables

STEP = 30
DIGITS = 6
# How many steps before and after the current one a code may belong to: a phone a few seconds off, a code typed at the
# end of its half minute
WINDOW = 1
SECRET_BYTES = 20


@sensitive_variables()
def new_secret():
    """A new secret: 160 random bits, in base32 without padding (32 letters and digits)."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii").rstrip("=")


@sensitive_variables()
def secret_bytes(secret):
    """The bytes of a base32 secret, with or without padding, spaces or lower case; ValueError if it is not base32."""
    text = "".join(secret.split()).upper()
    text += "=" * (-len(text) % 8)
    try:
        return base64.b32decode(text)
    except binascii.Error as error:
        raise ValueError("not a base32 secret") from error


@sensitive_variables()
def hotp(key, counter, digits=DIGITS, digest=hashlib.sha1):
    """The code of `counter` for the key (bytes), as a string of `digits` digits (RFC 4226, dynamic truncation)."""
    mac = hmac.new(key, struct.pack(">Q", counter), digest).digest()
    offset = mac[-1] & 0x0F
    number = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10 ** digits).zfill(digits)


def step_of(seconds):
    """The step that a Unix time falls in."""
    return int(seconds) // STEP


@sensitive_variables()
def code_at(secret, seconds, digits=DIGITS, digest=hashlib.sha1):
    """The code of a base32 secret at a Unix time."""
    return hotp(secret_bytes(secret), step_of(seconds), digits, digest)


def clean(code):
    """The code as typed, without spaces (apps show it as «123 456»); None unless it is DIGITS ASCII digits."""
    code = "".join(str(code or "").split())
    if len(code) != DIGITS or not code.isascii() or not code.isdigit():
        return None
    return code


@sensitive_variables()
def accepted_step(secret, code, seconds, last_step=0):
    """The step of the window around `seconds` whose code is `code`, if it is later than `last_step`; else None. Every
    step of the window is compared, in constant time, whichever matches: the answer does not depend on where it matched."""
    code = clean(code)
    if code is None:
        return None
    key = secret_bytes(secret)
    now = step_of(seconds)
    found = None
    for step in range(now - WINDOW, now + WINDOW + 1):
        if hmac.compare_digest(hotp(key, step), code) and step > last_step:
            found = step
    return found


@sensitive_variables()
def uri(secret, account, issuer):
    """The `otpauth://totp/` link an authenticator app reads: issuer and account in the label and the issuer again as a
    parameter, as the apps expect."""
    # Each part on its own: a colon inside one of them must not pass for the separator
    label = f"{quote(issuer, safe='@')}:{quote(account, safe='@')}"
    query = urlencode({"secret": secret, "issuer": issuer, "algorithm": "SHA1", "digits": DIGITS, "period": STEP},
                      quote_via=quote)
    return f"otpauth://totp/{label}?{query}"


@sensitive_variables()
def grouped(secret):
    """The secret in groups of four, to copy by hand into an app."""
    return " ".join(secret[index:index + 4] for index in range(0, len(secret), 4))
