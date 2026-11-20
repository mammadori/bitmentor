"""A link that opens the map already filtered: ?q=<text>&discount=1&lightning=1, read by static/map.js only."""

import json
import subprocess

from django.test import TestCase

from .test_scripts import STATIC, node_or_skip


class MapLink(TestCase):
    def test_the_page_never_writes_the_query_and_the_language_redirect_keeps_it(self):
        crafted = "/en/shops/map/?q=%3Cscript%3Ealert(1)%3C%2Fscript%3E&discount=1&lightning=1"
        page = self.client.get(crafted)
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "alert(1)")
        self.assertNotContains(page, "&lt;script&gt;")
        # The same page for everybody: the cached list does not depend on the query
        self.assertEqual(
            self.client.get("/en/shops/map/").content.count(b"shop-search"), page.content.count(b"shop-search")
        )
        moved = self.client.get("/shops/map/?q=bar&discount=1", HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(moved.status_code, 302)
        self.assertEqual(moved["Location"], "/en/shops/map/?q=bar&discount=1")

    def test_the_script_reads_the_filters_from_the_link_and_caps_the_text(self):
        node = node_or_skip(self)
        script = (STATIC / "map.js").read_text()
        start = script.index("const LINKED_TEXT")
        body = script[start : script.index("const linked = ", start)]
        harness = (
            body
            + r"""
const cases = ["", "?q=bakery&discount=1&lightning=1", "?q=%3Cb%3Ex%3C%2Fb%3E&discount=yes&lightning=0",
               "?q=" + "%F0%9F%8D%95".repeat(150), "?q=++caf%C3%A8++&q=other"];
process.stdout.write(JSON.stringify(cases.map(linkedFilters)));
"""
        )
        result = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        found = json.loads(result.stdout)
        self.assertEqual(found[0], {"q": "", "discount": False, "lightning": False})
        self.assertEqual(found[1], {"q": "bakery", "discount": True, "lightning": True})
        # The text stays text (it goes into input.value); only "1" turns a box on
        self.assertEqual(found[2], {"q": "<b>x</b>", "discount": False, "lightning": False})
        # At most 100 characters, a character never cut in half
        self.assertEqual(found[3]["q"], "\U0001f355" * 100)
        self.assertEqual(found[4]["q"], "cafè")

    def test_the_text_goes_only_into_the_search_box(self):
        script = (STATIC / "map.js").read_text()
        uses = [line.strip() for line in script.splitlines() if "linked." in line]
        self.assertEqual(
            uses,
            [
                'if (linked.q) $("shop-search").value = linked.q;',
                'if (linked.discount) $("shop-discount").checked = true;',
                'if (linked.lightning) $("shop-lightning").checked = true;',
            ],
        )
        self.assertNotIn("history.", script)
