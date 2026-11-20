"""Retiring the files of the Blossom server (website/blossom.py), off unless the community asks.

A hash is for ever, a site's disk is not. When the community replaces a file of the theme, the old hash would be a 404
at once. With `blossom_retire_days` in `community.yml`, the community puts the old file in `blossom-retired/` (beside
`theme/`, same folders if it likes) and for that many days from when bitmentor first sees it there the file is still
served, with a `Sunset` header saying until when (BUD-01). After that it is not served, even if it is still in the
folder: a 308 to a server of `blossom_mirrors` that has the same hash, or a 410 saying, when a file of the theme has the
same path, which hash is there now. bitmentor copies nothing and writes no file: the community decides what to keep,
and `deploy/blossom-retire.py` puts the old files in the folder for it, from git or from an older copy.

The mirrors are asked in the background, at most once a day for each file, never while a visitor waits: `HEAD
<mirror>/<sha256>`, https only, no redirect followed.
"""

import datetime
import ipaddress
import logging
import re
import threading
import time
from urllib.parse import urlsplit

import requests
from django.db import DatabaseError
from django.utils import timezone
from django.utils.http import http_date

from config import community as community_module
from config.community import community

from . import blossom

logger = logging.getLogger(__name__)

FOLDER = "blossom-retired"
MAX_DAYS = 3650
MAX_MIRRORS = 10
CHECK_EVERY = datetime.timedelta(days=1)
CHECK_AGAIN = datetime.timedelta(hours=1)  # after a mirror did not answer
CHECK_AT_ONCE = 20
CHECK_SECONDS = 5
CHECK_PAUSE = 600  # seconds between two looks at what is to ask, in a process
LOG_PAUSE = 60
CACHE_AFTER = "public, max-age=3600"  # of «it is there now» and «gone»: both can change
VERSION = re.compile(r"\.[0-9a-f]{8}(\.[A-Za-z0-9]{1,10})\Z")

_lock = threading.Lock()
_hashes = {}  # (path, size, mtime_ns) -> sha256
_state = {"root": None, "at": 0.0, "index": {}, "rows": {}, "checking": False, "checked": 0.0, "logged": 0.0}


def days():
    value = community().get("blossom_retire_days")
    return min(value, MAX_DAYS) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def mirrors():
    """The servers of `blossom_mirrors`: https, a name and nothing else (no address in numbers, no local name)."""
    value = community().get("blossom_mirrors")
    found = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, str):
            continue
        address = item.strip().rstrip("/").lower()
        if not address.startswith("https://") or set(address) & set("?#@\\ \t\r\n") or address in found:
            continue
        try:
            parts = urlsplit(address)
            host, _port = parts.hostname or "", parts.port
        except ValueError:
            continue
        if parts.path or "." not in host or host.endswith((".localhost", ".local", ".internal", ".")):
            continue
        try:
            ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            found.append(address)
    return found[:MAX_MIRRORS]


def reset():
    with _lock:
        _hashes.clear()
        _state.update(root=None, at=0.0, index={}, rows={}, checked=0.0, logged=0.0)


def scan(root):
    found = {}
    seen = set()
    for path in blossom.public_files(root / FOLDER):
        try:
            stat = path.stat()
            key = (str(path), stat.st_size, stat.st_mtime_ns)
            if key not in _hashes:
                _hashes[key] = blossom.sha256_of(path)
        except OSError:
            continue
        seen.add(key)
        found.setdefault(_hashes[key], (path, stat.st_size))
    for key in [key for key in _hashes if key not in seen]:
        del _hashes[key]
    return found


def remember(root, found):
    """The rows of the retired files, as the folder and the theme are now: a file new in the folder gets its row, and
    its days start; a hash the theme serves again loses its row, so that retired again it has its days again."""
    from .models import BlossomRetired

    live = blossom.index()
    rows = {row.sha256: row for row in BlossomRetired.objects.all()}
    back = [digest for digest in rows if digest in live]
    if back:
        BlossomRetired.objects.filter(sha256__in=back).delete()
    now = timezone.now()
    new = [
        BlossomRetired(sha256=digest, path=path.relative_to(root / FOLDER).as_posix()[:500], size=size, seen=now)
        for digest, (path, size) in found.items()
        if digest not in rows and digest not in live
    ]
    if new:
        BlossomRetired.objects.bulk_create(new, ignore_conflicts=True)
    if new or back:
        rows = {row.sha256: row for row in BlossomRetired.objects.all()}
    return rows


