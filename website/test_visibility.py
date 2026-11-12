"""Public and private parts of the pages (website/visibility.py)."""
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import yaml
from django.contrib.auth import get_user_model
from django.core import checks
from django.test import TestCase, override_settings

from config.community import community, private_file, staff_file
from shops.models import Shop
from website import visibility
from website.apps import wrong_marks
from website.views import page_html

ROOT = Path(__file__).resolve().parent.parent

PAGE = """Before.

::: private

A joke for friends.[^friends]

[^friends]: Told at the bar.

:::

::: public

The same thing with decorum.[^source]

:::

After.[^source]

[^source]: The source.
[^alone]: Called only in private.[^x]
"""

# What Caddy writes on every request from the site's address (Caddyfile)
SITE = {"X-Entrance": "site"}
# The words of the private parts: none of them may reach a public page
SECRETS = ("Pumpernickel", "Quagmire", "Zanzibar", "Wobbleton", "Gherkin", "Snorkel")

MIXED = """---
title: Mixed
description: A page with both views.
---
# Mixed

Open to all.[^all]

::: private

## Pumpernickel heading

A Quagmire story.[^secret]

[^secret]: A Zanzibar footnote.

:::

::: public

## Decent heading

A decent story.

:::

<div class="box">

::: private

Wobbleton inside a plain div.

:::

</div>

<section markdown="1">

::: private

**Gherkin** inside a section with Markdown.

:::

::: public

*Polite* inside a section with Markdown.

:::

</section>

[^all]: For everyone.
"""

HIDDEN = """---
title: Friends only
visibility: private
---
Snorkel and friends.
"""

STAFF = """---
title: Staff only
visibility: staff
---
Mandrake for the staff.
"""


