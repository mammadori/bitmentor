"""The names of the staff registers in the volunteers' area (team/area.py, `DIAGNOSIS_BY_CHOICE`): off unless the
community writes `team_area.staff_register_names: true` in community.yml. Off, one neutral line takes the place of the
findings; /admin/ is unchanged."""
from unittest.mock import patch

from django.core.checks import run_checks
from django.test import override_settings

from config.community import community
from shops.models import HelpRequest

from . import area
from .test_area import AreaBase

LINES = (
    "✗ [lightning_no_answer] Your Lightning wallet is not answering.", "    1.0 s",
    "! [staff_off] These registers are turned off, so they take no new payment: Giulietta, Romeo.",
    "    Giulietta turned off 2026-11-01 10:00 UTC; Romeo turned off 2026-11-02 10:00 UTC",
    "! [staff_unopened] These registers were never opened on a phone: Mercuzio.",
    "    Mercuzio made 2026-11-01 10:00 UTC, code used or expired",
    "! [staff_code_waiting] These registers wait for the person to scan their code: Benvolio.",
    "    Benvolio made 2026-11-03 10:00 UTC, code valid until 2026-11-04 10:00 UTC",
    "✓ [staff_ok] These registers are working: Tebaldo.", "    Tebaldo opened 2026-11-01",
    "✗ [staff_no_register] The registers for your staff cannot work.", "    2 staff register(s) turned on",
    "✓ [shop_visible] Your shop is on the map.", "    id=1",
)
STAFF = "\n".join(LINES)
NAMES = ("Giulietta", "Romeo", "Mercuzio", "Benvolio", "Tebaldo")
NEUTRAL = "Something about the registers of the staff: whoever has the administration sees it."


def written(block):
    """community.yml with this `team_area` block (None: without it)."""
    data = {**community()}
    data.pop("team_area", None)
    if block is not None:
        data["team_area"] = block
    return patch("team.area.community", return_value=data)


class StaffRegisterNames(AreaBase):
    def setUp(self):
        super().setUp()
        HelpRequest.objects.filter(pk=self.request.pk).update(summary=STAFF)

    def page(self):
        _key, browser = self.moderator()
        return browser.get("/team/help/").content.decode()

    def test_off_by_default_no_name_and_one_neutral_line(self):
        for block in (None, {}, {"staff_register_names": False}):
            with written(block):
                page = self.page()
            for name in NAMES:
                self.assertNotIn(name, page, block)
            for code in area.DIAGNOSIS_BY_CHOICE:
                self.assertNotIn(code, page, block)
            self.assertEqual(page.count(NEUTRAL), 1, block)
            for kept in ("lightning_no_answer", "staff_no_register", "shop_visible"):
                self.assertIn(kept, page, block)

    def test_the_neutral_line_takes_the_place_of_the_first(self):
        with written(None):
            copy = area.diagnosis_for_area(STAFF)
        self.assertEqual(copy.splitlines()[2], f"· {NEUTRAL}")
        self.assertEqual(area.diagnosis_for_area("✓ [shop_visible] On the map.", False), "✓ [shop_visible] On the map.")

    def test_on_the_findings_as_before(self):
        with written({"staff_register_names": True}):
            page = self.page()
        for name in NAMES:
            self.assertIn(name, page)
        for code in area.DIAGNOSIS_BY_CHOICE:
            self.assertIn(code, page)
        self.assertNotIn(NEUTRAL, page)

    def test_a_value_that_is_not_a_boolean_stays_off_and_is_told(self):
        for block in ({"staff_register_names": "yes"}, {"staff_register_names": 1}, "on"):
            with written(block):
                self.assertFalse(area.options()[area.STAFF_NAMES])
                warnings = [found for found in run_checks() if found.id == "team.W001"]
                self.assertEqual(len(warnings), 1, block)
                self.assertIn("team_area", warnings[0].msg)
                page = self.page()
            self.assertNotIn("Giulietta", page)
        with written({"staff_register_names": True, "other": 1}):
            self.assertIn("team_area.other", " ".join(area.problems()))
        for block in (None, {"staff_register_names": True}, {"staff_register_names": False}):
            with written(block):
                self.assertEqual(area.problems(), [])

    @override_settings(LANGUAGE_CODE="en")
    def test_the_admin_still_shows_the_whole_diagnosis(self):
        from shops.tests import admin_session

        admin_session(self.client)
        with written(None):
            page = self.client.get(f"/admin/shops/helprequest/{self.request.pk}/change/").content.decode()
        for name in NAMES:
            self.assertIn(name, page)
