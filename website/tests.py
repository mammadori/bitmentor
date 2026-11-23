import hashlib
import json
import os
import re
import shutil
import struct
import tempfile
import unittest
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import markdown
import yaml
from django.conf import settings
from django.templatetags.static import static
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import override

from config.community import community, community_data, menu
from shops.models import Shop
from shops.views import money_text
from website.templatetags.head import image_size, theme_image
from website.views import responsive

ROOT = Path(__file__).resolve().parent.parent
# Absolute addresses come from PUBLIC_URL, not from the Host header
PUBLIC = "https://website.example"


def blocked(rules, path):
    """How a search engine reads the Disallow lines: a start of an address, or with `$` at the end the whole address."""
    return any(path == rule[:-1] if rule.endswith("$") else path.startswith(rule) for rule in rules)


def png(width, height):
    """The header of a PNG: enough to read its dimensions."""
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + bytes(5)


@override_settings(PUBLIC_URL=PUBLIC)
class Pages(TestCase):
    def test_home_in_every_language(self):
        self.assertContains(self.client.get("/en/"), "Example community")
        self.assertContains(self.client.get("/it/"), "Benvenuti")

    def test_a_page_is_converted_once(self):
        """Markdown costs: the same page with the same text is converted only once per process."""
        from website.views import page_html

        page_html.cache_clear()
        with patch("website.views.markdown.markdown", wraps=markdown.markdown) as convert:
            first = self.client.get("/it/").content
            second = self.client.get("/it/").content
        self.assertEqual(convert.call_count, 1)
        self.assertEqual(first, second)

    def test_the_home_has_one_address(self):
        # /it/home/ showed the home with a canonical of its own: for search engines they were two identical pages
        for code, _name in settings.LANGUAGES:
            self.assertRedirects(self.client.get(f"/{code}/home/"), f"/{code}/", status_code=301)

    def test_language_switch_keeps_the_page(self):
        self.assertContains(self.client.get("/it/shops/map/"), 'href="/en/shops/map/"')

    def test_sitemap(self):
        self.assertContains(self.client.get("/sitemap.xml"), "<urlset")

    def test_the_sitemap_names_every_language_of_a_page(self):
        xml = self.client.get("/sitemap.xml").content.decode()
        entry = re.search(r"<url><loc>https://website.example/it/about/</loc>(.*?)</url>", xml)[1]
        self.assertIn('<xhtml:link rel="alternate" hreflang="it" href="https://website.example/it/about/"/>', entry)
        self.assertIn('<xhtml:link rel="alternate" hreflang="en" href="https://website.example/en/about/"/>', entry)
        default = f"https://website.example/{settings.LANGUAGE_CODE}/about/"
        self.assertIn(f'<xhtml:link rel="alternate" hreflang="x-default" href="{default}"/>', entry)
        self.assertIn(
            'hreflang="x-default" href="https://website.example/' + settings.LANGUAGE_CODE + '/shops/map/"', xml
        )
        self.assertIn('xmlns:xhtml="http://www.w3.org/1999/xhtml"', xml)

    def test_missing_page(self):
        self.assertEqual(self.client.get("/it/nonexistent/").status_code, 404)

    def test_note_backlink_speaks_the_language_of_the_page(self):
        with patch("website.views.parse", return_value=({}, "Text[^a]\n\n[^a]: Note.")):
            self.assertContains(self.client.get("/it/"), "Torna alla nota 1 nel testo")

    def test_page_image_becomes_absolute(self):
        with patch("website.views.parse", return_value=({"image": "/theme/a.png"}, "text")):
            self.assertContains(
                self.client.get("/it/"), 'property="og:image" content="https://website.example/theme/a.png"'
            )

    def test_absolute_page_image_is_left_alone(self):
        with patch("website.views.parse", return_value=({"image": "https://example.org/a.png"}, "text")):
            self.assertContains(self.client.get("/it/"), 'property="og:image" content="https://example.org/a.png"')


@override_settings(PUBLIC_URL=PUBLIC)
class Sharing(TestCase):
    """What WhatsApp, Telegram, Mastodon, Signal and search engines read."""

    def head(self, path):
        return self.client.get(path).content.decode().split("</head>")[0]

    def test_a_markdown_page_shares_its_front_matter(self):
        with patch("website.views.parse", return_value=({"title": "History", "description": "Year by year."}, "text")):
            head = self.head("/it/")
        self.assertIn("<title>History · Example community</title>", head)
        self.assertIn('<meta name="description" content="Year by year.">', head)
        self.assertIn('<meta property="og:title" content="History">', head)
        self.assertIn('<meta property="og:description" content="Year by year.">', head)

    def test_a_template_with_its_own_title_shares_that_title(self):
        shop = Shop.objects.create(name="Bar & Café", description="Brioche and coffee.", latitude=1, longitude=1)
        head = self.head(f"/it/shops/s/{shop.id}/")
        self.assertIn("<title>Bar &amp; Café · Example community</title>", head)
        self.assertIn('<meta property="og:title" content="Bar &amp; Café · Example community">', head)
        self.assertIn('<meta property="og:description" content="Brioche and coffee.">', head)

    def test_canonical_and_languages_are_absolute(self):
        head = self.head("/it/about/?utm_source=x")
        self.assertIn('<link rel="canonical" href="https://website.example/it/about/">', head)
        self.assertIn('<meta property="og:url" content="https://website.example/it/about/">', head)
        self.assertIn('<link rel="alternate" hreflang="it" href="https://website.example/it/about/">', head)
        self.assertIn('<link rel="alternate" hreflang="en" href="https://website.example/en/about/">', head)
        default = f"https://website.example/{settings.LANGUAGE_CODE}/about/"
        self.assertIn(f'<link rel="alternate" hreflang="x-default" href="{default}">', head)

    def test_the_site_and_the_language_are_named(self):
        head = self.head("/it/")
        self.assertIn('<meta property="og:type" content="website">', head)
        self.assertIn('<meta property="og:site_name" content="Example community">', head)
        self.assertIn('<meta property="og:locale" content="it_IT">', head)
        self.assertIn('<meta property="og:locale:alternate" content="en_US">', head)

    def test_another_host_does_not_end_up_in_the_links(self):
        with patch("website.views.parse", return_value=({"title": "About us", "image": "/theme/a.png"}, "text")):
            html = self.client.get("/it/about/", HTTP_HOST="other.onion").content.decode()
        self.assertNotIn("other.onion", html)
        self.assertIn('<link rel="canonical" href="https://website.example/it/about/">', html)
        self.assertNotIn("other.onion", self.client.get("/sitemap.xml", HTTP_HOST="other.onion").content.decode())

    def test_without_an_image_the_card_is_small(self):
        head = self.head("/it/")
        self.assertNotIn("og:image", head)
        self.assertIn('<meta name="twitter:card" content="summary">', head)


