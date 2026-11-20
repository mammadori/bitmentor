"""Passkeys as the second factor of /admin/ (config/webauthn.py, config/cbor.py, config/second_factor.py): a software
authenticator for the three algorithms, registration and sign-in end to end, every refusal, the modes, the sessions,
the page, the commands, the notices, the setting, the script."""

import base64
import hashlib
import json
import os
import re
import struct
import subprocess
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from django.conf import settings
from django.contrib import admin
from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.management import CommandError, call_command
from django.test import (
    Client,
    RequestFactory,
    SimpleTestCase,
    TestCase,
    TransactionTestCase,
    override_settings,
)

from shops.models import AdminPasskey, AdminPasskeyHandle, AdminSecondFactor
from shops.test_scripts import node_or_skip
from shops.tests import reset_limits, steady_limits

from . import cbor, webauthn
from .guard import ADMIN_SESSION
from .second_factor import FACTOR_SESSION, PENDING
from .test_second_factor import (
    CODE,
    LOGIN,
    PASSWORD,
    SETUP,
    T,
    admin_urls,
    clock,
    code,
    signed_in,
    turn_on,
)

User = get_user_model()
HOST = "localhost"
ORIGIN = f"https://{HOST}"
UP, UV, BE, BS, AT, ED = 0x01, 0x04, 0x08, 0x10, 0x40, 0x80
# A browser on https behind the proxy (SECURE_PROXY_SSL_HEADER): the test client's own `secure` is per request
HTTPS = {"HTTP_X_FORWARDED_PROTO": "https"}


# --- A CBOR encoder, for the tests only --------------------------------------------------------------------------------


def head(major, number):
    if number < 24:
        return bytes([major << 5 | number])
    for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
        if number < 1 << (8 * size):
            return bytes([major << 5 | info]) + number.to_bytes(size, "big")
    raise ValueError(number)


def encode(value):
    if value is False:
        return b"\xf4"
    if value is True:
        return b"\xf5"
    if value is None:
        return b"\xf6"
    if isinstance(value, int):
        return head(0, value) if value >= 0 else head(1, -1 - value)
    if isinstance(value, bytes):
        return head(2, len(value)) + value
    if isinstance(value, str):
        data = value.encode()
        return head(3, len(data)) + data
    if isinstance(value, list):
        return head(4, len(value)) + b"".join(encode(item) for item in value)
    if isinstance(value, dict):
        return head(5, len(value)) + b"".join(encode(key) + encode(item) for key, item in value.items())
    raise TypeError(value)


def b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# --- A software authenticator --------------------------------------------------------------------------------------


class Authenticator:
    """Makes and uses one passkey as a browser and an authenticator would, with switches to break every part."""

    def __init__(self, alg=webauthn.ES256, count=0, step=1, flags=UP | UV, credential_id=None):
        self.alg, self.count, self.step, self.flags = alg, count, step, flags
        self.credential_id = credential_id or os.urandom(16)
        if alg == webauthn.ES256:
            self.key = ec.generate_private_key(ec.SECP256R1())
        elif alg == webauthn.EDDSA:
            self.key = ed25519.Ed25519PrivateKey.generate()
        else:
            self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def cose(self):
        public = self.key.public_key()
        if self.alg == webauthn.ES256:
            numbers = public.public_numbers()
            return {1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")}
        if self.alg == webauthn.EDDSA:
            return {
                1: 1,
                3: -8,
                -1: 6,
                -2: public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw),
            }
        numbers = public.public_numbers()
        return {1: 3, 3: -257, -1: numbers.n.to_bytes(256, "big"), -2: numbers.e.to_bytes(3, "big")}

    def sign(self, data):
        if self.alg == webauthn.ES256:
            return self.key.sign(data, ec.ECDSA(hashes.SHA256()))
        if self.alg == webauthn.EDDSA:
            return self.key.sign(data)
        return self.key.sign(data, padding.PKCS1v15(), hashes.SHA256())

    @staticmethod
    def client_data(kind, challenge, origin=ORIGIN, **extra):
        return json.dumps(
            {"type": kind, "challenge": challenge, "origin": origin, "crossOrigin": False, **extra}
        ).encode()

    def create(
        self,
        options,
        origin=ORIGIN,
        kind="webauthn.create",
        rp=HOST,
        flags=None,
        challenge=None,
        cose=None,
        tail=b"",
        client=None,
        credential_id=None,
    ):
        """The JSON the page posts after navigator.credentials.create."""
        flags = (self.flags | AT) if flags is None else flags
        credential = self.credential_id if credential_id is None else credential_id
        client = client or self.client_data(kind, challenge or options["challenge"], origin)
        data = hashlib.sha256(rp.encode()).digest() + bytes([flags]) + struct.pack(">I", self.count)
        if flags & AT:
            data += bytes(16) + struct.pack(">H", len(credential)) + credential + encode(cose or self.cose())
        data += tail
        attestation = encode({"fmt": "none", "attStmt": {}, "authData": data})
        return json.dumps(
            {
                "id": b64(credential),
                "rawId": b64(credential),
                "type": "public-key",
                "response": {"clientDataJSON": b64(client), "attestationObject": b64(attestation)},
            }
        )

    def get(
        self,
        options,
        origin=ORIGIN,
        kind="webauthn.get",
        rp=HOST,
        flags=None,
        challenge=None,
        count=None,
        handle=None,
        tamper_signature=False,
        tamper_data=False,
        client=None,
    ):
        """The JSON the page posts after navigator.credentials.get."""
        if count is None:
            self.count += self.step
            count = self.count
        flags = self.flags if flags is None else flags
        client = client or self.client_data(kind, challenge or options["challenge"], origin)
        data = hashlib.sha256(rp.encode()).digest() + bytes([flags]) + struct.pack(">I", count)
        signature = self.sign(data + hashlib.sha256(client).digest())
        if tamper_signature:
            signature = signature[:-1] + bytes([signature[-1] ^ 1])
        if tamper_data:
            data = data[:-1] + bytes([data[-1] ^ 1])
        return json.dumps(
            {
                "id": b64(self.credential_id),
                "rawId": b64(self.credential_id),
                "type": "public-key",
                "response": {
                    "clientDataJSON": b64(client),
                    "authenticatorData": b64(data),
                    "signature": b64(signature),
                    "userHandle": handle,
                },
            }
        )


def browser():
    """A browser at the site's own origin."""
    return Client(HTTP_HOST=HOST, **HTTPS, headers={"accept-language": "en"})


SITE = override_settings(SITE_HOST=HOST, PUBLIC_URL=ORIGIN, ADMIN_SECOND_FACTOR="optional")


@SITE
class Base(TestCase):
    def setUp(self):
        reset_limits()
        self.staff = User.objects.create_user("volunteer", password=PASSWORD, is_staff=True)
        self.client = browser()
        background = patch("config.second_factor.in_background", side_effect=lambda work: work())
        background.start()
        self.addCleanup(background.stop)
        told = patch("shops.notifications.deliver_to_moderators", return_value=("TELEGRAM_MODERATION_CHAT", []))
        self.told = told.start()
        self.addCleanup(told.stop)

    def told_texts(self):
        return [call.args[1] for call in self.told.call_args_list]

    def password(self, client=None, username="volunteer", next="/admin/"):
        return (client or self.client).post(LOGIN, {"username": username, "password": PASSWORD, "next": next})

    def add(self, authenticator=None, client=None, name="phone", **broken):
        """Adds a passkey from the page, in a session already signed in; returns (answer, authenticator)."""
        client = client or self.client
        authenticator = authenticator or Authenticator()
        page = client.get(SETUP)
        self.assertEqual(page.status_code, 200)
        options = page.context["passkey_options"]
        answer = client.post(
            SETUP, {"action": "passkey-add", "name": name, "credential": authenticator.create(options, **broken)}
        )
        return answer, authenticator

    def register(self, user=None, alg=webauthn.ES256, name="phone", **options):
        """A passkey of `user` (the staff account by default), stored as the page would, without going through it."""
        user = user or self.staff
        authenticator = Authenticator(alg, **options)
        handle, _made = AdminPasskeyHandle.objects.get_or_create(
            user=user, defaults={"handle": webauthn.new_user_handle()}
        )
        challenge = webauthn.new_challenge()
        made = webauthn.verify_registration(authenticator.create({"challenge": challenge}), challenge)
        passkey = AdminPasskey.objects.create(
            user=user,
            credential_id=made.credential_id,
            public_key=made.public_key,
            algorithm=made.algorithm,
            sign_count=made.sign_count,
            name=name,
            backup_eligible=made.backup_eligible,
            backed_up=made.backed_up,
        )
        authenticator.passkey, authenticator.handle = passkey, handle.handle
        return authenticator

    def options(self, client=None):
        page = (client or self.client).get(CODE)
        self.assertEqual(page.status_code, 200)
        return page.context["passkey_options"]

    def sign_in(self, authenticator, client=None, seconds=T, **broken):
        client = client or self.client
        with clock(seconds):
            self.assertRedirects(self.password(client), CODE, fetch_redirect_response=False)
            return client.post(CODE, {"credential": authenticator.get(self.options(client), **broken)})

    def assert_refused(self, answer, client=None):
        self.assertEqual(answer.status_code, 200)
        self.assertTrue(answer.context["passkey_wrong"])
        self.assertContains(answer, "The passkey was not accepted.")
        self.assertFalse(signed_in(client or self.client))


