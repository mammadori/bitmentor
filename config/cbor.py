"""A small, strict CBOR decoder (RFC 8949) for what a passkey sends: the attestation object and the COSE public key
(config/webauthn.py). Decoding only: the site never writes CBOR.

Only what those use is read: unsigned and negative integers, byte strings, text strings, arrays, maps (with integer or
text keys, each once), and the simple values false, true and null. Everything else is refused with `CBORError`:
indefinite lengths, tags, floats, other simple values, reserved encodings, text that is not UTF-8, a length that does
not fit in what is left of the input, more than MAX_DEPTH levels of arrays and maps, input longer than MAX_SIZE, and,
in `decode`, bytes after the item. No error of the input raises anything else.
"""

MAX_SIZE = 16384
MAX_DEPTH = 6


class CBORError(ValueError):
    """The bytes are not CBOR this decoder accepts."""


def decode(data, max_size=MAX_SIZE):
    """The one item that is the whole of `data`."""
    value, end = decode_prefix(data, 0, max_size)
    if end != len(data):
        raise CBORError("bytes after the item")
    return value


def decode_prefix(data, offset=0, max_size=MAX_SIZE):
    """The item that starts at `offset`, and where it ends: for an item followed by other bytes (the COSE key inside
    the authenticator's data)."""
    if not isinstance(data, (bytes, bytearray)):
        raise CBORError("not bytes")
    if len(data) > max_size:
        raise CBORError("too long")
    if not 0 <= offset <= len(data):
        raise CBORError("outside the input")
    return _item(bytes(data), offset, 0)


def _argument(data, offset, info):
    """The number that follows an initial byte with `info` in its low five bits, and the offset after it."""
    if info < 24:
        return info, offset
    sizes = {24: 1, 25: 2, 26: 4, 27: 8}
    if info not in sizes:
        # 28-30 are reserved, 31 is an indefinite length
        raise CBORError("indefinite or reserved length")
    size = sizes[info]
    if offset + size > len(data):
        raise CBORError("truncated")
    return int.from_bytes(data[offset:offset + size], "big"), offset + size


def _item(data, offset, depth):
    if offset >= len(data):
        raise CBORError("truncated")
    initial = data[offset]
    major, info = initial >> 5, initial & 0x1F
    offset += 1
    if major == 7:
        simple = {20: False, 21: True, 22: None}
        if info not in simple:
            raise CBORError("a float or a simple value that is not false, true or null")
        return simple[info], offset
    if major == 6:
        raise CBORError("a tag")
    number, offset = _argument(data, offset, info)
    if major == 0:
        return number, offset
    if major == 1:
        return -1 - number, offset
    if major in (2, 3):
        if number > len(data) - offset:
            raise CBORError("a string longer than the input")
        chunk = data[offset:offset + number]
        offset += number
        if major == 2:
            return chunk, offset
        try:
            return chunk.decode("utf-8"), offset
        except UnicodeDecodeError:
            raise CBORError("text that is not UTF-8") from None
    if depth >= MAX_DEPTH:
        raise CBORError("nested too deep")
    # Every item takes at least one byte (a map's entry two): a count beyond what is left is refused before any loop
    if major == 4:
        if number > len(data) - offset:
            raise CBORError("an array longer than the input")
        items = []
        for _ in range(number):
            value, offset = _item(data, offset, depth + 1)
            items.append(value)
        return items, offset
    # major == 5, the only one left
    if number * 2 > len(data) - offset:
        raise CBORError("a map longer than the input")
    entries = {}
    for _ in range(number):
        key, offset = _item(data, offset, depth + 1)
        # bool is an int to Python, and True == 1: such a key would pass for another one
        if type(key) not in (int, str):
            raise CBORError("a map key that is not an integer or a text")
        if key in entries:
            raise CBORError("a map key twice")
        entries[key], offset = _item(data, offset, depth + 1)
    return entries, offset