@override_settings(PUBLIC_URL=PUBLIC)
class StructuredData(TestCase):
    def structured(self, path):
        html = self.client.get(path).content.decode()
        return json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)[1])["@graph"]

    def test_the_home_says_who_the_community_is(self):
        graph = self.structured("/it/")
        organization, website = graph
        self.assertEqual(organization["@type"], "Organization")
        self.assertEqual(organization["name"], "Example community")
        self.assertEqual(organization["url"], "https://website.example/")
        self.assertEqual(organization["logo"], "https://website.example/theme/logo.svg")
        self.assertEqual(organization["email"], "info@example.org")
        self.assertNotIn("sameAs", organization)
        self.assertEqual(website["@type"], "WebSite")
        self.assertEqual(website["publisher"], {"@id": organization["@id"]})
        self.assertEqual(sorted(website["inLanguage"]), ["en", "it"])

    def test_the_community_profiles_are_the_same_organization(self):
        data = {**community(), "links": [{"label": "Mastodon", "url": "https://social.example.org/@community"}]}
        with patch("config.community.community", return_value=data):
            organization = self.structured("/it/")[0]
        self.assertEqual(organization["sameAs"], ["https://social.example.org/@community"])

    def test_a_community_without_logo_or_profiles_still_has_a_home(self):
        """The logo is optional, as for the Nostr profile, and `links:` left empty is None: the home does not give a 500."""
        data = {**{key: value for key, value in community().items() if key != "logo"}, "links": None}
        with patch("config.community.community", return_value=data):
            organization = self.structured("/it/")[0]
        self.assertEqual(organization["name"], "Example community")
        self.assertFalse({"logo", "sameAs"} & set(organization))

    def test_a_page_says_where_it_is(self):
        with patch("website.views.parse", return_value=({"title": "About us"}, "text")):
            graph = self.structured("/it/about/")
        self.assertEqual(graph[0]["@type"], "BreadcrumbList")
        steps = [(step["position"], step["name"], step["item"]) for step in graph[0]["itemListElement"]]
        self.assertEqual(
            steps,
            [
                (1, "Example community", "https://website.example/it/"),
                (2, "About us", "https://website.example/it/about/"),
            ],
        )

    def test_a_page_says_when_it_was_updated(self):
        with patch(
            "website.views.parse", return_value=({"title": "Rules", "updated": "2026-11-23"}, "# Rules\n\ntext")
        ):
            response = self.client.get("/it/about/")
            graph = self.structured("/it/about/")
        self.assertContains(response, '</h1><p class="page-updated muted">Aggiornata il 23 novembre 2026</p>')
        self.assertContains(response, '<meta property="article:modified_time" content="2026-11-23">')
        page = next(node for node in graph if node["@type"] == "WebPage")
        self.assertEqual(
            (page["dateModified"], page["name"], page["url"]),
            ("2026-11-23", "Rules", "https://website.example/it/about/"),
        )

    def test_the_day_goes_first_on_a_page_without_a_title(self):
        with patch("website.views.parse", return_value=({"Updated": '"2026-11-23"'}, "text")):
            response = self.client.get("/en/about/")
        self.assertContains(response, '<article class="page"><p class="page-updated muted">Updated on')
        self.assertNotContains(response, "application/ld+json")

    def test_a_page_without_a_day_says_nothing(self):
        with patch("website.views.parse", return_value=({"title": "Rules"}, "# Rules")):
            response = self.client.get("/it/about/")
            graph = self.structured("/it/about/")
        self.assertNotContains(response, "page-updated")
        self.assertNotContains(response, "article:modified_time")
        self.assertEqual([node["@type"] for node in graph], ["BreadcrumbList"])

    def test_a_day_that_is_not_one_is_left_out(self):
        for wrong in ("yesterday", "23/11/2026", "2026-13-01"):
            with (
                self.subTest(wrong),
                patch("website.views.parse", return_value=({"title": "Rules", "updated": wrong}, "# Rules")),
            ):
                response = self.client.get("/it/about/")
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "page-updated")
                self.assertNotContains(response, "article:modified_time")

    def test_a_page_without_a_title_has_no_structured_data(self):
        with patch("website.views.parse", return_value=({}, "text")):
            self.assertNotContains(self.client.get("/it/about/"), "application/ld+json")
        self.assertNotContains(self.client.get("/it/shops/map/"), "application/ld+json")

    def test_a_title_cannot_close_the_script(self):
        with patch("website.views.parse", return_value=({"title": "</script><b>x</b> & y"}, "text")):
            html = self.client.get("/it/about/").content.decode()
            graph = self.structured("/it/about/")
        self.assertNotIn("</script><b>", html)
        self.assertEqual(graph[0]["itemListElement"][1]["name"], "</script><b>x</b> & y")


@override_settings(PUBLIC_URL=PUBLIC)
class ShareImage(TestCase):
    """The preview image: the one chosen by the page, otherwise the community's, with the dimensions."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / "community"
        shutil.copytree(ROOT / "examples/community", self.root)
        data = yaml.safe_load((self.root / "community.yml").read_text())
        data["share_image"] = {"it": "/theme/preview-it.png", "en": "/theme/preview.png"}
        (self.root / "community.yml").write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
        (self.root / "theme/preview.png").write_bytes(png(1200, 630))
        (self.root / "theme/preview-it.png").write_bytes(png(1200, 600))
        patcher = patch("config.community.COMMUNITY_DIR", self.root)
        patcher.start()
        self.addCleanup(community_data.cache_clear)
        self.addCleanup(community.cache_clear)
        self.addCleanup(patcher.stop)
        community.cache_clear()
        community_data.cache_clear()

    def head(self, path):
        return self.client.get(path).content.decode().split("</head>")[0]

    def test_every_page_has_the_community_image_with_its_size(self):
        head = self.head("/en/shops/map/")
        self.assertIn('<meta property="og:image" content="https://website.example/theme/preview.png">', head)
        self.assertIn('<meta property="og:image:width" content="1200">', head)
        self.assertIn('<meta property="og:image:height" content="630">', head)
        self.assertIn('<meta name="twitter:card" content="summary_large_image">', head)

    def test_the_image_can_speak_the_language_of_the_page(self):
        head = self.head("/it/")
        self.assertIn('<meta property="og:image" content="https://website.example/theme/preview-it.png">', head)
        self.assertIn('<meta property="og:image:height" content="600">', head)

    def test_a_page_chooses_its_own_image(self):
        (self.root / "theme/history.png").write_bytes(png(800, 418))
        with patch("website.views.parse", return_value=({"image": "/theme/history.png"}, "text")):
            head = self.head("/it/")
        self.assertIn('<meta property="og:image" content="https://website.example/theme/history.png">', head)
        self.assertIn('<meta property="og:image:width" content="800">', head)
        self.assertNotIn("preview", head)

    def test_a_jpeg_tells_its_size(self):
        jpeg = (
            b"\xff\xd8\xff\xe0"
            + struct.pack(">H", 16)
            + b"JFIF\x00"
            + bytes(9)
            + b"\xff\xff\xc2"
            + struct.pack(">HBHH", 17, 8, 630, 1200)
            + bytes(12)
        )
        (self.root / "theme/photo.jpg").write_bytes(jpeg)
        self.assertEqual(image_size(theme_image("/theme/photo.jpg")), (1200, 630))

    def test_other_files_have_no_size(self):
        self.assertIsNone(image_size(theme_image("/theme/theme.css")))
        self.assertIsNone(image_size(theme_image("/theme/missing.png")))
        self.assertIsNone(theme_image("/theme/../community.yml"))
        self.assertIsNone(theme_image("https://example.org/a.png"))


class PageConnections(TestCase):
    def csp(self, connect):
        with patch("website.views.parse", return_value=({"connect": connect}, "text")):
            return self.client.get("/it/").headers["Content-Security-Policy"]

    def test_a_page_opens_only_the_servers_it_declares(self):
        self.assertIn("connect-src 'self' https://geo.example.org", self.csp("https://geo.example.org"))

    def test_other_pages_stay_closed(self):
        self.assertNotIn("connect-src", self.client.get("/it/").headers["Content-Security-Policy"])

    def test_a_declared_server_cannot_smuggle_a_directive(self):
        self.assertNotIn("connect-src", self.csp("https://a.example.org;script-src"))
        self.assertNotIn("connect-src", self.csp("http://a.example.org"))

    def test_the_rest_of_the_policy_stays(self):
        self.assertIn("script-src 'self'", self.csp("https://geo.example.org"))


class ExampleCommunity(TestCase):
    PAGES = ("/{}/", "/{}/about/", "/{}/contact/", "/{}/privacy/", "/{}/shops/map/")

    def test_every_page_answers_in_every_language(self):
        for language, _ in settings.LANGUAGES:
            for path in self.PAGES:
                self.assertEqual(self.client.get(path.format(language)).status_code, 200, path)

    def test_the_home_shows_the_first_stops_on_a_map(self):
        html = self.client.get("/it/").content.decode()
        self.assertIn('id="route-map"', html)
        self.assertEqual(html.count('class="route-stop"'), 3)

    def test_the_menu_points_only_to_pages_that_exist(self):
        html = self.client.get("/en/").content.decode()
        for link in re.findall(r'href="(/en/[^"]*)"', html):
            self.assertEqual(self.client.get(link).status_code, 200, link)

    def test_a_menu_item_with_a_path_needs_no_slug(self):
        # The menu goes through every page: an entry written like this must not give a 500 to the whole site
        data = {**community(), "menu": [{"path": "shops/map/", "labels": {"it": "Mappa", "en": "Map"}}]}
        with patch("config.community.community", return_value=data):
            self.assertEqual(menu("en"), [{"href": "/en/shops/map/", "label": "Map"}])
            self.assertEqual(self.client.get("/en/").status_code, 200)

    def test_the_till_icon_says_what_the_logo_is(self):
        shop = Shop.objects.create(
            name="Bar", lightning_address="bar@ln.example.org", discount=0, latitude=1, longitude=1
        )
        icon = self.client.get(f"/register/{shop.id}/manifest.webmanifest").json()["icons"][0]
        self.assertEqual((icon["sizes"], icon["type"]), ("any", "image/svg+xml"))


@override_settings(LANGUAGES=[*settings.LANGUAGES, ("de", "Deutsch")], PUBLIC_URL=PUBLIC)
class ThreeLanguages(TestCase):
    """A community that adds a language: the software already speaks German, the pages are what the community wrote."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name) / "community"
        shutil.copytree(ROOT / "examples/community", root)
        data = yaml.safe_load((root / "community.yml").read_text())
        data["languages"]["de"] = "Deutsch"
        (root / "community.yml").write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
        (root / "pages/de").mkdir()
        (root / "pages/de/home.md").write_text("---\ntitle: Startseite\n---\nWillkommen bei {{ community.name }}.\n")
        patcher = patch("config.community.COMMUNITY_DIR", root)
        patcher.start()
        self.addCleanup(community_data.cache_clear)
        self.addCleanup(community.cache_clear)
        self.addCleanup(patcher.stop)
        community.cache_clear()
        community_data.cache_clear()

    def test_the_german_home_speaks_german(self):
        response = self.client.get("/de/")
        self.assertContains(response, '<html lang="de">')
        self.assertContains(response, "Willkommen bei Example community.")
        self.assertContains(response, "Zum Inhalt springen")
        self.assertContains(response, "Für Händler")

    def test_the_software_pages_speak_german(self):
        self.assertContains(self.client.get("/de/shops/map/"), "Wo man mit Bitcoin bezahlen kann")
        self.assertContains(self.client.get("/de/shops/"), "Mit E-Mail anmelden")

    def test_a_historic_shop_speaks_german(self):
        shop = Shop.objects.create(name="Alt", latitude=1, longitude=1, closed_on=date(2026, 11, 30))
        with patch("shops.models.timezone.localdate", return_value=date(2026, 11, 2)):
            page = self.client.get(f"/de/shops/s/{shop.id}/")
        self.assertContains(page, "Schließt am 30. November 2026")
        with patch("shops.models.timezone.localdate", return_value=date(2026, 12, 1)):
            page = self.client.get(f"/de/shops/s/{shop.id}/")
        self.assertContains(page, "Historisch · geschlossen am 30. November 2026")
        self.assertContains(self.client.get("/de/shops/map/"), 'data-closed="Historisch · geschlossen am {date}"')

    def test_money_is_written_the_german_way(self):
        with override("de"):
            self.assertEqual(money_text(Decimal("1234.5"), "EUR"), "1234,50 EUR")

    def test_every_language_is_offered(self):
        html = self.client.get("/it/shops/map/").content.decode()
        for code, _ in settings.LANGUAGES:
            self.assertIn(
                f'<link rel="alternate" hreflang="{code}" href="https://website.example/{code}/shops/map/">', html
            )
            self.assertIn(f'<a href="/{code}/shops/map/" hreflang="{code}"', html)

    def test_a_page_missing_in_german_is_a_404_and_is_not_announced(self):
        self.assertEqual(self.client.get("/de/about/").status_code, 404)
        html = self.client.get("/it/about/").content.decode()
        self.assertIn('hreflang="en" href="https://website.example/en/about/"', html)
        self.assertNotIn("/de/about/", html)
        self.assertIn('<a href="/de/" hreflang="de"', html)

    def test_the_sitemap_has_the_german_pages_only(self):
        xml = self.client.get("/sitemap.xml").content.decode()
        self.assertIn("<loc>https://website.example/de/</loc>", xml)
        self.assertIn("<loc>https://website.example/de/shops/map/</loc>", xml)
        self.assertNotIn("/de/about/", xml)
        self.assertIn('hreflang="de" href="https://website.example/de/"', xml)

    def test_untranslated_community_texts_fall_back_to_the_default_language(self):
        self.assertContains(self.client.get("/de/"), "Where to pay in bitcoin, close to home.")


