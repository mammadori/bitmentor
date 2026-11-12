"""Loads the community folder: everything that is not software lives there."""
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from django.conf import settings
from django.utils.translation import get_language

COMMUNITY_DIR = Path(os.environ.get("COMMUNITY_DIR", Path(__file__).parent.parent / "examples/community"))


@lru_cache
def community():
    return yaml.safe_load((COMMUNITY_DIR / "community.yml").read_text())


@lru_cache
def community_data():
    """The community's JSON files, available in pages and templates as `data.<name>`."""
    return {path.stem: json.loads(path.read_text()) for path in (COMMUNITY_DIR / "data").glob("*.json")}


def page_path(language, slug):
    return COMMUNITY_DIR / "pages" / language / f"{slug}.md"


def page_slugs(language):
    return sorted(path.stem for path in (COMMUNITY_DIR / "pages" / language).glob("*.md"))


FRONT_MATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n", re.S)
VISIBILITIES = {"": "public", "public": "public", "private": "private", "staff": "staff"}


def normalized(text):
    """A page's text with Unix line ends and without a byte order mark: an editor on Windows writes both."""
    return text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def front_matter(text):
    """The optional metadata at the top of a page and its body, from the normalized text. The only reader of the
    metadata: the page, the menu, the sitemap and the checks see the same `visibility`."""
    text = normalized(text)
    match = FRONT_MATTER.match(text)
    if not match:
        return {}, text
    meta = {}
    for line in match[1].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    return meta, text[match.end():]


def visibility(meta):
    """`public`, `private`, `staff`, or None for a value the software does not understand: the page is then treated as private,
    and the check website.W005 says so. The key in any case, the value in any case and between quotes."""
    value = next((value for key, value in meta.items() if key.lower() == "visibility"), "")
    return VISIBILITIES.get(value.strip().strip("\"'").strip().lower())


def private_meta(meta):
    """Private as a whole, for the menu, the sitemap, the static copy and the links: `staff` pages too."""
    return visibility(meta) != "public"


def staff_meta(meta):
    return visibility(meta) == "staff"


@lru_cache(maxsize=1024)
def staff_file(path, modified):
    return staff_meta(front_matter(path.read_text())[0])


def staff_page(language, slug):
    """True if the page is for the staff only (`visibility: staff`)."""
    path = page_path(language, slug)
    try:
        return staff_file(path, path.stat().st_mtime_ns)
    except OSError:
        return False


def page_open_to(request, language, slug):
    """May this request open the private page? A staff page needs the staff view, any other private page the private view."""
    if staff_page(language, slug):
        return staff_view(request)
    return private_view(request)


@lru_cache(maxsize=1024)
def private_file(path, modified):
    """True if the page is private as a whole (website/visibility.py). By file and time of change: the menu asks on every
    page."""
    return private_meta(front_matter(path.read_text())[0])


def private_page(language, slug):
    path = page_path(language, slug)
    try:
        return private_file(path, path.stat().st_mtime_ns)
    except OSError:
        return False


def private_link(language, value):
    """True if `value`, a path after `/<lang>/` as in `menu`, is a private page: nobody links to one."""
    slug = re.split(r"[?#]", value, maxsplit=1)[0].strip("/")
    return bool(slug) and "/" not in slug and private_page(language, slug)


def public_slugs(language):
    """The pages anyone can see: the sitemap, the menu and every copy of the site list only these."""
    return [slug for slug in page_slugs(language) if not private_page(language, slug)]


def localized(value, language):
    """The text in the requested language; if the community has not written it yet, the one in the default language."""
    if not isinstance(value, dict):
        return value
    for code in (language, settings.LANGUAGE_CODE):
        if code in value:
            return value[code]
    return next(iter(value.values()))


def menu(language, key="menu"):
    # `path` wins over `slug`, and an entry with `path` needs no `slug`: get(…, default) would read the slug anyway
    # A private page is never in the menu, not even for whoever can see it: nobody else learns that it exists
    return [{"href": f"/{language}/{item['path'] if 'path' in item else item['slug'] + '/'}", "label": localized(item["labels"], language)}
            for item in community().get(key, [])
            if not private_link(language, item["path"] if "path" in item else item["slug"])]


