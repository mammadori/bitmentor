"""The checks of the addresses written in community.yml or by an owner (shops/register.py, meetups/conf.py), held
against one table: they share one core (config/weburl.py) and must keep answering as they did, a malformed port
included, which is refused and never raises. CSP and SSRF depend on them: a looser answer opens a door."""
from django.test import SimpleTestCase

from meetups.conf import safe_link

from .register import explorer_url, own_explorer_url, relay_url

CASES = [
    ('https://mempool.space/api', ['https://mempool.space/api', 'https://mempool.space/api', None, 'https://mempool.space/api']),
    ('https://mempool.space/api/', ['https://mempool.space/api', 'https://mempool.space/api', None, 'https://mempool.space/api/']),
    ('HTTPS://Mempool.Space/api', ['https://mempool.space/api', 'https://mempool.space/api', None, 'HTTPS://Mempool.Space/api']),
    ('http://mempool.space/api', [None, None, None, '']),
    ('https://mempool.space:443/api', [None, 'https://mempool.space:443/api', None, 'https://mempool.space:443/api']),
    ('https://mempool.space:8443/api', [None, 'https://mempool.space:8443/api', None, 'https://mempool.space:8443/api']),
    ('https://mempool.space:0/api', [None, None, None, 'https://mempool.space:0/api']),
    ('https://mempool.space:x/api', [None, None, None, '']),
    ('https://user@mempool.space/api', [None, None, None, '']),
    ('https://user:pw@mempool.space/api', [None, None, None, '']),
    ('https://:@mempool.space/api', [None, None, None, '']),
    ('https://mempool.space/api?x=1', [None, None, None, 'https://mempool.space/api?x=1']),
    ('https://mempool.space/api#f', [None, None, None, 'https://mempool.space/api#f']),
    ('https://127.0.0.1/api', [None, None, None, 'https://127.0.0.1/api']),
    ('https://[::1]/api', [None, None, None, 'https://[::1]/api']),
    ('https://localhost/api', [None, None, None, 'https://localhost/api']),
    ('https:///api', [None, None, None, '']),
    ('https://mempool.space/a b', [None, None, None, '']),
    ('https://mempool.space/a%20b', [None, None, None, 'https://mempool.space/a%20b']),
    ('https://evil.example\\.mempool.space/api', [None, None, None, '']),
    ('https://mempool.space./api', [None, None, None, 'https://mempool.space./api']),
    ('https://xn--bcher-kva.example/api', ['https://xn--bcher-kva.example/api', 'https://xn--bcher-kva.example/api', None, 'https://xn--bcher-kva.example/api']),
    ('https://bücher.example/api', [None, None, None, '']),
    ('wss://relay.example.org', [None, None, 'wss://relay.example.org', '']),
    ('wss://relay.example.org/', [None, None, 'wss://relay.example.org', '']),
    ('wss://relay.example.org:443', [None, None, None, '']),
    ('wss://u@relay.example.org', [None, None, None, '']),
    ('wss://relay.example.org:x', [None, None, None, '']),
    ('ws://relay.example.org', [None, None, None, '']),
    ('//relay.example.org', [None, None, None, '']),
    ('https://[::1', [None, None, None, '']),
    ('https://a.b:99999/x', [None, None, None, '']),
    ('  https://mempool.space/api  ', ['https://mempool.space/api', 'https://mempool.space/api', None, '']),
    ('', [None, None, None, '']),
    (None, [None, None, None, '']),
    (42, [None, None, None, '']),
    ('https://mempool.space/api\n', ['https://mempool.space/api', 'https://mempool.space/api', None, '']),
    ('https://-bad.example/x', [None, None, None, 'https://-bad.example/x']),
    ('https://a..b.example/x', [None, None, None, 'https://a..b.example/x']),
    ('https://example.org/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', ['https://example.org/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', None, None, 'https://example.org/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa']),
]



class Validators(SimpleTestCase):
    def test_every_address_gets_the_same_answer_as_before(self):
        for value, expected in CASES:
            with self.subTest(value=value):
                self.assertEqual([explorer_url(value), own_explorer_url(value), relay_url(value), safe_link(value)],
                                 expected)