# ---------------------------------------------------------------------------------------------------------------------
# The decoder and the verifier on their own
# ---------------------------------------------------------------------------------------------------------------------


class Cbor(SimpleTestCase):
    def test_what_it_reads(self):
        value = {1: 2, 3: -7, -1: b"\x00" * 32, "fmt": "none", "list": [True, False, None, 0, 23, 24, 2**40]}
        self.assertEqual(cbor.decode(encode(value)), value)
        self.assertEqual(cbor.decode_prefix(encode(5) + b"rest"), (5, 1))

    def test_what_it_refuses(self):
        deep = b"\x81" * 10 + b"\x00"
        cases = {
            "empty": b"",
            "truncated integer": b"\x19\x01",
            "truncated string": b"\x45abc",
            "truncated map": b"\xa2\x01\x02",
            "indefinite bytes": b"\x5f\x41a\xff",
            "indefinite map": b"\xbf\x01\x02\xff",
            "indefinite array": b"\x9f\x01\xff",
            "reserved": b"\x1c",
            "huge string": b"\x5b" + (2**63).to_bytes(8, "big"),
            "huge array": b"\x9b" + (2**63).to_bytes(8, "big"),
            "huge map": b"\xba\xff\xff\xff\xff",
            "deep": deep,
            "trailing": b"\x01\x02",
            "tag": b"\xc2\x41\x01",
            "float": b"\xfa\x00\x00\x00\x00",
            "undefined": b"\xf7",
            "simple": b"\xf8\x20",
            "not utf-8": b"\x62\xff\xfe",
            "duplicate key": b"\xa2\x01\x01\x01\x02",
            "bool key": b"\xa1\xf5\x01",
            "bytes key": b"\xa1\x41a\x01",
            "too long": b"\x40" * (cbor.MAX_SIZE + 1),
        }
        for name, data in cases.items():
            with self.subTest(name), self.assertRaises(cbor.CBORError):
                cbor.decode(data)
        with self.assertRaises(cbor.CBORError):
            cbor.decode("not bytes")

    def test_random_bytes_never_raise_anything_else(self):
        import random

        rng = random.Random(42)
        for _ in range(3000):
            data = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 40)))
            try:
                cbor.decode(data)
            except cbor.CBORError:
                pass


@override_settings(SITE_HOST=HOST, PUBLIC_URL=ORIGIN)
class Verifier(SimpleTestCase):
    def test_the_three_algorithms(self):
        for alg in webauthn.ALGORITHMS:
            with self.subTest(alg):
                authenticator = Authenticator(alg)
                challenge = webauthn.new_challenge()
                made = webauthn.verify_registration(authenticator.create({"challenge": challenge}), challenge)
                self.assertEqual((made.algorithm, made.credential_id), (alg, b64(authenticator.credential_id)))
                challenge = webauthn.new_challenge()
                assertion = webauthn.parse_assertion(authenticator.get({"challenge": challenge}))
                self.assertEqual(
                    webauthn.verify_assertion(assertion, challenge, made.public_key, alg, 0, None, False), (1, False)
                )

    def test_the_options(self):
        options = webauthn.creation_options("c", "h", "anna", "Community", ["x"])
        self.assertEqual([item["alg"] for item in options["pubKeyCredParams"]], [-7, -8, -257])
        self.assertEqual(options["rp"], {"id": HOST, "name": "Community"})
        self.assertEqual(options["attestation"], "none")
        self.assertEqual(options["authenticatorSelection"]["residentKey"], "discouraged")
        self.assertEqual(options["authenticatorSelection"]["userVerification"], "preferred")
        self.assertEqual(options["excludeCredentials"], [{"type": "public-key", "id": "x"}])
        self.assertEqual(webauthn.request_options("c", ["x"])["allowCredentials"], [{"type": "public-key", "id": "x"}])
        self.assertEqual(len(unb64(webauthn.new_challenge())), 32)

    def test_unsupported_keys(self):
        authenticator = Authenticator()
        good = authenticator.cose()
        rsa_small = rsa.generate_private_key(public_exponent=65537, key_size=1024).public_key().public_numbers()
        cases = {
            "ES384": {**good, 3: -35},
            "no algorithm": {key: value for key, value in good.items() if key != 3},
            "another curve": {**good, -1: 2},
            "bool curve": {**good, -1: True},
            "short x": {**good, -2: b"\x01" * 31},
            "not on the curve": {**good, -3: b"\x01" * 32},
            "EdDSA with EC2": {**good, 3: -8},
            "RSA of 1024 bits": {1: 3, 3: -257, -1: rsa_small.n.to_bytes(128, "big"), -2: b"\x01\x00\x01"},
            "RSA as text": {1: 3, 3: -257, -1: "n", -2: "e"},
            "bool algorithm": {**good, 3: True},
        }
        for name, cose in cases.items():
            with self.subTest(name), self.assertRaises(webauthn.Refused):
                webauthn.public_key(cose)

    def test_malformed_posts_are_refused_and_never_raise_anything_else(self):
        authenticator = Authenticator()
        challenge = webauthn.new_challenge()
        good = json.loads(authenticator.create({"challenge": challenge}))
        response = good["response"]
        posts = [
            "",
            "null",
            "[]",
            "{",
            "{}",
            '{"a": NaN}',
            "[" * 100000,
            '{"type": "public-key", "type": "x"}',
            json.dumps({**good, "rawId": "***"}),
            json.dumps({**good, "rawId": "A"}),
            json.dumps({**good, "rawId": "AAAA="}),
            json.dumps({**good, "id": "other"}),
            json.dumps({**good, "rawId": "A" * 2000, "id": "A" * 2000}),
            json.dumps({**good, "type": "x"}),
            json.dumps({**good, "response": "x"}),
            json.dumps({**good, "response": {}}),
            "x" * 40000,
            json.dumps({**good, "response": {**response, "clientDataJSON": b64(b"\xff\xfe")}}),
            json.dumps({**good, "response": {**response, "clientDataJSON": b64(b"[1]")}}),
            json.dumps({**good, "response": {**response, "attestationObject": b64(b"\xbf")}}),
        ]
        attestation = unb64(response["attestationObject"])
        for cut in (1, 10, 50, len(attestation) - 1):
            posts.append(json.dumps({**good, "response": {**response, "attestationObject": b64(attestation[:cut])}}))
        posts.append(json.dumps({**good, "response": {**response, "attestationObject": b64(attestation + b"\x00")}}))
        for posted in posts:
            with self.subTest(posted[:60]), self.assertRaises(webauthn.Refused):
                webauthn.verify_registration(posted, challenge)
            with self.subTest(posted[:60]), self.assertRaises(webauthn.Refused):
                webauthn.parse_assertion(posted)

    def test_authenticator_data(self):
        rp = hashlib.sha256(HOST.encode()).digest()
        cases = {
            "short": rp + b"\x01",
            "another rp": hashlib.sha256(b"evil.example").digest() + b"\x01\x00\x00\x00\x00",
            "not present": rp + b"\x04\x00\x00\x00\x00",
            "backed up but not eligible": rp + bytes([UP | BS]) + b"\x00\x00\x00\x00",
            "attested data in a sign-in": rp + bytes([UP | AT]) + b"\x00\x00\x00\x00",
            "extensions missing": rp + bytes([UP | ED]) + b"\x00\x00\x00\x00",
            "extensions not a map": rp + bytes([UP | ED]) + b"\x00\x00\x00\x00" + encode([1]),
            "trailing": rp + bytes([UP]) + b"\x00\x00\x00\x00" + b"\x00",
        }
        for name, data in cases.items():
            with self.subTest(name), self.assertRaises(webauthn.Refused):
                webauthn.parse_authenticator_data(data, attested=False)
        with_extensions = rp + bytes([UP | ED]) + b"\x00\x00\x00\x07" + encode({"credProps": {"rk": False}})
        self.assertEqual(webauthn.parse_authenticator_data(with_extensions, attested=False).sign_count, 7)
        with self.assertRaises(webauthn.Refused):
            webauthn.parse_authenticator_data(rp + bytes([UP]) + b"\x00\x00\x00\x00", attested=True)


# ---------------------------------------------------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------------------------------------------------


