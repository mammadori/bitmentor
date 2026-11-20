"""Passkeys (WebAuthn level 2) for the second factor of /admin/ (config/second_factor.py), with `cryptography` and the
small CBOR decoder of config/cbor.py: no other library.

**What the site asks.** At registration: ES256, EdDSA (Ed25519) or RS256, in that order, attestation "none", no
resident key (the password comes first: there is no sign-in without a username), user verification preferred. At
sign-in: one of the account's own credentials, user verification preferred. The browser shows the site's host
(`SITE_HOST`) as the passkey's scope (`rp.id`): a passkey works only there, never on another host, a subdomain or the
onion.

**What it checks**, refusing anything else with `Refused` (whose reason is for the log and the tests, never for the
page): the browser's client data (`type`, the exact challenge, the site's own origin `PUBLIC_URL`, not cross-origin),
the authenticator's data (the hash of `rp.id`, the user-present flag, at registration the attested credential, its id
at most MAX_CREDENTIAL_ID bytes, its COSE key of one of the three algorithms, a valid point or modulus; no bytes left
over), and, at sign-in, the signature over the authenticator's data and the hash of the client data with the stored
key. **Not checked:** the attestation statement, whatever its format (only the credential's id and key are read from
the authenticator's data): which brand of authenticator made a passkey is not this site's business.

The sign count, the account the credential belongs to and the challenge's lifetime and single use are the caller's.
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from django.conf import settings

from config import cbor

ES256, EDDSA, RS256 = -7, -8, -257
# In the order the site prefers them
ALGORITHMS = (ES256, EDDSA, RS256)
CHALLENGE_BYTES = 32
CHALLENGE_SECONDS = 300
# The longest credential id WebAuthn allows
MAX_CREDENTIAL_ID = 1023
# What a browser posts, as JSON text, and its parts once decoded
MAX_POSTED = 32768
MAX_CLIENT_DATA = 4096
MAX_ATTESTATION = 16384
MAX_AUTHENTICATOR_DATA = 2048
MAX_SIGNATURE = 1024
MAX_USER_HANDLE = 64
# RSA keys of fewer bits are refused, and of more too (a 16384-bit key is only work for the server)
RSA_BITS = (2048, 4096)
# The flags of the authenticator's data
UP, UV, BE, BS, AT, ED = 0x01, 0x04, 0x08, 0x10, 0x40, 0x80
TIMEOUT_MS = 120000
BASE64URL = re.compile(r"[A-Za-z0-9_-]*")


class Refused(Exception):
    """Not a valid answer of a passkey for this site and this challenge. The reason is for the log and the tests."""


class Replayed(Refused):
    """The signature is right but the sign count did not grow: the authenticator may have been cloned."""


def b64url(data):
    """Bytes as base64url without padding, as WebAuthn's JSON writes them."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def unb64url(text, limit):
    """The bytes of a base64url text without padding, at most `limit` of them; Refused for anything else."""
    if (
        not isinstance(text, str)
        or len(text) > (limit * 4 + 2) // 3
        or not BASE64URL.fullmatch(text)
        or len(text) % 4 == 1
    ):
        raise Refused("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def new_challenge():
    return b64url(secrets.token_bytes(CHALLENGE_BYTES))


def new_user_handle():
    """The opaque id of an account for the authenticators: random, never the username or the key of the row."""
    return b64url(secrets.token_bytes(32))


def rp_id():
    return settings.SITE_HOST


def origin():
    return settings.PUBLIC_URL


def creation_options(challenge, user_handle, username, rp_name, exclude):
    """The options of navigator.credentials.create, as JSON (binary values in base64url): `exclude` are the
    account's credential ids already registered."""
    return {
        "challenge": challenge,
        "rp": {"id": rp_id(), "name": rp_name},
        "user": {"id": user_handle, "name": username, "displayName": username},
        "pubKeyCredParams": [{"type": "public-key", "alg": alg} for alg in ALGORITHMS],
        "timeout": TIMEOUT_MS,
        "attestation": "none",
        "authenticatorSelection": {
            "residentKey": "discouraged",
            "requireResidentKey": False,
            "userVerification": "preferred",
        },
        "excludeCredentials": [{"type": "public-key", "id": credential} for credential in exclude],
    }


def request_options(challenge, allow):
    """The options of navigator.credentials.get: `allow` are the account's credential ids."""
    return {
        "challenge": challenge,
        "rpId": rp_id(),
        "timeout": TIMEOUT_MS,
        "userVerification": "preferred",
        "allowCredentials": [{"type": "public-key", "id": credential} for credential in allow],
    }


# --- What the browser posts ------------------------------------------------------------------------------------------


def _no_duplicates(pairs):
    found = {}
    for key, value in pairs:
        if key in found:
            raise Refused("a JSON key twice")
        found[key] = value
    return found


def _json(text, limit):
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError:
            raise Refused("not UTF-8") from None
    if not isinstance(text, str) or not text or len(text) > limit:
        raise Refused("no JSON, or too long")
    try:
        value = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_constant)
    except (ValueError, RecursionError):
        raise Refused("not JSON") from None
    if not isinstance(value, dict):
        raise Refused("not a JSON object")
    return value


