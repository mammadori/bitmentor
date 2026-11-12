"""The register is one screen, like an app: on the smallest phone it is made for (360×640) the keypad, the amount and the
OK key are all on the screen, without scrolling, for the owner's and the staff register, in the light and the dark
colours of the theme. The page as the site gives it, with its styles, measured in a headless Chromium; skipped without
one, an error with REQUIRE_BROWSER=1."""
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest import skipUnless

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase

from config import features
from config.community import COMMUNITY_DIR

from .models import Shop, StaffRegister
from .test_experimental import on
from .test_staff import staff_page
from .tests import reset_limits

BROWSER = os.environ.get("BROWSER_BIN") or shutil.which("chromium-headless-shell")
STATIC = settings.BASE_DIR / "static"
# After the styles, the page writes in its title how many pixels it is taller than the window, and whether the OK key is
# inside the window
MEASURE = """<script>
addEventListener("load", () => {
  for (const id of %s) document.getElementById(id)?.removeAttribute("hidden");
  const ok = document.querySelector(".keys .ok").getBoundingClientRect();
  document.title = `overflow=${document.scrollingElement.scrollHeight - innerHeight};ok=${ok.bottom <= innerHeight && ok.height >= 44}`;
});
</script>"""


@skipUnless(BROWSER or os.environ.get("REQUIRE_BROWSER") == "1", "no chromium-headless-shell")
class OneScreen(TestCase):
    def setUp(self):
        reset_limits()
        self.owner = get_user_model().objects.create(username="owner")
        self.shop = Shop.objects.create(owner=self.owner, name="The Bakery at the Corner of the Long Street",
                                        lightning_address="a-rather-long-address@ln.example.org")
        self.client.force_login(self.owner)

    def staff_html(self, name="Anna"):
        self.client.post(f"/en/shops/registers/{self.shop.id}/", {"name": name})
        register = StaffRegister.objects.get(shop=self.shop, name=name)
        phone = self.client_class()
        phone.post("/en/shops/register/open/", {"code": register.code})
        register.refresh_from_db()
        return phone.get(staff_page(register)).content.decode()

    def measure(self, html, dark=False, shown=(), size=(360, 640)):
        """The page with its styles in place of the links, without its scripts: the keypad as it first appears."""
        self.assertTrue(BROWSER, "REQUIRE_BROWSER=1 but chromium-headless-shell is missing: BROWSER_BIN=/path")
        styles = {"base.css": STATIC / "base.css", "register.css": STATIC / "register.css",
                  "theme.css": Path(COMMUNITY_DIR) / "theme" / "theme.css"}

        def inline(match):
            name = match.group(1).rsplit("/", 1)[-1]
            name = re.sub(r"\.[0-9a-f]{12}\.css$", ".css", name)
            path = styles.get(name)
            return f"<style>{path.read_text()}</style>" if path and path.exists() else ""

        html = re.sub(r'<link rel="stylesheet" href="([^"]+)">', inline, html)
        html = re.sub(r"<script src=[^>]*></script>", "", html)
        html = html.replace("</body>", MEASURE % list(shown) + "</body>")
        if dark:
            html = html.replace("<head>", '<head><meta name="color-scheme" content="dark">')
        with tempfile.TemporaryDirectory() as folder:
            page = Path(folder) / "register.html"
            page.write_text(html)
            flags = ["--force-dark-mode", "--blink-settings=preferredColorScheme=0"] if dark else []
            result = subprocess.run([BROWSER, "--no-sandbox", "--disable-gpu", f"--user-data-dir={folder}/profile",
                                     f"--window-size={size[0]},{size[1]}", *flags, "--virtual-time-budget=3000",
                                     "--dump-dom", page.as_uri()], capture_output=True, text=True, timeout=60, check=False)
        found = re.search(r"<title>overflow=(-?\d+);ok=(\w+)</title>", result.stdout)
        self.assertTrue(found, result.stderr[-2000:])
        return int(found.group(1)), found.group(2) == "true"

    def test_the_keypad_fits_the_smallest_phone_without_scrolling(self):
        owner = self.client.get(f"/en/shops/register/{self.shop.id}/").content.decode()
        staff = self.staff_html()
        self.assertNotIn('id="chain-ok"', owner)
        # With on-chain turned on (an experimental feature: off, the page has no «OK, on-chain» to show) the keypad has
        # one more line under it
        with on(features.ONCHAIN):
            owner_chain = self.client.get(f"/en/shops/register/{self.shop.id}/").content.decode()
            staff_chain = self.staff_html("Bruno")
        self.assertIn('id="chain-ok"', owner_chain)
        self.assertIn('id="chain-ok"', staff_chain)
        for where, html in [("owner", owner), ("staff", staff), ("owner, on-chain on", owner_chain),
                            ("staff, on-chain on", staff_chain)]:
            for dark in (False, True):
                # Empty, and with the discount line, the on-chain key and the shop's note shown
                for shown in ((), ("discount-fixed", "chain-ok", "note")):
                    overflow, ok_visible = self.measure(html, dark=dark, shown=shown)
                    self.assertLessEqual(overflow, 0, f"{where}, dark={dark}, shown={shown}")
                    self.assertTrue(ok_visible, f"{where}, dark={dark}, shown={shown}")
