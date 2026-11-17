"""Read-only Blossom (BUD-01): the community's public theme files, asked for by their SHA-256.

Anyone can check a file against its hash and copy it to another Blossom server. No upload, no authorization.
BUD-01 wants every endpoint at the root of the domain, so the server lives on hosts of its own, never under a path of
the site: with `blossom: true` in `community.yml` and BLOSSOM_HOSTS in the environment, each of those hosts answers
`/<sha256>` and `/list` and nothing else. The first host is the one the list gives; the others are the same server
under another name.

Every answer, errors included, carries CORS, and every error says why in `X-Reason`: 400 for a name that is not a hash,
404 for a hash not served, 403 «read-only server» for an upload, a mirror, a media request, PUT and DELETE.
`/list` is not BUD-02's `/list/<pubkey>`: nobody signs these files. It gives every file served, with its hash, its
size, its type, its address on the site and on Blossom, so that a copy can be made elsewhere. The requests for a blob
are counted, with no personal data, in `website/blossom_visits.py`.
"""
import hashlib
import mimetypes
import os
import re
import threading
import time
from pathlib import Path

from django.conf import settings
from django.http.request import split_domain_port
from django.http import FileResponse, HttpResponse, JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt

from config import community as community_module
from config.community import community

# Only what the site already serves from `theme/` and is a file for people: pictures, documents, sound, video, archives.
# Sources, styles, scripts and data (JSON) are not in the list on purpose.
EXTENSIONS = frozenset((".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".svg", ".ico", ".pdf", ".epub", ".mp3", ".ogg",
                        ".opus", ".m4a", ".wav", ".flac", ".mp4", ".webm", ".mov", ".zip", ".gz", ".tar", ".tgz", ".woff2"))
NAME = re.compile(r"([0-9a-fA-F]{64})(\.[A-Za-z0-9]{1,10})?\Z")
RANGE = re.compile(r"bytes=(\d{1,15})-(\d{0,15})\Z")
RESCAN_SECONDS = 30
CHUNK = 1024 * 1024
CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Expose-Headers": "*"}
READING = ("GET", "HEAD", "OPTIONS")
# The writing endpoints of BUD-02, BUD-04 and BUD-05: refused, with the reason, not «not found»
WRITING = frozenset(("upload", "mirror", "media", "report"))

_lock = threading.Lock()
_hashes = {}  # (path, size, mtime_ns) -> sha256: a file is read once, again only if it changes
_state = {"root": None, "at": 0.0, "index": {}}


def enabled():
    return community().get("blossom") is True


def server():
    """The address of the Blossom server, on its first host; empty without a host."""
    if not settings.BLOSSOM_HOSTS:
        return ""
    return f"{settings.PUBLIC_URL.partition('://')[0]}://{settings.BLOSSOM_HOSTS[0]}"


def public_files(base):
    """The files of a folder that are public: no hidden file or folder, no link, only the listed extensions."""
    found = []
    for folder, dirs, files in os.walk(base):
        dirs[:] = sorted(name for name in dirs if not name.startswith(".") and not os.path.islink(os.path.join(folder, name)))
        for name in sorted(files):
            path = Path(folder, name)
            if name.startswith(".") or path.suffix.lower() not in EXTENSIONS or path.is_symlink() or not path.is_file():
                continue
            found.append(path)
    return found


