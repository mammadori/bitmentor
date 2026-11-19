"""The volunteers' area (TEAM_AREA, team/area.py): who gets in, what it shows and does, and everything that must not
open."""
import re
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import yaml
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core import signing
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from shops import moderators
from shops.management.commands import preflight
from shops.models import HelpRequest, Profile
from shops.test_dashboard import register
from shops.test_guard import sign_in_as_staff_session

from . import access, area, views
from .models import TeamAction, TeamKey
from .tests import METHODS, SITE, Base, k1_of, signed

ROOT = Path(__file__).resolve().parent.parent
User = get_user_model()
# Every page of the area but the sign-in, with the method each takes
PAGES = ("/team/today/", "/team/help/", "/team/me/", "/team/osm/", "/team/changes/")
CHANGES = ("/team/help/{number}/solved/", "/team/help/{number}/waiting/", "/team/me/end/", "/team/osm/{number}/done/",
           "/team/shops/{shop}/approve/")
ADDRESS = "merchant@wallet.example"
RECOVERY = "owner.secret@mail.example"


def gone(test, browser, verb, url, what=""):
    """Nothing of the area answers: a 404, or for /team/ itself the answer it had before the area existed, the site's
    redirect to the same address in the default language, which is a 404 too."""
    response = getattr(browser, verb)(url)
    if response.status_code == 302 and url == "/team/":
        test.assertEqual(response["Location"], "/en/team/", f"{what} {verb} {url}")
        test.assertEqual(browser.get(response["Location"]).status_code, 404, f"{what} {verb} {url}")
    else:
        test.assertEqual(response.status_code, 404, f"{what} {verb} {url}")


@override_settings(TEAM_AREA=True)
class AreaBase(Base):
    def setUp(self):
        super().setUp()
        self.owner = User.objects.create_user("owner")
        Profile.objects.create(user=self.owner, email=RECOVERY)
        self.shop = register("Bar", owner=self.owner, lightning_address=ADDRESS, lightning_checked_address=ADDRESS,
                             phone="+39 000 111", telegram_chat="@barchat")
        self.request = HelpRequest.objects.create(
            shop=self.shop, symptom="pay", note=f"write to {RECOVERY} or pay {ADDRESS}", channel="email",
            summary=f"✗ [lightning_no_answer] Your Lightning wallet is not answering.\n    {ADDRESS}; 1.0 s\n"
                    "✓ [owner_recovery_email] ok\n    signs in with: email link; recovery email o***@m***.example")

    def moderator(self, name="Mara", browser=None):
        """A moderator's key and a browser signed in with it."""
        key, wallet = self.member(name, role="moderator")
        browser = browser or self.browser()
        self.assertEqual(self.sign_in(browser, wallet).json(), {"state": "ok"})
        return key, browser

    def urls(self, number=None):
        number = number or self.request.pk
        return [*PAGES, "/team/", "/team/shops/", *(url.format(number=number, shop=self.shop.pk) for url in CHANGES)]


class Off(AreaBase):
    """TEAM_AREA off (the default), or on without TEAM_ACCESS: nothing of the area exists, also for a moderator's pass."""

    def test_every_address_is_a_404_to_every_method(self):
        _key, browser = self.moderator()
        for settings_ in ({"TEAM_AREA": False}, {"TEAM_ACCESS": False}, {"TEAM_ACCESS": False, "TEAM_AREA": True}):
            with override_settings(**settings_):
                for url in self.urls():
                    for verb in METHODS:
                        gone(self, browser, verb, url, settings_)
        self.request.refresh_from_db()
        self.assertIsNone(self.request.resolved)
        self.assertFalse(TeamAction.objects.exists())

    def test_with_the_area_off_production_is_as_before(self):
        _key, browser = self.moderator()
        with override_settings(TEAM_AREA=False, SITE_MODE="production"):
            for url in ("/team/login/", "/team/poll/", "/team/auth/", "/team/join.js", "/team/", "/team/today/"):
                gone(self, browser, "get", url)

    def test_the_admin_shows_nothing_of_it(self):
        key, _browser = self.moderator()
        TeamAction.write(key, TeamAction.Action.SOLVED, "x")
        boss = self.client_class()
        sign_in_as_staff_session(boss, User.objects.create_superuser("boss"))
        with override_settings(TEAM_AREA=False):
            self.assertNotIn("teamaction", boss.get("/admin/").content.decode())
            self.assertEqual(boss.get(reverse("admin:team_teamaction_changelist")).status_code, 403)
            page = boss.get(reverse("admin:team_teamkey_change", args=[key.pk])).content.decode()
            self.assertNotIn("teamaction", page)
        self.assertIn("teamaction", boss.get("/admin/").content.decode())
        self.assertIn(reverse("admin:team_teamaction_changelist"),
                      boss.get(reverse("admin:team_teamkey_change", args=[key.pk])).content.decode())