class ThemeOwnsItsColours(TestCase):
    def test_text_on_the_primary_colour_is_a_token(self):
        css = (ROOT / "static/base.css").read_text()
        self.assertNotIn("color: #fff", css)
        self.assertIn("--on-primary: #fff", css)


class Responsive(TestCase):
    def test_reduced_copies_become_a_srcset(self):
        with patch(
            "website.views.reduced_copies", return_value=[("/theme/a-480.webp", 480), ("/theme/a-800.webp", 800)]
        ):
            html = responsive('<img src="/theme/a.webp" alt="x" width="1200" height="600">')
        self.assertIn('srcset="/theme/a-480.webp 480w, /theme/a-800.webp 800w, /theme/a.webp 1200w"', html)
        self.assertIn('decoding="async"', html)

    def test_only_images_after_the_first_are_lazy(self):
        html = responsive('<img src="/theme/a.png" alt=""><img src="/theme/b.png" alt="">')
        self.assertEqual(html.count('fetchpriority="high"'), 1)
        self.assertEqual(html.count('loading="lazy"'), 1)

    def test_copies_wider_than_the_original_are_ignored(self):
        with patch(
            "website.views.reduced_copies", return_value=[("/theme/a-480.webp", 480), ("/theme/a-800.webp", 800)]
        ):
            html = responsive('<img src="/theme/a.webp" alt="x" width="600" height="600">')
        self.assertIn('srcset="/theme/a-480.webp 480w, /theme/a.webp 600w"', html)

    def test_eager_image_stays_eager(self):
        html = responsive('<img src="/theme/a.png" alt="" loading="eager">')
        self.assertNotIn('loading="lazy"', html)

    def test_other_images_are_left_alone(self):
        html = '<img src="https://example.org/a.webp" alt="">'
        self.assertEqual(responsive(html), html)


class StaticFiles(TestCase):
    def test_static_files_carry_a_fingerprint_and_a_long_cache(self):
        url = static("base.css")
        if url.endswith("/base.css"):
            self.skipTest("without collectstatic there is no manifest")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("immutable", response["Cache-Control"])
        self.assertIn(url, self.client.get(f"/{settings.LANGUAGE_CODE}/").content.decode())
        # whoever quotes the name without the fingerprint still finds it
        self.assertEqual(self.client.get("/static/base.css").status_code, 200)