class Filter(TestCase):
    """The same cases as the community's own tool: the same text gives the same result."""

    def test_public_view(self):
        text = visibility.filtered(PAGE)
        self.assertIn("The same thing with decorum.", text)
        self.assertNotIn("joke", text)
        self.assertNotIn("Told at the bar", text)
        self.assertNotIn(":::", text)
        self.assertNotIn("[^alone]", text)  # nobody calls it: gone
        self.assertIn("[^source]: The source.", text)

    def test_private_view(self):
        text = visibility.filtered(PAGE, private=True)
        self.assertIn("A joke for friends.", text)
        self.assertIn("[^friends]: Told at the bar.", text)
        self.assertNotIn("with decorum", text)
        self.assertNotIn(":::", text)

    def test_without_marks_nothing_changes(self):
        text = "One.\n\nTwo.[^a]\n\n[^a]: Note.\n"
        self.assertEqual(visibility.filtered(text), text)
        self.assertEqual(visibility.filtered(text, private=True), text)

    def test_wrong_marks(self):
        for wrong in ("::: private\nA\n", ":::\n", "::: private\n::: public\n:::\n:::\n"):
            with self.assertRaises(visibility.MarkError):
                visibility.filtered(wrong)

    def test_a_mark_inside_code_is_an_error(self):
        for text in ("```\n::: private\n```\n", "~~~~\n::: private\n~~~\n:::\n~~~~\n", "```\n::: private\n",
                     "  ```python\n:::\n  ```\n"):
            with self.assertRaises(visibility.MarkError, msg=text):
                visibility.filtered(text)

    def test_code_without_marks_stays(self):
        text = "```\ncode ~~~\n```\n\n~~~\nmore\n~~~\n"
        self.assertEqual(visibility.filtered(text), text)

    def test_a_fence_closes_only_with_its_own_kind_and_length(self):
        """An unbalanced fence or a line of tildes cannot hide a mark and make the private part public."""
        for text in ("~~~~~~\n\n::: private\nSecret.\n:::\n", "````\n```\n::: private\nSecret.\n:::\n````\n",
                     "~~~\n```\n::: private\nSecret.\n:::\n~~~\n"):
            with self.assertRaises(visibility.MarkError, msg=text):
                visibility.filtered(text)
        # Backticks in the info string: not a fence, the marks work
        self.assertEqual(visibility.filtered("``` a`b\n\n::: private\nSecret.\n:::\n"), "``` a`b\n\n")

    def test_a_mark_written_otherwise_is_an_error(self):
        for line in ("::: Private", "::: PUBLIC", "::: secret", "  ::: private", "\t:::", " :::", ":::private text",
                     "::::", "::: private :::", ":::private", ":::public"):
            with self.assertRaises(visibility.MarkError, msg=line):
                visibility.filtered(f"Text.\n\n{line}\n\nSecret.\n\n:::\n")

    def test_windows_line_ends_and_bom(self):
        text = "\ufeffBefore.\r\n\r\n::: private\r\nSecret.\r\n:::\r\n\r\nAfter.\r\n"
        self.assertEqual(visibility.filtered(text), "Before.\n\nAfter.\n")
        from config.community import front_matter
        meta, body = front_matter("\ufeff---\r\ntitle: X\r\nvisibility: private\r\n---\r\nText\r\n")
        self.assertEqual((meta, body), ({"title": "X", "visibility": "private"}, "Text\n"))
        self.assertTrue(visibility.is_private_page(meta))

    def test_a_note_of_many_paragraphs_goes_whole(self):
        text = ("Text.\n\n[^long]: First paragraph.\n    still the first.\n\n    Second paragraph.\n\n"
                "        code in the note\n\nAfter.\n")
        self.assertEqual(visibility.filtered(text), "Text.\n\n\nAfter.\n")

    def test_a_lazy_line_belongs_to_its_note(self):
        self.assertEqual(visibility.filtered("Text.\n\n[^a]: First line\nlazy line.\n\nAfter.\n"), "Text.\n\n\nAfter.\n")

    def test_an_indented_definition_is_a_definition(self):
        self.assertEqual(visibility.filtered("Text.\n\n   [^a]: Note.\n\nAfter.\n"), "Text.\n\n\nAfter.\n")

    def test_notes_called_only_by_notes_that_go_go_too(self):
        text = "Text.[^kept]\n\n[^kept]: Kept.\n[^gone]: Gone, calls [^chain].\n[^chain]: Chain.\n[^self]: Me [^self].\n"
        self.assertEqual(visibility.filtered(text), "Text.[^kept]\n\n[^kept]: Kept.\n")

    def test_a_note_called_from_a_kept_note_stays(self):
        text = "Text.[^a]\n\n[^a]: See [^b].\n[^b]: B.\n"
        self.assertEqual(visibility.filtered(text), text)

    def test_an_orphan_note_goes_with_its_indented_lines(self):
        text = "Text.\n\n[^gone]: First line.\n    Second line.\n\nEnd.\n"
        self.assertEqual(visibility.filtered(text), "Text.\n\n\nEnd.\n")

    def test_private_page(self):
        from config.community import visibility as read
        self.assertTrue(visibility.is_private_page({"title": "X", "visibility": "private"}))
        for meta in ({"visibility": "Private"}, {"Visibility": "private"}, {"visibility": '"private"'}, {"visibility": "'PRIVATE'"}):
            self.assertEqual(read(meta), "private", meta)
        for meta in ({"title": "X"}, {"visibility": "public"}, {"visibility": ""}):
            self.assertEqual(read(meta), "public", meta)
        # A value not understood: private, and the check says so
        self.assertIsNone(read({"visibility": "members"}))
        self.assertTrue(visibility.is_private_page({"visibility": "hidden"}))


