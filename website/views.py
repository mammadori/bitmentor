import json
import mimetypes
import posixpath
import re
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from uuid import UUID

import markdown
import segno
from django.conf import settings
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.template import engines
from django.template.loader import select_template
from django.urls import reverse
from django.utils._os import safe_join
from django.utils.html import escape
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.safestring import mark_safe
from django.utils.translation import get_language, gettext as _, override
from django.views.static import serve

from config import ratelimit
from config.community import (COMMUNITY_DIR, community, front_matter, localized, page_path,
                              public_slugs)
from config.middleware import CSP, csp_with_connect
from meetups import conf as meetups_conf
from shops import details
from shops.directory import featured_shops, statistics
from website.visibility import COOKIE, MarkError, filtered, is_private_page, is_staff_page

EXTENSIONS = ["attr_list", "fenced_code", "md_in_html", "tables", "sane_lists", "footnotes"]
THEME_IMAGE = re.compile(r'<img\b[^>]*\bsrc="(/theme/[^"]+\.(?:webp|jpg|png))"[^>]*>')
REDUCED_WIDTHS = (480, 800)
RANGE = re.compile(r"bytes=(\d{1,15})-(\d{0,15})$")
RANGE_CHUNK = 4 * 1024 * 1024
SWITCHES_PER_HOUR = 120
COOKIE_SECONDS = 365 * 24 * 3600
LONG_LIVED = (".woff2", ".webp", ".jpg", ".png", ".svg", ".pdf", ".mp4", ".webm", ".epub")
SERVICE_WORKER = re.compile(r"sw(-[a-z]+)?\.js\Z")  # it lives in /theme/, but the theme may use it for a page outside it
CONNECT_ORIGIN = re.compile(r"https://[a-z0-9.-]+\Z")
# The shop pages that robots.txt closes in every language: those of people who have an account. The map and a shop's
# page stay open. The till and the sign stay open too, as they are found in a link or a QR: with Disallow a search
# engine does not open them, does not see their noindex and may keep the address in its results. The panel and the
# sign-in are `shops/` and nothing else: with `$` the line closes only that address, with `?` the same with `?next=`.
PRIVATE_SHOP_PATHS = ("$", "?", "edit/", "remove/", "new/", "recovery/", "email/", "card/", "first-day/",
                      "window/", "registers/", "openstreetmap/")
# Missing in the slim image: without them, fonts arrive as application/octet-stream
for kind, extension in (("font/woff2", ".woff2"), ("application/epub+zip", ".epub"), ("video/webm", ".webm"), ("text/vtt", ".vtt")):
    mimetypes.add_type(kind, extension)


def theme_file(request, path):
    """The theme files: long cache for the heavy ones, and Range so that phones can play the videos.

    CSS, JS and JSON change together with the HTML at every release: with `no-cache` the browser checks them every time,
    and if they have not changed `serve` answers 304 with no body. With a timed cache the new HTML would use the old ones.
    """
    response = serve(request, path, document_root=COMMUNITY_DIR / "theme")
    response["Cache-Control"] = "public, max-age=2592000" if path.endswith(LONG_LIVED) else "no-cache"
    response["Accept-Ranges"] = "bytes"
    if SERVICE_WORKER.match(path):
        response["Service-Worker-Allowed"] = "/"
    match = RANGE.match(request.headers.get("Range", ""))
    if not match or response.status_code != 200:
        return response
    response.close()
    # The file `serve` has just answered with, read as it reads it: normalised and inside the theme. The path as written
    # can start with a slash (/theme//logo.svg), and joined as it is it would be a path from the root of the disk
    file = Path(safe_join(COMMUNITY_DIR / "theme", posixpath.normpath(path).lstrip("/")))
    size = file.stat().st_size
    start = int(match[1])
    end = min(int(match[2] or size - 1), size - 1, start + RANGE_CHUNK - 1)
    if start >= size or end < start:
        return HttpResponse(status=416, headers={"Content-Range": f"bytes */{size}"})
    with file.open("rb") as stream:
        stream.seek(start)
        chunk = stream.read(end - start + 1)
    return HttpResponse(chunk, status=206, content_type=mimetypes.guess_type(path)[0], headers={
        "Content-Range": f"bytes {start}-{end}/{size}", "Accept-Ranges": "bytes", "Cache-Control": response["Cache-Control"]})


@lru_cache(maxsize=1024)
def reduced_copies(src):
    """The reduced copies next to the original: `photo-480.webp` and `photo-800.webp` for `photo.webp`."""
    stem, dot, extension = src.rpartition(".")
    found = []
    for width in REDUCED_WIDTHS:
        name = f"{stem}-{width}.webp"
        if (COMMUNITY_DIR / name.removeprefix("/")).exists():
            found.append((name, width))
    return found