class ThemeFiles(TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        theme = Path(self.folder.name) / "theme"
        theme.mkdir()
        (theme / "video.mp4").write_bytes(bytes(range(100)))
        (theme / "style.css").write_text("a{}")
        patcher = patch("website.views.COMMUNITY_DIR", Path(self.folder.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_image_format_is_cached_long(self):
        """An image a page uses beside its WebP copy (AVIF in a <picture>) must not be asked again at every page."""
        theme = Path(self.folder.name) / "theme"
        for name, kind in (
            ("a.avif", "image/avif"),
            ("a.jpeg", "image/jpeg"),
            ("a.gif", "image/gif"),
            ("a.webp", "image/webp"),
        ):
            (theme / name).write_bytes(b"x")
            response = self.client.get(f"/theme/{name}")
            self.assertEqual(response["Cache-Control"], "public, max-age=2592000", name)
            self.assertEqual(response["Content-Type"], kind, name)
        self.assertEqual(self.client.get("/theme/style.css")["Cache-Control"], "no-cache")

    def test_heavy_files_are_cached_long(self):
        response = self.client.get("/theme/video.mp4")
        self.assertEqual(response.status_code, 200)
        self.assertIn("max-age=2592000", response["Cache-Control"])
        self.assertEqual(response["Accept-Ranges"], "bytes")

    def test_fonts_arrive_as_fonts(self):
        (Path(self.folder.name) / "theme" / "typeface.woff2").write_bytes(b"wOF2")
        response = self.client.get("/theme/typeface.woff2")
        self.assertEqual(response["Content-Type"], "font/woff2")
        self.assertIn("max-age=2592000", response["Cache-Control"])

    def test_stylesheets_are_checked_every_time(self):
        self.assertEqual(self.client.get("/theme/style.css")["Cache-Control"], "no-cache")

    def test_an_unchanged_stylesheet_answers_304(self):
        modified = self.client.get("/theme/style.css")["Last-Modified"]
        response = self.client.get("/theme/style.css", headers={"If-Modified-Since": modified})
        self.assertEqual(response.status_code, 304)
        self.assertEqual(response.content, b"")
        self.assertEqual(response["Cache-Control"], "no-cache")

    def test_a_published_stylesheet_arrives_at_once(self):
        modified = self.client.get("/theme/style.css")["Last-Modified"]
        style = Path(self.folder.name) / "theme" / "style.css"
        style.write_text("b{}")
        later = style.stat().st_mtime + 60
        os.utime(style, (later, later))
        response = self.client.get("/theme/style.css", headers={"If-Modified-Since": modified})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"b{}")

    def test_range_gets_part_of_the_file(self):
        response = self.client.get("/theme/video.mp4", headers={"Range": "bytes=10-19"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response["Content-Range"], "bytes 10-19/100")
        self.assertEqual(response.content, bytes(range(10, 20)))

    def test_open_range_reaches_the_end(self):
        response = self.client.get("/theme/video.mp4", headers={"Range": "bytes=90-"})
        self.assertEqual(response["Content-Range"], "bytes 90-99/100")

    def test_range_past_the_end_is_refused(self):
        response = self.client.get("/theme/video.mp4", headers={"Range": "bytes=500-"})
        self.assertEqual(response.status_code, 416)

    def test_a_range_reads_the_file_of_the_theme_however_the_path_is_written(self):
        """A second slash made the path one from the root of the disk for the Range, after `serve` had found the theme's
        file: an error for anybody who asked, and a file outside the theme where both happened to exist."""
        for path in ("/theme//video.mp4", "/theme/./video.mp4", "/theme/x/../video.mp4"):
            response = self.client.get(path, headers={"Range": "bytes=10-19"})
            self.assertEqual((response.status_code, response.content), (206, bytes(range(10, 20))), path)
        self.assertEqual(self.client.get("/theme//etc/hostname", headers={"Range": "bytes=0-9"}).status_code, 404)

    def test_theme_service_worker_may_control_pages(self):
        (Path(self.folder.name) / "theme" / "sw.js").write_text("")
        (Path(self.folder.name) / "theme" / "sw-website.js").write_text("")
        self.assertEqual(self.client.get("/theme/sw.js")["Service-Worker-Allowed"], "/")
        self.assertEqual(self.client.get("/theme/sw-website.js")["Service-Worker-Allowed"], "/")
        self.assertNotIn("Service-Worker-Allowed", self.client.get("/theme/style.css"))

    def test_path_outside_the_theme_is_refused(self):
        self.assertIn(self.client.get("/theme/../secret").status_code, (400, 404))


# The README's origin block, the one place where a community is named
# (the marker is written in two pieces, or this file would carry one too)
MARK = "<!-- " + "origin -->"
ORIGIN = re.compile(re.escape(MARK) + ".*?<!-- /origin -->", re.S)


class Neutral(TestCase):
    def test_no_community_in_the_software(self):
        names = os.environ.get("FORBIDDEN_NAMES")
        if not names:
            self.skipTest("FORBIDDEN_NAMES not set")
        names = re.compile(names, re.I)
        for path in ROOT.rglob("*"):
            if path.is_file() and not {".git", "staticroot", "__pycache__"} & set(path.parts):
                text = path.read_text(errors="ignore")
                if path == ROOT / "README.md":
                    text = ORIGIN.sub("", text)
                self.assertIsNone(names.search(text), path)

    def test_the_origin_is_one_short_block_of_the_readme(self):
        # The README may say where bitmentor comes from, in one marked block, and nowhere else. The image has no README
        # (.dockerignore): there deploy/tests/test_documents.py checks it
        if not (ROOT / "README.md").exists():
            self.skipTest("no README.md in this tree")
        blocks = ORIGIN.findall((ROOT / "README.md").read_text())
        self.assertEqual(len(blocks), 1)
        self.assertLess(blocks[0].count("\n"), 6)
        for path in ROOT.rglob("*"):
            if (
                path.is_file()
                and path != ROOT / "README.md"
                and not {".git", "staticroot", "__pycache__"} & set(path.parts)
            ):
                self.assertNotIn(MARK, path.read_text(errors="ignore"), path)


class ShopTextInPages(TestCase):
    PAGE = "{% for shop in featured %}{{ shop.name }} {{ shop.city }}{% endfor %}"

    def render(self, **fields):
        Shop.objects.create(discount=5, latitude=1, longitude=1, **fields)
        with patch("website.views.parse", return_value=({}, self.PAGE)):
            return self.client.get("/it/").content.decode()

    def test_a_shop_name_cannot_become_a_link(self):
        html = self.render(name="[pay here](javascript:alert(1))")
        self.assertNotIn('href="javascript', html)

    def test_a_shop_name_cannot_become_an_attribute(self):
        html = self.render(name="![x](y){onerror=alert(1)}", city="*a*{onmouseover=alert(2)}")
        self.assertNotIn("<img", html.split('<article class="page">')[1].split("</article>")[0])
        self.assertNotIn(" onerror", html)
        self.assertNotIn(" onmouseover", html)

    def test_a_shop_name_is_still_readable(self):
        self.assertIn("Caff", self.render(name="Caff\u00e8 & Co"))


class SiteModes(TestCase):
    def test_only_production_can_be_indexed(self):
        self.assertContains(self.client.get("/robots.txt"), "Disallow: /\n")
        with override_settings(SITE_MODE="production", PUBLIC_URL=PUBLIC):
            text = self.client.get("/robots.txt", HTTP_HOST="other.onion").content.decode()
        self.assertNotIn("Disallow: /\n", text)
        self.assertIn("Sitemap: https://website.example/sitemap.xml", text)

    def test_production_closes_the_private_shop_pages_in_every_language(self):
        with override_settings(SITE_MODE="production", PUBLIC_URL=PUBLIC):
            text = self.client.get("/robots.txt").content.decode()
        rules = [line.removeprefix("Disallow: ") for line in text.splitlines() if line.startswith("Disallow: ")]
        shop = uuid.uuid4()
        for code, _name in settings.LANGUAGES:
            with override(code):
                closed = [
                    reverse(name, args=[shop])
                    for name in ("edit_shop", "delete_shop", "staff_registers", "till_card", "first_day")
                ]
                # the panel and the login are the same page, even when the login remembers where to go back to
                closed += [
                    reverse("shops"),
                    f"{reverse('shops')}?next=/",
                    reverse("new_shop"),
                    reverse("recovery"),
                    reverse("recovery_login"),
                    reverse("confirm_email"),
                ]
                public = [
                    reverse("home"),
                    reverse("page", args=["about"]),
                    reverse("shop_map"),
                    reverse("shop_detail", args=[shop]),
                ]
                # the till and the sign open, so the engine reads their noindex (NotIndexed in shops/tests.py)
                public += [reverse("register", args=[shop]), reverse("shop_sign", args=[shop])]
            for path in closed:
                self.assertTrue(blocked(rules, path), path)
            for path in public:
                self.assertFalse(blocked(rules, path), path)

    def test_only_production_pages_can_be_indexed(self):
        for mode in ("development", "maintenance"):
            with override_settings(SITE_MODE=mode):
                self.assertContains(self.client.get("/it/"), '<meta name="robots" content="noindex, nofollow">')
        with override_settings(SITE_MODE="production"):
            self.assertNotContains(self.client.get("/it/"), 'name="robots"')

    def test_a_missing_page_is_not_indexed_and_names_no_canonical(self):
        with override_settings(SITE_MODE="production"):
            response = self.client.get("/it/nonexistent/")
        self.assertContains(response, '<meta name="robots" content="noindex">', status_code=404)
        self.assertNotContains(response, 'rel="canonical"', status_code=404)

    def test_every_mode_has_its_caddy_rules(self):
        for mode in settings.SITE_MODES:
            self.assertTrue((Path(settings.BASE_DIR) / "caddy" / f"{mode}.caddy").is_file(), mode)


class Headers(TestCase):
    def test_csp_on_every_page(self):
        policy = self.client.get("/it/").headers["Content-Security-Policy"]
        self.assertIn("script-src 'self'", policy)
        self.assertNotIn("'unsafe-inline' 'unsafe-eval'", policy)
        self.assertIn("frame-ancestors 'none'", policy)
        self.assertIn("Content-Security-Policy", self.client.get("/admin/login/").headers)

    def test_the_map_tiles_and_the_qr_are_allowed(self):
        policy = self.client.get("/it/").headers["Content-Security-Policy"]
        self.assertIn("https://tile.openstreetmap.org", policy)
        self.assertIn("data:", policy)

    def test_no_template_needs_inline_scripts(self):
        # application/ld+json is data for search engines: the browser does not run it and the CSP does not look at it
        for path in (ROOT / "templates").rglob("*.html"):
            text = path.read_text()
            self.assertIsNone(re.search(r'<script(?![^>]*\b(src=|type="application/ld\+json"))', text), path)
            self.assertIsNone(re.search(r"\son(click|load|error|submit|change)=", text), path)


class ThemeRange(TestCase):
    def fetch(self, value):
        with tempfile.TemporaryDirectory() as folder:
            theme = Path(folder) / "theme"
            theme.mkdir()
            (theme / "a.txt").write_bytes(b"0123456789")
            with patch("website.views.COMMUNITY_DIR", Path(folder)):
                return self.client.get("/theme/a.txt", headers={"Range": value})

    def test_a_range_gives_those_bytes(self):
        self.assertEqual(self.fetch("bytes=2-4").content, b"234")

    def test_a_reversed_range_is_refused(self):
        self.assertEqual(self.fetch("bytes=5-2").status_code, 416)

    def test_an_absurd_range_is_ignored(self):
        self.assertEqual(self.fetch("bytes=" + "9" * 5000 + "-").status_code, 200)


class OnionLocation(TestCase):
    """ONION_LOCATION goes to Caddy as it is: manage.py check says when it is not an onion's address (website.W007)."""

    NAME = "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuvwx.onion"

    def found(self, value):
        from website.apps import wrong_onion_location

        with override_settings(ONION_LOCATION=value):
            return [problem.id for problem in wrong_onion_location(None)]

    def test_unset_or_an_onion_address_is_fine(self):
        for value in ("", f"http://{self.NAME}", f"https://{self.NAME}"):
            with self.subTest(value):
                self.assertEqual(self.found(value), [])

    def test_anything_else_is_named(self):
        for value in (
            f"http://{self.NAME}/",
            self.NAME,
            f"ftp://{self.NAME}",
            "https://example.org",
            "http://short.onion",
            f"http://{self.NAME}\nX-Other: 1",
        ):
            with self.subTest(value):
                self.assertEqual(self.found(value), ["website.W007"])


@override_settings(PUBLIC_URL=PUBLIC)
@override_settings(
    BLOSSOM_HOSTS=("blossom.example.org", "files.example.net"),
    ALLOWED_HOSTS=["blossom.example.org", "files.example.net", "testserver"],
)
class Blossom(TestCase):
    """Read-only Blossom (BUD-01): the theme files by their SHA-256, on the hosts of BLOSSOM_HOSTS, only when
    `blossom: true`."""

    def setUp(self):
        from website import blossom

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / "community"
        shutil.copytree(ROOT / "examples/community", self.root)
        self.photo = b"\x89PNG not really a picture"
        (self.root / "theme/photo.png").write_bytes(self.photo)
        (self.root / "theme/.hidden.png").write_bytes(b"hidden")
        (self.root / "theme/code.js").write_bytes(b"alert(1)")
        (self.root / "theme/data.json").write_bytes(b'{"private": 1}')
        self.digest = hashlib.sha256(self.photo).hexdigest()
        patcher = patch("config.community.COMMUNITY_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(community.cache_clear)
        self.addCleanup(blossom.reset)
        from website import blossom_visits

        blossom_visits.reset()
        self.addCleanup(blossom_visits.reset)
        self.site = self.client
        self.client = Client(HTTP_HOST="blossom.example.org")
        self.turn(True)

    def turn(self, on):
        from website import blossom

        data = yaml.safe_load((self.root / "community.yml").read_text())
        data["blossom"] = on
        (self.root / "community.yml").write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
        community.cache_clear()
        blossom.reset()

    def test_the_right_hash_gives_the_bytes_with_cors_and_a_long_cache(self):
        for address in (f"/{self.digest}", f"/{self.digest}.png", f"/{self.digest.upper()}"):
            response = self.client.get(address)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b"".join(response.streaming_content), self.photo)
            self.assertEqual(response["Content-Type"], "image/png")
            self.assertEqual(response["Content-Length"], str(len(self.photo)))
            self.assertEqual(response["Access-Control-Allow-Origin"], "*")
            self.assertIn("immutable", response["Cache-Control"])
            self.assertIn("max-age=31536000", response["Cache-Control"])

    def test_head_has_the_headers_and_no_body(self):
        response = self.client.head(f"/{self.digest}.png")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Length"], str(len(self.photo)))
        self.assertEqual(response["Access-Control-Allow-Origin"], "*")
        self.assertEqual(response.content, b"")

    def test_a_range_gives_part_of_the_file(self):
        response = self.client.get(f"/{self.digest}", HTTP_RANGE="bytes=1-3")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(b"".join(response.streaming_content), self.photo[1:4])
        self.assertEqual(response["Content-Range"], f"bytes 1-3/{len(self.photo)}")

    def test_the_preflight_is_answered(self):
        response = self.client.options(f"/{self.digest}")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["Access-Control-Allow-Origin"], "*")
        self.assertIn("GET", response["Access-Control-Allow-Methods"])

    def test_an_unknown_hash_is_404(self):
        self.assertEqual(self.client.get("/" + "0" * 64).status_code, 404)
        self.assertEqual(self.client.get("/not-a-hash").status_code, 400)

    def test_what_is_not_public_is_not_served(self):
        for name, content in ((".hidden.png", b"hidden"), ("code.js", b"alert(1)"), ("data.json", b'{"private": 1}')):
            self.assertEqual(self.client.get("/" + hashlib.sha256(content).hexdigest()).status_code, 404, name)
        listed = self.client.get("/list").json()["files"]
        self.assertFalse([item for item in listed if item["url"].endswith((".js", ".json", ".hidden.png"))])

    def test_a_changed_file_is_found_by_its_new_hash(self):
        from website import blossom

        (self.root / "theme/photo.png").write_bytes(b"new bytes")
        blossom.reset()
        self.assertEqual(self.client.get(f"/{self.digest}").status_code, 404)
        self.assertEqual(self.client.get("/" + hashlib.sha256(b"new bytes").hexdigest()).status_code, 200)

    def test_off_everything_is_404(self):
        self.turn(False)
        self.assertEqual(self.client.get(f"/{self.digest}").status_code, 404)
        self.assertEqual(self.client.head(f"/{self.digest}").status_code, 404)
        self.assertEqual(self.client.get("/list").status_code, 404)

    def test_the_list(self):
        response = self.client.get("/list")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Access-Control-Allow-Origin"], "*")
        data = response.json()
        self.assertEqual(data["server"], "https://blossom.example.org")
        item = next(item for item in data["files"] if item["sha256"] == self.digest)
        self.assertEqual(
            item,
            {
                "sha256": self.digest,
                "size": len(self.photo),
                "type": "image/png",
                "url": f"{PUBLIC}/theme/photo.png",
                "blossom": f"https://blossom.example.org/{self.digest}.png",
            },
        )

    def test_every_error_has_cors_and_its_reason(self):
        cases = [
            ("get", "/" + "0" * 64, 404, "blob not found"),
            ("get", "/not-a-hash", 400, "not a sha256, with an optional extension"),
            ("get", f"/{self.digest}.p-g", 400, "not a sha256, with an optional extension"),
            ("put", "/upload", 403, "read-only server"),
            ("get", "/upload", 403, "read-only server"),
            ("put", "/mirror", 403, "read-only server"),
            ("head", "/media", 403, "read-only server"),
            ("delete", f"/{self.digest}", 403, "read-only server"),
            ("post", "/list", 403, "read-only server"),
        ]
        for method, address, status, reason in cases:
            response = getattr(self.client, method)(address)
            self.assertEqual((response.status_code, response["X-Reason"]), (status, reason), (method, address))
            self.assertEqual(response["Access-Control-Allow-Origin"], "*", address)
        self.turn(False)
        response = self.client.get(f"/{self.digest}")
        self.assertEqual((response.status_code, response["X-Reason"]), (404, "blossom is off"))
        self.assertEqual(response["Access-Control-Allow-Origin"], "*")

    def test_the_preflight_lets_a_client_ask_to_upload_and_read_the_refusal(self):
        response = self.client.options("/upload")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["Access-Control-Allow-Headers"], "Authorization, *")
        self.assertIn("PUT", response["Access-Control-Allow-Methods"])

    def test_the_hosts_serve_at_the_root_and_nothing_else_and_the_site_has_no_blossom(self):
        for name in ("blossom.example.org", "files.example.net"):
            response = self.client.get(f"/{self.digest}.png", HTTP_HOST=name)
            self.assertEqual(response.status_code, 200, name)
            self.assertEqual(b"".join(response.streaming_content), self.photo)
            # Every name gives the first as the server
            self.assertEqual(self.client.get("/list", HTTP_HOST=name).json()["server"], "https://blossom.example.org")
        for address, status in (("/", 404), ("/en/", 400), ("/admin/", 400), ("/upload", 403)):
            for name in (
                "blossom.example.org",
                "blossom.example.org.",
                "BLOSSOM.example.org:8443",
                "files.example.net",
            ):
                response = self.client.get(address, HTTP_HOST=name)
                self.assertEqual(response.status_code, status, (name, address))
                self.assertNotIn("Set-Cookie", response, address)
        self.assertIn("Content-Security-Policy", self.client.get("/"))
        # The site's own host has no Blossom: not at its root, not under a path
        for address in (f"/{self.digest}", f"/blossom/{self.digest}", "/blossom/list", "/list"):
            response = self.site.get(address)
            self.assertNotEqual(response.status_code, 200, address)
            self.assertNotIn("X-Reason", response, address)

    def test_the_users_are_counted_without_their_address(self):
        from website import blossom_visits
        from website.models import BlossomVisit

        phone = {"REMOTE_ADDR": "93.40.1.7", "HTTP_USER_AGENT": "Amethyst/1"}
        self.client.get(f"/{self.digest}", **phone)
        self.client.get(f"/{self.digest}", **{**phone, "REMOTE_ADDR": "93.40.1.99"})  # same /24, same app
        for agent in ("a", "b", "c"):  # another User-Agent at every request is still one user
            self.client.get(f"/{self.digest}", REMOTE_ADDR="93.40.1.8", HTTP_USER_AGENT=agent)
        self.client.get("/list", REMOTE_ADDR="81.2.3.4", HTTP_USER_AGENT="Primal")  # only the list
        self.client.get(f"/{self.digest}", REMOTE_ADDR="10.0.0.5", HTTP_USER_AGENT="X")  # private
        self.client.get(f"/{self.digest}", REMOTE_ADDR="66.249.1.1", HTTP_USER_AGENT="Googlebot/2.1")
        with override_settings(DEV_ALLOWED_IPS="5.6.7.0/24"):
            self.client.get(f"/{self.digest}", REMOTE_ADDR="5.6.7.8", HTTP_USER_AGENT="Y")
        self.client.get("/" + "0" * 64, REMOTE_ADDR="81.2.3.4", HTTP_USER_AGENT="Z")  # not a file served
        self.assertEqual(blossom_visits.users(), 1)
        rows = list(BlossomVisit.objects.values_list("who", flat=True))
        self.assertEqual(len(rows), 2)
        self.assertFalse([row for row in rows if "93.40" in row or "Amethyst" in row])

    def test_the_moderators_are_told_once_at_three_users(self):
        from website.models import BlossomMilestone

        told = []
        with (
            patch("shops.notifications.deliver_to_moderators", side_effect=lambda *a: told.append(a) or ("x", [])),
            patch("threading.Thread.start", lambda thread: thread.run()),
        ):
            for number in range(1, 6):
                with self.captureOnCommitCallbacks(execute=True):
                    self.client.get(f"/{self.digest}", REMOTE_ADDR=f"93.40.{number}.1", HTTP_USER_AGENT="A")
                self.assertEqual(len(told), 0 if number < 3 else 1, number)
        self.assertIn("3 different users", told[0][1])
        self.assertEqual(list(BlossomMilestone.objects.values_list("users", flat=True)), [3])

    def test_a_file_gone_since_the_index_is_a_404_not_an_error(self):
        self.client.get("/list")
        (self.root / "theme/photo.png").unlink()
        response = self.client.get(f"/{self.digest}")
        self.assertEqual((response.status_code, response["X-Reason"]), (404, "blob not found"))

    def test_the_count_forgets_after_90_days(self):
        import datetime

        from website import blossom_visits
        from website.models import BlossomVisit

        old = timezone.localdate() - datetime.timedelta(days=91)
        BlossomVisit.objects.create(day=old, month=old.strftime("%Y-%m"), who="a" * 64, kind="blob")
        self.client.get(f"/{self.digest}", REMOTE_ADDR="93.40.1.7", HTTP_USER_AGENT="A")
        self.assertFalse(BlossomVisit.objects.filter(day=old).exists())
        self.assertEqual(blossom_visits.users(), 1)

    def retire(self, **keys):
        """An old file in the folder of the retired ones, and the community's settings for them."""
        from website import blossom

        self.old = b"the photo as it was"
        self.old_digest = hashlib.sha256(self.old).hexdigest()
        (self.root / "blossom-retired").mkdir(exist_ok=True)
        (self.root / "blossom-retired/photo.png").write_bytes(self.old)
        data = yaml.safe_load((self.root / "community.yml").read_text())
        data.update(keys)
        (self.root / "community.yml").write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
        community.cache_clear()
        blossom.reset()

    def test_a_retired_file_is_not_served_unless_the_community_asks(self):
        self.retire()
        response = self.client.get(f"/{self.old_digest}")
        self.assertEqual((response.status_code, response["X-Reason"]), (404, "blob not found"))
        from website.models import BlossomRetired

        self.assertFalse(BlossomRetired.objects.exists())

    def test_a_retired_file_is_served_for_its_days_with_sunset_then_gone(self):
        import datetime

        from django.utils.http import parse_http_date

        from website import blossom
        from website.models import BlossomRetired

        self.retire(blossom_retire_days=30)
        response = self.client.get(f"/{self.old_digest}.png")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), self.old)
        self.assertEqual(response["Access-Control-Allow-Origin"], "*")
        self.assertEqual(response["Cache-Control"], "public, max-age=86400")
        row = BlossomRetired.objects.get()
        self.assertEqual((row.sha256, row.path), (self.old_digest, "photo.png"))
        self.assertEqual(parse_http_date(response["Sunset"]), int((row.seen + datetime.timedelta(days=30)).timestamp()))
        self.assertEqual(self.client.head(f"/{self.old_digest}")["Content-Length"], str(len(self.old)))
        self.assertNotIn(self.old_digest, {item["sha256"] for item in self.client.get("/list").json()["files"]})
        # The file of the theme is served as ever, with no Sunset
        self.assertNotIn("Sunset", self.client.get(f"/{self.digest}"))
        # Past its days it is gone, though still in the folder; the answer names the file that has its path now
        BlossomRetired.objects.update(seen=row.seen - datetime.timedelta(days=31))
        blossom.reset()
        with self.assertNumQueries(1):  # the rows are read with the folder, not for each request
            response = self.client.get(f"/{self.old_digest}")
            self.assertEqual(self.client.get("/" + "1" * 64).status_code, 404)
        self.assertEqual((response.status_code, response["X-Reason"]), (410, f"retired; the file is now {self.digest}"))
        self.assertEqual(response["Access-Control-Allow-Origin"], "*")
        self.assertEqual(response["Cache-Control"], "public, max-age=3600")
        # Within its days but taken out of the folder: gone too, and the row remembers
        BlossomRetired.objects.update(seen=row.seen)
        (self.root / "blossom-retired/photo.png").unlink()
        (self.root / "theme/photo.png").unlink()
        blossom.reset()
        response = self.client.get(f"/{self.old_digest}")
        self.assertEqual((response.status_code, response["X-Reason"]), (410, "retired"))
        # Back in the theme it is a file like the others; retired again later, it has its days again
        (self.root / "theme/photo.png").write_bytes(self.old)
        blossom.reset()
        response = self.client.get(f"/{self.old_digest}")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Sunset", response)
        self.client.get("/" + "1" * 64)  # any hash the theme has not makes the site look at the folder
        self.assertFalse(BlossomRetired.objects.exists())
        (self.root / "theme/photo.png").unlink()
        self.retire(blossom_retire_days=30)
        self.assertIn("Sunset", self.client.get(f"/{self.old_digest}"))

    def test_a_file_that_changed_since_the_index_is_a_404_nobody_keeps(self):
        (self.root / "theme/photo.png").write_bytes(self.photo + b" and more")
        response = self.client.get(f"/{self.digest}")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("Cache-Control", response)

    def test_the_days_of_a_retired_file_start_when_it_is_in_the_folder_not_when_asked(self):
        from website.models import BlossomRetired

        self.retire(blossom_retire_days=30)
        self.client.get("/" + "1" * 64)
        self.assertEqual(list(BlossomRetired.objects.values_list("sha256", "size")), [(self.old_digest, len(self.old))])
        for value in (True, 0, -3, "30", None):
            self.retire(blossom_retire_days=value)
            self.assertEqual(self.client.get(f"/{self.old_digest}").status_code, 404, value)

    def test_past_its_days_a_retired_file_is_found_at_a_mirror_that_has_it(self):
        import datetime

        import requests

        from website import blossom, blossom_retire
        from website.models import BlossomRetired

        self.retire(
            blossom_retire_days=30,
            blossom_mirrors=[
                "https://files.example.net/",
                "http://plain.example.net",
                "https://a.example.net/path",
                "https://u@b.example.net",
                7,
                "https://localhost",
                "https://10.0.0.5",
                "https://[::1]:8443",
                "https://169.254.169.254",
                "https://db.internal",
            ],
        )
        self.assertEqual(blossom_retire.mirrors(), ["https://files.example.net"])
        asked = []

        answers = []

        def head(address, **options):
            asked.append((address, options["allow_redirects"]))
            answer = answers.pop(0)
            if answer is None:
                raise requests.ConnectionError("down")
            return type("Response", (), {"status_code": answer[0], "headers": {"Content-Length": str(answer[1])}})()

        def ask(*given):
            answers[:] = given
            BlossomRetired.objects.update(checked=None)
            with patch("website.blossom_retire.requests.head", head):
                blossom_retire.check_mirrors()
                blossom_retire.check_mirrors()  # each file once a day
            blossom.reset()
            return BlossomRetired.objects.get().mirror

        with patch("website.blossom_retire.check_soon"):
            self.client.get(f"/{self.old_digest}")
            # A page that answers 200 to anything is not the file; then the mirror has it; then it is down: still known
            self.assertEqual(ask((200, 5)), "")
            self.assertEqual(ask((200, len(self.old))), "https://files.example.net")
            self.assertEqual(ask(None), "https://files.example.net")
            self.assertEqual(asked, [(f"https://files.example.net/{self.old_digest}", False)] * 3)
            self.assertEqual(self.client.get(f"/{self.old_digest}").status_code, 200)  # within its days: from here
            BlossomRetired.objects.update(seen=timezone.now() - datetime.timedelta(days=31))
            blossom.reset()
            response = self.client.get(f"/{self.old_digest}")
            self.assertEqual(response.status_code, 308)
            self.assertEqual(response["Location"], f"https://files.example.net/{self.old_digest}.png")
            self.assertEqual(response["Access-Control-Allow-Origin"], "*")
            self.assertEqual(response["Cache-Control"], "public, max-age=3600")
            # The mirror says it has it no more; and one the community no longer lists is not sent to
            self.assertEqual(ask((404, 0)), "")
            self.assertEqual(self.client.get(f"/{self.old_digest}").status_code, 410)
            self.assertEqual(ask((200, len(self.old))), "https://files.example.net")
            self.retire(blossom_retire_days=30, blossom_mirrors=["https://other.example.net"])
            self.assertEqual(self.client.get(f"/{self.old_digest}").status_code, 410)

    def test_the_mirrors_are_asked_in_the_background_and_not_at_every_request(self):
        from website import blossom_retire

        self.retire(blossom_retire_days=30, blossom_mirrors=["https://files.example.net"])
        started = []
        with patch(
            "website.blossom_retire.threading.Thread",
            lambda **options: type("T", (), {"start": lambda self: started.append(options["target"])})(),
        ):
            for _ in range(3):
                self.client.get(f"/{self.old_digest}")
        self.assertEqual(len(started), 1)
        with patch("website.blossom_retire.check_mirrors") as check, patch("django.db.connection.close"):
            started[0]()
        check.assert_called_once()
        self.assertFalse(blossom_retire._state["checking"])

    def test_the_tool_puts_aside_the_old_files_from_an_older_copy(self):
        import importlib.util

        from website import blossom

        spec = importlib.util.spec_from_file_location("blossom_retire_tool", ROOT / "deploy/blossom-retire.py")
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        self.assertEqual(tool.EXTENSIONS, blossom.EXTENSIONS)
        old = self.root.parent / "before"
        (old / "theme/img").mkdir(parents=True)
        (old / "theme/photo.png").write_bytes(b"first photo")
        (old / "theme/img/same.png").write_bytes(self.photo)  # these bytes are still in the theme, elsewhere
        (old / "theme/code.js").write_bytes(b"old code")
        (old / "theme/.secret.png").write_bytes(b"hidden")
        tool.main([str(self.root), "--from", str(old)])
        tool.main([str(self.root), "--from", str(old)])  # again changes nothing
        retired = self.root / "blossom-retired"
        self.assertEqual(
            sorted(path.relative_to(retired).as_posix() for path in retired.rglob("*") if path.is_file()), ["photo.png"]
        )
        # A second old version of the same path keeps both, and the site serves them and knows their path
        (old / "theme/photo.png").write_bytes(b"second photo")
        tool.main([str(self.root), "--from", str(old)])
        second = hashlib.sha256(b"second photo").hexdigest()
        self.assertEqual((retired / f"photo.{second[:8]}.png").read_bytes(), b"second photo")
        self.retire(blossom_retire_days=1)
        (retired / "photo.png").write_bytes(b"first photo")
        blossom.reset()
        for content in (b"first photo", b"second photo"):
            self.assertEqual(self.client.get("/" + hashlib.sha256(content).hexdigest()).status_code, 200)
        from website.models import BlossomRetired

        BlossomRetired.objects.update(seen=timezone.now() - timezone.timedelta(days=2))
        blossom.reset()
        self.assertEqual(self.client.get(f"/{second}")["X-Reason"], f"retired; the file is now {self.digest}")

    @unittest.skipUnless(shutil.which("git"), "no git on this machine")
    def test_the_tool_puts_aside_the_old_files_from_git(self):
        import importlib.util
        import subprocess

        spec = importlib.util.spec_from_file_location("blossom_retire_tool", ROOT / "deploy/blossom-retire.py")
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        # What the theme had at a commit
        work = self.root.parent / "checkout"
        (work / "theme").mkdir(parents=True)
        (work / "theme/logo.svg").write_bytes(b"<svg>old</svg>")

        def git(*arguments):
            subprocess.run(
                ["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@example.org", *arguments],
                check=True,
                capture_output=True,
            )

        git("init", "-q")
        git("add", ".")
        git("commit", "-q", "-m", "one")
        (work / "theme/logo.svg").write_bytes(b"<svg>new</svg>")
        (work / "theme/kept.png").write_bytes(b"kept")
        git("add", ".")
        git("commit", "-q", "-m", "two")
        tool.main([str(work), "--git", "HEAD~1", "--prune", "30"])
        self.assertEqual((work / "blossom-retired/logo.svg").read_bytes(), b"<svg>old</svg>")
        self.assertEqual(sorted(path.name for path in (work / "blossom-retired").iterdir()), ["logo.svg"])
        with self.assertRaises(SystemExit):
            tool.main([str(work), "--git=--output=/tmp/x"])
        # --prune removes what was put aside long ago
        os.utime(work / "blossom-retired/logo.svg", (0, 0))
        tool.main([str(work), "--prune", "30"])
        self.assertFalse((work / "blossom-retired/logo.svg").exists())