class Registration(Base):
    def setUp(self):
        super().setUp()
        with clock(T):
            self.assertRedirects(self.password(), "/admin/", fetch_redirect_response=False)

    def test_each_algorithm_end_to_end(self):
        for alg in webauthn.ALGORITHMS:
            with self.subTest(alg):
                AdminPasskey.objects.all().delete()
                reset_limits()
                client = browser()
                with clock(T):
                    self.password(client)
                    answer, authenticator = self.add(Authenticator(alg), client=client, name=" my  phone\n")
                self.assertRedirects(answer, SETUP, fetch_redirect_response=False)
                passkey = AdminPasskey.objects.get(user=self.staff)
                self.assertEqual(
                    (passkey.algorithm, passkey.name, passkey.credential_id),
                    (alg, "my phone", b64(authenticator.credential_id)),
                )
                # The session proved it: it is one opened with it now
                self.assertEqual(client.session[FACTOR_SESSION][:2], ["passkey", passkey.pk])
                # And the next sign-in asks for it
                other = browser()
                answer = self.sign_in(authenticator, client=other)
                self.assertRedirects(answer, "/admin/", fetch_redirect_response=False)
                self.assertTrue(signed_in(other))
                self.assertEqual(other.session[FACTOR_SESSION], ["passkey", passkey.pk, T])
                self.assertEqual(AdminPasskey.objects.get(pk=passkey.pk).sign_count, 1)
                self.assertIsNotNone(AdminPasskey.objects.get(pk=passkey.pk).last_used)

    def test_the_options_of_the_page(self):
        first = self.register(name="old")
        self.client = browser()
        self.sign_in(first)
        with clock(T + 60):
            options = self.client.get(SETUP).context["passkey_options"]
            page = self.client.get(SETUP)
        handle = AdminPasskeyHandle.objects.get(user=self.staff).handle
        self.assertEqual(options["user"]["id"], handle)
        self.assertNotIn(str(self.staff.pk), [options["user"]["id"]])
        self.assertNotEqual(unb64(handle), b"volunteer")
        self.assertEqual(options["excludeCredentials"], [{"type": "public-key", "id": first.passkey.credential_id}])
        self.assertEqual(options["rp"]["id"], HOST)
        self.assertContains(page, 'id="passkey-options"')
        self.assertRegex(page.content.decode(), r"admin-passkey(\.[0-9a-f]+)?\.js")  # with or without its fingerprint

    def test_the_handle_is_the_same_for_every_passkey_and_random(self):
        with clock(T):
            self.add()
        handle = AdminPasskeyHandle.objects.get(user=self.staff).handle
        with clock(T):
            self.add()
        self.assertEqual(AdminPasskey.objects.filter(user=self.staff).count(), 2)
        self.assertEqual(AdminPasskeyHandle.objects.get(user=self.staff).handle, handle)
        self.assertEqual(len(unb64(handle)), 32)

    def refused(self, **broken):
        before = AdminPasskey.objects.count()
        with clock(T):
            answer, _authenticator = self.add(**broken)
        self.assertEqual(answer.status_code, 200)
        self.assertContains(answer, "The passkey was not added")
        self.assertEqual(AdminPasskey.objects.count(), before)

    def test_wrong_origins(self):
        for origin in (
            "http://localhost",
            "https://evil.example",
            "https://sub.localhost",
            "https://localhost:8443",
            "http://" + "a" * 56 + ".onion",
            "https://localhost.",
            "",
        ):
            with self.subTest(origin):
                self.refused(origin=origin)

    def test_wrong_rp(self):
        self.refused(rp="evil.example")

    def test_type_swapped(self):
        self.refused(kind="webauthn.get")

    def test_wrong_challenge(self):
        self.refused(challenge=webauthn.new_challenge())

    def test_cross_origin(self):
        authenticator = Authenticator()
        page = self.client.get(SETUP)
        challenge = page.context["passkey_options"]["challenge"]
        client = Authenticator.client_data("webauthn.create", challenge, crossOrigin=True)
        with clock(T):
            answer = self.client.post(
                SETUP, {"action": "passkey-add", "name": "x", "credential": authenticator.create({}, client=client)}
            )
        self.assertContains(answer, "The passkey was not added")

    def test_user_not_present(self):
        self.refused(flags=AT)

    def test_attested_data_missing(self):
        self.refused(flags=UP)

    def test_unsupported_algorithm(self):
        authenticator = Authenticator()
        self.refused(authenticator=authenticator, cose={**authenticator.cose(), 3: -35})

    def test_trailing_bytes_and_bad_extensions(self):
        self.refused(tail=b"\x00")
        self.refused(flags=UP | AT | ED, tail=encode([1]))

    def test_a_credential_id_too_long(self):
        self.refused(credential_id=b"\x01" * 1024)

    def test_a_credential_id_that_is_not_the_raw_id(self):
        authenticator = Authenticator()
        page = self.client.get(SETUP)
        posted = json.loads(authenticator.create(page.context["passkey_options"]))
        posted["rawId"] = posted["id"] = b64(b"another id")
        with clock(T):
            answer = self.client.post(SETUP, {"action": "passkey-add", "name": "x", "credential": json.dumps(posted)})
        self.assertContains(answer, "The passkey was not added")

    def test_the_challenge_is_used_once_and_lasts_five_minutes(self):
        authenticator = Authenticator()
        with clock(T):
            options = self.client.get(SETUP).context["passkey_options"]
            self.client.post(SETUP, {"action": "passkey-add", "name": "x", "credential": authenticator.create(options)})
            self.assertEqual(AdminPasskey.objects.count(), 1)
            # Another authenticator, with the same challenge: gone
            again = self.client.post(
                SETUP, {"action": "passkey-add", "name": "x", "credential": Authenticator().create(options)}
            )
            self.assertContains(again, "The passkey was not added")
            self.assertEqual(AdminPasskey.objects.count(), 1)
            options = self.client.get(SETUP).context["passkey_options"]
        with clock(T + webauthn.CHALLENGE_SECONDS):
            late = self.client.post(
                SETUP, {"action": "passkey-add", "name": "x", "credential": Authenticator().create(options)}
            )
        self.assertContains(late, "The passkey was not added")

    def test_a_challenge_of_another_session_is_refused(self):
        other = browser()
        with clock(T):
            self.password(other)
            options = other.get(SETUP).context["passkey_options"]
            self.client.get(SETUP)
            answer = self.client.post(
                SETUP, {"action": "passkey-add", "name": "x", "credential": Authenticator().create(options)}
            )
        self.assertContains(answer, "The passkey was not added")

    def test_a_credential_id_already_on_the_site(self):
        other = User.objects.create_user("other", password=PASSWORD, is_staff=True)
        taken = self.register(other)
        self.refused(authenticator=Authenticator(credential_id=taken.credential_id))
        self.assertEqual(AdminPasskey.objects.get().user, other)

    def test_malformed_never_fails_the_page(self):
        for posted in ("", "{", "[]", '{"type": "public-key"}', "\x00" * 100, "x" * 100000):
            with self.subTest(posted[:10]):
                self.client.get(SETUP)
                answer = self.client.post(SETUP, {"action": "passkey-add", "name": "x", "credential": posted})
                self.assertContains(answer, "The passkey was not added")

    def test_a_handle_made_meanwhile_by_another_session_refuses_the_older_options(self):
        with clock(T):
            options = self.client.get(SETUP).context["passkey_options"]
            AdminPasskeyHandle.objects.create(user=self.staff, handle=webauthn.new_user_handle())
            answer = self.client.post(
                SETUP, {"action": "passkey-add", "name": "x", "credential": Authenticator().create(options)}
            )
        self.assertContains(answer, "The passkey was not added")

    def test_told_and_written(self):
        with clock(T), self.captureOnCommitCallbacks(execute=True):
            self.add()
        self.assertEqual(len(self.told_texts()), 1)
        self.assertIn("The staff account volunteer added a passkey.", self.told_texts()[0])
        self.assertEqual(LogEntry.objects.get().change_message, "Passkey added")

    def test_the_flags_are_shown(self):
        with clock(T):
            self.add(Authenticator(flags=UP | UV | BE | BS), name="synced one")
            self.add(Authenticator(), name="key")
            page = self.client.get(SETUP)
        self.assertContains(page, "synced one")
        self.assertContains(page, "synced")
        self.assertContains(page, "on this device only")
        self.assertEqual(
            sorted(AdminPasskey.objects.values_list("backup_eligible", "backed_up")), [(False, False), (True, True)]
        )

    def test_the_name_is_text(self):
        with clock(T):
            self.add(name="<b>x</b>")
            page = self.client.get(SETUP)
        self.assertContains(page, "&lt;b&gt;x&lt;/b&gt;")
        self.assertNotContains(page, "<b>x</b>")


# ---------------------------------------------------------------------------------------------------------------------
# The sign-in
# ---------------------------------------------------------------------------------------------------------------------