class WhoGetsIn(AreaBase):
    def test_a_moderator_gets_the_pages(self):
        key, browser = self.moderator()
        for url in PAGES:
            response = browser.get(url)
            self.assertEqual(response.status_code, 200, url)
            self.assertIn("private, no-store", response["Cache-Control"], url)
            self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow", url)
            self.assertEqual(response["Referrer-Policy"], "no-referrer", url)
            self.assertContains(response, '<meta name="robots" content="noindex, nofollow">')
            self.assertContains(response, key.name)
            # No account: the pass sets no user, and no session is opened
            self.assertFalse(response.wsgi_request.user.is_authenticated, url)
            self.assertNotIn("sessionid", response.cookies)
        # The sign-in page sends a moderator with a pass to the day's list
        self.assertRedirects(browser.get("/team/"), "/team/today/", fetch_redirect_response=False)

    def test_the_roles_team_and_staff_get_404(self):
        for role in ("team", "staff"):
            _key, wallet = self.member(f"Someone {role}", role=role)
            browser = self.browser()
            self.sign_in(browser, wallet)
            self.assertTrue(self.has_pass(browser))
            self.refused_but_the_sign_in(browser, role)
            # The sign-in page says the role does not open the area, to the holder of that pass only
            self.assertContains(browser.get("/team/"), "does not open this area")
            self.assertNotContains(self.browser().get("/team/"), "does not open this area")
        self.assertFalse(TeamAction.objects.exists())

    def test_a_revoked_key_an_ended_pass_and_a_deleted_key_lose_it_at_the_next_request(self):
        for change in ("revoke", "end", "delete"):
            key, browser = self.moderator(f"Mara {change}")
            self.assertEqual(browser.get("/team/today/").status_code, 200)
            keys = TeamKey.objects.filter(pk=key.pk)
            {"revoke": TeamKey.revoke, "end": TeamKey.end_passes, "delete": lambda found: found.delete()}[change](keys)
            self.refused_but_the_sign_in(browser, change)
        self.assertFalse(TeamAction.objects.exists())

    def refused_but_the_sign_in(self, browser, what):
        """Every address of the area answers 404 to GET and POST, but the sign-in page, which is a page (GET only)."""
        for url in self.urls():
            self.assertEqual(browser.get(url).status_code, 200 if url == "/team/" else 404, f"{what} GET {url}")
            self.assertEqual(browser.post(url).status_code, 405 if url == "/team/" else 404, f"{what} POST {url}")

    def test_a_pass_older_than_a_week_loses_it(self):
        _key, browser = self.moderator()
        later = timezone.now() + timedelta(seconds=access.PASS_AGE + 60)
        with patch("time.time", return_value=later.timestamp()):
            self.assertEqual(browser.get("/team/today/").status_code, 404)

    def test_the_onion_and_no_entrance_get_nothing(self):
        _key, browser = self.moderator()
        cookie = browser.cookies["team_pass"].value
        for entrance in ({"X-Entrance": "onion"}, {}, {"X-Entrance": "SITE"}):
            for mode in ("development", "production"):
                with override_settings(SITE_MODE=mode):
                    other = self.client_class(headers=entrance)
                    other.cookies["team_pass"] = cookie
                    for url in self.urls():
                        for verb in ("get", "post"):
                            gone(self, other, verb, url, f"{entrance} {mode}")

    def test_maintenance_closes_it(self):
        _key, browser = self.moderator()
        with override_settings(SITE_MODE="maintenance"):
            for url in self.urls():
                for verb in ("get", "post"):
                    gone(self, browser, verb, url, "maintenance")

    def test_a_staff_session_without_a_pass_gets_404(self):
        boss = self.browser()
        sign_in_as_staff_session(boss, User.objects.create_superuser("boss"))
        moderator = User.objects.create_user("mod", is_staff=True)
        moderator.groups.add(Group.objects.get(name=moderators.GROUP))
        helper = self.browser()
        sign_in_as_staff_session(helper, moderator)
        for browser in (boss, helper):
            self.assertEqual(browser.get("/admin/today/").status_code, 200)
            for url in self.urls():
                if url == "/team/":
                    continue
                for verb in ("get", "post"):
                    self.assertEqual(getattr(browser, verb)(url).status_code, 404, f"{verb} {url}")
        self.request.refresh_from_db()
        self.assertIsNone(self.request.resolved)

    def test_a_pass_from_a_site_under_construction_works_once_the_site_is_open(self):
        _key, browser = self.moderator()
        with override_settings(SITE_MODE="production"):
            self.assertEqual(browser.get("/team/today/").status_code, 200)
            # It is still nothing more than the area: no private part, no allowed address
            request = self.browser().get("/team/today/").wsgi_request
            request.COOKIES["team_pass"] = browser.cookies["team_pass"].value
            self.assertIsNone(access.holder(request))
            # Caddy's question is not answered on an open site
            self.assertEqual(browser.get("/team/check/").status_code, 404)