def responsive(html):
    """The theme images weigh less without whoever writes the pages having to think about it."""
    first = True

    def rewrite(match):
        nonlocal first
        tag = match[0]
        extra = []
        width = re.search(r'\bwidth="(\d+)"', tag)
        copies = [(name, size) for name, size in reduced_copies(match[1]) if width and size < int(width[1])]
        if copies and "srcset=" not in tag:
            options = [f"{name} {size}w" for name, size in copies] + [f"{match[1]} {width[1]}w"]
            extra += [f'srcset="{", ".join(options)}"', 'sizes="auto, (min-width: 1240px) 700px, 100vw"']
        if "loading=" not in tag:
            extra.append('fetchpriority="high"' if first else 'loading="lazy"')
        first = False
        if "decoding=" not in tag:
            extra.append('decoding="async"')
        return f"{tag[:-1].rstrip('/ ')} {' '.join(extra)}>" if extra else tag
    return THEME_IMAGE.sub(rewrite, html)


def inert_text(value):
    """A text inside a Markdown page: outside letters, digits and spaces every character becomes an entity."""
    return mark_safe("".join(char if char.isalnum() or char == " " else f"&#{ord(char)};" for char in value))


def inert_value(value):
    """A value of a shop for a page of the community: texts made inert, numbers, dates and ids as they are, lists and
    dictionaries (`public_details`) value by value. Anything else, a model, a manager, a method, is empty: a page must not
    reach the owner, the register or a column through it."""
    if isinstance(value, str):
        return inert_text(value)
    if isinstance(value, dict):
        return {key: inert_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [inert_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, Decimal, date, datetime, UUID)):
        return value
    return ""


class InertShop:
    """A shop inside a Markdown page: its texts must not become links, images or attributes.

    Anyone with an account writes them, and Markdown filters neither `javascript:` nor `{onerror=...}`.
    Outside letters, digits and spaces every character becomes an entity: it stays readable and is no longer syntax.
    The shop itself is `_shop`, and nothing that starts with an underscore is passed on: `{{ shop.shop.phone }}` is empty.
    """

    def __init__(self, shop):
        self._shop = shop

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        # A page of the community may write {{ shop.<a detail> }}: what the owner hides (shops/details.py) is empty there too
        if name in details.CHOICES and not details.chosen(self._shop, name):
            return ""
        return inert_value(getattr(self._shop, name))


def parse(text):
    """Returns the optional leading metadata and the body of the page."""
    return front_matter(text)


def route():
    language = get_language()
    stops = [{**stop, "name": localized(stop["name"], language), "text": localized(stop["text"], language)}
             for stop in community().get("route", [])]
    return {"route": stops, "route_json": json.dumps(stops)}


def donations():
    return [{"label": label, "value": value, "qr": segno.make(value, error="m").svg_data_uri(scale=5, border=2)}
            for label, value in community().get("donations", {}).items()]


def convert(text, backlink):
    return responsive(markdown.markdown(text, extensions=EXTENSIONS, extension_configs={"footnotes": {"BACKLINK_TITLE": backlink}}))


@lru_cache(maxsize=128)
def page_html(text, backlink):
    """The Markdown of a page as HTML. For the record it takes hundreds of milliseconds per request, and the result
    depends only on these two arguments: the key is the text already passed through the template, so new statistics
    and shops make a new key. Each process keeps its own 128 pages, a few tens of MB at most."""
    return convert(text, backlink)


def page(request, slug):
    # Every answer of a page, a 404 too, has the same cache headers: they must not tell which pages have a private part
    request.visibility_varies = True
    return render_page(request, slug)


def render_page(request, slug):
    """A page of the community, rendered."""
    request.visibility_varies = True
    path = page_path(get_language(), slug)
    if not path.exists():
        raise Http404
    meta, text = parse(path.read_text())
    # The private part goes before the template and Markdown: what the page does not show is never rendered, and the
    # converted text, the key of page_html's cache, is already the one of this view
    private = request.visibility.private
    if is_staff_page(meta):
        if not request.visibility.staff:
            raise Http404
        private = request.visibility.private
    elif is_private_page(meta) and not private:
        raise Http404
    try:
        text = filtered(text, private)
    except MarkError:
        raise Http404  # wrong marks hide the page rather than show what might be private: the check website.W003 says where
    featured = featured_shops(city=community().get("map", {}).get("featured_city"))
    data = {"stats": statistics(), "featured": [InertShop(shop) for shop in featured], **route(), "donations": donations()}
    html = engines["django"].from_string(text).render(data, request)
    body = page_html(html, _("Back to note %d in the text"))
    template = select_template([f"website/{slug}.html", "website/page.html"])
    response = render(request, template.template.name, {
        "body": body, "slug": slug, "page_title": meta.get("title", ""), "page_description": meta.get("description", ""),
        "page_image": meta.get("image", ""), "wide": meta.get("layout") == "wide"})
    origins = [origin for origin in meta.get("connect", "").split() if CONNECT_ORIGIN.match(origin)]
    if origins:
        response.headers["Content-Security-Policy"] = csp_with_connect(origins)
    # A public page the community lets other sites show in a frame (`embeddable` in community.yml): never a private or
    # staff page, and never the panel or the register, which are not community pages
    if not is_staff_page(meta) and not is_private_page(meta) and embeddable(slug):
        csp = response.headers.get("Content-Security-Policy", CSP)
        response.headers["Content-Security-Policy"] = csp.replace("frame-ancestors 'none'", f"frame-ancestors {embed_from()}")
        response.xframe_options_exempt = True
    return response


