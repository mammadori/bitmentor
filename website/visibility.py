"""Public and private parts of the community's pages.

A page can hold blocks that only some visitors see, marked on lines of their own:

    ::: private
    Text for friends.
    :::

    ::: public
    The same thing said in public.
    :::

Whoever has the private view gets the `private` blocks and not the `public` ones; everyone else the opposite, with no
trace of the private part: the marks go, and so do the footnote definitions nobody calls any more. The rule works on
the raw text, before the page's template and before Markdown: a block can sit inside an HTML element that Markdown
leaves alone. A page with `visibility: private` in its front matter is private as a whole: a 404 for everyone else,
and never in the sitemap or the menu.

Who has the private view is decided here, on the server: an address in DEV_ALLOWED_IPS, the owner of a certified
shop that the association approved by hand (shops/certified.py, `sees_private_part`). A cookie only lets them switch
the private part off and on again; on its own it gives nothing. The static copy of the site (X-Static-Copy: 1) is
always public.

A page with `visibility: staff` is a 404 for everyone but accounts with is_staff (staff and admin) and the allowed
addresses; shop owners, certified or not, get nothing. It is private as a whole for the menu, the sitemap and the copy.
"""
import ipaddress
import re

from django.conf import settings
from django.utils.cache import patch_cache_control, patch_vary_headers

from config import ratelimit
from config.community import normalized, private_meta, staff_meta

MARK = re.compile(r":::(?:[ \t]+(private|public))?[ \t]*")
# CommonMark fences: up to three spaces, then three or more backticks or tildes; the closing fence uses the same
# character, at least as many, and nothing after it
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
DEFINITION = re.compile(r" {0,3}\[\^([^\]]+)\]:")
CALL = re.compile(r"\[\^([^\]]+)\](?!:)")
INDENTED = re.compile(r"(    |\t)")
COOKIE = "private_view"
# Written by Caddy on every request it sends to Django from the site's address, and replaced on the onion's
# (Caddyfile): an allowed address counts only on the site
ENTRANCE = "X-Entrance"
# The shorthand Caddy accepts in client_ip, with the same networks
PRIVATE_RANGES = ("192.168.0.0/16", "172.16.0.0/12", "10.0.0.0/8", "127.0.0.1/8", "fd00::/8", "::1/128")
# Networks that are never one person: internal, loopback, link-local, shared by a carrier
INTERNAL = [ipaddress.ip_network(network) for network in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16", "100.64.0.0/10", "0.0.0.0/8",
    "fc00::/7", "fe80::/10", "::1/128", "::/128")]
LOOPBACK = {ipaddress.ip_network("127.0.0.1/32"), ipaddress.ip_network("::1/128")}
WIDEST = {4: 24, 6: 48}


class MarkError(ValueError):
    """Wrong marks: a block opened and never closed, a closing mark too many, a block inside another, a mark written
    differently (indented, in capitals, another word) or inside a fenced code block."""


def lines_by_block(text):
    """The normalized text line by line, with the block it sits in: [(line, None | "private" | "public" | "mark")].

    Every line that starts with `:::`, spaces aside, is a mark or an error: a mark written otherwise would become text
    everyone reads. Inside a fenced code block too: there it cannot be a mark, and it is an error rather than code."""
    found, inside, opened, fence = [], None, 0, None
    for number, line in enumerate(text.split("\n"), 1):
        opening = FENCE.fullmatch(line)
        if fence is None and opening and not (opening[1][0] == "`" and "`" in opening[2]):
            fence = opening[1]
        elif fence is not None and opening and opening[1][0] == fence[0] and len(opening[1]) >= len(fence) \
                and not opening[2].strip():
            fence = None
        elif not line.lstrip().startswith(":::"):
            found.append((line, inside))
            continue
        else:
            match = MARK.fullmatch(line)
            if fence is not None:
                raise MarkError(f"line {number}: {line!r} inside a fenced code block")
            if not match:
                raise MarkError(f"line {number}: {line!r} is not a mark: ':::', '::: private' or '::: public', "
                                "at the start of the line, in small letters")
            if match[1]:
                if inside:
                    raise MarkError(f"line {number}: {line!r} inside a {inside} block opened on line {opened}")
                inside, opened = match[1], number
            else:
                if not inside:
                    raise MarkError(f"line {number}: ':::' without an open block")
                inside = None
            found.append((line, "mark"))
            continue
        found.append((line, inside))
    if inside:
        raise MarkError(f"line {opened}: {inside} block never closed")
    return found


def footnote_segments(lines):
    """The lines grouped as Python-Markdown reads footnotes: [(label or None, lines)]. A definition goes on with the
    lines right after it, up to an empty line or another definition, and after empty lines with indented lines (more
    paragraphs of the same note)."""
    segments, label = [], None
    for index, line in enumerate(lines):
        match = DEFINITION.match(line)
        if match:
            label = match[1]
            segments.append((label, [line]))
            continue
        if label is not None:
            if line.strip():
                if segments[-1][1][-1].strip() or INDENTED.match(line):
                    segments[-1][1].append(line)
                    continue
            else:
                after = next((later for later in lines[index + 1:] if later.strip()), "")
                if INDENTED.match(after) and not DEFINITION.match(after):
                    segments[-1][1].append(line)
                    continue
            label = None
        if segments and segments[-1][0] is None:
            segments[-1][1].append(line)
        else:
            segments.append((None, [line]))
    return segments


