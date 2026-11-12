"""The head of the pages: what search engines and the apps that show a link preview read."""
import json
import locale
import struct
from functools import lru_cache

from django import template
from django.conf import settings
from django.urls import reverse
from django.utils.encoding import escape_uri_path, iri_to_uri
from django.utils.safestring import mark_safe
from django.utils.translation import get_language, to_locale

import config.community as community_config

register = template.Library()
# The JPEG markers that open a frame and give its size (not C4, C8, CC)
JPEG_FRAMES = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
# Inside <script> no text must be able to close the tag: as Django's json_script does
JSON_ESCAPES = {ord("<"): "\\u003C", ord(">"): "\\u003E", ord("&"): "\\u0026"}


class CaptureNode(template.Node):
    def __init__(self, nodelist, name):
        self.nodelist = nodelist
        self.name = name

    def render(self, context):
        context[self.name] = mark_safe(self.nodelist.render(context).strip())
        return ""


@register.tag
def capture(parser, token):
    """`{% capture as name %}…{% endcapture %}` renders the content once and puts it in `name`.

    It serves to use a block twice: the title a template chooses with `{% block title %}` goes into og:title too.
    """
    bits = token.split_contents()
    if len(bits) != 3 or bits[1] != "as":
        raise template.TemplateSyntaxError("capture is used like this: {% capture as name %}…{% endcapture %}")
    nodelist = parser.parse(("endcapture",))
    parser.delete_first_token()
    return CaptureNode(nodelist, bits[2])


def absolute(address):
    """The address with PUBLIC_URL in front, never with the Host header: whoever opens the site from the onion or under
    another name must not end up in the links that are shared nor in those search engines read. An address that is
    already complete stays as it is."""
    return f"{settings.PUBLIC_URL}{address}" if address.startswith("/") else address


def theme_image(src):
    """The file of a theme image, `/theme/...`; None for external addresses and for paths that leave the theme."""
    if not src.startswith("/theme/"):
        return None
    theme = (community_config.COMMUNITY_DIR / "theme").resolve()
    file = (theme / src.removeprefix("/theme/")).resolve()
    return file if file.is_relative_to(theme) else None


@lru_cache(maxsize=256)
def image_size(file):
    """Width and height of a JPEG or a PNG, read from the header; None if the file is missing or of another type."""
    try:
        with open(file, "rb") as stream:
            head = stream.read(24)
            if head.startswith(b"\x89PNG\r\n\x1a\n"):
                return struct.unpack(">II", head[16:24])
            if not head.startswith(b"\xff\xd8"):
                return None
            stream.seek(2)
            while stream.read(1) == b"\xff":
                code = stream.read(1)
                while code == b"\xff":
                    code = stream.read(1)
                (length,) = struct.unpack(">H", stream.read(2))
                if code and code[0] in JPEG_FRAMES:
                    height, width = struct.unpack(">xHH", stream.read(5))
                    return width, height
                stream.seek(length - 2, 1)
    except (OSError, struct.error):
        pass
    return None


def og_locale(code):
    """Open Graph wants language and country, `it_IT`: if the language does not give the country, Python's table does."""
    name = to_locale(code)
    return name if "_" in name else locale.normalize(name).split(".")[0].split("@")[0]


def structured_data(data, slug, title, canonical, languages):
    """schema.org data made only of what the community wrote: on the home page who it is, on the other pages where one is."""
    root = absolute("/")
    if slug == "home":
        organization = {"@type": "Organization", "@id": f"{root}#organization", "name": data["name"], "url": root}
        # The logo is optional, and `links:` with no elements is None
        if data.get("logo"):
            organization["logo"] = absolute(f"/theme/{data['logo']}")
        if data.get("email"):
            organization["email"] = data["email"]
        profiles = [link["url"] for link in data.get("links") or [] if link.get("url")]
        if profiles:
            organization["sameAs"] = profiles
        graph = [organization, {"@type": "WebSite", "@id": f"{root}#website", "name": data["name"], "url": root,
                                "inLanguage": languages, "publisher": {"@id": f"{root}#organization"}}]
    elif slug and title:
        graph = [{"@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": data["name"], "item": absolute(reverse("home"))},
            {"@type": "ListItem", "position": 2, "name": title, "item": canonical},
        ]}]
    else:
        return ""
    text = json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False)
    return mark_safe(text.translate(JSON_ESCAPES))


@register.inclusion_tag("partials/page_meta.html", takes_context=True)
def page_meta(context):
    """Canonical address, the page in the other languages and the preview for whoever shares the link.

    The addresses are absolute, with PUBLIC_URL: relative hreflang and og:url are not valid.
    x-default is the page in the default language.
    """
    request = context["request"]
    data = community_config.community()
    language = get_language()
    found = [(alternate["code"], absolute(escape_uri_path(alternate["href"])))
             for alternate in context.get("alternates", []) if alternate["exists"]]
    default = dict(found).get(settings.LANGUAGE_CODE)
    # The image chosen by the page with `image:`, otherwise the community's, `share_image`
    image = context.get("page_image") or community_config.localized(data.get("share_image", ""), language)
    file = theme_image(image) if image else None
    width, height = (file and image_size(file)) or (None, None)
    canonical = absolute(escape_uri_path(request.path))
    return {
        # Outside production Caddy already says it with X-Robots-Tag: this applies to whoever reaches the site without going through it
        "indexable": settings.SITE_MODE == "production",
        "canonical": canonical,
        "alternates": [*found, *([("x-default", default)] if default else [])],
        "site_name": data["name"],
        "title": context.get("page_title") or context.get("head_title") or data["name"],
        "description": context.get("head_description", ""),
        "locale": og_locale(language),
        "other_locales": [og_locale(code) for code, _href in found if code != language],
        "image": {"url": absolute(iri_to_uri(image)), "width": width, "height": height} if image else None,
        "structured": structured_data(data, context.get("slug"), context.get("page_title"), canonical,
                                      [code for code, _href in found]),
    }