class SignIn(Base):
    def setUp(self):
        super().setUp()
        self.key = self.register()

    def test_password_then_passkey(self):
        with clock(T):
            self.assertRedirects(self.password(), CODE, fetch_redirect_response=False)
            self.assertFalse(signed_in(self.client))
            pending_key = self.client.session.session_key
            page = self.client.get(CODE)
            self.assertContains(page, "Now confirm with your passkey")
            self.assertNotContains(page, 'name="code"')
            options = page.context["passkey_options"]
            self.assertEqual(
                options["allowCredentials"], [{"type": "public-key", "id": self.key.passkey.credential_id}]
            )
            self.assertEqual((options["rpId"], options["userVerification"]), (HOST, "preferred"))
            answer = self.client.post(CODE, {"credential": self.key.get(options)})
        self.assertRedirects(answer, "/admin/", fetch_redirect_response=False)
        session = self.client.session
        self.assertTrue(session[ADMIN_SESSION])
        self.assertNotEqual(session.session_key, pending_key)
        self.assertNotIn(PENDING, session)
        data = signing.loads(answer.cookies["admin_device"].value, salt="admin-known-device")
        self.assertEqual(data["factor"], ["passkey", self.key.passkey.pk])
        with clock(T + 60):
            self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_with_the_user_handle(self):
        answer = self.sign_in(self.key, handle=self.key.handle)
        self.assertRedirects(answer, "/admin/", fetch_redirect_response=False)

    def test_the_password_alone_reaches_nothing(self):
        urls = admin_urls()
        with clock(T):
            self.password(next="/admin/shops/shop/")
            for url in urls:
                for method in ("get", "post"):
                    answer = getattr(self.client, method)(url)
                    self.assertEqual(answer.status_code, 302, f"{method} {url}")
                    self.assertTrue(answer["Location"].startswith(LOGIN), f"{method} {url}")
            self.assertFalse(signed_in(self.client))
            answer = self.client.post(CODE, {"credential": self.key.get(self.options())})
        self.assertRedirects(answer, "/admin/shops/shop/", fetch_redirect_response=False)

    def refused(self, **broken):
        reset_limits()
        answer = self.sign_in(self.key, client=browser(), **broken)
        self.assertEqual(answer.status_code, 200)
        self.assertTrue(answer.context["passkey_wrong"])
        return answer

    def test_wrong_origins(self):
        for origin in (
            "http://localhost",
            "https://evil.example",
            "https://sub.localhost",
            "https://localhost:443x",
            "http://" + "a" * 56 + ".onion",
        ):
            with self.subTest(origin):
                self.refused(origin=origin)

    def test_every_refusal(self):
        cases = {
            "rp": {"rp": "evil.example"},
            "type": {"kind": "webauthn.create"},
            "challenge": {"challenge": webauthn.new_challenge()},
            "not present": {"flags": UV},
            "signature": {"tamper_signature": True},
            "data": {"tamper_data": True},
            "handle": {"handle": b64(b"another")},
            "bad handle": {"handle": "***"},
        }
        for name, broken in cases.items():
            with self.subTest(name):
                self.refused(**broken)
        self.assertFalse(AdminPasskey.objects.exclude(last_used=None).exists())

    def test_every_refusal_answers_the_same(self):
        def plain(answer):
            text = answer.content.decode()
            text = re.sub(r'name="csrfmiddlewaretoken" value="[^"]*"', "", text)
            return re.sub(
                r'<script id="passkey-options" type="application/json">.*?</script>', "", text, flags=re.DOTALL
            )

        pages = {
            plain(self.refused(**broken))
            for broken in (
                {"rp": "evil.example"},
                {"tamper_signature": True},
                {"origin": "https://evil.example"},
                {"flags": UV},
            )
        }
        other = User.objects.create_user("other", is_staff=True)
        stranger = self.register(other)
        reset_limits()
        client = browser()
        with clock(T):
            self.password(client)
            pages.add(plain(client.post(CODE, {"credential": stranger.get(self.options(client))})))
            self.options(client)
            pages.add(plain(client.post(CODE, {"credential": "{"})))
        self.assertEqual(len(pages), 1)

    def test_a_create_response_replayed_as_a_sign_in(self):
        with clock(T):
            self.password()
            options = self.options()
            answer = self.client.post(CODE, {"credential": self.key.create(options)})
        self.assert_refused(answer)
        with clock(T):
            options = self.options()
            answer = self.client.post(CODE, {"credential": self.key.create(options, kind="webauthn.get")})
        self.assert_refused(answer)

    def test_a_sign_in_response_posted_as_a_registration(self):
        with clock(T):
            self.assertRedirects(self.sign_in(self.key), "/admin/", fetch_redirect_response=False)
            options = self.client.get(SETUP).context["passkey_options"]
            fresh = Authenticator()
            answer = self.client.post(SETUP, {"action": "passkey-add", "name": "x", "credential": fresh.get(options)})
            self.assertContains(answer, "The passkey was not added")
            options = self.client.get(SETUP).context["passkey_options"]
            answer = self.client.post(
                SETUP, {"action": "passkey-add", "name": "x", "credential": fresh.get(options, kind="webauthn.create")}
            )
            self.assertContains(answer, "The passkey was not added")
        self.assertEqual(AdminPasskey.objects.count(), 1)

    def test_the_challenge_is_used_once(self):
        with clock(T):
            self.password()
            options = self.options()
            self.client.post(CODE, {"credential": self.key.get(options, tamper_signature=True)})
            # The same challenge again, right this time: gone
            self.assert_refused(self.client.post(CODE, {"credential": self.key.get(options)}))
            # A replay of a right one after the sign-in, in another browser that passed the password
            other = browser()
            self.password(other)
            self.options(other)
            right = self.key.get(options)
        self.assert_refused(other.post(CODE, {"credential": right}), other)

    def test_an_expired_challenge(self):
        with clock(T):
            self.password()
        with patch("config.second_factor.webauthn.CHALLENGE_SECONDS", 10):
            with clock(T):
                options = self.options()
            with clock(T + 11):
                self.assert_refused(self.client.post(CODE, {"credential": self.key.get(options)}))

    def test_a_challenge_of_another_pending_state(self):
        with clock(T):
            self.password()
            options = self.options()
            # The password again in the same browser: a new pending state, and the old challenge is gone with it
            self.password()
            self.options()
            self.assert_refused(self.client.post(CODE, {"credential": self.key.get(options)}))
            # The challenge of another browser's pending state
            other = browser()
            self.password(other)
            theirs = self.options(other)
            self.assert_refused(self.client.post(CODE, {"credential": self.key.get(theirs)}))

    def test_a_credential_of_another_account(self):
        other = User.objects.create_user("other", password=PASSWORD, is_staff=True)
        theirs = self.register(other)
        with clock(T):
            self.password()
            self.assert_refused(self.client.post(CODE, {"credential": theirs.get(self.options())}))

    def test_an_unknown_credential(self):
        with clock(T):
            self.password()
            self.assert_refused(self.client.post(CODE, {"credential": Authenticator().get(self.options())}))

    def test_malformed_posts_never_fail_the_page(self):
        good = json.loads(self.key.get({"challenge": "x"}))
        posts = [
            "",
            "{",
            "[]",
            "null",
            json.dumps({**good, "response": {**good["response"], "signature": "*"}}),
            json.dumps({**good, "response": {**good["response"], "authenticatorData": b64(b"\x00" * 3000)}}),
            json.dumps({**good, "response": {**good["response"], "clientDataJSON": b64(b"{" * 3000)}}),
            "x" * 200000,
        ]
        with clock(T):
            self.password()
            for posted in posts:
                with self.subTest(posted[:20]):
                    self.options()
                    self.assert_refused(self.client.post(CODE, {"credential": posted}))

    def test_wrong_passkeys_count_against_the_address_limits(self):
        with steady_limits(), clock(T):
            self.password()
            for _ in range(9):
                self.options()
                self.assertEqual(self.client.post(CODE, {"credential": "{"}).status_code, 200)
            options = self.options()
            refused = self.client.post(CODE, {"credential": self.key.get(options)})
            self.assertEqual(refused.status_code, 429)
            self.assertContains(refused, "Too many tries", status_code=429)
            self.assertFalse(signed_in(self.client))

    def test_wrong_passkeys_do_not_use_up_the_accounts_codes(self):
        """A passkey cannot be guessed: no budget of its own, only the address limits."""
        with steady_limits(), clock(T):
            self.password()
            for number in range(8):
                self.options()
                answer = self.client.post(CODE, {"credential": "{"}, REMOTE_ADDR=f"203.0.113.{number}")
                self.assertEqual(answer.status_code, 200)
            answer = self.client.post(CODE, {"credential": self.key.get(self.options())}, REMOTE_ADDR="203.0.113.99")
        self.assertRedirects(answer, "/admin/", fetch_redirect_response=False)

    def test_a_refused_passkey_is_told_once_a_day_and_written(self):
        with clock(T):
            self.password()
            self.assert_refused(
                self.client.post(CODE, {"credential": self.key.get(self.options(), tamper_signature=True)})
            )
            self.assert_refused(self.client.post(CODE, {"credential": self.key.get(self.options(), rp="x.example")}))
        texts = self.told_texts()
        self.assertEqual(len(texts), 1)
        self.assertIn(
            "A refused passkey after the right password for the staff account volunteer: its password may be "
            "known to someone else.",
            texts[0],
        )
        self.assertEqual(
            list(LogEntry.objects.values_list("change_message", flat=True)),
            ["A refused passkey after the right password"],
        )

    def test_two_requests_of_one_session_with_one_challenge(self):
        """The second reads the session before the first saved it: the challenge is still there, but spent."""
        with clock(T):
            self.password()
            options = self.options()
            saved = self.client.session[PENDING]
            self.assert_refused(self.client.post(CODE, {"credential": self.key.get(options, tamper_signature=True)}))
            session = self.client.session
            session[PENDING] = saved
            session.save()
            self.assert_refused(self.client.post(CODE, {"credential": self.key.get(options)}))

    def test_a_challenge_that_cannot_be_marked_spent_is_refused(self):
        from django.db import DatabaseError

        from shops.models import RequestCount

        with steady_limits(), clock(T):
            self.password()
            options = self.options()
            with patch.object(RequestCount.objects, "create", side_effect=DatabaseError("down")):
                self.assert_refused(self.client.post(CODE, {"credential": self.key.get(options)}))

    def test_an_empty_user_handle_is_none(self):
        self.assertRedirects(self.sign_in(self.key, handle=""), "/admin/", fetch_redirect_response=False)

    def test_the_backup_flags(self):
        synced = self.register(name="synced", flags=UP | UV | BE)
        self.assertTrue(synced.passkey.backup_eligible)
        reset_limits()
        # A credential does not become syncable, nor stop being so
        self.assertTrue(self.sign_in(synced, client=browser(), flags=UP | UV).context["passkey_wrong"])
        reset_limits()
        self.assertTrue(self.sign_in(self.key, client=browser(), flags=UP | UV | BE).context["passkey_wrong"])
        # Whether it is backed up now is read at each sign-in
        reset_limits()
        self.assertRedirects(
            self.sign_in(synced, client=browser(), flags=UP | UV | BE | BS), "/admin/", fetch_redirect_response=False
        )
        self.assertTrue(AdminPasskey.objects.get(pk=synced.passkey.pk).backed_up)
        reset_limits()
        self.assertRedirects(
            self.sign_in(synced, client=browser(), flags=UP | UV | BE), "/admin/", fetch_redirect_response=False
        )
        self.assertFalse(AdminPasskey.objects.get(pk=synced.passkey.pk).backed_up)

    def test_a_count_that_does_not_grow_is_refused_and_told(self):
        self.assertRedirects(self.sign_in(self.key), "/admin/", fetch_redirect_response=False)
        self.assertEqual(AdminPasskey.objects.get().sign_count, 1)
        # A copy of the authenticator, with the same count
        reset_limits()
        answer = self.sign_in(self.key, client=browser(), count=1)
        self.assertTrue(answer.context["passkey_wrong"])
        self.assertContains(answer, "The passkey was not accepted.")
        self.assertEqual(AdminPasskey.objects.get().sign_count, 1)
        texts = self.told_texts()
        self.assertEqual(len(texts), 1)
        self.assertIn("A passkey of the staff account volunteer signed with a count that did not grow", texts[0])
        self.assertEqual(
            LogEntry.objects.get().change_message, "A passkey signed with a count that did not grow: refused"
        )
        # A lower one too, and told once a day
        answer = self.sign_in(self.key, client=browser(), count=0)
        self.assertTrue(answer.context["passkey_wrong"])
        self.assertEqual(len(self.told_texts()), 1)
        # A greater one is fine
        self.assertRedirects(
            self.sign_in(self.key, client=browser(), count=5), "/admin/", fetch_redirect_response=False
        )

    def test_counts_that_stay_at_zero(self):
        """Many synced passkeys never count: zero and zero is accepted, again and again."""
        key = self.register(name="synced", step=0)
        for _ in range(3):
            reset_limits()
            self.assertRedirects(self.sign_in(key, client=browser()), "/admin/", fetch_redirect_response=False)
        # But once it counted, zero is a copy
        AdminPasskey.objects.filter(pk=key.passkey.pk).update(sign_count=3)
        reset_limits()
        self.assertTrue(self.sign_in(key, client=browser()).context["passkey_wrong"])

    def test_the_count_is_updated_atomically(self):
        with clock(T):
            self.password()
            options = self.options()
            posted = self.key.get(options)
            # Another sign-in stored a greater count between the check and the update
            real = AdminPasskey.objects.filter

            def racing(*args, **kwargs):
                rows = real(*args, **kwargs)
                if "sign_count" in kwargs:
                    AdminPasskey.objects.update(sign_count=9)
                return rows

            with patch.object(AdminPasskey.objects, "filter", side_effect=racing):
                self.assert_refused(self.client.post(CODE, {"credential": posted}))

    def test_a_moderator_too(self):
        moderator = User.objects.create_user("moderator", password=PASSWORD)
        call_command("moderators", "add", "moderator", stdout=StringIO())
        key = self.register(moderator)
        with clock(T):
            self.assertRedirects(self.password(username="moderator"), CODE, fetch_redirect_response=False)
            self.assertRedirects(
                self.client.post(CODE, {"credential": key.get(self.options())}),
                "/admin/",
                fetch_redirect_response=False,
            )