def without_orphan_notes(text):
    """Drops the footnote definitions nobody calls, with all their lines, until nothing changes: a note called only from
    a note that goes, goes too. A note calling itself does not count."""
    segments = footnote_segments(text.split("\n"))
    while True:
        called = set()
        for label, lines in segments:
            called.update(found for found in CALL.findall("\n".join(lines)) if found != label)
        kept = [(label, lines) for label, lines in segments if label is None or label in called]
        if len(kept) == len(segments):
            return "\n".join(line for _label, lines in segments for line in lines)
        segments = kept


def filtered(text, private=False):
    """The text as seen with the private view (private=True) or by anyone else. MarkError if the marks are wrong."""
    keep = "private" if private else "public"
    lines = [line for line, where in lines_by_block(normalized(text)) if where is None or where == keep]
    return without_orphan_notes(re.sub(r"\n{3,}", "\n\n", "\n".join(lines)))


def is_staff_page(meta):
    return staff_meta(meta)


def is_private_page(meta):
    """Private as a whole: `visibility: private`, or a value the software does not understand."""
    return private_meta(meta)


def allowed_networks():
    """DEV_ALLOWED_IPS as networks, written as for Caddy's client_ip: addresses or networks separated by spaces, or
    `private_ranges`. What is not an address is left out (the check website.W004 says so)."""
    networks = []
    for entry in settings.DEV_ALLOWED_IPS.split():
        for item in PRIVATE_RANGES if entry == "private_ranges" else (entry,):
            try:
                networks.append(ipaddress.ip_network(item, strict=False))
            except ValueError:
                pass
    return networks


def visitor_address(request):
    """The visitor's address, as the request limits read it (config.ratelimit.visitor_ip): the last of X-Forwarded-For,
    which Caddy always writes itself, with what it trusts from TRUSTED_PROXIES."""
    return ratelimit.visitor_ip(request)


def address_problems():
    """What in DEV_ALLOWED_IPS would give the private view to more than the people meant: entries that are not
    addresses, internal or loopback networks (the onion and the Docker gateway come from there) and wide networks."""
    problems = []
    for entry in settings.DEV_ALLOWED_IPS.split():
        if entry == "private_ranges":
            problems.append("`private_ranges` covers the internal networks: every onion visitor would see the private part")
            continue
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            problems.append(f"{entry!r} is not an address or a network")
            continue
        if network not in LOOPBACK and any(network.overlaps(internal) for internal in INTERNAL if internal.version == network.version):
            problems.append(f"{entry} is an internal network: the onion, the Docker gateway or the machine itself come from there")
        elif network.prefixlen < WIDEST[network.version]:
            problems.append(f"{entry} is wider than /{WIDEST[network.version]}: many people come from there")
    return problems


def from_allowed_address(request):
    """Only on the site's address: on the onion's, Caddy writes `X-Entrance: onion`, and the address it passes is the
    one of the onion service, inside."""
    if request.headers.get(ENTRANCE) != "site":
        return False
    ip = visitor_address(request)
    if ip is not None and any(ip in network for network in allowed_networks() if network.version == ip.version):
        return True
    # TEAM_ACCESS: a browser with a valid team pass counts as an allowed address, on a site under construction only
    # (team/access.py: the same judgement Caddy asks for before showing it the site). Off, this is always False
    from team import access

    return access.holder(request) is not None


def entitled_account(user):
    """True if the account may see the private part: the owner of a certified shop approved by the association, or an
    account the community's own rule lets in (config/hooks.py, `private_ok`), asked only when the first says no."""
    from config import hooks
    from shops.certified import sees_private_part

    if not (user and user.is_authenticated and user.is_active):
        return False
    return sees_private_part(user) or hooks.yes("private_ok", user)


class Visibility:
    """What the request may see, computed once and only when asked."""

    def __init__(self, request):
        self.request = request
        self._entitled = None

    @property
    def static_copy(self):
        return self.request.headers.get("X-Static-Copy") == "1"

    @property
    def allowed_address(self):
        return not self.static_copy and from_allowed_address(self.request)

    @property
    def entitled(self):
        """May this visitor have the private view? Never for the static copy of the site."""
        if self._entitled is None:
            self._entitled = not self.static_copy and (self.allowed_address
                                                       or entitled_account(getattr(self.request, "user", None)))
        return self._entitled

    @property
    def staff(self):
        """Does this visitor see the pages marked `visibility: staff`? Staff and admins, or an allowed address; never the
        static copy. The cookie does not switch it."""
        # StaffApart (config/guard.py) turns the staff into a visitor outside /admin/: the account is in staff_user
        user = getattr(self.request, "staff_user", None)
        return not self.static_copy and (self.allowed_address
                                         or bool(user and user.is_active and (user.is_staff or user.is_superuser)))

    @property
    def private(self):
        """Does this visitor see the private part now? Switched on unless they switched it off."""
        return self.entitled and self.request.COOKIES.get(COOKIE) != "off"


class VisibilityMiddleware:
    """`request.visibility` for views and templates. A response that depended on the private view is never kept by a
    shared cache: the same address can be one visitor or another, and the view depends on the address, which Vary
    cannot name."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.visibility = Visibility(request)
        response = self.get_response(request)
        if request.visibility._entitled or getattr(request, "visibility_varies", False):
            patch_cache_control(response, private=True, no_cache=True)
            patch_vary_headers(response, ["Cookie"])
        return response


def context(request):
    visibility = getattr(request, "visibility", None)
    if visibility is None or not visibility.entitled:
        return {}
    return {"visibility": {"private": visibility.private, "mode": settings.SITE_MODE if visibility.allowed_address else ""}}