def embeddable(slug):
    """Whether `slug` matches one of the patterns of `embeddable` in community.yml (shell-style: `game-*`)."""
    from fnmatch import fnmatchcase

    patterns = community().get("embeddable") or []
    return isinstance(patterns, list) and any(isinstance(p, str) and fnmatchcase(slug, p) for p in patterns)


def embed_from():
    """Which sites may show those pages: `embed_from` in community.yml, a list of https origins; every site when missing."""
    origins = [origin for origin in community().get("embed_from") or [] if isinstance(origin, str) and CONNECT_ORIGIN.match(origin)]
    return " ".join(origins) if origins else "*"


def sitemap(request):
    """Every page in every language it exists in, with the other languages and x-default next to it, like the pages' hreflang."""
    base = settings.PUBLIC_URL
    languages = list(community()["languages"])
    versions = {}
    for language in languages:
        for slug in public_slugs(language):
            address = f"{base}/{language}/" if slug == "home" else f"{base}/{language}/{slug}/"
            versions.setdefault(slug, {})[language] = address
    shop_map, meetups = {}, {}
    for language in languages:
        with override(language):
            shop_map[language] = f"{base}{reverse('shop_map')}"
            # The meetup board has its own address unless a page of the community has taken it (meetups/views.py)
            if meetups_conf.enabled() and "meetups" not in public_slugs(language):
                meetups[language] = f"{base}{reverse('meetups')}"
    entries = []
    for language in languages:
        for group in [versions[slug] for slug in public_slugs(language)] + [shop_map] + ([meetups] if language in meetups else []):
            alternates = list(group.items())
            if settings.LANGUAGE_CODE in group:
                alternates.append(("x-default", group[settings.LANGUAGE_CODE]))
            links = "".join(f'<xhtml:link rel="alternate" hreflang="{code}" href="{escape(url)}"/>' for code, url in alternates)
            entries.append(f"<url><loc>{escape(group[language])}</loc>{links}</url>")
    return HttpResponse('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
                        f' xmlns:xhtml="http://www.w3.org/1999/xhtml">{"".join(entries)}</urlset>',
                        content_type="application/xml")


def robots(request):
    """Indexable only in production: outside production Caddy answers first, this applies if the site is reached directly."""
    if settings.SITE_MODE != "production":
        return HttpResponse("User-agent: *\nDisallow: /\n", content_type="text/plain")
    paths = ["/admin/", "/register/", "/lnurl-auth/"]
    paths += [f"/{language}/shops/{page}" for language in community()["languages"] for page in PRIVATE_SHOP_PATHS]
    closed = "".join(f"Disallow: {path}\n" for path in paths)
    return HttpResponse(f"User-agent: *\n{closed}Sitemap: {settings.PUBLIC_URL}/sitemap.xml\n",
                        content_type="text/plain")


def switch_view(request):
    """Switches the private part of the pages off or on for whoever may see it, and goes back to the page. The cookie
    only remembers the choice: who may see is decided again at every request (website/visibility.py). For everyone else
    this address does not exist."""
    if not request.visibility.entitled:
        raise Http404
    if request.method != "POST":
        return HttpResponse(status=405, headers={"Allow": "POST"})
    if ratelimit.over_limit(f"view:{ratelimit.client_ip(request)}", SWITCHES_PER_HOUR, 3600):
        return HttpResponse(status=429)
    target = request.POST.get("next", "")
    if not (target.startswith("/") and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()})):
        target = "/"
    response = HttpResponseRedirect(target)
    response.set_cookie(COOKIE, "on" if request.POST.get("private") == "on" else "off", max_age=COOKIE_SECONDS,
                        secure=settings.SESSION_COOKIE_SECURE, httponly=True, samesite="Lax")
    return response