def community_link(value, language):
    """A link written in `community.yml`: a page of the site as its path after `/<lang>/`, like `menu`, or a full
    `https://` address; either can be written per language. ValueError with the reason if it cannot be used."""
    value = text_value(localized(value or None, language), "the link")
    if re.search(r"[\s\\]", value):
        raise ValueError(f"no spaces or backslashes in a link: {value!r}")
    if value.startswith("https://"):
        if not urlsplit(value).hostname:
            raise ValueError(f"no server in {value!r}")
        return value
    if value.startswith("/") or "://" in value:
        raise ValueError(f"a page goes without the leading slash, an address with https://: {value!r}")
    # `www.example.org/x`, `mailto:` or `javascript:` would become a page of the site that does not exist
    if re.search(r"[.:]", re.split(r"[/?#]", value, maxsplit=1)[0]):
        raise ValueError(f"{value!r} is not a page of the site: an address starts with https://")
    if private_link(language, value):
        raise ValueError(f"{value!r} is a private page: only some visitors can open it")
    return f"/{language}/{value}"


def text_value(value, name):
    """`value`, already in the page's language, if it is some text; ValueError otherwise. YAML reads `2015` as a number."""
    if value is None or value == "" or value == {}:
        raise ValueError(f"{name} is missing")
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text, between quotes: {value!r}")
    if not value.strip():
        raise ValueError(f"{name} is missing")
    return value


def shop_page_links(language, external_id=None, warn=None):
    """What `shop_page` in `community.yml` adds to a shop page, in `language`: `wallet`, the page for whoever has no
    wallet yet, and `stories`, the community's pages about a shop, listed under its `external_id` (the number in the
    imported list, the same on every installation). A shop without `external_id` has no stories; `external_id=None`
    reads them all, for the checks. What cannot be used is left out and told to `warn`: the page stays up."""
    found = {"wallet": "", "stories": []}
    say = warn or (lambda problem: None)
    data = community().get("shop_page")
    if data is None:
        return found
    if not isinstance(data, dict):
        say("`shop_page` is not a list of fields")
        return found
    if data.get("wallet") is not None:
        try:
            found["wallet"] = community_link(data["wallet"], language)
        except ValueError as error:
            say(f"`shop_page.wallet`: {error}")
    stories = data.get("stories")
    if stories is None or external_id == "":
        return found
    if not isinstance(stories, dict):
        say("`shop_page.stories` goes by the shop's external_id, like `12: [{path: …, text: …}]`")
        return found
    for key, links in stories.items():
        # YAML reads `12:` as a number, and external_id is text
        if external_id is not None and str(key) != external_id:
            continue
        for number, link in enumerate(links if isinstance(links, list) else [links], 1):
            try:
                if not isinstance(link, dict):
                    raise ValueError("it is not a list of fields")
                text = text_value(localized(link.get("text") or None, language), "`text`")
                found["stories"].append({"href": community_link(link.get("path"), language), "text": text})
            except ValueError as error:
                say(f"`shop_page.stories` {key}, link {number}: {error}")
    return found


# The phones and computers a wallet of `shop_form.wallets` runs on, as the guide shows them (shops/wallet_steps.html)
WALLET_PLATFORMS = ("android", "ios", "windows", "macos", "linux", "web")
# What the community states for every wallet it lists: the software shows none without all three
WALLET_PROMISES = ("open_source", "self_custody", "free")
# What a wallet can send, for the customers' path: a shop that takes only one of them needs a wallet that does it
WALLET_METHODS = ("lightning", "onchain")
WALLET_NAME_LENGTH = 40
WALLET_TEXT_LENGTH = 200


def shop_wallet(entry, language):
    """One wallet of `shop_form.wallets`, in `language`, or ValueError with the reason.

    The rule this enforces (MiCAR): a community may only suggest tools that are open source, non-custodial (the
    merchant alone holds the keys, so nobody holds money for them) and free. The software cannot check that, so it
    asks the community to state it for every wallet, with `open_source`, `self_custody` and `free` all `true`, and
    shows no wallet without them. Never an exchange or a custodial wallet: they need a licence, and the money would be
    someone else's to hold."""
    if not isinstance(entry, dict):
        raise ValueError("it is not a list of fields")
    name = text_value(localized(entry.get("name") or None, language), "`name`").strip()
    if len(name) > WALLET_NAME_LENGTH:
        raise ValueError(f"`name` is longer than {WALLET_NAME_LENGTH} characters")
    missing = [promise for promise in WALLET_PROMISES if entry.get(promise) is not True]
    if missing:
        raise ValueError(f"only open source, self-custody and free wallets: write {', '.join(f'`{key}: true`' for key in missing)} "
                         "once you are sure, or leave the wallet out")
    link = community_link(entry.get("link"), language)
    description = localized(entry.get("description") or "", language)
    if description != "":
        description = text_value(description, "`description`").strip()
        if len(description) > WALLET_TEXT_LENGTH:
            raise ValueError(f"`description` is longer than {WALLET_TEXT_LENGTH} characters")
    platforms = entry.get("platforms") or []
    platforms = platforms if isinstance(platforms, list) else [platforms]
    unknown = [str(item) for item in platforms if str(item).lower() not in WALLET_PLATFORMS]
    if unknown:
        raise ValueError(f"`platforms` can only be {', '.join(WALLET_PLATFORMS)}, not {', '.join(unknown)}")
    # What the wallet can send, for a customer: both unless the community says otherwise
    methods = entry.get("methods") or list(WALLET_METHODS)
    methods = methods if isinstance(methods, list) else [methods]
    unknown = [str(item) for item in methods if str(item).lower() not in WALLET_METHODS]
    if unknown:
        raise ValueError(f"`methods` can only be {', '.join(WALLET_METHODS)}, not {', '.join(unknown)}")
    return {"name": name, "link": link, "description": description,
            "platforms": [str(item).lower() for item in platforms], "methods": [str(item).lower() for item in methods]}


