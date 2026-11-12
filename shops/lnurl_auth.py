"""LUD-04 login: the wallet signs a challenge with the key dedicated to this domain."""
import re

import bech32
import coincurve

K1 = re.compile(r"[0-9a-f]{64}")
# A key has only one form: the compressed one, as LUD-04 asks, otherwise it would open two accounts
LINKING_KEY = re.compile(r"0[23][0-9a-f]{64}")


def encode_lnurl(url):
    return bech32.bech32_encode("lnurl", bech32.convertbits(url.encode(), 8, 5)).upper()


def verify(k1, signature, key):
    if not LINKING_KEY.fullmatch(key):
        return False
    try:
        public_key = coincurve.PublicKey(bytes.fromhex(key))
        return public_key.verify(bytes.fromhex(signature), bytes.fromhex(k1), hasher=None)
    except ValueError:
        return False
