"""The admin wears the cyberpunk theme: its pages load admin-theme.css, which exists and has no external resource."""
import re

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import reverse

from shops.test_guard import sign_in_as_staff_session


class AdminTheme(TestCase):
    def test_the_admin_loads_the_sheet(self):
        user = get_user_model().objects.create_user("theme", password="x", is_staff=True, is_superuser=True)
        sign_in_as_staff_session(self.client, user)
        html = self.client.get(reverse("admin:index")).content.decode()
        self.assertRegex(html, r'href="[^"]*admin-theme(\.[0-9a-f]+)?\.css"')
        self.assertIn("staff-zone", html)

    def test_the_sheet_calls_nothing_outside(self):
        path = finders.find("admin-theme.css")
        self.assertTrue(path)
        css = open(path, encoding="utf-8").read()
        self.assertFalse(re.search(r"https?://|@import", css))