class SourceLink(TestCase):
    """AGPLv3, section 13: every page offers its users the source of the version the site runs."""

    def test_every_page_links_the_source(self):
        response = self.client.get("/en/")
        self.assertContains(response, 'href="https://github.com/mammadori/bitmentor"')
        self.assertContains(response, "Source code (AGPLv3)")

    def test_the_link_follows_the_setting(self):
        from django.test import override_settings

        with override_settings(SOURCE_URL="https://git.example.org/our/bitmentor"):
            self.assertContains(self.client.get("/en/"), 'href="https://git.example.org/our/bitmentor"')

    def test_the_pages_without_the_sites_footer_link_it_too(self):
        # The registers, the printable sheets, the admin and the «Page expired» page are not built on base.html: the
        # same link is in the register's menu, among the tools of a sheet (never on the printed sheet) and in the
        # admin's footer
        from django.contrib.auth import get_user_model
        from django.test import Client

        from config import features
        from shops.models import StaffRegister
        from shops.test_guard import sign_in_as_staff_session
        from shops.tests import reset_limits

        reset_limits()
        owner = get_user_model().objects.create(username="owner")
        shop = Shop.objects.create(owner=owner, name="Bar", lightning_address="till@ln.example.org")
        staff = StaffRegister.objects.create(shop=shop, name="Anna")
        self.client.force_login(owner)
        admin = self.client_class()
        sign_in_as_staff_session(
            admin, get_user_model().objects.create_user("root", password="x", is_staff=True, is_superuser=True)
        )
        pages = [
            (self.client, f"/en/shops/register/{shop.id}/"),
            (self.client, f"/en/shops/sign/{shop.id}/"),
            (self.client, f"/en/shops/card/{shop.id}/"),
            (self.client, f"/en/shops/customer-card/{shop.id}/"),
            (self.client_class(), f"/en/shops/register/p/{staff.id}/?k={staff.key}"),
            (admin, reverse("admin:index")),
            (admin, reverse("admin:shops_shop_changelist")),
            (admin, reverse("admin_dashboard")),
            (self.client_class(), reverse("admin:login")),
        ]
        if features.enabled("practice"):
            pages += [
                (self.client, f"/en/shops/practice/{shop.id}/"),
                (self.client_class(), f"/en/shops/register/p/{staff.id}/practice/?k={staff.key}"),
            ]
        with override_settings(SOURCE_URL="https://git.example.org/our/bitmentor"):
            for client, path in pages:
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertNotContains(response, '<footer class="bottom">')
                    self.assertContains(
                        response,
                        '<a href="https://git.example.org/our/bitmentor" rel="noopener">Source code (AGPLv3)</a>',
                        count=1,
                    )
            with self.assertLogs("django.security.csrf", "WARNING"):
                expired = Client(enforce_csrf_checks=True).post("/en/shops/sign-out/")
            self.assertContains(expired, "Page expired", status_code=403)
            self.assertContains(expired, 'href="https://git.example.org/our/bitmentor"', status_code=403)

    def test_the_link_is_never_on_the_keypad_nor_on_a_printed_sheet(self):
        from django.contrib.auth import get_user_model

        owner = get_user_model().objects.create(username="owner")
        shop = Shop.objects.create(owner=owner, name="Bar", lightning_address="till@ln.example.org")
        self.client.force_login(owner)
        register = self.client.get(f"/en/shops/register/{shop.id}/").content.decode()
        keypad = register[register.index('<section id="keypad"') : register.index('<dialog id="menu"')]
        self.assertNotIn(settings.SOURCE_URL, keypad)
        menu_sheet = register[register.index('<dialog id="menu"') : register.index("</dialog>")]
        self.assertIn(settings.SOURCE_URL, menu_sheet)
        # A sheet's tools are not printed (`no-print`), and the link is among them
        sign = self.client.get(f"/en/shops/sign/{shop.id}/").content.decode()
        self.assertIn("no-print", next(line for line in sign.splitlines() if settings.SOURCE_URL in line))
        for path, name in ((f"/en/shops/card/{shop.id}/", "till"), (f"/en/shops/customer-card/{shop.id}/", "customer")):
            page = self.client.get(path).content.decode()
            tools, sheet = (
                page.index(f'<div class="{name}-tools no-print">'),
                page.index(f'<article class="{name}-sheet">'),
            )
            self.assertTrue(tools < page.index(settings.SOURCE_URL) < sheet, path)


