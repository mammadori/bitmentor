import re

from django.apps import AppConfig
from django.conf import settings
from django.core import checks

from config.community import customer_links, shop_form_links, shop_page_links


def skipped_shop_links(app_configs, **kwargs):
    """A link of `shop_page` that cannot be used stays off the shop pages without saying so on every page: `manage.py
    check` and the `web` log at startup say why, because `migrate` runs the same checks. In every language: a path or a
    text wrong in one language only leaves that language alone without the line. A problem found in some languages only
    says in which."""
    found = {}
    for code, _name in settings.LANGUAGES:
        shop_page_links(code, warn=lambda problem, code=code: found.setdefault(problem, {}).setdefault(code))
    return [checks.Warning(problem if len(codes) == len(settings.LANGUAGES) else f"{problem} (in {', '.join(codes)})",
                           hint="The format is in the README, under \"The shop page\"", id="website.W002")
            for problem, codes in found.items()]


def wrong_marks(app_configs, **kwargs):
    """The private part of the pages (website/visibility.py). A page with wrong marks answers 404 rather than risk showing a
    private block (W003); a `visibility` the software does not understand makes the page private (W005). DEV_ALLOWED_IPS
    must name people, not networks everyone shares (W004)."""
    from config.community import COMMUNITY_DIR, front_matter, visibility
    from website.visibility import MarkError, address_problems, lines_by_block

    problems = []
    for path in sorted((COMMUNITY_DIR / "pages").glob("*/*.md")):
        name = f"{path.parent.name}/{path.name}"
        meta, body = front_matter(path.read_text())
        try:
            lines_by_block(body)
        except MarkError as error:
            problems.append(checks.Warning(f"{name}: {error}", hint="The page answers 404 until the marks are right", id="website.W003"))
        if visibility(meta) is None:
            problems.append(checks.Warning(f"{name}: visibility is {meta.get('visibility')!r}", id="website.W005",
                                           hint="Write `visibility: private`, `staff` or `public`; until then the page is private"))
    problems += [checks.Warning(f"DEV_ALLOWED_IPS: {problem}", id="website.W004", hint="It also gives the private part of the pages")
                 for problem in address_problems()]
    return problems

def skipped_form_links(app_configs, **kwargs):
    """The same for `shop_form`, the link under the Lightning address of the new shop form."""
    found = {}
    for code, _name in settings.LANGUAGES:
        shop_form_links(code, warn=lambda problem, code=code: found.setdefault(problem, {}).setdefault(code))
    return [checks.Warning(problem if len(codes) == len(settings.LANGUAGES) else f"{problem} (in {', '.join(codes)})",
                           hint="The format is in the README, under \"The new shop form\"", id="website.W006")
            for problem, codes in found.items()]


def skipped_customer_links(app_configs, **kwargs):
    """The same for what the customers' path takes from `shop_page`: the wallets it offers to whoever has none and the
    page on getting the first bitcoin. A wallet that is not open source, non-custodial and free stays off the list."""
    found = {}
    for code, _name in settings.LANGUAGES:
        customer_links(code, warn=lambda problem, code=code: found.setdefault(problem, {}).setdefault(code))
    return [checks.Warning(problem if len(codes) == len(settings.LANGUAGES) else f"{problem} (in {', '.join(codes)})",
                           hint="The format is in docs/features/customer-path.md", id="website.W008")
            for problem, codes in found.items()]


def wrong_osm_settings(app_configs, **kwargs):
    """What the `osm` section of `community.yml` says that cannot be used (shops/osm.py): without a usable area the whole
    feature is off, and any other field left at its default. The format is in docs/features/openstreetmap.md."""
    from config.community import community
    from shops.osm import settings_from

    problems = []
    settings_from(community().get("osm"), problems.append)
    return [checks.Warning(problem, hint="The format is in docs/features/openstreetmap.md", id="website.W012") for problem in problems]


ONION_ADDRESS = re.compile(r"https?://[a-z2-7]{56}\.onion")


def wrong_onion_location(app_configs, **kwargs):
    """ONION_LOCATION goes to Caddy as it is, with the page's path after it: http(s)://<56 letters>.onion, no slash."""
    if not settings.ONION_LOCATION or ONION_ADDRESS.fullmatch(settings.ONION_LOCATION):
        return []
    return [checks.Warning(f"ONION_LOCATION is {settings.ONION_LOCATION!r}", id="website.W007",
                           hint="Write the onion's address as http://<name>.onion, with no slash at the end: "
                                "docker compose exec tor cat /var/lib/tor/site/hostname")]


class WebsiteConfig(AppConfig):
    name = "website"

    def ready(self):
        checks.register(skipped_shop_links)
        checks.register(wrong_marks)

        checks.register(skipped_form_links)
        checks.register(skipped_customer_links)
        checks.register(wrong_onion_location)
        checks.register(wrong_osm_settings)
