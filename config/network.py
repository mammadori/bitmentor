"""What a server chosen by someone else may lead us to: only public addresses, and names asked within a time limit.

Used wherever bitmentor connects to a host written by a user or sent by another server (Lightning addresses, Nostr
relays, OpenStreetMap)."""
import ipaddress
import socket
import threading

# IPv4-compatible IPv6 addresses (RFC 4291, deprecated): `::127.0.0.1` is not "global" for anyone, but `ipaddress` says it is
IPV4_COMPATIBLE = ipaddress.ip_network("::/96")
# The well-known NAT64 prefix (RFC 6052): behind a NAT64 gateway 64:ff9b::7f00:1 is 127.0.0.1. What counts is the IPv4
# address inside, so a server on an IPv6-only network with DNS64 still reaches the public ones
NAT64 = ipaddress.ip_network("64:ff9b::/96")
# A name is asked for at most this long, and only this many at once per process: a lookup past its time keeps its place
# until it really ends, and the others are refused at once instead of waiting
DNS_SECONDS = 4
DNS_AT_ONCE = 8
DNS_SLOTS = threading.BoundedSemaphore(DNS_AT_ONCE)


def is_public(address):
    """True for an address anyone on the internet can have. An IPv4 address carried inside an IPv6 one (mapped, NAT64)
    counts as that IPv4 address; an IPv4-compatible one is never public."""
    ip = ipaddress.ip_address(address)
    if ip.version == 6:
        if ip.ipv4_mapped:
            return ip.ipv4_mapped.is_global
        if ip in IPV4_COMPATIBLE:
            return False
        if ip in NAT64:
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF).is_global
    return ip.is_global


class SlowName(TimeoutError):
    """No answer for a name in time, or too many names being asked already: nothing is known about the name. A caller
    that needs the name to go on fails closed ("busy, try again"), and does not let a library ask it again unbounded."""


def resolve(host, port=443, seconds=None, **options):
    """socket.getaddrinfo, waited for at most `seconds` and never more than DNS_SECONDS: pass what is left of the
    caller's own deadline. The resolver has no timeout of its own we can set, so the lookup runs in a thread: the caller
    stops waiting on time, while that thread, and its place among DNS_AT_ONCE, stay taken until the lookup really ends.
    SlowName (a TimeoutError, so an OSError) past the time, with no time left, or when DNS_AT_ONCE lookups are already
    running.

    Connect to the addresses returned. Handing the name to a library that resolves it again (urllib3, socket.create_
    connection) makes a second lookup with no limit at all, in the caller's thread, and lets a name that changes its
    answer lead somewhere else."""
    limit = DNS_SECONDS if seconds is None else min(seconds, DNS_SECONDS)
    if limit <= 0:
        raise SlowName(f"no time left to ask for {host}")
    # The semaphore taken is the one released, even if the module's is replaced meanwhile (the tests do)
    slots = DNS_SLOTS
    if not slots.acquire(blocking=False):
        raise SlowName(f"too many names being asked: {host}")
    box = {}

    def ask():
        try:
            box["found"] = socket.getaddrinfo(host, port, **options)
        except BaseException as error:  # noqa: BLE001 - handed to the caller as it is
            box["error"] = error
        finally:
            slots.release()

    worker = threading.Thread(target=ask, daemon=True, name="dns")
    try:
        worker.start()
    except RuntimeError as error:
        # "can't start new thread": it never ran: its place would stay taken for ever
        slots.release()
        raise SlowName(f"no thread to ask for {host}") from error
    worker.join(limit)
    if worker.is_alive():
        raise SlowName(f"no answer in time: {host}")
    if "error" in box:
        raise box["error"]
    return box["found"]