def shop_form_links(language, warn=None):
    """What `shop_form` in `community.yml` adds to the new shop form, in `language`: `wallet`, the community's page on
    the wallets that work with the register, linked from "Where do I find it?" under the Lightning address, and
    `wallets`, the wallets the step-by-step guide offers to whoever has none yet (shop_wallet). The software names no
    wallet. A link or a wallet that cannot be used is left out and told to `warn`."""
    found = {"wallet": "", "wallets": []}
    say = warn or (lambda problem: None)
    data = community().get("shop_form")
    if data is None:
        return found
    if not isinstance(data, dict):
        say("`shop_form` is not a list of fields")
        return found
    if data.get("wallet") is not None:
        try:
            found["wallet"] = community_link(data["wallet"], language)
        except ValueError as error:
            say(f"`shop_form.wallet`: {error}")
    wallets = data.get("wallets")
    if wallets is None:
        return found
    for number, entry in enumerate(wallets if isinstance(wallets, list) else [wallets], 1):
        try:
            found["wallets"].append(shop_wallet(entry, language))
        except ValueError as error:
            say(f"`shop_form.wallets`, wallet {number}: {error}")
    return found


def customer_links(language, warn=None):
    """What the customers' path (shops/customer.py) takes from `community.yml`, in `language`: `wallets`, the wallets
    it offers to whoever has none, and `first_bitcoin`, the community's page on getting the first bitcoin.

    The wallets are those of `shop_page.wallets` if the community wrote any, else those of `shop_form.wallets`: the same
    rule as for a merchant, only open source, non-custodial and free ones (shop_wallet), and the software names none. A
    wallet or a link that cannot be used is left out and told to `warn`."""
    found = {"wallets": [], "first_bitcoin": ""}
    say = warn or (lambda problem: None)
    data = community().get("shop_page")
    if not isinstance(data, dict):
        data = {}
    if data.get("first_bitcoin") is not None:
        try:
            found["first_bitcoin"] = community_link(data["first_bitcoin"], language)
        except ValueError as error:
            say(f"`shop_page.first_bitcoin`: {error}")
    wallets = data.get("wallets")
    if wallets is None:
        found["wallets"] = shop_form_links(language)["wallets"]
        return found
    for number, entry in enumerate(wallets if isinstance(wallets, list) else [wallets], 1):
        try:
            found["wallets"].append(shop_wallet(entry, language))
        except ValueError as error:
            say(f"`shop_page.wallets`, wallet {number}: {error}")
    return found


def alternates(request, current):
    """The page in every language. A Markdown page missing in a language is not announced with hreflang (`exists`), and the
    selector leads to the home of that language instead of a 404."""
    tail = re.sub(rf"^/{re.escape(current)}(/|$)", "/", request.path)
    match = request.resolver_match
    slug = match.kwargs.get("slug") if match and match.view_name in ("home", "page") else None
    found = []
    for code, name in community()["languages"].items():
        exists = slug is None or (page_path(code, slug).exists() and (not private_page(code, slug) or page_open_to(request, code, slug)))
        found.append({"code": code, "name": name, "href": f"/{code}{tail}" if exists else f"/{code}/",
                      "active": code == current, "exists": exists})
    return found


def staff_view(request):
    visibility = getattr(request, "visibility", None)
    return bool(visibility and visibility.staff)


def private_view(request):
    visibility = getattr(request, "visibility", None)
    return bool(visibility and visibility.private)


def context(request):
    from config import features  # features reads `community`, defined above

    language = get_language()
    data = community()
    return {
        "features": features.states(),
        "experimental": features.experimental_states(),
        "community": data,
        "tagline": localized(data.get("tagline", ""), language),
        "menu": menu(language),
        "footer_menu": menu(language, "footer_menu"),
        "alternates": alternates(request, language),
        "data": community_data(),
        "source_url": settings.SOURCE_URL,
    }
