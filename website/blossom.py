"""Read-only Blossom (BUD-01): the community's public theme files, asked for by their SHA-256.

Anyone can check a file against its hash and copy it to another Blossom server. No upload, no authorization.
Served under `/blossom/`, so a client is given `https://<site>/blossom` as the server and asks for `<server>/<sha256>`.
Turned on by `blossom: true` in `community.yml`.
"""
import hashlib
import mimetypes
import os
import re
import threading
import time
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse, JsonResponse, StreamingHttpResponse

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

_lock = threading.Lock()
_hashes = {}  # (path, size, mtime_ns) -> sha256: a file is read once, again only if it changes
_state = {"root": None, "at": 0.0, "index": {}}


def enabled():
    return community().get("blossom") is True


def public_files(root):
    """The files of `<root>/theme` that are public: no hidden file or folder, no link, only the listed extensions."""
    base = root / "theme"
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
    for path in public_files(root):
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


def kind(path):
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def with_headers(response, immutable=True):
    for key, value in CORS.items():
        response[key] = value
    if immutable:
        response["Cache-Control"] = "public, max-age=31536000, immutable"
        response["Accept-Ranges"] = "bytes"
    return response


def preflight():
    response = HttpResponse(status=204)
    response["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
    response["Access-Control-Allow-Headers"] = "*"
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


def blob(request, name):
    """`GET`/`HEAD /blossom/<sha256>` or `/blossom/<sha256>.<ext>` (BUD-01). The extension is only a hint: the hash decides."""
    if request.method == "OPTIONS":
        return preflight() if enabled() else HttpResponse(status=404)
    if not enabled():
        raise Http404
    match = NAME.match(name)
    found = index().get(match[1].lower()) if match else None
    if not found:
        raise Http404
    path, size = found
    content_type = kind(path)
    if request.method == "HEAD":
        response = HttpResponse(content_type=content_type)
        response["Content-Length"] = str(size)
        return with_headers(response)
    span = RANGE.match(request.headers.get("Range", ""))
    if span:
        start = int(span[1])
        end = min(int(span[2] or size - 1), size - 1)
        if start >= size or end < start:
            response = HttpResponse(status=416)
            response["Content-Range"] = f"bytes */{size}"
            return with_headers(response)
        return with_headers(ranged(path, size, start, end, content_type))
    response = FileResponse(path.open("rb"), content_type=content_type)
    response["Content-Length"] = str(size)
    return with_headers(response)


def listing(request):
    """`GET /blossom/list`: every file served, with its hash, so a copy can be made elsewhere."""
    if not enabled():
        raise Http404
    root = community_module.COMMUNITY_DIR / "theme"
    base = settings.PUBLIC_URL
    files = []
    for digest, (path, size) in sorted(index().items(), key=lambda item: str(item[1][0])):
        extension = path.suffix.lower()
        files.append({"sha256": digest, "size": size, "type": kind(path),
                      "url": f"{base}/theme/{path.relative_to(root).as_posix()}",
                      "blossom": f"{base}/blossom/{digest}{extension}"})
    response = JsonResponse({"server": f"{base}/blossom", "files": files}, json_dumps_params={"ensure_ascii": False})
    response["Access-Control-Allow-Origin"] = "*"
    response["Cache-Control"] = "public, max-age=300"
    return response
