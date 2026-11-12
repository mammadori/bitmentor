"""Nostr public keys as people write them (npub, NIP-19) and as events carry them (64 hexadecimal digits)."""
import re

import bech32

HEX_KEY = re.compile(r"[0-9a-f]{64}")


def to_hex(text):
    """The 64-digit hexadecimal key from an `npub1…` or from the hexadecimal itself; ValueError if it is neither. Case does
    not matter for the hexadecimal; a Bech32 string is all lowercase or all uppercase, as the standard says."""
    if not isinstance(text, str):
        raise ValueError("not text")
    text = text.strip()
    if text.lower().startswith("npub1"):
        hrp, data = bech32.bech32_decode(text)
        raw = bech32.convertbits(data, 5, 8, False) if data else None
        if hrp != "npub" or raw is None or len(raw) != 32:
            raise ValueError("this is not a valid npub: a character is wrong or missing")
        return bytes(raw).hex()
    text = text.lower()
    if not HEX_KEY.fullmatch(text):
        raise ValueError("a key is an npub1… or 64 hexadecimal digits")
    return text


def to_npub(pubkey):
    return bech32.bech32_encode("npub", bech32.convertbits(bytes.fromhex(pubkey), 8, 5))