@SITE
class Onion(Base):
    def test_no_passkeys_on_the_onion_or_another_name(self):
        key = self.register()
        turn_on(self.staff)
        for client in (
            Client(HTTP_HOST=HOST, **HTTPS, headers={"x-entrance": "onion"}),
            Client(HTTP_HOST="127.0.0.1", **HTTPS),
            Client(HTTP_HOST=HOST),
        ):
            with self.subTest(client), clock(T):
                reset_limits()
                self.password(client)
                page = client.get(CODE)
                self.assertNotIn("passkey_options", page.context)
                self.assertContains(page, "Passkeys work only at https://localhost, not at this address.")
                # The code still works there
                self.assertContains(page, 'name="code"')
                # A passkey posted there: its origin is not the site's
                self.assertTrue(client.post(CODE, {"credential": key.get({"challenge": "x"})}).context["passkey_wrong"])

    def test_the_page_says_so_and_adds_none(self):
        client = Client(HTTP_HOST=HOST, **HTTPS, headers={"x-entrance": "onion"})
        self.password(client)
        page = client.get(SETUP)
        self.assertContains(page, "Passkeys work only at https://localhost, not at this address.")
        self.assertNotIn("passkey_options", page.context)
        answer = client.post(SETUP, {"action": "passkey-add", "name": "x", "credential": "{}"})
        self.assertContains(answer, "The passkey was not added")


# ---------------------------------------------------------------------------------------------------------------------
# The sessions, the day, the device
# ---------------------------------------------------------------------------------------------------------------------


class Sessions(Base):
    def test_removing_a_ends_the_sessions_opened_with_a_not_b(self):
        a, b = self.register(name="A"), self.register(name="B")
        with_a, with_b = browser(), browser()
        self.assertRedirects(self.sign_in(a, client=with_a), "/admin/", fetch_redirect_response=False)
        reset_limits()
        self.assertRedirects(self.sign_in(b, client=with_b), "/admin/", fetch_redirect_response=False)
        with clock(T + 60), self.captureOnCommitCallbacks(execute=True):
            answer = with_b.post(SETUP, {"action": "passkey-remove", "passkey": a.passkey.pk})
            self.assertRedirects(answer, SETUP, fetch_redirect_response=False)
            self.assertFalse(AdminPasskey.objects.filter(pk=a.passkey.pk).exists())
            self.assertEqual(with_b.get("/admin/").status_code, 200)
            self.assertRedirects(with_a.get("/admin/"), f"{LOGIN}?next=/admin/", fetch_redirect_response=False)
            self.assertFalse(signed_in(with_a))
        self.assertIn("The staff account volunteer removed a passkey.", self.told_texts()[0])
        self.assertEqual(LogEntry.objects.get(change_message="Passkey removed").object_id, str(self.staff.pk))

    def test_removing_the_one_of_this_session_ends_it(self):
        a, _b = self.register(name="A"), self.register(name="B")
        self.sign_in(a)
        with clock(T + 60):
            self.client.post(SETUP, {"action": "passkey-remove", "passkey": a.passkey.pk})
            self.assertEqual(self.client.get("/admin/").status_code, 302)
        self.assertFalse(signed_in(self.client))

    def test_a_session_of_the_code_holds_while_a_passkey_is_added_and_removed(self):
        factor = turn_on(self.staff)
        with clock(T):
            self.password()
            self.client.post(CODE, {"code": code(factor)})
            self.assertEqual(self.client.session[FACTOR_SESSION][:2], ["totp", factor.pk])
            self.add()
            # Opened with the code, it stays so, with its own day
            self.assertEqual(self.client.session[FACTOR_SESSION], ["totp", factor.pk, T])
            passkey = AdminPasskey.objects.get()
            self.client.post(SETUP, {"action": "passkey-remove", "passkey": passkey.pk})
            self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_a_session_of_a_passkey_holds_when_the_code_is_turned_off(self):
        factor = turn_on(self.staff)
        key = self.register()
        self.sign_in(key)
        with clock(T + 60):
            answer = self.client.post(SETUP, {"action": "off", "code": code(factor, T + 60)})
            self.assertRedirects(answer, SETUP, fetch_redirect_response=False)
            self.assertFalse(AdminSecondFactor.objects.exists())
            self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_a_session_of_the_code_ends_when_the_code_goes_and_passkeys_stay(self):
        factor = turn_on(self.staff)
        self.register()
        with clock(T):
            self.password()
            self.client.post(CODE, {"code": code(factor)})
        with clock(T + 60):
            self.client.post(SETUP, {"action": "off", "code": code(factor, T + 60)})
            self.assertEqual(self.client.get("/admin/").status_code, 302)

    def test_a_session_of_the_password_alone_ends_when_a_passkey_is_added_elsewhere(self):
        with clock(T):
            self.password()
            self.assertEqual(self.client.get("/admin/").status_code, 200)
            self.register()
            self.assertRedirects(self.client.get("/admin/"), f"{LOGIN}?next=/admin/", fetch_redirect_response=False)

    def test_a_marker_of_the_old_shape_ends_the_session(self):
        key = self.register()
        self.sign_in(key)
        session = self.client.session
        session[FACTOR_SESSION] = [key.passkey.pk, T]
        session.save()
        with clock(T + 60):
            self.assertEqual(self.client.get("/admin/").status_code, 302)

    def test_a_passkey_and_a_totp_with_the_same_key_are_not_mixed_up(self):
        key = self.register()
        factor = turn_on(self.staff)
        AdminPasskey.objects.filter(pk=key.passkey.pk).update(id=factor.pk)
        key.passkey = AdminPasskey.objects.get()
        self.sign_in(key)
        AdminPasskey.objects.all().delete()
        with clock(T + 60):
            # Opened with the passkey: the TOTP of the same number does not hold it
            self.assertEqual(self.client.get("/admin/").status_code, 302)

    def test_the_day(self):
        key = self.register()
        self.sign_in(key)
        with clock(T + 24 * 3600 - 1):
            self.assertEqual(self.client.get("/admin/").status_code, 200)
        with clock(T + 24 * 3600):
            self.assertEqual(self.client.get("/admin/").status_code, 302)
        self.assertFalse(signed_in(self.client))

    def test_the_known_browser_is_bound_to_the_passkey(self):
        from config.urls import known_device

        a, b = self.register(name="A"), self.register(name="B")
        answer = self.sign_in(a)
        cookie = answer.cookies["admin_device"].value
        request = RequestFactory().post(LOGIN, {"username": "volunteer"})
        request.COOKIES["admin_device"] = cookie
        self.assertTrue(known_device(request))
        AdminPasskey.objects.filter(pk=a.passkey.pk).delete()
        self.assertFalse(known_device(request))
        del b

    def test_a_cookie_of_the_password_alone_is_a_stranger_once_a_passkey_is_added(self):
        from config.urls import known_device

        answer = self.password()
        request = RequestFactory().post(LOGIN, {"username": "volunteer"})
        request.COOKIES["admin_device"] = answer.cookies["admin_device"].value
        self.assertTrue(known_device(request))
        self.register()
        self.assertFalse(known_device(request))

    def test_a_reset_or_a_new_passkey_ends_the_wait(self):
        key = self.register()
        with clock(T):
            self.password()
            options = self.options()
            self.register(name="new")
            self.assertRedirects(
                self.client.post(CODE, {"credential": key.get(options)}), LOGIN, fetch_redirect_response=False
            )
            self.password()
            options = self.options()
            call_command("admin_passkey", "reset", "volunteer", stdout=StringIO())
            self.assertRedirects(
                self.client.post(CODE, {"credential": key.get(options)}), LOGIN, fetch_redirect_response=False
            )
        self.assertFalse(signed_in(self.client))


