"""The community's own code (config/hooks.py, COMMUNITY_HOOKS): what each hook changes, and that a mistake in it is no
answer, never an error of a page, an open private part or a locked register."""
import sys
import types
from datetime import datetime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import checks
from django.db import DatabaseError
from django.http import HttpResponse
from django.test import TestCase, override_settings
from django.urls import path

from config import hooks
from website.visibility import entitled_account

from . import business_day, dashboard, training
from .models import Shop
from .test_training import Base, changed, configured

MODULE = "community_hooks_of_the_tests"


class Hooked:
    """with Hooked(name=function, ...): the site runs with a hooks module that has those functions."""

    def __init__(self, **functions):
        self.functions = functions
        self.settings = override_settings(COMMUNITY_HOOKS=MODULE)

    def __enter__(self):
        module = types.ModuleType(MODULE)
        for name, function in self.functions.items():
            setattr(module, name, function)
        sys.modules[MODULE] = module
        self.settings.enable()
        hooks.module.cache_clear()
        return module

    def __exit__(self, *exc):
        self.settings.disable()
        sys.modules.pop(MODULE, None)
        hooks.module.cache_clear()


def fails(*args):
    raise RuntimeError("a mistake in the community's code")


# The addresses of a community with code of its own, as docs/architecture.md says to write them: its own first, then
# the software's, in the same list (the language prefix of the software's works only in the root list)
def own_page(request):
    return HttpResponse("the community's own page")


from config.urls import urlpatterns as software  # noqa: E402

urlpatterns = [path("guestbook/", own_page), *software]