def index():
    """(hash -> (path, size) of the files in the folder, hash -> row), looked at as often as the theme's. No request
    reads the database for a hash: an unknown one costs nothing."""
    root = community_module.COMMUNITY_DIR
    with _lock:
        if _state["root"] != root or time.monotonic() - _state["at"] > blossom.RESCAN_SECONDS:
            found = scan(root)
            try:
                rows = remember(root, found)
            except DatabaseError:
                rows = _state["rows"] if _state["root"] == root else {}
                if time.monotonic() - _state["logged"] > LOG_PAUSE:
                    _state["logged"] = time.monotonic()
                    logger.exception("blossom: the retired files could not be read")
            _state.update(root=root, at=time.monotonic(), index=found, rows=rows)
        return _state["index"], _state["rows"]


def answer(request, digest):
    """What to say for a hash the theme no longer has: the retired file, where it is now, «gone», or None (unknown)."""
    keep = days()
    if not keep:
        return None
    files, rows = index()
    row = rows.get(digest)
    if row is None:
        return None
    check_soon()
    found = files.get(digest)
    until = row.seen + datetime.timedelta(days=keep)
    left = int((until - timezone.now()).total_seconds())
    if found and left > 0:
        response = blossom.send(request, *found)
        if response is not None:
            from . import blossom_visits

            blossom_visits.seen(request, blossom_visits.BLOB)
            response["Sunset"] = http_date(until.timestamp())
            response["Cache-Control"] = f"public, max-age={min(left, 86400)}"
            return blossom.with_headers(response)
    extension = "." + row.path.rsplit(".", 1)[1].lower() if "." in row.path.rsplit("/", 1)[-1] else ""
    if row.mirror and row.mirror in mirrors():
        response = blossom.refused(308, "retired here, kept by another server")
        response["Location"] = f"{row.mirror}/{digest}{extension}"
        response["Cache-Control"] = CACHE_AFTER
        return response
    # The path it had in the theme: deploy/blossom-retire.py adds the hash's start to the name of a second old version
    was = VERSION.sub(r"\1", row.path)
    now = next(
        (
            other
            for other, (path, _size) in blossom.index().items()
            if path.relative_to(community_module.COMMUNITY_DIR / "theme").as_posix() in (row.path, was)
        ),
        "",
    )
    response = blossom.refused(410, f"retired; the file is now {now}" if now else "retired")
    response["Cache-Control"] = CACHE_AFTER
    return response


def has(mirror, digest, size):
    """Whether a mirror has the blob: a plain 200 to HEAD with the file's own length, no redirect followed. None when
    the mirror did not answer."""
    try:
        response = requests.head(f"{mirror}/{digest}", timeout=CHECK_SECONDS, allow_redirects=False)
    except requests.RequestException:
        return None
    if 500 <= response.status_code < 600:
        return None
    return response.status_code == 200 and response.headers.get("Content-Length") == str(size)


def check_mirrors():
    """Asks the mirrors for some of the retired files not asked about today, and writes down who has each. A mirror
    that does not answer changes nothing: what was known stays, and the file is asked about again in an hour."""
    from .models import BlossomRetired

    servers = mirrors()
    if not servers or not days():
        return
    now = timezone.now()
    due = BlossomRetired.objects.filter(checked__isnull=True) | BlossomRetired.objects.filter(
        checked__lt=now - CHECK_EVERY
    )
    for row in due.order_by("checked", "seen")[:CHECK_AT_ONCE]:
        # Taken by one process only: whoever changes the row asks
        if not BlossomRetired.objects.filter(pk=row.pk, checked=row.checked).update(checked=now):
            continue
        answers = [(server, has(server, row.sha256, row.size)) for server in servers]
        mirror = next((server for server, answer in answers if answer), "")
        if mirror or all(answer is False for _server, answer in answers):
            BlossomRetired.objects.filter(pk=row.pk).update(mirror=mirror)
        else:
            BlossomRetired.objects.filter(pk=row.pk).update(checked=now - CHECK_EVERY + CHECK_AGAIN)


def check_soon():
    """The mirrors are asked in a thread of their own, one at a time and not more often than every CHECK_PAUSE, so no
    visitor waits for them and none makes the site ask."""
    if not mirrors():
        return
    with _lock:
        if _state["checking"] or (_state["checked"] and time.monotonic() - _state["checked"] < CHECK_PAUSE):
            return
        _state.update(checking=True, checked=time.monotonic())

    def run():
        from django.db import connection

        try:
            check_mirrors()
        except Exception:
            logger.exception("blossom: the mirrors could not be asked")
        finally:
            connection.close()
            with _lock:
                _state["checking"] = False

    try:
        threading.Thread(target=run, daemon=True).start()
    except RuntimeError:
        with _lock:
            _state["checking"] = False