# ---------------------------------------------------------------------------------------------------------------------
# The modes
# ---------------------------------------------------------------------------------------------------------------------


class Optional(Base):
    def test_either_signs_in(self):
        factor = turn_on(self.staff)
        key = self.register()
        with clock(T):
            self.password()
            page = self.client.get(CODE)
            self.assertContains(page, 'name="code"')
            self.assertIn("passkey_options", page.context)
            self.assertRedirects(
                self.client.post(CODE, {"code": code(factor)}), "/admin/", fetch_redirect_response=False
            )
        reset_limits()
        self.assertRedirects(self.sign_in(key, client=browser()), "/admin/", fetch_redirect_response=False)

    def test_the_last_one_can_go(self):
        key = self.register()
        self.sign_in(key)
        with clock(T + 60):
            self.client.post(SETUP, {"action": "passkey-remove", "passkey": key.passkey.pk})
        self.assertFalse(AdminPasskey.objects.exists())
        # And the account is back to the password alone
        self.assertRedirects(self.password(browser()), "/admin/", fetch_redirect_response=False)

    def test_every_way_together_each_account_its_own(self):
        """The password alone, a code, a passkey, side by side; nobody is sent anywhere."""
        with_code = User.objects.create_user("coder", password=PASSWORD, is_staff=True)
        with_key = User.objects.create_user("keyholder", password=PASSWORD, is_staff=True)
        factor = turn_on(with_code)
        key = self.register(with_key)
        alone = browser()
        self.assertRedirects(self.password(alone), "/admin/", fetch_redirect_response=False)
        self.assertEqual(alone.get("/admin/").status_code, 200)
        coder = browser()
        with clock(T):
            self.password(coder, username="coder")
            self.assertRedirects(coder.post(CODE, {"code": code(factor)}), "/admin/", fetch_redirect_response=False)
            self.assertEqual(coder.get("/admin/").status_code, 200)
        reset_limits()
        holder = browser()
        with clock(T):
            self.password(holder, username="keyholder")
            self.assertRedirects(
                holder.post(CODE, {"credential": key.get(self.options(holder))}),
                "/admin/",
                fetch_redirect_response=False,
            )
            self.assertEqual(holder.get("/admin/").status_code, 200)
        # The account without one still has no page in between
        self.assertEqual(alone.get("/admin/").status_code, 200)


@override_settings(ADMIN_SECOND_FACTOR="required")
class Required(Base):
    def test_a_passkey_is_enough(self):
        with clock(T):
            self.password()
            self.assertRedirects(self.client.get("/admin/"), SETUP, fetch_redirect_response=False)
            page = self.client.get(SETUP)
            self.assertContains(page, "This site asks every staff account for a second factor")
            self.add()
            self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_a_removal_at_the_same_moment_is_seen(self):
        """The code is turned off while another browser removes the passkey: the check is made again, locked."""
        factor = turn_on(self.staff)
        key = self.register()
        with clock(T):
            self.password()
            self.client.post(CODE, {"code": code(factor)})

            def meanwhile(*args, **kwargs):
                AdminPasskey.objects.filter(pk=key.passkey.pk).delete()
                return True

            with patch("config.second_factor.take_code", side_effect=meanwhile):
                answer = self.client.post(SETUP, {"action": "off", "code": "123456"})
        self.assertContains(answer, "This is the last second factor of your account")
        self.assertTrue(AdminSecondFactor.objects.filter(pk=factor.pk).exists())

    def test_the_last_factor_cannot_go(self):
        key = self.register()
        self.sign_in(key)
        with clock(T + 60):
            answer = self.client.post(SETUP, {"action": "passkey-remove", "passkey": key.passkey.pk})
            self.assertContains(answer, "This is the last second factor of your account")
            self.assertTrue(AdminPasskey.objects.exists())
            # With a code too, the passkey may go, and then the code may not
            factor = turn_on(self.staff)
            self.client.post(SETUP, {"action": "passkey-remove", "passkey": key.passkey.pk})
            self.assertFalse(AdminPasskey.objects.exists())
        reset_limits()
        with clock(T + 120):
            self.password()
            self.client.post(CODE, {"code": code(factor, T + 120)})
            answer = self.client.post(SETUP, {"action": "off", "code": code(factor, T + 150)})
        self.assertContains(answer, "This is the last second factor of your account")
        self.assertTrue(AdminSecondFactor.objects.exists())