class NoHooks(TestCase):
    def setUp(self):
        hooks.module.cache_clear()

    def test_without_the_setting_nothing_is_called(self):
        self.assertIsNone(hooks.module())
        self.assertFalse(hooks.defined("private_ok"))
        self.assertEqual((hooks.call("private_ok", None), hooks.yes("private_ok", None), hooks.day("training_grace_until", None),
                          hooks.rows("dashboard_items")), (None, False, None, []))
        self.assertEqual(hooks.problems(), [])

    def test_a_module_that_cannot_be_imported_is_told_and_calls_nothing(self):
        with override_settings(COMMUNITY_HOOKS="no_such_package.hooks"):
            hooks.module.cache_clear()
            with self.assertLogs("config.hooks", "ERROR"):
                self.assertIsNone(hooks.module())
            # Not at every request: the log would fill up
            with self.assertNoLogs("config.hooks", "ERROR"):
                self.assertIsNone(hooks.module())
            found = [item for item in checks.run_checks() if item.id == "shops.W012"]
            self.assertEqual(len(found), 1)
            self.assertIn("no_such_package.hooks cannot be imported", found[0].msg)
        hooks.module.cache_clear()

    def test_a_failed_import_is_tried_again_and_never_kept_for_good(self):
        """One worker that failed to import the module once must not answer without hooks for the rest of its life."""
        with override_settings(COMMUNITY_HOOKS=MODULE):
            hooks.module.cache_clear()
            with self.assertLogs("config.hooks", "ERROR"):
                self.assertIsNone(hooks.module())
            sys.modules[MODULE] = types.ModuleType(MODULE)
            self.addCleanup(sys.modules.pop, MODULE, None)
            self.assertIsNone(hooks.module())
            with patch("config.hooks.time.monotonic", return_value=hooks.time.monotonic() + hooks.RETRY_SECONDS + 1):
                self.assertIs(hooks.module(), sys.modules[MODULE])
        hooks.module.cache_clear()

    def test_the_setting_is_followed_when_it_changes(self):
        with Hooked(private_ok=lambda user: True):
            self.assertTrue(hooks.defined("private_ok"))
            with override_settings(COMMUNITY_HOOKS=""):
                self.assertFalse(hooks.defined("private_ok"))
            with override_settings(COMMUNITY_HOOKS=7):
                self.assertFalse(hooks.defined("private_ok"))
                self.assertIn("dotted name", "\n".join(hooks.problems()))

    def test_a_module_that_raises_when_read_counts_as_missing(self):
        with Hooked() as module:
            def broken(name):
                raise RuntimeError(name)
            module.__getattr__ = broken
            with self.assertLogs("config.hooks", "ERROR"):
                self.assertFalse(hooks.defined("private_ok"))

    @override_settings(ROOT_URLCONF="shops.test_hooks")
    def test_the_addresses_of_a_community_s_own_code_as_the_documents_say(self):
        self.assertContains(self.client.get("/guestbook/"), "the community's own page")
        self.assertEqual(self.client.get("/en/").status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertEqual(self.client.get("/admin/login/").status_code, 200)

    def test_a_hook_that_is_not_a_function_is_told(self):
        with Hooked(private_ok=True):
            self.assertFalse(hooks.defined("private_ok"))
            self.assertIn("private_ok is not a function", "\n".join(hooks.problems()))


@override_settings(NETWORK="mainnet")
class Grace(Base):
    def setUp(self):
        super().setUp()
        hooks.module.cache_clear()
        self.today = business_day.shift_day(None)

    def test_the_community_s_day_keeps_the_register_open_until_it_is_over(self):
        self.assertTrue(training.locked(self.shop))
        with Hooked(training_grace_until=lambda user: self.today + timedelta(days=3)) as module:
            seen = []
            module.training_grace_until = lambda user: seen.append(user) or self.today + timedelta(days=3)
            self.assertFalse(training.locked(self.shop))
            self.assertEqual(seen, [self.owner])
            self.assertEqual(self.client.get(f"/en/shops/register/{self.shop.id}/").status_code, 200)
            state = training.standing(self.owner)
            self.assertEqual((state.locked, state.grace["days"], state.grace["until"]), (False, 3, self.today + timedelta(days=3)))
            panel = self.client.get("/en/shops/")
            self.assertContains(panel, "Your register keeps working.")
            self.assertContains(panel, "You have 3 more days")
        with Hooked(training_grace_until=lambda user: self.today):
            self.assertFalse(training.locked(self.shop))
            self.assertContains(self.client.get("/en/shops/"), "The last day is")
        with Hooked(training_grace_until=lambda user: self.today - timedelta(days=1)):
            self.assertTrue(training.locked(self.shop))
            self.assertIsNone(training.standing(self.owner).grace)
            self.assertNotContains(self.client.get("/en/shops/"), "Your register keeps working.")

    def test_no_day_for_this_owner_is_the_rule_of_everybody(self):
        with Hooked(training_grace_until=lambda user: None):
            self.assertTrue(training.locked(self.shop))

    def test_the_pass_needs_no_day_and_the_day_is_not_asked(self):
        self.pass_the_basics()
        asked = []
        with Hooked(training_grace_until=lambda user: asked.append(user)):
            self.assertFalse(training.locked(self.shop))
            self.assertIsNone(training.standing(self.owner).grace)
            self.client.get("/en/shops/")
        self.assertEqual(asked, [])

    def test_the_day_is_asked_once_for_the_panel_s_standing(self):
        """Two answers in one page could say «locked» and «three days left» together."""
        asked = []
        with Hooked(training_grace_until=lambda user: asked.append(user) or self.today + timedelta(days=3)):
            state = training.standing(self.owner)
        self.assertEqual((len(asked), state.locked, state.grace["days"]), (1, False, 3))

    def test_a_database_that_does_not_answer_inside_the_rule_never_locks(self):
        """As for the software's own queries: `locked` never closes a till because the database is away. Any other
        mistake of the rule gives no days: the register waits for the pass, as without the rule."""
        def away(user):
            raise DatabaseError("the community's table does not answer")
        with Hooked(training_grace_until=away):
            self.assertFalse(training.locked(self.shop))
            self.assertEqual(self.client.get(f"/en/shops/register/{self.shop.id}/").status_code, 200)

    def test_a_mistake_in_the_hook_never_opens_and_never_breaks(self):
        """What is not a day, or raises, is no answer: the register waits for the pass as without the hook."""
        for answer in (fails, lambda user: "tomorrow", lambda user: True, lambda user: 20991231,
                       lambda user: datetime(2099, 12, 31, 9, 0)):
            with self.subTest(answer), Hooked(training_grace_until=answer), self.assertLogs("config.hooks", "ERROR"):
                self.assertTrue(training.locked(self.shop))
                self.assertEqual(self.client.get("/en/shops/").status_code, 200)

    def test_only_where_the_register_would_wait(self):
        """Nothing is said, and the hook is not asked, on a test network or where the community only reminds."""
        asked = []
        rule = lambda user: asked.append(user)  # noqa: E731
        with Hooked(training_grace_until=rule):
            with override_settings(NETWORK="signet"):
                self.assertFalse(training.locked(self.shop))
                self.assertIsNone(training.standing(self.owner).grace)
            for block in (changed(gate="reminder"), changed(since=self.today + timedelta(days=5)), None):
                with configured(block):
                    self.assertFalse(training.locked(self.shop))
        self.assertEqual(asked, [])

    def test_the_practice_done_elsewhere_counts(self):
        self.assertFalse(training.standing(self.owner).steps["practice"]["earned"])
        with Hooked(training_practice_done=lambda user: user == self.owner):
            self.assertTrue(training.standing(self.owner).steps["practice"]["earned"])
            other = get_user_model().objects.create(username="other")
            Shop.objects.create(owner=other, name="Theirs", lightning_address="t@ln.example.org")
            self.assertFalse(training.standing(other).steps["practice"]["earned"])
        for answer in (fails, lambda user: "yes", lambda user: 1):
            with self.subTest(answer), Hooked(training_practice_done=answer), self.assertLogs("config.hooks", "ERROR"):
                self.assertFalse(training.standing(self.owner).steps["practice"]["earned"])


class Private(TestCase):
    def setUp(self):
        hooks.module.cache_clear()
        self.user = get_user_model().objects.create(username="old-merchant")

    def test_the_community_s_rule_lets_an_account_in_when_ours_says_no(self):
        self.assertFalse(entitled_account(self.user))
        with Hooked(private_ok=lambda user: user.username == "old-merchant"):
            self.assertTrue(entitled_account(self.user))
            self.assertFalse(entitled_account(get_user_model().objects.create(username="someone")))

    def test_never_for_a_visitor_or_an_account_turned_off(self):
        asked = []
        with Hooked(private_ok=lambda user: asked.append(user) or True):
            self.assertFalse(entitled_account(None))
            self.user.is_active = False
            self.assertFalse(entitled_account(self.user))
        self.assertEqual(asked, [])

    def test_only_a_plain_yes_opens(self):
        for answer in (fails, lambda user: "yes", lambda user: 1, lambda user: [user]):
            with self.subTest(answer), Hooked(private_ok=answer), self.assertLogs("config.hooks", "ERROR"):
                self.assertFalse(entitled_account(self.user))
        with Hooked(private_ok=lambda user: None):
            self.assertFalse(entitled_account(self.user))


class DashboardLines(TestCase):
    def setUp(self):
        hooks.module.cache_clear()

    def lines(self, rows):
        with Hooked(dashboard_items=lambda: rows):
            return [item for item in dashboard.summary()["items"] if item.code.startswith("community")]

    def test_the_community_s_lines_are_in_the_list(self):
        found = self.lines([{"what": "Three old links were used this month.", "do": "Write to their shops.", "level": "warning",
                             "code": "old_links", "links": [{"text": "The list", "url": "/admin/"},
                                                            {"text": "Outside", "url": "https://example.org/x"}]},
                            {"what": "For your information.", "do": "Nothing."}])
        self.assertEqual([(item.level, item.code, item.what, item.do) for item in found],
                         [("warning", "community_old_links", "Three old links were used this month.", "Write to their shops."),
                          ("info", found[1].code, "For your information.", "Nothing.")])
        # A line without a code is named by its text: another text is another line for the morning message
        self.assertRegex(found[1].code, r"^community_[0-9a-f]{12}$")
        other = self.lines([{"what": "Something else.", "do": "Nothing."}])
        self.assertNotEqual(other[0].code, found[1].code)
        self.assertEqual(found[0].links, [{"text": "The list", "url": "/admin/"}, {"text": "Outside", "url": "https://example.org/x"}])
        self.assertIsNone(found[0].shop)

    def test_what_cannot_be_shown_is_left_out(self):
        found = self.lines(["a text", {"what": "", "do": "x"}, {"what": "x"}, {"what": 3, "do": 4},
                            {"what": "Kept.", "do": "Look.", "level": "urgent", "code": "Bad Code!",
                             "links": [{"text": "Script", "url": "javascript:alert(1)"}, {"text": "Elsewhere", "url": "//evil.example/"},
                                       {"text": "Plain", "url": "http://example.org/"}, "nothing"]}])
        self.assertEqual([(item.level, item.what, item.links) for item in found], [("info", "Kept.", [])])
        self.assertRegex(found[0].code, r"^community_[0-9a-f]{12}$")

    def test_links_that_are_not_a_list_and_odd_addresses_are_left_out(self):
        for links in (5, True, "a link", {"text": "x", "url": "/"}):
            found = self.lines([{"what": "Kept.", "do": "Look.", "links": links}])
            self.assertEqual([(item.what, item.links) for item in found], [("Kept.", [])], links)
        odd = ["/\\evil.example", "/\tx", "/ x", "https://", "https://a b", "/" + "x" * 600, "ftp://example.org/"]
        found = self.lines([{"what": "Kept.", "do": "Look.",
                             "links": [{"text": "x", "url": url} for url in odd] + [{"text": "Home", "url": "/"},
                                                                                     {"text": "Page", "url": "/admin/today/"}]}])
        self.assertEqual(found[0].links, [{"text": "Home", "url": "/"}, {"text": "Page", "url": "/admin/today/"}])

    def test_a_line_is_one_line_of_plain_text(self):
        """The lines also go to a terminal, an email and Telegram, where a new line or a control character is not harmless."""
        found = self.lines([{"what": "First\nsecond\x1b[31m line\t here", "do": "  Do \r\n this  ",
                             "links": [{"text": "A\nlink", "url": "/admin/"}]}])
        self.assertNotRegex(found[0].what + found[0].do + found[0].links[0]["text"], r"[\x00-\x1f]")
        self.assertEqual(found[0].do, "Do this")

    def test_whatever_the_lines_do_the_software_s_own_stay(self):
        with Hooked(dashboard_items=lambda: []), patch("shops.dashboard.community_items", side_effect=RuntimeError("unexpected")), \
                self.assertLogs("shops.dashboard", "ERROR"):
            self.assertIn("items", dashboard.summary())

    def test_a_mistake_in_the_hook_leaves_the_list_as_it_is(self):
        for answer in (fails, lambda: "lines", lambda: {"what": "x", "do": "y"}):
            with self.subTest(answer), Hooked(dashboard_items=answer), self.assertLogs("config.hooks", "ERROR"):
                self.assertEqual([item for item in dashboard.summary()["items"] if item.code.startswith("community")], [])