@override_settings(SITE_MODE="production")
class SignInOnAnOpenSite(AreaBase):
    def test_the_whole_sign_in(self):
        key, wallet = self.member("Mara", role="moderator")
        page = self.client.get("/team/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("private, no-store", page["Cache-Control"])
        self.assertIn(views.K1_COOKIE, page.cookies)
        content = page.content.decode()
        lnurl = re.search(r'href="lightning:([A-Z0-9]+)"', content)[1]
        self.assertIn('data-poll="/team/poll/"', content)
        self.assertIn('src="/team/join.js"', content)
        # Nothing about keys, and no inline script (the page's script is a file)
        self.assertNotIn("Mara", content)
        self.assertNotIn("<script>", content)
        self.assertEqual(self.client.get("/team/join.js").status_code, 200)
        self.assertEqual(self.client.get("/team/poll/").json(), {"state": "pending"})
        self.assertEqual(self.client.get("/team/auth/", signed(k1_of(lnurl), wallet)).json(), {"status": "OK"})
        self.assertEqual(self.client.get("/team/poll/").json(), {"state": "ok"})
        self.assertEqual(self.client.get("/team/today/").status_code, 200)
        key.refresh_from_db()
        self.assertIsNotNone(key.last_pass)
        # The team hears of it, in words for an open site
        from django.core import mail
        self.assertIn("signed in with their wallet in a new browser", mail.outbox[-1].body)

    def test_a_wallet_that_is_no_team_key_gets_nothing(self):
        page = self.client.get("/team/").content.decode()
        lnurl = re.search(r'href="lightning:([A-Z0-9]+)"', page)[1]
        stranger = __import__("coincurve").PrivateKey()
        self.client.get("/team/auth/", signed(k1_of(lnurl), stranger))
        self.assertEqual(self.client.get("/team/poll/").json(), {"state": "denied"})
        self.assertNotIn("team_pass", self.client.cookies)
        self.assertEqual(self.client.get("/team/today/").status_code, 404)

    def test_the_sign_in_page_has_the_limits_of_the_codes(self):
        with patch("team.views.CODES", (2, 6)), patch.object(area, "CODES", (2, 6)):
            for _ in range(2):
                self.assertEqual(self.client.get("/team/").status_code, 200)
            # The page and /team/login/ count together
            busy = self.client.get("/team/login/")
            self.assertEqual(busy.status_code, 429)
            busy = self.client.get("/team/")
            self.assertEqual(busy.status_code, 429)
            self.assertIn("Retry-After", busy)
        # A browser asking again gets its own code back, not a new one
        reset = __import__("shops.tests", fromlist=["reset_limits"]).reset_limits
        reset()
        from .models import TeamChallenge
        before = TeamChallenge.objects.count()
        self.client.get("/team/")
        self.assertEqual(TeamChallenge.objects.count(), before)

    def test_when_the_codes_are_all_taken_the_page_says_so(self):
        with patch("team.models.CHALLENGES_ALIVE", 0):
            response = self.browser().get("/team/")
        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Too many requests", status_code=429)

    def test_an_invite_can_be_made_and_taken_on_an_open_site(self):
        boss = self.client_class()
        sign_in_as_staff_session(boss, User.objects.create_superuser("boss"))
        made = boss.post(reverse("admin:team_teaminvite_add"), {"name": "Nina", "role": "moderator"})
        self.assertEqual(made.status_code, 200)
        _invite, token = self.invite("Nina", "moderator")
        page = self.client.get(f"/team/join/{token}/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'data-done="/team/"')
        with override_settings(TEAM_AREA=False):
            self.assertEqual(boss.post(reverse("admin:team_teaminvite_add"), {"name": "Eve", "role": "team"}).status_code, 403)


class HelpRequests(AreaBase):
    def test_the_page_has_what_a_volunteer_needs_and_nothing_more(self):
        _key, browser = self.moderator()
        self.request.channel = "phone"
        self.request.save()
        page = browser.get("/team/help/").content.decode()
        for text in ("Bar", "A customer cannot pay", "+39 000 111", "lightning_no_answer", "me…@wallet.example"):
            self.assertIn(text, page)
        self.assertNotIn(ADDRESS, page)
        self.assertNotIn(RECOVERY, page)
        self.assertNotIn("/admin/", page)
        HelpRequest.objects.filter(pk=self.request.pk).update(channel="telegram")
        self.assertIn("@barchat", browser.get("/team/help/").content.decode())
        HelpRequest.objects.filter(pk=self.request.pk).update(channel="email")
        page = browser.get("/team/help/").content.decode()
        self.assertIn("The address is not shown here", page)
        self.assertNotIn(RECOVERY, page)

    def test_solved_and_waiting_again_with_their_rows(self):
        key, browser = self.moderator()
        url = reverse("team_help_solved", args=[self.request.pk])
        response = browser.post(url)
        self.assertRedirects(response, f"/team/help/#request-{self.request.pk}", fetch_redirect_response=False)
        self.request.refresh_from_db()
        self.assertIsNotNone(self.request.resolved)
        self.assertEqual(self.request.resolved_by, f"[team key {key.pk}] Mara")
        row = TeamAction.objects.get()
        self.assertEqual((row.key, row.name, row.action), (key, "Mara", TeamAction.Action.SOLVED))
        self.assertEqual(row.target, f"help request {self.request.pk}: Bar")
        # Solved twice: nothing changes, no row
        browser.post(url)
        self.assertEqual(TeamAction.objects.count(), 1)
        # The solved request is on the page, with who solved it, and can be put back
        self.assertContains(browser.get("/team/help/"), f"[team key {key.pk}] Mara")
        browser.post(reverse("team_help_waiting", args=[self.request.pk]))
        self.request.refresh_from_db()
        self.assertIsNone(self.request.resolved)
        self.assertEqual(self.request.resolved_by, "")
        self.assertEqual(list(TeamAction.objects.values_list("action", flat=True)), ["waiting", "solved"])
        browser.post(reverse("team_help_waiting", args=[self.request.pk]))
        self.assertEqual(TeamAction.objects.count(), 2)

    def test_the_same_effect_as_the_admins_action(self):
        """The admin's «Mark as solved» writes the same fields: the day's list loses the request either way."""
        _key, browser = self.moderator()
        self.assertIn("help_request", [item.code for item, _links in browser.get("/team/today/").context["rows"]])
        browser.post(reverse("team_help_solved", args=[self.request.pk]))
        self.assertNotIn("help_request", [item.code for item, _links in browser.get("/team/today/").context["rows"]])

    def test_a_failed_action_writes_no_row(self):
        _key, browser = self.moderator()
        for url in (reverse("team_help_solved", args=[999999]), reverse("team_help_waiting", args=[999999])):
            self.assertEqual(browser.post(url).status_code, 404)
        for url in (reverse("team_help_solved", args=[self.request.pk]), "/team/me/end/"):
            self.assertEqual(browser.get(url).status_code, 405)
        self.assertEqual(browser.post("/team/today/").status_code, 405)
        self.assertFalse(TeamAction.objects.exists())
        self.request.refresh_from_db()
        self.assertIsNone(self.request.resolved)

    def test_csrf_on_every_post(self):
        self.moderator()
        strict = self.client_class(enforce_csrf_checks=True, headers=SITE)
        self.moderator("Other", browser=strict)
        for url in (reverse("team_help_solved", args=[self.request.pk]), "/team/me/end/"):
            self.assertEqual(strict.post(url).status_code, 403, url)
        self.assertFalse(TeamAction.objects.exists())
        self.request.refresh_from_db()
        self.assertIsNone(self.request.resolved)
        # With the token of the page it works
        page = strict.get("/team/help/").content.decode()
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page)[1]
        self.assertEqual(strict.post(reverse("team_help_solved", args=[self.request.pk]),
                                     {"csrfmiddlewaretoken": token}).status_code, 302)
        self.assertEqual(TeamAction.objects.count(), 1)

    def test_limits(self):
        _key, browser = self.moderator()
        with patch.object(area, "CHANGES", (2, 6)):
            for _ in range(2):
                self.assertEqual(browser.post(reverse("team_help_waiting", args=[self.request.pk])).status_code, 302)
            refused = browser.post(reverse("team_help_solved", args=[self.request.pk]))
            self.assertEqual(refused.status_code, 429)
            self.assertIn("Retry-After", refused)
        self.request.refresh_from_db()
        self.assertIsNone(self.request.resolved)
        with patch.object(area, "PAGES", (1, 3)):
            self.assertEqual(browser.get("/team/help/").status_code, 200)
            self.assertEqual(browser.get("/team/today/").status_code, 429)


class TheLog(AreaBase):
    def test_read_only_in_the_admin_and_kept_after_the_key(self):
        key, browser = self.moderator()
        browser.post(reverse("team_help_solved", args=[self.request.pk]))
        row = TeamAction.objects.get()
        boss = self.client_class()
        sign_in_as_staff_session(boss, User.objects.create_superuser("boss"))
        changelist = reverse("admin:team_teamaction_changelist")
        self.assertContains(boss.get(changelist), "Mara")
        self.assertEqual(boss.get(f"{changelist}?key__id__exact={key.pk}&action=solved").status_code, 200)
        self.assertEqual(boss.get(reverse("admin:team_teamaction_add")).status_code, 403)
        change = reverse("admin:team_teamaction_change", args=[row.pk])
        self.assertEqual(boss.get(change).status_code, 200)
        self.assertEqual(boss.post(change, {"name": "Eve", "action": "solved"}).status_code, 403)
        self.assertEqual(boss.post(reverse("admin:team_teamaction_delete", args=[row.pk]), {"post": "yes"}).status_code, 403)
        self.assertEqual(boss.post(changelist, {"action": "delete_selected", "_selected_action": [row.pk]}).status_code, 200)
        self.assertTrue(TeamAction.objects.filter(pk=row.pk).exists())
        # The key's page links to its rows
        self.assertContains(boss.get(reverse("admin:team_teamkey_change", args=[key.pk])),
                            f"{changelist}?key__id__exact={key.pk}")
        key.delete()
        row.refresh_from_db()
        self.assertIsNone(row.key)
        self.assertEqual(row.name, "Mara")


class Me(AreaBase):
    def test_who_you_are_and_the_end_of_this_pass(self):
        key, browser = self.moderator()
        page = browser.get("/team/me/")
        self.assertContains(page, "Mara")
        self.assertContains(page, "moderator")
        self.assertGreater(page.context["ends"], timezone.now() + timedelta(days=6))
        ended = browser.post("/team/me/end/")
        self.assertRedirects(ended, "/team/", fetch_redirect_response=False)
        self.assertEqual(ended.cookies["team_pass"]["max-age"], 0)
        self.assertEqual(TeamAction.objects.get().action, TeamAction.Action.SIGNED_OUT)
        self.assertEqual(browser.get("/team/today/").status_code, 404)
        # The key stays, and so do its other browsers
        self.assertTrue(TeamKey.active().filter(pk=key.pk).exists())


class TheDaysList(AreaBase):
    def test_the_same_items_as_the_admins_for_a_moderator(self):
        register("Quiet")
        register("Broken", lightning_status="no_answer", lightning_since=timezone.now() - timedelta(days=2))
        _key, browser = self.moderator()
        account = User.objects.create_user("mod", is_staff=True)
        account.groups.add(Group.objects.get(name=moderators.GROUP))
        admin = self.client_class()
        sign_in_as_staff_session(admin, account)
        theirs = admin.get("/admin/today/")
        self.assertEqual(theirs.status_code, 200)
        ours = browser.get("/team/today/")

        def shape(items):
            return [(item.code, item.shop.pk if item.shop else None, item.what, item.do) for item in items]

        self.assertEqual(shape(item for item, _links in ours.context["rows"]), shape(theirs.context["items"]))
        self.assertGreaterEqual(len(ours.context["rows"]), 2)
        self.assertFalse(theirs.context["can_approve"])
        page = ours.content.decode()
        self.assertNotIn("/admin/", page)
        self.assertNotIn(ADDRESS, page)
        self.assertNotIn(RECOVERY, page)
        self.assertIn("me…@wallet.example", page)
        # No button of the admin's: approving is not here
        self.assertNotIn("Approve", page)

    def test_the_rights_are_the_groups(self):
        self.assertEqual(area.RIGHTS, {f"{label}.{codename}" for label, codename, _what in moderators.PERMISSIONS})
        self.assertNotIn("shops.change_shop", area.RIGHTS)

    def test_links_into_the_admin_are_left_out(self):
        for url in ("/admin/", "/admin", "/admin/shops/shop/1/change/", "https://example.org/admin/x", "//admin/",
                    "/./admin/", "/ADMIN/", "/Admin/today/", "///admin//", "/en/../admin/", "/%61dmin/", "/admin?x=1",
                    "HTTPS://example.org//Admin/", "/x\\..\\admin/", "https://example.org\\admin/",
                    "https://example.org/x\\..\\admin/", "//example.org/admin/", "\\admin\\", "/%5cadmin/"):
            self.assertTrue(area.into_admin(url), url)
        for url in ("/en/shops/", "https://example.org/en/map/", "/administration-guide/", "/en/admin/", "/team/today/",
                    "//example.org/en/shops/", "/x\\y/"):
            self.assertFalse(area.into_admin(url), url)

    def test_the_page_speaks_the_visitors_language(self):
        _key, browser = self.moderator()
        page = browser.get("/team/today/", headers={"Accept-Language": "it"}).content.decode()
        self.assertIn('<html lang="it">', page)
        self.assertIn("Area dei volontari", page)
        self.assertNotIn("Asked for help:", page)

    def test_addresses_are_shortened(self):
        self.assertEqual(area.shortened(f"pay {ADDRESS}; then"), "pay me…@wallet.example; then")
        # The URL forms of a Lightning address (LUD-16, LUD-17)
        for form in ("https://wallet.example/.well-known/lnurlp/merchant", "lnurlp://wallet.example/.well-known/lnurlp/merchant",
                     "HTTP://wallet.example/.well-known/lnurlp/merchant?amount=1"):
            cut = area.shortened(f"see {form} now")
            self.assertNotIn("merchant", cut, form)
            self.assertIn("/.well-known/lnurlp/me…", cut, form)
        self.assertEqual(area.shortened("«a@b.c»"), "«a@b.c»")
        self.assertNotIn(RECOVERY, area.shortened(f"<{RECOVERY}>"))


class Preflight(AreaBase):
    def test_notes(self):
        self.member("Mara", role="moderator")
        self.member("Tom", role="team")
        notes = preflight.team_notes()
        self.assertIn("the volunteers' area is on (TEAM_AREA=on): 1 moderator keys can open it, 0 of them may approve new shops (/team/)", notes)
        with override_settings(SITE_MODE="production"):
            self.assertIn("it only signs the volunteers in to their area (/team/)", " ".join(preflight.team_notes()))
        with override_settings(TEAM_ACCESS=False):
            self.assertEqual(preflight.team_notes(), [
                ("the volunteers' area is on (TEAM_AREA=on) but team access is off: it does nothing until TEAM_ACCESS=on "
                "(docs/features/team-access.md)")])
        with override_settings(TEAM_AREA=False):
            self.assertNotIn("volunteers", " ".join(preflight.team_notes()))


class Switch(AreaBase):
    def test_a_wrong_value_stops_the_site(self):
        import importlib

        from django.core.exceptions import ImproperlyConfigured

        import config.settings as module

        for value, expected in (("on", True), ("off", False), ("", False)):
            with patch.dict("os.environ", {"TEAM_AREA": value}):
                self.assertIs(importlib.reload(module).TEAM_AREA, expected, value)
        with patch.dict("os.environ", {"TEAM_AREA": "yes"}), self.assertRaises(ImproperlyConfigured):
            importlib.reload(module)
        importlib.reload(module)

    def test_documented_and_handed_to_web(self):
        web = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["web"]["environment"]
        self.assertEqual(web["TEAM_AREA"], "${TEAM_AREA:-off}")
        if (ROOT / ".env.example").is_file():
            self.assertIn("\nTEAM_AREA=off\n", (ROOT / ".env.example").read_text())
        if (ROOT / "docs").is_dir():
            self.assertIn("| `TEAM_AREA` | `off` |", (ROOT / "docs" / "configuration.md").read_text())
            page = (ROOT / "docs" / "features" / "team-access.md").read_text()
            for word in ("TEAM_AREA=on", "/team/today/", "/team/help/", "moderator"):
                self.assertIn(word, page)
            self.assertIn("`team.TeamAction`", (ROOT / "docs" / "privacy.md").read_text())


class CsrfAfterTheGate(AreaBase):
    """The middleware would answer 403 to a POST without a token as soon as the address resolves, before the gate: that
    would tell a stranger the area exists. The gate comes first, CSRF after it."""

    def strict(self):
        return self.client_class(enforce_csrf_checks=True, headers=SITE)

    def posts(self):
        return [url.format(number=self.request.pk, shop=self.shop.pk) for url in CHANGES] + list(PAGES)

    def test_area_off(self):
        browser = self.strict()
        self.moderator(browser=browser)
        for settings_ in ({"TEAM_AREA": False}, {"TEAM_ACCESS": False}):
            with override_settings(**settings_):
                for url in [*self.posts(), "/team/"]:
                    gone(self, browser, "post", url, settings_)

    def test_area_on_without_a_pass(self):
        stranger = self.strict()
        for url in self.posts():
            self.assertEqual(stranger.post(url).status_code, 404, url)
        # A pass of another role is no pass for the area
        _key, wallet = self.member("Tom", role="team")
        team = self.strict()
        self.sign_in(team, wallet)
        for url in self.posts():
            self.assertEqual(team.post(url).status_code, 404, url)
        # The sign-in page is public with the area on: a POST there is refused for its method
        self.assertEqual(stranger.post("/team/").status_code, 405)

    def test_area_on_with_a_pass_and_no_token(self):
        browser = self.strict()
        self.moderator(browser=browser)
        for url in (url.format(number=self.request.pk, shop=self.shop.pk) for url in CHANGES):
            self.assertEqual(browser.post(url).status_code, 403, url)
        self.assertFalse(TeamAction.objects.exists())


class TheDiagnosisCopy(AreaBase):
    """What the area keeps of the diagnosis copied into a request: nothing about the owner's account and sign-in."""

    def test_every_code_of_the_diagnosis_is_classified(self):
        source = (ROOT / "shops" / "diagnosis.py").read_text() + (ROOT / "shops" / "helpdesk.py").read_text()
        codes = set(re.findall(r'(?:run\.add|Finding)\(\s*"([a-z0-9_]+)"', source))
        self.assertGreater(len(codes), 40)
        self.assertEqual(area.DIAGNOSIS_SHOWN & area.DIAGNOSIS_HIDDEN, set())
        self.assertEqual(area.DIAGNOSIS_SHOWN & area.DIAGNOSIS_BY_CHOICE, set())
        self.assertEqual(area.DIAGNOSIS_HIDDEN & area.DIAGNOSIS_BY_CHOICE, set())
        # A new code fails here until somebody decides whether the area may show it
        self.assertEqual(codes, area.DIAGNOSIS_SHOWN | area.DIAGNOSIS_HIDDEN | area.DIAGNOSIS_BY_CHOICE)
        # The findings that name the staff registers are the community's choice: every staff_* code that names one
        self.assertEqual(area.DIAGNOSIS_BY_CHOICE, {"staff_code_waiting", "staff_off", "staff_ok", "staff_unopened"})
        # An unknown code stays out, whatever the community chose
        for choice in (False, True):
            self.assertEqual(area.diagnosis_for_area("! [a_code_nobody_knows] New.\n    detail", choice), "")

    def test_the_owners_account_is_left_out(self):
        summary = "\n".join([
            "✗ [lightning_no_answer] Your Lightning wallet is not answering.", f"    {ADDRESS}; 1.0 s",
            "! [owner_is_staff] This shop belongs to a staff account.",
            "    signs in with: wallet key 02abcdef…; last login 2026-11-01 10:00 UTC; staff account",
            "✓ [owner_recovery_email] You can sign in with a link.",
            "    signs in with: wallet key 02abcdef…, 2 connected wallet(s); last login 2026-11-01 10:00 UTC; recovery email o***@m***.example",
            "! [owner_no_recovery_email] There is no email to get back in.", "    signs in with: email link; never logged in",
            "✗ [account_changed] The administration changed your account.",
            "    1 change(s): 2026-11-02 09:00 UTC username via admin by bossname; told: email",
            "! [account_changed_other] Something changed on another account.", "    1 change(s): by otherboss",
            "! [a_code_nobody_knows] Something new.", "    secret detail",
            "✓ [shop_visible] Your shop is on the map.", "    id=1",
        ])
        HelpRequest.objects.filter(pk=self.request.pk).update(summary=summary)
        _key, browser = self.moderator()
        page = browser.get("/team/help/").content.decode()
        for gone_text in ("02abcdef", "last login", "o***@", "staff account", "never logged in",
                          "secret detail", "a_code_nobody_knows", ADDRESS):
            self.assertNotIn(gone_text, page)
        for kept in ("lightning_no_answer", "me…@wallet.example; 1.0 s", "shop_visible", "id=1",
                     # The changes to the accounts are what the area's register page shows: kept, with who did them
                     "account_changed", "bossname", "otherboss"):
            self.assertIn(kept, page)
        self.assertEqual(area.diagnosis_for_area("(the diagnosis could not be made: TimeoutError)"),
                         "(the diagnosis could not be made: TimeoutError)")

    def test_who_solved_it_from_the_admin_is_not_named(self):
        HelpRequest.objects.filter(pk=self.request.pk).update(resolved=timezone.now(), resolved_by="bossname")
        _key, browser = self.moderator()
        page = browser.get("/team/help/").content.decode()
        self.assertNotIn("bossname", page)
        self.assertIn("from the administration", page)


class TheCodesCookie(AreaBase):
    def test_a_cookie_of_the_old_name_is_read_by_nothing(self):
        _key, wallet = self.member("Mara", role="moderator")
        page = self.client.get("/team/").content.decode()
        lnurl = re.search(r'href="lightning:([A-Z0-9]+)"', page)[1]
        value = self.client.cookies[views.K1_COOKIE].value
        planted = self.browser()
        planted.cookies["team_k1"] = signing.get_cookie_signer(salt="team_k1" + views.K1_SALT).sign(k1_of(lnurl))
        self.client.get("/team/auth/", signed(k1_of(lnurl), wallet))
        self.assertEqual(planted.get("/team/poll/").json(), {"state": "expired"})
        self.assertNotIn("team_pass", planted.cookies)
        self.assertTrue(value)
        self.assertEqual(self.client.get("/team/poll/").json(), {"state": "ok"})