def _constant(name):
    raise Refused(f"{name} in JSON")


def _credential(text):
    """The posted PublicKeyCredential, as JSON: its id (base64url, as rawId) and its response."""
    data = _json(text, MAX_POSTED)
    if data.get("type") != "public-key":
        raise Refused("not a public-key credential")
    raw = unb64url(data.get("rawId"), MAX_CREDENTIAL_ID)
    if not raw or data.get("id") != b64url(raw):
        raise Refused("id and rawId differ, or empty")
    response = data.get("response")
    if not isinstance(response, dict):
        raise Refused("no response")
    return raw, response


def check_client_data(raw, kind, challenge):
    """The browser's client data: of `kind` (webauthn.create or webauthn.get), for `challenge`, from the site's own
    origin, not inside another site's frame."""
    data = _json(raw, MAX_CLIENT_DATA)
    if data.get("type") != kind:
        raise Refused("another type")
    sent = data.get("challenge")
    if not (
        isinstance(sent, str)
        and isinstance(challenge, str)
        and challenge
        and hmac.compare_digest(sent.encode(), challenge.encode())
    ):
        raise Refused("another challenge")
    if data.get("origin") != origin():
        raise Refused("another origin")
    if data.get("crossOrigin", False) is not False or "topOrigin" in data:
        raise Refused("cross-origin")


@dataclass
class AuthenticatorData:
    flags: int
    sign_count: int
    credential_id: bytes = b""
    cose_key: dict = None


def parse_authenticator_data(data, attested):
    """The authenticator's data, checked: the hash of rp.id, user present, BS only with BE, the attested credential
    data only and always at registration (`attested`), the extensions as a CBOR map, nothing left over."""
    if not isinstance(data, bytes) or not 37 <= len(data) <= MAX_ATTESTATION:
        raise Refused("authenticator data of a wrong size")
    if not hmac.compare_digest(data[:32], hashlib.sha256(rp_id().encode("idna")).digest()):
        raise Refused("another rp id")
    flags = data[32]
    found = AuthenticatorData(flags, int.from_bytes(data[33:37], "big"))
    if not flags & UP:
        raise Refused("user not present")
    if flags & BS and not flags & BE:
        raise Refused("backed up but not backup eligible")
    offset = 37
    if bool(flags & AT) != attested:
        raise Refused("attested credential data where it should not be, or missing")
    try:
        if attested:
            if len(data) < offset + 18:
                raise Refused("truncated attested credential data")
            length = int.from_bytes(data[offset + 16 : offset + 18], "big")
            offset += 18
            if not 1 <= length <= MAX_CREDENTIAL_ID or offset + length > len(data):
                raise Refused("credential id of a wrong size")
            found.credential_id = data[offset : offset + length]
            offset += length
            found.cose_key, offset = cbor.decode_prefix(data, offset, MAX_ATTESTATION)
            if not isinstance(found.cose_key, dict):
                raise Refused("the public key is not a map")
        if flags & ED:
            extensions, offset = cbor.decode_prefix(data, offset, MAX_ATTESTATION)
            if not isinstance(extensions, dict):
                raise Refused("extensions that are not a map")
    except cbor.CBORError as error:
        raise Refused(f"CBOR: {error}") from None
    if offset != len(data):
        raise Refused("bytes after the authenticator data")
    return found


def _int(value):
    # bool is an int to Python: not here
    return type(value) is int


def _bytes(cose, label, size=None):
    value = cose.get(label)
    if not isinstance(value, bytes) or (size is not None and len(value) != size):
        raise Refused(f"COSE parameter {label} missing or of a wrong size")
    return value


def public_key(cose):
    """(the key as SubjectPublicKeyInfo DER, its algorithm) of a COSE key of one of ALGORITHMS; Refused otherwise,
    and for a point not on the curve or an RSA modulus outside RSA_BITS."""
    kty, alg = cose.get(1), cose.get(3)
    if not (_int(kty) and _int(alg)) or alg not in ALGORITHMS:
        raise Refused("an algorithm the site does not accept")
    try:
        if alg == ES256:
            if kty != 2 or cose.get(-1) != 1 or not _int(cose.get(-1)):
                raise Refused("ES256 that is not EC2 on P-256")
            x, y = _bytes(cose, -2, 32), _bytes(cose, -3, 32)
            key = ec.EllipticCurvePublicNumbers(
                int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1()
            ).public_key()
        elif alg == EDDSA:
            if kty != 1 or cose.get(-1) != 6 or not _int(cose.get(-1)):
                raise Refused("EdDSA that is not OKP Ed25519")
            key = ed25519.Ed25519PublicKey.from_public_bytes(_bytes(cose, -2, 32))
        else:
            if kty != 3:
                raise Refused("RS256 that is not RSA")
            n, e = _bytes(cose, -1), _bytes(cose, -2)
            if len(e) > 8:
                raise Refused("an RSA exponent too long")
            modulus = int.from_bytes(n, "big")
            if not RSA_BITS[0] <= modulus.bit_length() <= RSA_BITS[1]:
                raise Refused("an RSA key of a size the site does not accept")
            key = rsa.RSAPublicNumbers(int.from_bytes(e, "big"), modulus).public_key()
    except (ValueError, TypeError) as error:
        raise Refused(f"not a valid key: {error}") from None
    return key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo), alg


