"""The common core of the checks of an address written in community.yml or by an owner: the right scheme, a server, no
user name or password, a port that is a number. Each caller adds its own rules (a domain name only, no port, a plain
path, …): these addresses end up in the Content-Security-Policy and in requests the site or the browser makes, so a
check must refuse, never raise, and never be looser than here."""
from urllib.parse import urlsplit


def parts(value, scheme):
    """(the urlsplit parts, the host in lower case, the port or None) of `value` stripped, or None when it is not a
    string, does not parse, has another scheme, no host, a user name or password, or a port that is not a number."""
    if not isinstance(value, str):
        return None
    try:
        split = urlsplit(value.strip())
        port = split.port
    except ValueError:
        return None
    host = (split.hostname or "").lower()
    if split.scheme.lower() != scheme or not host or split.username is not None or split.password is not None:
        return None
    return split, host, port