@override_settings(ADMIN_SECOND_FACTOR="passkey")
class PasskeyMode(Base):
    def test_a_code_is_neither_asked_nor_accepted(self):
        factor = turn_on(self.staff)
        key = self.register()
        with clock(T):
            self.password()
            page = self.client.get(CODE)
            self.assertNotContains(page, 'name="code"')
            answer = self.client.post(CODE, {"code": code(factor)})
            self.assertFalse(signed_in(self.client))
            self.assertEqual(answer.status_code, 200)
            self.assertRedirects(
                self.client.post(CODE, {"credential": key.get(self.options())}),
                "/admin/",
                fetch_redirect_response=False,
            )
            page = self.client.get(SETUP)
            self.assertContains(page, "This site accepts only passkeys: the code from the app is neither asked for")
            self.assertContains(page, "Add at least two")

    def test_an_account_with_a_code_only_adds_a_passkey_with_its_code_once(self):
        factor = turn_on(self.staff)
        with clock(T):
            # The password alone opens nothing: the code is asked, this once
            self.assertRedirects(self.password(next="/admin/shops/shop/"), CODE, fetch_redirect_response=False)
            self.assertFalse(signed_in(self.client))
            page = self.client.get(CODE)
            self.assertContains(page, "type the code from your app this once")
            self.assertContains(page, 'name="code"')
            answer = self.client.post(CODE, {"code": code(factor)})
            # Wherever the sign-in wanted to go: the page of the second factor, and no known browser
            self.assertRedirects(answer, SETUP, fetch_redirect_response=False)
            self.assertNotIn("admin_device", answer.cookies)
            self.assertEqual(self.client.session[FACTOR_SESSION], ["totp-bootstrap", factor.pk, T])
            page = self.client.get(SETUP)
            self.assertContains(page, "only to add a passkey")
            self.assertNotContains(page, "Turn the second factor off")
            # Nothing else of /admin/
            for url in admin_urls():
                if url == SETUP:
                    continue
                for method in ("get", "post"):
                    reached = getattr(self.client, method)(url)
                    self.assertEqual(reached.status_code, 302, f"{method} {url}")
                    self.assertEqual(reached["Location"], SETUP, f"{method} {url}")
            # Nor taking the code away
            refused = self.client.post(SETUP, {"action": "off", "code": code(factor, T + 30)})
            self.assertContains(refused, "cannot turn the code off")
            self.assertTrue(AdminSecondFactor.objects.filter(pk=factor.pk).exists())
            # Another browser opened with the code meanwhile
            other = browser()
            self.password(other)
            other.post(CODE, {"code": code(factor, T + 30)})
            self.assertEqual(other.get(SETUP).status_code, 200)
            # The passkey: this session becomes one opened with it, and reaches /admin/
            self.add()
            passkey = AdminPasskey.objects.get()
            self.assertEqual(self.client.session[FACTOR_SESSION][:2], ["passkey", passkey.pk])
            self.assertEqual(self.client.get("/admin/").status_code, 200)
            # The other browser's session of the code ends, and the code is no longer asked nor accepted
            self.assertRedirects(other.get(SETUP), f"{LOGIN}?next={SETUP}", fetch_redirect_response=False)
            self.assertFalse(signed_in(other))
            reset_limits()
            third = browser()
            self.password(third)
            self.assertNotContains(third.get(CODE), 'name="code"')
            third.post(CODE, {"code": code(factor, T + 90)})
            self.assertFalse(signed_in(third))

    def test_the_password_alone_cannot_add_a_passkey_to_an_account_with_a_code(self):
        factor = turn_on(self.staff)
        with clock(T):
            self.password()
            for answer in (
                self.client.get(SETUP),
                self.client.post(SETUP, {"action": "passkey-add", "name": "x", "credential": "{}"}),
            ):
                self.assertEqual(answer.status_code, 302)
                self.assertTrue(answer["Location"].startswith(LOGIN))
        self.assertFalse(AdminPasskey.objects.exists())
        # A session of the password alone from before the code was turned on ends too
        AdminSecondFactor.objects.all().delete()
        old = browser()
        with clock(T):
            self.password(old)
            self.assertEqual(old.get(SETUP).status_code, 200)
            AdminSecondFactor.objects.create(user=self.staff, secret=factor.secret, confirmed=factor.confirmed)
            answer = old.post(SETUP, {"action": "passkey-add", "name": "x", "credential": "{}"})
            self.assertEqual(answer.status_code, 302)
            self.assertFalse(signed_in(old))
        self.assertFalse(AdminPasskey.objects.exists())

    def test_a_temporary_password_is_changed_first_and_then_the_passkey(self):
        factor = turn_on(self.staff)
        call_command("temporary_password", "volunteer", stdout=StringIO(), stderr=StringIO())
        with clock(T):
            self.password()
            self.assertRedirects(self.client.post(CODE, {"code": code(factor)}), SETUP, fetch_redirect_response=False)
            self.assertRedirects(self.client.get("/admin/"), "/admin/password_change/", fetch_redirect_response=False)
            self.assertEqual(self.client.get("/admin/password_change/").status_code, 200)
            new = "a brand new secret 8421 z"
            self.client.post(
                "/admin/password_change/", {"old_password": PASSWORD, "new_password1": new, "new_password2": new}
            )
            self.assertRedirects(self.client.get("/admin/"), SETUP, fetch_redirect_response=False)
            self.assertEqual(self.client.get(SETUP).status_code, 200)

    def test_wrong_codes_of_the_bootstrap_are_counted_and_told(self):
        factor = turn_on(self.staff)
        with steady_limits(), clock(T):
            self.client.post(
                LOGIN, {"username": "volunteer", "password": PASSWORD, "next": "/admin/"}, REMOTE_ADDR="203.0.113.200"
            )
            for number in range(5):
                answer = self.client.post(CODE, {"code": "000000"}, REMOTE_ADDR=f"203.0.113.{number}")
                self.assertContains(answer, "That code is not right")
            refused = self.client.post(CODE, {"code": code(factor)}, REMOTE_ADDR="203.0.113.100")
            self.assertEqual(refused.status_code, 429)
            self.assertFalse(signed_in(self.client))
        texts = self.told_texts()
        self.assertEqual(len(texts), 1)
        self.assertIn("A wrong second-factor code after the right password for the staff account volunteer", texts[0])
        self.assertEqual(LogEntry.objects.get().change_message, "A wrong second-factor code after the right password")

    def test_no_code_is_set_up(self):
        with clock(T):
            self.password()
            page = self.client.get(SETUP)
        self.assertContains(page, "a code from an app cannot be set up")
        self.assertFalse(AdminSecondFactor.objects.exists())
        self.assertRedirects(
            self.client.post(SETUP, {"action": "on", "code": "123456"}), SETUP, fetch_redirect_response=False
        )

    def test_a_session_of_the_code_ends(self):
        factor = turn_on(self.staff)
        with override_settings(ADMIN_SECOND_FACTOR="optional"), clock(T):
            self.password()
            self.client.post(CODE, {"code": code(factor)})
            self.assertEqual(self.client.get("/admin/").status_code, 200)
        with clock(T + 60):
            self.assertEqual(self.client.get("/admin/").status_code, 302)
        self.assertFalse(signed_in(self.client))

    def test_the_last_passkey_cannot_go_and_a_code_does_not_count(self):
        turn_on(self.staff)
        key = self.register()
        self.sign_in(key)
        with clock(T + 60):
            answer = self.client.post(SETUP, {"action": "passkey-remove", "passkey": key.passkey.pk})
        self.assertContains(answer, "This is the last second factor of your account")
        self.assertTrue(AdminPasskey.objects.exists())


# ---------------------------------------------------------------------------------------------------------------------
# The page: rename, remove, nobody else's
# ---------------------------------------------------------------------------------------------------------------------


class Page(Base):
    def setUp(self):
        super().setUp()
        self.key = self.register(name="mine")
        self.sign_in(self.key)

    def test_rename(self):
        with clock(T + 60):
            self.client.post(
                SETUP,
                {"action": "passkey-rename", "passkey": self.key.passkey.pk, "name": "key on my keyring" + "x" * 100},
            )
            self.assertEqual(AdminPasskey.objects.get().name, ("key on my keyring" + "x" * 100)[:60])
            self.assertContains(self.client.get(SETUP), "key on my keyring")

    def test_nobody_sees_or_touches_another_accounts_passkeys(self):
        other = User.objects.create_superuser("boss", password=PASSWORD)
        theirs = self.register(self.staff, name="secret-name-of-volunteer")
        boss = browser()
        with clock(T + 60):
            self.password(boss, username="boss")
            pages = [
                SETUP,
                "/admin/",
                f"/admin/auth/user/{self.staff.pk}/change/",
                f"/admin/auth/user/{self.staff.pk}/history/",
                "/admin/auth/user/",
            ]
            for url in pages:
                page = boss.get(url)
                self.assertEqual(page.status_code, 200, url)
                text = page.content.decode()
                self.assertNotIn("secret-name-of-volunteer", text, url)
                self.assertNotIn(theirs.passkey.credential_id, text, url)
                self.assertNotIn(self.key.passkey.credential_id, text, url)
            for action in ("passkey-remove", "passkey-rename"):
                boss.post(SETUP, {"action": action, "passkey": theirs.passkey.pk, "name": "taken"})
            self.assertEqual(AdminPasskey.objects.get(pk=theirs.passkey.pk).name, "secret-name-of-volunteer")
            for lookup in (
                "admin_passkeys__credential_id__startswith",
                "admin_passkeys__name",
                "admin_passkey_handle__handle__startswith",
            ):
                self.assertEqual(boss.get(f"/admin/auth/user/?{lookup}=a").status_code, 400, lookup)
        del other

    def test_bad_keys_do_nothing(self):
        with clock(T + 60):
            for value in ("", "x", "-1", "99999999999999999999999", "1.0", "١"):
                answer = self.client.post(SETUP, {"action": "passkey-remove", "passkey": value})
                self.assertRedirects(answer, SETUP, fetch_redirect_response=False)
        self.assertTrue(AdminPasskey.objects.exists())

    def test_the_models_are_not_in_the_admin(self):
        self.assertNotIn(AdminPasskey, admin.site._registry)
        self.assertNotIn(AdminPasskeyHandle, admin.site._registry)

    def test_in_italian_and_german(self):
        from django.utils import translation

        with clock(T + 60):
            page = self.client.get(SETUP, headers={"accept-language": "it"})
        self.assertContains(page, "Aggiungi una passkey")
        # The example community has no German: the catalogue, formal
        with translation.override("de"):
            self.assertEqual(
                translation.gettext("Your account has no passkey yet."), "Ihr Konto hat noch keinen Passkey."
            )


# ---------------------------------------------------------------------------------------------------------------------
# The commands, preflight, the setting
# ---------------------------------------------------------------------------------------------------------------------


class Commands(Base):
    def test_list(self):
        self.register()
        self.register()
        User.objects.create_user("anna", is_staff=True)
        out = StringIO()
        call_command("admin_passkey", "list", stdout=out)
        text = out.getvalue()
        self.assertIn("ADMIN_SECOND_FACTOR is optional. Staff accounts: 2, with a passkey: 1", text)
        self.assertIn("  volunteer  2", text)
        self.assertIn("  anna  0", text)
        self.assertNotIn(AdminPasskey.objects.first().credential_id, text)
        out = StringIO()
        with override_settings(ADMIN_SECOND_FACTOR="passkey"):
            call_command("admin_totp", "list", stdout=out)
        self.assertIn("ADMIN_SECOND_FACTOR is passkey.", out.getvalue())

    def test_reset(self):
        key = self.register()
        self.register()
        self.sign_in(key)
        out = StringIO()
        with self.captureOnCommitCallbacks(execute=True):
            call_command("admin_passkey", "reset", "volunteer", stdout=out)
        self.assertIn("volunteer: 2 passkeys removed", out.getvalue())
        self.assertFalse(AdminPasskey.objects.exists())
        self.assertFalse(AdminPasskeyHandle.objects.exists())
        self.assertIn(
            "The passkeys of the staff account volunteer were removed with manage.py admin_passkey reset.",
            self.told_texts()[0],
        )
        self.assertEqual(LogEntry.objects.get().change_message, "Passkeys removed with manage.py admin_passkey reset")
        with clock(T + 60):
            self.assertEqual(self.client.get("/admin/").status_code, 302)
        self.assertRedirects(self.password(browser()), "/admin/", fetch_redirect_response=False)

    def test_reset_of_an_account_without_or_of_nobody(self):
        out = StringIO()
        call_command("admin_passkey", "reset", "volunteer", stdout=out)
        self.assertIn("has no passkey", out.getvalue())
        self.assertFalse(LogEntry.objects.exists())
        with self.assertRaises(CommandError):
            call_command("admin_passkey", "reset", "nobody", stdout=StringIO())

    def test_reset_says_what_is_left(self):
        turn_on(self.staff)
        self.register()
        out = StringIO()
        call_command("admin_passkey", "reset", "volunteer", stdout=out)
        self.assertIn("it signs in with its code from the app", out.getvalue())

    @override_settings(ADMIN_ALLOWED_IPS="", ADMIN_SECOND_FACTOR="passkey", SITE_HOST="shops.example.org")
    def test_preflight_in_passkey_mode(self):
        from shops.management.commands.preflight import admin_notes

        turn_on(self.staff)
        notes = admin_notes()
        self.assertEqual(len(notes), 1)
        self.assertIn("1 of 1 staff accounts have no passkey: they are sent to set one up", notes[0])
        AdminPasskey.objects.create(user=self.staff, credential_id="x", public_key=b"", algorithm=webauthn.ES256)
        self.assertEqual(admin_notes(), [])