def sha256_of(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(root):
    index = {}
    seen = set()
    for path in public_files(root / "theme"):
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        seen.add(key)
        if key not in _hashes:
            _hashes[key] = sha256_of(path)
        index.setdefault(_hashes[key], (path, stat.st_size))
    # Forget the hashes of files that changed or are gone
    for key in [key for key in _hashes if key not in seen]:
        del _hashes[key]
    return index


def index():
    """hash -> (path, size). Built when first asked, then looked at again at most every RESCAN_SECONDS; a file whose
    size or modification time did not change is not read again."""
    root = community_module.COMMUNITY_DIR
    with _lock:
        if _state["root"] != root or time.monotonic() - _state["at"] > RESCAN_SECONDS:
            _state.update(root=root, at=time.monotonic(), index=build(root))
        return _state["index"]


def reset():
    with _lock:
        _hashes.clear()
        _state.update(root=None, at=0.0, index={})
    from . import blossom_retire
    blossom_retire.reset()


def kind(path):
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def with_headers(response, immutable=True):
    for key, value in CORS.items():
        response[key] = value
    if immutable:
        response.setdefault("Cache-Control", "public, max-age=31536000, immutable")
        response["Accept-Ranges"] = "bytes"
    return response


def refused(status, reason):
    """An error as BUD-01 asks: the reason in X-Reason, with CORS, so a client on another site can read it."""
    response = HttpResponse(reason, status=status, content_type="text/plain; charset=utf-8")
    response["X-Reason"] = reason
    return with_headers(response, immutable=False)


def preflight():
    response = HttpResponse(status=204)
    # PUT and DELETE are named so that a client gets to ask, and reads the 403 with its reason
    response["Access-Control-Allow-Methods"] = "GET, HEAD, PUT, DELETE, OPTIONS"
    response["Access-Control-Allow-Headers"] = "Authorization, *"
    response["Access-Control-Max-Age"] = "86400"
    return with_headers(response, immutable=False)


def ranged(path, size, start, end, content_type):
    def chunks():
        with path.open("rb") as stream:
            stream.seek(start)
            left = end - start + 1
            while left > 0:
                data = stream.read(min(CHUNK, left))
                if not data:
                    break
                left -= len(data)
                yield data

    response = StreamingHttpResponse(chunks(), status=206, content_type=content_type)
    response["Content-Length"] = str(end - start + 1)
    response["Content-Range"] = f"bytes {start}-{end}/{size}"
    return response


@csrf_exempt
def blob(request, name):
    """`GET`/`HEAD /<sha256>` or `/<sha256>.<ext>` (BUD-01). The extension is only a hint: the hash decides."""
    if not enabled():
        return refused(404, "blossom is off")
    if request.method == "OPTIONS":
        return preflight()
    if request.method not in READING or name.lower() in WRITING:
        return refused(403, "read-only server")
    match = NAME.match(name)
    if not match:
        return refused(400, "not a sha256, with an optional extension")
    found = index().get(match[1].lower())
    if not found:
        from . import blossom_retire
        return blossom_retire.answer(request, match[1].lower()) or refused(404, "blob not found")
    from . import blossom_visits
    blossom_visits.seen(request, blossom_visits.BLOB)
    response = send(request, *found)
    return with_headers(response) if response else refused(404, "blob not found")


def send(request, path, size):
    """The file, its headers alone, or a part of it; None when it changed or went since the index was made."""
    try:
        if path.stat().st_size != size:
            raise FileNotFoundError(path)
    except OSError:
        # A 404 now, its new hash at the next look
        reset()
        return None
    content_type = kind(path)
    if request.method == "HEAD":
        response = HttpResponse(content_type=content_type)
        response["Content-Length"] = str(size)
        return response
    span = RANGE.match(request.headers.get("Range", ""))
    if span:
        start = int(span[1])
        end = min(int(span[2] or size - 1), size - 1)
        if start >= size or end < start:
            response = HttpResponse(status=416)
            response["Content-Range"] = f"bytes */{size}"
            return response
        return ranged(path, size, start, end, content_type)
    response = FileResponse(path.open("rb"), content_type=content_type)
    response["Content-Length"] = str(size)
    return response


@csrf_exempt
def listing(request):
    """`GET /list`: every file served, with its hash, so a copy can be made elsewhere."""
    if not enabled():
        return refused(404, "blossom is off")
    if request.method == "OPTIONS":
        return preflight()
    if request.method not in READING:
        return refused(403, "read-only server")
    from . import blossom_visits
    blossom_visits.seen(request, blossom_visits.LIST)
    root = community_module.COMMUNITY_DIR / "theme"
    base = settings.PUBLIC_URL
    blossom = server()
    files = []
    for digest, (path, size) in sorted(index().items(), key=lambda item: str(item[1][0])):
        extension = path.suffix.lower()
        files.append({"sha256": digest, "size": size, "type": kind(path),
                      "url": f"{base}/theme/{path.relative_to(root).as_posix()}",
                      "blossom": f"{blossom}/{digest}{extension}"})
    response = JsonResponse({"server": blossom, "files": files}, json_dumps_params={"ensure_ascii": False})
    response["Cache-Control"] = "public, max-age=300"
    return with_headers(response, immutable=False)


class DedicatedHost:
    """The hosts of BLOSSOM_HOSTS are only the Blossom server, at their root: `/<sha256>`, `/list`, the rest refused."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # The name without port and final dot: «host.:8443» is the same host, and must not reach the site
        if split_domain_port(request.get_host())[0].rstrip(".") not in settings.BLOSSOM_HOSTS:
            return self.get_response(request)
        name = request.path.removeprefix("/")
        if name == "list":
            return listing(request)
        if not name:
            return refused(404, "ask for /<sha256> or /list")
        return blob(request, name)
