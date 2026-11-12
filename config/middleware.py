"""Headers that Django does not set by itself."""
from django.conf import settings

CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'inline-speculation-rules'",
    # Leaflet and the admin write inline styles: a style does not run code
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: https://tile.openstreetmap.org",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])
PERMISSIONS = "camera=(), microphone=(), payment=(), usb=(), geolocation=(self)"
# Pages and data are read only from the site. Public files stay open to everyone: the theme's logo is the Nostr profile
# picture, and a Nostr client shows it from another site.
PUBLIC_FILES = ("/static/", "/theme/")


def csp_with_connect(origins):
    """The CSP of a page that talks to other servers: it declares them, and they are not opened to the other pages."""
    return f"{CSP}; connect-src 'self' {' '.join(origins)}"


class SecurityHeaders:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("Permissions-Policy", PERMISSIONS)
        response.headers.setdefault("Cross-Origin-Resource-Policy",
                                    "cross-origin" if request.path.startswith(PUBLIC_FILES) else "same-origin")
        return response


class HeadWithoutBody:
    """A response to HEAD has the headers and no body. gunicorn sends files (FileResponse, with sendfile) also
    to HEAD: with persistent connections to Caddy those extra bytes spoil the next response on the same
    connection, which may belong to another visitor, and Caddy answers 502. It sits first: it closes the response."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.method != "HEAD":
            return response
        if response.streaming:
            stream = getattr(response, "file_to_stream", None)
            if stream is not None:
                stream.close()
                response.file_to_stream = None
            response.streaming_content = []
        else:
            response.content = b""
        return response


class PrivateForAccounts:
    """A page answered to someone signed in is that person's: the panel, a shop's form, the owner's register. No cache
    keeps it, not even the browser's on disk, where the next person at a shared computer would find it with Back. A
    response that says its own Cache-Control (the theme's files, the register's service worker, the staff pages) keeps
    it. Without the session's cookie nobody is signed in, and the user is not looked for (config/guard.py): a visitor's
    page does not start to depend on the cookie. Last in MIDDLEWARE: it sees the view's answer before the others."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if (settings.SESSION_COOKIE_NAME in request.COOKIES and not response.has_header("Cache-Control")
                and (getattr(request, "staff_user", None) is not None or request.user.is_authenticated)):
            response["Cache-Control"] = "private, no-store"
        return response