@override_settings(ADMIN_SECOND_FACTOR="passkey")
class BootstrapOutsideTheAdmin(Base):
    """The session opened with the code only to add a first passkey, out of /admin/: a visitor, not signed out."""

    def test_the_browsers_own_requests_do_not_sign_it_out(self):
        factor = turn_on(self.staff)
        with clock(T):
            self.password()
            self.client.post(CODE, {"code": code(factor)})
            for url in ("/favicon.ico", "/en/shops/map/", "/en/shops/", "/en/shops/wallet/"):
                answer = self.client.get(url)
                self.assertTrue(signed_in(self.client), url)
                # Staff nowhere, and no merchant
                self.assertFalse(answer.wsgi_request.user.is_authenticated, url)
                self.assertIsNone(answer.wsgi_request.staff_user, url)
            self.assertEqual(self.client.get(SETUP).status_code, 200)
            for url in ("/admin/", "/admin/auth/user/", "/admin/today/"):
                self.assertRedirects(self.client.get(url), SETUP, fetch_redirect_response=False)
            self.add()
            self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_one_whose_mark_no_longer_holds_is_signed_out_there(self):
        factor = turn_on(self.staff)
        with clock(T):
            self.password()
            self.client.post(CODE, {"code": code(factor)})
        with clock(T + 24 * 3600):
            self.client.get("/en/shops/map/")
        self.assertFalse(signed_in(self.client))


class GoneChallenge(Base):
    """A refused passkey is told only when it answered a challenge the session still had."""

    def test_a_replaced_or_expired_challenge_is_not_told(self):
        key = self.register()
        with clock(T):
            self.password()
            first = self.options()
            # A second tab replaced it
            self.options()
            self.assert_refused(self.client.post(CODE, {"credential": key.get(first)}))
            # And none left: the one of the second tab went with that try
            self.assert_refused(self.client.post(CODE, {"credential": key.get(first)}))
            with patch("config.webauthn.CHALLENGE_SECONDS", 10):
                options = self.options()
        with clock(T + 11):
            self.assert_refused(self.client.post(CODE, {"credential": key.get(options)}))
        self.assertEqual(self.told_texts(), [])
        self.assertFalse(LogEntry.objects.exists())
        # A real refusal against a live challenge is told, the page the same
        with clock(T + 20):
            self.assert_refused(self.client.post(CODE, {"credential": key.get(self.options(), tamper_signature=True)}))
        self.assertEqual(len(self.told_texts()), 1)
        self.assertEqual(
            list(LogEntry.objects.values_list("change_message", flat=True)),
            ["A refused passkey after the right password"],
        )


# ---------------------------------------------------------------------------------------------------------------------
# The script
# ---------------------------------------------------------------------------------------------------------------------

SCRIPT = settings.BASE_DIR / "static" / "admin-passkey.js"
HARNESS = r"""
require(process.argv[2]);
const p = globalThis.adminPasskey;
const bytes = (buffer) => Array.from(new Uint8Array(buffer));
const all = new Uint8Array(256).map((_, index) => index);
const errors = [];
for (const bad of ["A", "a+b", "a/b", "ab==", "ab=", null, 7]) {
  try { p.fromBase64url(bad); errors.push(false); } catch (error) { errors.push(error instanceof TypeError); }
}
const created = p.creationOptions({ challenge: "AQID", user: { id: "BAU", name: "anna" }, rp: { id: "localhost" },
  excludeCredentials: [{ type: "public-key", id: "Bg" }] });
const requested = p.requestOptions({ challenge: "AQID", allowCredentials: [{ type: "public-key", id: "Bw" }],
  userVerification: "preferred" });
const attestation = { id: "CA", rawId: new Uint8Array([8]).buffer, type: "public-key",
  response: { clientDataJSON: new Uint8Array([1, 2]).buffer, attestationObject: new Uint8Array([3]).buffer } };
const assertion = { id: "CQ", rawId: new Uint8Array([9]).buffer, type: "public-key",
  response: { clientDataJSON: new Uint8Array([1]).buffer, authenticatorData: new Uint8Array([2]).buffer,
    signature: new Uint8Array([255, 254]).buffer, userHandle: null } };
const handled = { ...assertion, response: { ...assertion.response, userHandle: new Uint8Array([4, 5]).buffer } };
const empty = { ...assertion, response: { ...assertion.response, userHandle: new Uint8Array([]).buffer } };
process.stdout.write(JSON.stringify({
  round: bytes(p.fromBase64url(p.toBase64url(all))).join() === Array.from(all).join(),
  text: p.toBase64url(new Uint8Array([251, 255, 191])),
  view: p.toBase64url(new Uint8Array([0, 251, 255, 191, 0]).subarray(1, 4)),
  empty: bytes(p.fromBase64url("")),
  errors,
  created: [bytes(created.challenge), bytes(created.user.id), created.user.name, bytes(created.excludeCredentials[0].id),
    created.rp.id],
  requested: [bytes(requested.challenge), bytes(requested.allowCredentials[0].id), requested.userVerification],
  none: p.requestOptions({ challenge: "" }).allowCredentials,
  attestation: p.credentialJSON(attestation),
  assertion: p.credentialJSON(assertion),
  handled: p.credentialJSON(handled).response.userHandle,
  emptyHandle: p.credentialJSON(empty).response.userHandle,
  supported: [p.supported({}), p.supported({ PublicKeyCredential() {}, navigator: { credentials: {} } }),
    p.supported({ PublicKeyCredential() {}, navigator: {} })],
  failure: [p.failure("No.", { name: "NotAllowedError" }), p.failure("No.", null)],
}));
"""


class Script(SimpleTestCase):
    def test_the_pure_functions(self):
        node = node_or_skip(self)
        with tempfile.TemporaryDirectory() as folder:
            harness = Path(folder) / "harness.js"
            harness.write_text(HARNESS)
            result = subprocess.run(
                [node, str(harness), str(SCRIPT)], capture_output=True, text=True, timeout=60, check=False
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertTrue(out["round"])
        self.assertEqual(out["text"], "-_-_")
        self.assertEqual(out["view"], "-_-_")
        self.assertEqual(out["empty"], [])
        self.assertEqual(out["errors"], [True] * 7)
        self.assertEqual(out["created"], [[1, 2, 3], [4, 5], "anna", [6], "localhost"])
        self.assertEqual(out["requested"], [[1, 2, 3], [7], "preferred"])
        self.assertEqual(out["none"], [])
        self.assertEqual(
            out["attestation"],
            {
                "id": "CA",
                "rawId": "CA",
                "type": "public-key",
                "response": {"clientDataJSON": "AQI", "attestationObject": "Aw"},
            },
        )
        self.assertEqual(
            out["assertion"],
            {
                "id": "CQ",
                "rawId": "CQ",
                "type": "public-key",
                "response": {"clientDataJSON": "AQ", "authenticatorData": "Ag", "signature": "__4", "userHandle": None},
            },
        )
        self.assertEqual(out["handled"], "BAU")
        self.assertIsNone(out["emptyHandle"])
        self.assertEqual(out["supported"], [False, True, False])
        self.assertEqual(out["failure"], ["No. (NotAllowedError)", "No. (Error)"])

    def test_it_sends_nothing_but_its_form(self):
        from shops.test_scripts import assert_talks_to_nobody

        assert_talks_to_nobody(self, "admin-passkey.js")
        text = SCRIPT.read_text()
        self.assertIn("form.submit()", text)

    def test_the_templates_load_it_from_a_file(self):
        for name in ("second_factor.html", "second_factor_code.html"):
            text = (settings.BASE_DIR / "templates" / "admin" / name).read_text()
            self.assertIn("{% static 'admin-passkey.js' %}", text)
            self.assertIn('json_script:"passkey-options"', text)
            self.assertNotIn("<script>", text)


@SITE
class ResetSaysWhoWasTold(TransactionTestCase):
    """The command tells the moderators after its commit, synchronously, and says how it went."""

    def test_told_and_not_told(self):
        user = User.objects.create_user("volunteer", password=PASSWORD, is_staff=True)
        for answer, words in (
            (("TELEGRAM_MODERATION_CHAT", []), "told: TELEGRAM_MODERATION_CHAT"),
            ((None, ["Telegram refused it"]), "not told: Telegram refused it"),
        ):
            Base.register(None, user)
            out = StringIO()
            with patch("shops.notifications.deliver_to_moderators", return_value=answer):
                call_command("admin_passkey", "reset", "volunteer", stdout=out)
            self.assertIn(words, out.getvalue())