@dataclass
class Registration:
    credential_id: str
    public_key: bytes
    algorithm: int
    sign_count: int
    backup_eligible: bool
    backed_up: bool


def verify_registration(posted, challenge):
    """A Registration from the JSON the page posted after navigator.credentials.create, for `challenge`."""
    raw_id, response = _credential(posted)
    check_client_data(unb64url(response.get("clientDataJSON"), MAX_CLIENT_DATA), "webauthn.create", challenge)
    try:
        attestation = cbor.decode(unb64url(response.get("attestationObject"), MAX_ATTESTATION), MAX_ATTESTATION)
    except cbor.CBORError as error:
        raise Refused(f"CBOR: {error}") from None
    if not (
        isinstance(attestation, dict)
        and set(attestation) == {"fmt", "attStmt", "authData"}
        and isinstance(attestation["fmt"], str)
        and isinstance(attestation["attStmt"], dict)
    ):
        raise Refused("not an attestation object")
    # The statement is not verified (attestation "none"): only the credential's id and key are read
    data = parse_authenticator_data(attestation["authData"], attested=True)
    if not hmac.compare_digest(data.credential_id, raw_id):
        raise Refused("the credential id is not the one of the authenticator data")
    key, alg = public_key(data.cose_key)
    return Registration(b64url(raw_id), key, alg, data.sign_count, bool(data.flags & BE), bool(data.flags & BS))


@dataclass
class Assertion:
    credential_id: str
    client_data: bytes
    authenticator_data: bytes
    signature: bytes
    user_handle: bytes


def parse_assertion(posted):
    """The parts of the JSON the page posted after navigator.credentials.get, decoded but not verified yet: the
    caller looks the credential up first."""
    raw_id, response = _credential(posted)
    # An empty user handle is none: what an authenticator sends for a credential that is not discoverable
    handle = response.get("userHandle")
    return Assertion(
        b64url(raw_id),
        unb64url(response.get("clientDataJSON"), MAX_CLIENT_DATA),
        unb64url(response.get("authenticatorData"), MAX_AUTHENTICATOR_DATA),
        unb64url(response.get("signature"), MAX_SIGNATURE),
        None if handle in (None, "") else unb64url(handle, MAX_USER_HANDLE) or None,
    )


def verify_assertion(assertion, challenge, key_der, algorithm, stored_count, user_handle, backup_eligible):
    """(the new sign count, the BS flag) if the assertion is valid for `challenge` with the stored key and its BE flag
    is the one stored at registration (a credential does not change whether it can be synced); Refused otherwise, and
    Replayed for a right signature whose count did not grow when either count is not zero."""
    check_client_data(assertion.client_data, "webauthn.get", challenge)
    data = parse_authenticator_data(assertion.authenticator_data, attested=False)
    if bool(data.flags & BE) != bool(backup_eligible):
        raise Refused("a BE flag other than at registration")
    # Without a resident key the authenticator may send no user handle; one that it sends must be the account's
    if assertion.user_handle is not None and not hmac.compare_digest(
        assertion.user_handle, unb64url(user_handle, MAX_USER_HANDLE)
    ):
        raise Refused("another user handle")
    signed = assertion.authenticator_data + hashlib.sha256(assertion.client_data).digest()
    try:
        key = serialization.load_der_public_key(bytes(key_der))
        if algorithm == ES256 and isinstance(key, ec.EllipticCurvePublicKey):
            key.verify(assertion.signature, signed, ec.ECDSA(hashes.SHA256()))
        elif algorithm == EDDSA and isinstance(key, ed25519.Ed25519PublicKey):
            key.verify(assertion.signature, signed)
        elif algorithm == RS256 and isinstance(key, rsa.RSAPublicKey):
            key.verify(assertion.signature, signed, padding.PKCS1v15(), hashes.SHA256())
        else:
            raise Refused("a stored key of another algorithm")
    except (InvalidSignature, ValueError, TypeError):
        raise Refused("a wrong signature") from None
    if (stored_count or data.sign_count) and data.sign_count <= stored_count:
        raise Replayed("the sign count did not grow")
    return data.sign_count, bool(data.flags & BS)