class CommunityWithPrivatePages(TestCase):
    """A copy of the example community with a mixed page, a private page and a menu item pointing to it."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name) / "community"
        shutil.copytree(ROOT / "examples/community", root)
        data = yaml.safe_load((root / "community.yml").read_text())
        data["menu"].append({"slug": "friends", "labels": {"it": "Amici", "en": "Friends"}})
        (root / "community.yml").write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
        for language in ("it", "en"):
            (root / f"pages/{language}/mixed.md").write_text(MIXED)
            (root / f"pages/{language}/friends.md").write_text(HIDDEN)
            (root / f"pages/{language}/staff-only.md").write_text(STAFF)
        patcher = patch("config.community.COMMUNITY_DIR", root)
        patcher.start()
        self.addCleanup(community.cache_clear)
        self.addCleanup(patcher.stop)
        community.cache_clear()
        private_file.cache_clear()
        staff_file.cache_clear()
        page_html.cache_clear()

    def get(self, path, **headers):
        return self.client.get(path, headers=headers)

    def assertPublic(self, html):
        for word in SECRETS:
            self.assertNotIn(word, html)
        self.assertNotIn(":::", html)


class PublicView(CommunityWithPrivatePages):
    def test_the_public_page_has_no_trace_of_the_private_part(self):
        response = self.get("/it/mixed/")
        html = response.content.decode()
        self.assertPublic(html)
        self.assertIn("Decent heading", html)
        self.assertIn("<em>Polite</em>", html)  # Markdown inside the section still works
        self.assertIn("For everyone.", html)
        self.assertNotIn("private", html.split("<main")[1].split("</main>")[0].lower())
        self.assertNotIn("view-notice", html)

    def test_notes_are_numbered_without_gaps(self):
        html = self.get("/it/mixed/").content.decode()
        self.assertIn('id="fn:all"', html)
        self.assertNotIn('id="fn:secret"', html)
        self.assertIn('<a class="footnote-ref" href="#fn:all">1</a>', html)

    def test_a_private_page_is_a_404(self):
        self.assertEqual(self.get("/it/friends/").status_code, 404)
        self.assertEqual(self.get("/en/friends/").status_code, 404)

    def test_a_private_page_is_in_no_list(self):
        for path in ("/sitemap.xml", "/it/", "/it/mixed/", "/en/shops/map/"):
            html = self.get(path).content.decode()
            self.assertNotIn("friends", html, path)
            self.assertNotIn("Snorkel", html, path)

    def test_the_cookie_alone_gives_nothing(self):
        self.client.cookies[visibility.COOKIE] = "on"
        html = self.get("/it/mixed/").content.decode()
        self.assertPublic(html)
        self.assertEqual(self.get("/it/friends/").status_code, 404)

    def test_the_switch_does_not_exist_for_others(self):
        """It answers like any address that does not exist: the language middleware sends it to /<language>/view/, a 404."""
        unknown = self.client.post("/nonexistent/", {"private": "on"})
        response = self.client.post("/view/", {"private": "on", "next": "/it/"})
        self.assertEqual((response.status_code, response["Location"]), (unknown.status_code, unknown["Location"].replace("nonexistent", "view")))
        self.assertEqual(self.client.get("/view/", follow=True).status_code, 404)
        self.assertNotIn(visibility.COOKIE, response.cookies)

    @override_settings(DEV_ALLOWED_IPS="203.0.113.7")
    def test_a_forged_header_from_outside_gives_nothing(self):
        """Caddy writes the last X-Forwarded-For: a visitor can only put addresses before it. Other headers count for
        nothing."""
        self.assertPublic(self.get("/it/mixed/", **{**SITE, "X-Forwarded-For": "203.0.113.7, 198.51.100.1"}).content.decode())
        for header in ("X-Real-IP", "Client-IP", "Forwarded", "X-Client-IP"):
            self.assertPublic(self.get("/it/mixed/", **{**SITE, header: "203.0.113.7", "X-Forwarded-For": "198.51.100.1"}).content.decode())

    def test_every_page_has_the_same_cache_headers(self):
        """With curl -I nobody learns which pages have a private part, or which addresses are private pages."""
        headers = set()
        for path in ("/it/mixed/", "/it/about/", "/it/", "/it/friends/", "/it/nonexistent/"):
            response = self.get(path)
            headers.add((response["Cache-Control"], response["Vary"]))
        self.assertEqual(len(headers), 1, headers)
        self.assertIn("private", headers.pop()[0])

    @override_settings(DEV_ALLOWED_IPS="203.0.113.7")
    def test_the_onion_never_has_an_allowed_address(self):
        """On the onion Caddy writes X-Entrance: onion, and the address it passes may be one a visitor chose."""
        for entrance in ("onion", "", "Site", "site, site"):
            self.assertPublic(self.get("/it/mixed/", **{"X-Entrance": entrance, "X-Forwarded-For": "203.0.113.7"}).content.decode())
        self.assertPublic(self.client.get("/it/mixed/", headers={"X-Forwarded-For": "203.0.113.7"}).content.decode())

    def test_links_to_private_pages_are_left_out(self):
        from config import community as community_config
        data = {**community(), "menu": [{"path": "friends/", "labels": {"it": "Amici", "en": "Friends"}},
                                        {"path": "friends/#x", "labels": {"it": "Amici", "en": "Friends"}},
                                        {"path": "shops/map/", "labels": {"it": "Mappa", "en": "Map"}}],
                "shop_page": {"wallet": "friends/", "stories": {"7": [{"path": "friends/", "text": "Friends"},
                                                                    {"path": "stories/", "text": "Stories"}]}}}
        warnings = []
        with patch("config.community.community", return_value=data):
            self.assertEqual([item["href"] for item in community_config.menu("it")], ["/it/shops/map/"])
            links = community_config.shop_page_links("it", "7", warnings.append)
        self.assertEqual(links, {"wallet": "", "stories": [{"href": "/it/stories/", "text": "Stories"}]})
        self.assertEqual(len(warnings), 2)
        self.assertTrue(all("private page" in warning for warning in warnings))
        with patch("shops.views.GUIDE_PAGE", "friends"):
            from shops.views import guide
            self.assertEqual(guide(), "")
        with patch("shops.views.GUIDE_PAGE", "mixed"):
            self.assertEqual(guide(), "/en/mixed/")

    def test_wrong_marks_hide_the_page_and_the_check_says_where(self):
        broken = "---\ntitle: Broken\n---\n::: private\nWobbleton.\n"
        with patch("website.views.parse", return_value=({}, broken.split("---\n", 2)[2])):
            self.assertEqual(self.get("/it/mixed/").status_code, 404)
        from config import community as community_config
        (community_config.COMMUNITY_DIR / "pages/it/broken.md").write_text(broken)
        problems = wrong_marks(None)
        self.assertEqual([problem.id for problem in problems], ["website.W003"])
        self.assertIn("it/broken.md", problems[0].msg)
        self.assertIsInstance(problems[0], checks.Warning)

    def test_a_visibility_not_understood_is_private_and_the_check_says_so(self):
        from config import community as community_config
        (community_config.COMMUNITY_DIR / "pages/it/strange.md").write_text("---\ntitle: X\nvisibility: members\n---\nText\n")
        self.assertEqual([problem.id for problem in wrong_marks(None)], ["website.W005"])
        self.assertEqual(self.get("/it/strange/").status_code, 404)
        self.assertNotIn("strange", self.get("/sitemap.xml").content.decode())

    def test_the_check_warns_about_shared_networks(self):
        cases = {"203.0.113.7 2001:db8::1": 0, "127.0.0.1 ::1": 0, "private_ranges": 1, "172.17.0.1": 1, "10.0.0.0/8": 1,
                 "192.168.1.5": 1, "fd00::1": 1, "fe80::1": 1, "100.64.0.1": 1, "198.51.0.0/16": 1, "2001:db8::/32": 1,
                 "203.0.113.0/24 2001:db8:1::/48": 0, "not-an-address": 1, "127.0.0.0/8": 1}
        for value, count in cases.items():
            with self.settings(DEV_ALLOWED_IPS=value):
                found = [problem for problem in wrong_marks(None) if problem.id == "website.W004"]
            self.assertEqual(len(found), count, (value, [problem.msg for problem in found]))


@override_settings(DEV_ALLOWED_IPS="10.1.2.0/24 203.0.113.7", SITE_MODE="development")
class AllowedAddress(CommunityWithPrivatePages):
    def get(self, path, **headers):
        return self.client.get(path, headers={**SITE, "X-Forwarded-For": "203.0.113.7", **headers})

    def test_the_private_part_is_on_from_the_start(self):
        html = self.get("/it/mixed/").content.decode()
        for word in ("Pumpernickel", "Quagmire", "Zanzibar", "Wobbleton", "Gherkin"):
            self.assertIn(word, html)
        self.assertNotIn("Decent heading", html)
        self.assertNotIn("Polite", html)
        self.assertNotIn(":::", html)
        self.assertIn("Vista privata", html)
        self.assertIn("modalità del sito: development", html)
        self.assertIn("Nascondi la parte privata", html)

    def test_a_network_counts_too(self):
        html = self.client.get("/it/mixed/", headers={**SITE, "X-Forwarded-For": "10.1.2.99"}).content.decode()
        self.assertIn("Quagmire", html)

    def test_the_private_page_opens(self):
        self.assertContains(self.get("/it/friends/"), "Snorkel")

    def test_but_it_stays_out_of_menus_and_sitemap(self):
        self.assertNotIn("friends", self.get("/sitemap.xml").content.decode())
        self.assertNotIn('href="/it/friends/"', self.get("/it/").content.decode())

    def test_the_static_copy_is_always_public(self):
        html = self.get("/it/mixed/", **{"X-Static-Copy": "1"}).content.decode()
        self.assertPublic(html)
        self.assertNotIn("view-notice", html)
        self.assertEqual(self.get("/it/friends/", **{"X-Static-Copy": "1"}).status_code, 404)

    def test_switching_off_and_on(self):
        response = self.client.post("/view/", {"private": "off", "next": "/it/mixed/"}, headers={**SITE, "X-Forwarded-For": "203.0.113.7"})
        self.assertRedirects(response, "/it/mixed/", fetch_redirect_response=False)
        cookie = response.cookies[visibility.COOKIE]
        self.assertEqual(cookie.value, "off")
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        html = self.get("/it/mixed/").content.decode()
        self.assertPublic(html)
        self.assertIn("Vista pubblica", html)
        self.assertIn("Mostra la parte privata", html)
        self.assertEqual(self.get("/it/friends/").status_code, 404)
        self.client.post("/view/", {"private": "on", "next": "/it/mixed/"}, headers={**SITE, "X-Forwarded-For": "203.0.113.7"})
        self.assertIn("Quagmire", self.get("/it/mixed/").content.decode())

    def test_the_switch_goes_back_only_to_this_site(self):
        for target in ("https://evil.example/", "//evil.example/", "/\\evil.example", "javascript:alert(1)", ""):
            response = self.client.post("/view/", {"private": "off", "next": target}, headers={**SITE, "X-Forwarded-For": "203.0.113.7"})
            self.assertEqual(response["Location"], "/", target)

    def test_the_switch_needs_csrf(self):
        client = self.client_class(enforce_csrf_checks=True)
        response = client.post("/view/", {"private": "off", "next": "/it/"}, headers={**SITE, "X-Forwarded-For": "203.0.113.7"})
        self.assertEqual(response.status_code, 403)
        page = client.get("/it/mixed/", headers={**SITE, "X-Forwarded-For": "203.0.113.7"})
        token = page.context["csrf_token"]
        response = client.post("/view/", {"private": "off", "next": "/it/", "csrfmiddlewaretoken": token},
                               headers={**SITE, "X-Forwarded-For": "203.0.113.7"})
        self.assertEqual(response.status_code, 302)

    def test_the_switch_answers_only_to_post(self):
        self.assertEqual(self.get("/view/").status_code, 405)

    def test_answers_for_the_private_view_stay_private(self):
        for path in ("/it/", "/it/mixed/", "/it/shops/map/"):
            response = self.get(path)
            self.assertIn("private", response["Cache-Control"], path)
            self.assertIn("no-cache", response["Cache-Control"], path)
            self.assertIn("Cookie", response["Vary"], path)

    def test_the_cache_does_not_mix_the_views(self):
        """The same page, in turn to one view and the other, from the same process: each gets its own."""
        outside = {"X-Forwarded-For": "198.51.100.1"}
        for _round in range(2):
            self.assertIn("Quagmire", self.get("/it/mixed/").content.decode())
            self.assertPublic(self.client.get("/it/mixed/", headers=outside).content.decode())

    def test_german_speaks_german(self):
        from django.template.loader import render_to_string
        from django.utils import translation
        with translation.override("de"):
            html = render_to_string("partials/view_notice.html", {"visibility": {"private": True, "mode": "development"}})
        self.assertIn("Private Ansicht", html)
        self.assertIn("Privaten Teil ausblenden", html)


@override_settings(DEV_ALLOWED_IPS="203.0.113.7", SITE_MODE="production")
class AllowedAddressInProduction(CommunityWithPrivatePages):
    def test_every_mode(self):
        html = self.client.get("/en/mixed/", headers={**SITE, "X-Forwarded-For": "203.0.113.7"}).content.decode()
        self.assertIn("Quagmire", html)
        self.assertIn("site mode: production", html)


class Accounts(CommunityWithPrivatePages):
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user("owner")
        self.shop = Shop.objects.create(name="Bar", owner=self.user, discount=0, latitude=1, longitude=1)
        self.client.force_login(self.user)

    def test_an_ordinary_owner_sees_the_public_part(self):
        html = self.client.get("/it/mixed/").content.decode()
        self.assertPublic(html)
        self.assertNotIn("view-notice", html)

    def test_an_approved_owner_gets_the_private_view_without_the_mode(self):
        from datetime import date

        from django.utils import timezone
        Shop.objects.filter(pk=self.shop.pk).update(certified_month=date(2026, 1, 1), private_approved=timezone.now(),
                                                    private_approved_by="staffer")
        html = self.client.get("/it/mixed/").content.decode()
        self.assertIn("Quagmire", html)
        self.assertIn("Vista privata", html)
        self.assertNotIn("modalità del sito", html)

    def test_a_certified_owner_sees_the_private_part(self):
        with patch("shops.certified.sees_private_part", return_value=True):
            self.assertIn("Quagmire", self.client.get("/it/mixed/").content.decode())

    def test_a_certified_owner_needs_the_approval_of_the_association(self):
        from datetime import date

        from django.utils import timezone
        Shop.objects.filter(pk=self.shop.pk).update(certified_month=date(2026, 1, 1))
        self.assertNotIn("Quagmire", self.client.get("/it/mixed/").content.decode())
        Shop.objects.filter(pk=self.shop.pk).update(private_approved=timezone.now(), private_approved_by="staffer")
        self.assertIn("Quagmire", self.client.get("/it/mixed/").content.decode())
        Shop.objects.filter(pk=self.shop.pk).update(private_approved=None, private_approved_by="")
        self.assertNotIn("Quagmire", self.client.get("/it/mixed/").content.decode())

    def test_nobody_is_certified_yet(self):
        from shops.certified import owns_certified_shop
        self.assertFalse(owns_certified_shop(self.user))


class ExamplePage(TestCase):
    def test_the_example_page_has_both_views(self):
        page_html.cache_clear()
        public = self.client.get("/en/stories/").content.decode()
        self.assertIn("all together", public)
        self.assertNotIn("cake", public)
        self.assertNotIn("dinner", public)
        self.assertIn("You find them on the map.", public)
        with override_settings(DEV_ALLOWED_IPS="127.0.0.1"):
            private = self.client.get("/en/stories/", headers=SITE).content.decode()
        self.assertIn("cake", private)
        self.assertIn("dinner", private)
        self.assertNotIn("all together", private)


class StaffPage(CommunityWithPrivatePages):
    def test_anonymous_gets_a_404(self):
        self.assertEqual(self.client.get("/it/staff-only/").status_code, 404)

    def test_a_certified_owner_gets_a_404(self):
        owner = get_user_model().objects.create_user("owner")
        self.client.force_login(owner)
        with patch("shops.certified.sees_private_part", return_value=True):
            self.assertEqual(self.client.get("/it/staff-only/").status_code, 404)

    def test_staff_sees_it(self):
        self.client.force_login(get_user_model().objects.create_user("staffer", is_staff=True))
        self.assertContains(self.client.get("/it/staff-only/"), "Mandrake")

    @override_settings(DEV_ALLOWED_IPS="203.0.113.7")
    def test_an_allowed_address_sees_it(self):
        response = self.client.get("/it/staff-only/", headers={**SITE, "X-Forwarded-For": "203.0.113.7"})
        self.assertContains(response, "Mandrake")

    def test_the_static_copy_never_has_it(self):
        self.client.force_login(get_user_model().objects.create_user("staffer", is_staff=True))
        self.assertEqual(self.client.get("/it/staff-only/", headers={"X-Static-Copy": "1"}).status_code, 404)

    def test_it_is_in_no_list(self):
        self.client.force_login(get_user_model().objects.create_user("staffer", is_staff=True))
        for path in ("/sitemap.xml", "/it/"):
            self.assertNotIn("staff-only", self.client.get(path).content.decode(), path)