class Embeddable(TestCase):
    """Pages the community lets other sites frame (`embeddable`): only those, only public ones, never the rest."""

    def framed(self, block):
        from unittest.mock import patch

        from config.community import community

        return patch("website.views.community", return_value={**community(), **block})

    def test_a_listed_page_can_be_framed_and_the_others_cannot(self):
        with self.framed({"embeddable": ["abo*"]}):
            page = self.client.get("/en/about/")
            other = self.client.get("/en/contact/")
        self.assertIn("frame-ancestors *", page["Content-Security-Policy"])
        self.assertNotIn("X-Frame-Options", page)
        self.assertIn("frame-ancestors 'none'", other["Content-Security-Policy"])
        self.assertEqual(other["X-Frame-Options"], "DENY")

    def test_the_sites_that_may_frame_them_can_be_named(self):
        with self.framed({"embeddable": ["about"], "embed_from": ["https://blog.example.org", "javascript:x"]}):
            page = self.client.get("/en/about/")
        self.assertIn("frame-ancestors https://blog.example.org;", page["Content-Security-Policy"] + ";")

    def test_the_panel_stays_closed_whatever_the_list(self):
        with self.framed({"embeddable": ["*"]}):
            panel = self.client.get("/en/shops/")
        self.assertEqual(panel["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", panel["Content-Security-Policy"])


class NativeReviewed(TestCase):
    """The language switcher marks the languages no native speaker has read (config/languages.py)."""

    def setUp(self):
        from config import languages

        self.languages = languages
        languages.declared.cache_clear()
        self.addCleanup(languages.declared.cache_clear)

    def test_the_catalogs_say_it_of_themselves(self):
        self.assertIs(self.languages.declared("it"), True)
        self.assertIs(self.languages.declared("de"), False)
        # English is the language the texts are written in: the template speaks for it
        self.assertIs(self.languages.declared("en"), False)
        # A language with no catalog: nobody says, and nothing is marked
        self.assertIsNone(self.languages.declared("fi"))
        self.assertTrue(self.languages.reviewed("fi"))

    def test_the_languages_translated_but_not_read_say_so(self):
        for code in ("es", "fr", "nl", "pt", "sl"):
            self.assertIs(self.languages.declared(code), False, code)

    def test_a_header_is_read_from_the_first_entry_only(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "django.po"
            path.write_text(
                'msgid ""\nmsgstr ""\n"Language: fr\\n"\n"X-Native-Reviewed: 2026-11\\n"\n\nmsgid "a"\nmsgstr "b"\n'
            )
            self.assertEqual(self.languages.header(path), "2026-11")
            path.write_text('msgid ""\nmsgstr ""\n"Language: fr\\n"\n\nmsgid "X-Native-Reviewed: yes"\nmsgstr "b"\n')
            self.assertIsNone(self.languages.header(path))
            self.assertIsNone(self.languages.header(Path(folder) / "missing.po"))

    def test_the_community_has_the_last_word(self):
        data = dict(community())
        with patch("config.languages.community", return_value={**data, "native_reviewed": {"de": True, "it": False}}):
            self.assertTrue(self.languages.reviewed("de"))
            self.assertFalse(self.languages.reviewed("it"))
            self.assertFalse(self.languages.reviewed("en"))
        with patch("config.languages.community", return_value={**data, "native_reviewed": True}):
            self.assertTrue(self.languages.reviewed("en"))
        # A value that is not yes or no for a language leaves the catalog's word
        with patch("config.languages.community", return_value={**data, "native_reviewed": {"de": "maybe"}}):
            self.assertFalse(self.languages.reviewed("de"))

    def test_a_text_missing_in_a_language_is_shown_in_english_before_the_default_language(self):
        from config.community import fallbacks, localized

        text = {"it": "Ciao", "en": "Hello", "de": "Hallo"}
        languages = [("it", "Italiano"), ("en", "English"), ("de", "Deutsch"), ("fr", "Français")]
        data = dict(community())
        with override_settings(LANGUAGE_CODE="it", LANGUAGES=languages):
            self.assertEqual(localized(text, "de"), "Hallo")
            self.assertEqual(localized(text, "fr"), "Hello")
            self.assertEqual(localized({"it": "Ciao"}, "fr"), "Ciao")
            self.assertEqual(localized("Ciao", "fr"), "Ciao")
            self.assertEqual(fallbacks("fr"), ["fr", "en", "it"])
            # The community says otherwise: one language for all, one at a time, or none
            with patch("config.community.community", return_value={**data, "fallback_language": "de"}):
                self.assertEqual(localized(text, "fr"), "Hallo")
            with patch("config.community.community", return_value={**data, "fallback_language": {"fr": "it"}}):
                self.assertEqual(localized(text, "fr"), "Ciao")
                self.assertEqual(localized({"it": "Ciao", "en": "Hello"}, "de"), "Hello")
            # The key left empty, or a language the mapping does not name: English, as with no key
            for unsaid in (None, {"de": "it"}, {"fr": None}):
                with patch("config.community.community", return_value={**data, "fallback_language": unsaid}):
                    self.assertEqual(localized(text, "fr"), "Hello", unsaid)
            for off in (False, "xx", 3, {"fr": False}):
                with patch("config.community.community", return_value={**data, "fallback_language": off}):
                    self.assertEqual(localized(text, "fr"), "Ciao", off)
        # A site without English: the default language, as before
        with override_settings(LANGUAGE_CODE="it", LANGUAGES=[("it", "Italiano"), ("fr", "Français")]):
            self.assertEqual(localized(text, "fr"), "Ciao")
            self.assertEqual(localized({"de": "Hallo"}, "fr"), "Hallo")

    def test_the_mark_is_in_the_switcher_and_never_on_the_community_s_own_language(self):
        # The example community: English first, then Italian. English is not read, but it is the community's own
        page = self.client.get("/en/").content.decode()
        self.assertNotIn("langs-note", page)
        self.assertNotIn('aria-hidden="true">*', page)
        with override_settings(LANGUAGE_CODE="it"):
            page = self.client.get("/it/").content.decode()
            self.assertIn('lang="en" title="English" aria-label="English" aria-describedby="langs-note"', page)
            self.assertIn('en<span aria-hidden="true">*</span>', page)
            self.assertIn(
                '<small class="langs-note" id="langs-note">* Non ancora riletto da un madrelingua.</small>', page
            )
            self.assertNotIn('it<span aria-hidden="true">*</span>', page)
