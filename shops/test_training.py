"""The merchant's training, the «pass» (shops/training.py): the modules the community writes, the quiz, the declarations that
are the written proof of the information, the badges, the unlock of the real till from the day the community wrote, the three rules of
a staff register and the export for the association. The community's `training` block is the example one of
examples/community/community.yml, read back from its comments, so the example is tested too."""
import csv
import io
import json
import re
import time
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from unittest import skipUnless
from unittest.mock import patch

import yaml
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from config.community import COMMUNITY_DIR, community

from . import business_day, certified, training
from .models import Declaration, LightningStatus, ModuleResult, Shop, StaffRegister, Trainee
from .tests import admin_session, reset_limits
from .training_views import TRIES

UTC = dt_timezone.utc


def example():
    """The `training` block of examples/community/community.yml, from its comments."""
    text = (COMMUNITY_DIR / "community.yml").read_text()
    # The block ends at the first line that is only a «#»
    block = text[text.index("# training:"):].split("\n#\n")[0]
    return yaml.safe_load("\n".join(line[2:] if line.startswith("# ") else "" for line in block.split("\n")))["training"]


EXAMPLE = example()
EXAMPLE_WITHOUT_QUICK = {key: value for key, value in EXAMPLE.items() if key != "quick_start"}
# The right answers of the example, as the form sends them (the index of the option, from 0), by module
RIGHT = {"wallet": {"q0": 0, "q1": 0}, "paid": {"q0": 1, "q1": 1}, "problems": {"q0": 1, "q1": 1}}
TERMS = {"path": "about/"}


def configured(training_block=EXAMPLE, **top):
    return patch("shops.training.community", return_value={**community(), "training": training_block, **top})


def changed(**changes):
    return {**EXAMPLE, **changes}


class Base(TestCase):
    # Without the quick start: the lock of the example as it was; QuickStart has its own
    block = EXAMPLE_WITHOUT_QUICK
    top = {}

    def setUp(self):
        reset_limits()
        patcher = configured(self.block, **self.top)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.owner = get_user_model().objects.create(username="owner")
        self.shop = Shop.objects.create(owner=self.owner, name="Bar", lightning_address="s@ln.example.org")
        self.client.force_login(self.owner)

    def module_page(self, name):
        return f"/en/shops/training/{name}/"

    def quiz(self, name, answers=None, client=None):
        return (client or self.client).post(self.module_page(name), {"action": "quiz", **(RIGHT[name] if answers is None else answers)})

    def declare(self, name, client=None, ids=None):
        module = training.find_module(training.parse(), name)
        ids = [statement.id for statement in module.statements] if ids is None else ids
        return (client or self.client).post(self.module_page(name), {"action": "declare", "ticked": ids})

    def finish(self, *names, client=None):
        for name in names or ("wallet", "paid", "problems"):
            self.quiz(name, client=client)
            self.declare(name, client=client)

    def pass_the_basics(self, user=None, when=None):
        Trainee.objects.update_or_create(user=user or self.owner, defaults={"base_earned": when or timezone.now(),
                                                                            "reviewed": when or timezone.now()})


class Config(Base):
    def test_off_when_the_block_is_missing(self):
        with configured(None):
            self.assertIsNone(training.parse())
            self.assertEqual(self.client.get("/en/shops/training/").status_code, 404)
            self.assertEqual(self.client.get(self.module_page("wallet")).status_code, 404)
            self.assertNotContains(self.client.get("/en/shops/"), "Your merchant pass")
            self.assertFalse(training.locked(self.shop))

    def test_the_example_in_the_community_file_is_valid(self):
        self.assertEqual(training.problems(), [])
        cfg = training.parse()
        self.assertEqual([module.id for module in cfg.modules], ["wallet", "paid", "problems"])
        self.assertEqual((cfg.gate, cfg.review_months, cfg.since), ("unlock", 12, None))
        self.assertEqual(len(cfg.staff_rules), 3)
        for module in cfg.modules:
            self.assertTrue(2 <= len(module.questions) <= 4)

    def test_what_cannot_be_used_is_left_out_and_told(self):
        broken = changed(gate="never", review_months=0, since="soon", staff_rules=["x" * 700],
                         modules=[*EXAMPLE["modules"], {"id": "wallet", "title": "again", "questions": []},
                                  {"id": "Bad Id", "title": "x"}, {"id": "empty", "title": "x"},
                                  {"id": "oops", "title": "x", "questions": [{"text": "q", "options": ["a", "b"], "answer": 3, "explain": "e"}]},
                                  {"id": "review", "title": "reserved", "declarations": [{"id": "a", "text": "t"}]}])
        with configured(broken):
            found = training.problems()
            cfg = training.parse()
        text = "\n".join(found)
        for part in ("training.gate", "training.review_months", "training.since", "training.staff_rules", "no other module has",
                     "needs questions or declarations", "the number of the right option"):
            self.assertIn(part, text)
        self.assertEqual([module.id for module in cfg.modules], ["wallet", "paid", "problems"])
        # A day that cannot be read makes the training wait (no day comes after the last one), it does not start it now
        self.assertEqual((cfg.gate, cfg.review_months, cfg.since, cfg.staff_rules), ("unlock", 12, date.max, ()))

    def test_no_usable_module_keeps_the_training_off(self):
        with configured({"modules": [{"id": "x"}]}):
            self.assertIsNone(training.parse())
            self.assertTrue(training.problems())

    def test_the_check_framework_reports_the_problems(self):
        from .apps import wrong_training

        self.assertEqual(wrong_training(None), [])
        with configured(changed(gate="never")):
            self.assertEqual([found.id for found in wrong_training(None)], ["shops.W005"])

    def test_the_version_of_a_text_follows_its_words_not_the_order_of_the_languages(self):
        a = training.version_of("m:a", {"en": "Hello", "it": "Ciao"})
        self.assertEqual(a, training.version_of("m:a", {"it": "Ciao", "en": "Hello"}))
        self.assertNotEqual(a, training.version_of("m:a", {"en": "Hello", "it": "Ciao!"}))
        self.assertNotEqual(a, training.version_of("m:b", {"en": "Hello", "it": "Ciao"}))
        self.assertEqual(len(a), 64)

    def test_the_terms_key_of_the_community_is_read_under_both_names(self):
        for key in ("help_terms", "merchant_terms"):
            with configured(EXAMPLE, **{key: TERMS}):
                cfg = training.parse()
                self.assertEqual([module.id for module in training.all_modules(cfg)][0], "terms")
                self.assertEqual(training.terms_link("en"), "/en/about/")
        with configured(EXAMPLE, help_terms={"path": "https://example.org/x y"}):
            self.assertIsNone(training.parse().terms)


class Quiz(Base):
    def test_the_page_shows_the_questions_and_not_the_explanations_or_the_answers(self):
        page = self.client.get(self.module_page("wallet"))
        self.assertContains(page, "Who must know the recovery words of your wallet?")
        self.assertNotContains(page, "Only you. The words are the key")
        self.assertEqual(re.findall(r'type="radio"[^>]*checked', page.content.decode()), [])
        # The declarations wait for the questions
        self.assertContains(page, "After the questions you confirm a few statements.")

    def test_a_wrong_answer_reads_the_line_that_explains_and_tries_again_with_no_penalty(self):
        page = self.quiz("wallet", {"q0": 1, "q1": 0})
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Only you. The words are the key to your money")
        self.assertContains(page, "Try again")
        result = ModuleResult.objects.get(user=self.owner, module="wallet")
        self.assertEqual((result.attempts, result.passed), (1, None))
        # The right answer stays chosen, the wrong one is cleared
        self.assertEqual(page.content.decode().count(" checked"), 1)
        for _ in range(5):
            self.quiz("wallet", {"q0": 2, "q1": 2})
        self.assertEqual(ModuleResult.objects.get(user=self.owner, module="wallet").attempts, 6)
        done = self.quiz("wallet")
        self.assertEqual(done.status_code, 302)
        result = ModuleResult.objects.get(user=self.owner, module="wallet")
        self.assertIsNotNone(result.passed)
        self.assertEqual(result.attempts, 7)

    def test_no_answer_at_all_is_wrong_and_nothing_breaks(self):
        self.assertEqual(self.quiz("wallet", {}).status_code, 200)
        self.assertEqual(self.quiz("wallet", {"q0": "x", "q1": "999"}).status_code, 200)
        self.assertIsNone(ModuleResult.objects.get(user=self.owner, module="wallet").passed)

    def test_a_passed_quiz_stays_passed(self):
        self.quiz("wallet")
        first = ModuleResult.objects.get(user=self.owner, module="wallet").passed
        self.quiz("wallet", {"q0": 3, "q1": 3})
        self.assertEqual(ModuleResult.objects.get(user=self.owner, module="wallet").passed, first)

    def test_the_declarations_need_the_quiz_first(self):
        self.declare("wallet")
        self.assertEqual(Declaration.objects.count(), 0)

    def test_it_works_without_javascript_every_form_is_plain(self):
        page = self.client.get(self.module_page("wallet")).content.decode()
        # No script in the page but the site's own, and no handler in a tag (the policy allows neither)
        self.assertEqual(re.findall(r"<script(?![^>]*\bsrc=)", page), [])
        self.assertEqual(re.findall(r"\son[a-z]+=", page), [])
        self.assertIn('<form method="post"', page)
        self.assertIn("csrfmiddlewaretoken", page)

    def test_an_unknown_module_is_not_found(self):
        self.assertEqual(self.client.get(self.module_page("nothing")).status_code, 404)
        self.assertEqual(self.client.post(self.module_page("nothing"), {"action": "quiz"}).status_code, 404)

    def test_too_many_tries_in_a_short_time_wait(self):
        # One window of the limit for the whole loop: a slow run must not cross into the next one and start counting again
        with patch("config.ratelimit.time.time", return_value=time.time()):
            for _ in range(TRIES + 5):
                self.client.post(self.module_page("wallet"), {"action": "quiz", "q0": 9})
        self.assertEqual(ModuleResult.objects.get(user=self.owner, module="wallet").attempts, TRIES)


class Declarations(Base):
    def test_a_declaration_keeps_version_text_date_account_and_shop_and_nothing_else(self):
        self.quiz("wallet")
        before = timezone.now()
        response = self.client.post(self.module_page("wallet"), {"action": "declare", "ticked": ["words-mine"]},
                                    headers={"X-Forwarded-For": "203.0.113.9"}, REMOTE_ADDR="203.0.113.9")
        self.assertEqual(response.status_code, 302)
        found = Declaration.objects.get()
        module = training.find_module(training.parse(), "wallet")
        statement = module.statements[0]
        self.assertEqual((found.user, found.shop, found.who, found.shop_name), (self.owner, self.shop, "owner", "Bar"))
        self.assertEqual((found.key, found.version, found.language), ("wallet:words-mine", statement.version, "en"))
        self.assertEqual(found.text, "The recovery words of my wallet stay with me only. I wrote them on paper and I know where it is.")
        self.assertGreaterEqual(found.made, before)
        # No address of the connection, anywhere in the row
        self.assertNotIn("203.0.113.9", repr(sorted(vars(found).items())))
        self.assertEqual(sorted(field.name for field in Declaration._meta.fields),
                         sorted(["id", "user", "shop", "register", "who", "shop_name", "key", "version", "text", "language", "made"]))

    def test_the_same_declaration_twice_is_one_row(self):
        self.quiz("wallet")
        for _ in range(3):
            self.declare("wallet", ids=["words-mine"])
        self.assertEqual(Declaration.objects.count(), 1)

    def test_a_module_is_done_when_the_quiz_and_every_statement_are(self):
        self.quiz("wallet")
        self.declare("wallet", ids=["words-mine"])
        item = training.standing(self.owner).modules[0]
        self.assertEqual((item["state"], item["statements_done"], item["statements_total"]), ("declare", 1, 2))
        self.declare("wallet")
        self.assertEqual(training.standing(self.owner).modules[0]["state"], "done")

    def test_the_text_in_the_language_of_the_page_is_what_is_kept(self):
        self.quiz("wallet")
        self.client.post("/it" + self.module_page("wallet")[3:], {"action": "declare", "ticked": ["words-mine"]})
        found = Declaration.objects.get()
        self.assertEqual((found.language, found.text[:20]), ("it", "Le parole di recuper"))
        # Another language of the same text is the same version: it is not asked again
        self.assertEqual(training.standing(self.owner).modules[0]["states"][0], "done")

    def test_a_changed_text_is_a_new_version_asked_again_and_the_old_row_stays(self):
        self.finish()
        self.assertTrue(training.standing(self.owner).base)
        old = Declaration.objects.get(key="wallet:words-mine")
        edited = json.loads(json.dumps(EXAMPLE))
        edited["modules"][0]["declarations"][0]["text"]["en"] = "My recovery words are mine only, written on paper."
        with configured(edited):
            state = training.standing(self.owner)
            self.assertEqual(state.modules[0]["state"], "update")
            self.assertEqual(state.modules[0]["states"], ["old", "done"])
            # The pass is a fact of the past: it stays; the panel asks again, nothing locks
            self.assertTrue(state.base)
            page = self.client.get(self.module_page("wallet"))
            self.assertContains(page, "This text changed since you confirmed it")
            self.assertContains(page, "My recovery words are mine only")
            self.declare("wallet", ids=["words-mine"])
            self.assertEqual(training.standing(self.owner).modules[0]["state"], "done")
        rows = Declaration.objects.filter(key="wallet:words-mine").order_by("made", "pk")
        self.assertEqual(rows.count(), 2)
        self.assertEqual(rows[0].pk, old.pk)
        self.assertNotEqual(rows[0].version, rows[1].version)
        self.assertEqual(rows[0].text, old.text)

    def test_a_translation_changed_in_another_language_is_a_new_version_too(self):
        self.finish()
        edited = json.loads(json.dumps(EXAMPLE))
        edited["modules"][0]["declarations"][0]["text"]["it"] += " Davvero."
        with configured(edited):
            self.assertEqual(training.standing(self.owner).modules[0]["states"][0], "old")

    def test_the_basic_pass_is_earned_once_when_every_module_is_done(self):
        self.finish("wallet", "paid")
        self.assertFalse(Trainee.objects.get(user=self.owner).base_earned)
        self.finish("problems")
        trainee = Trainee.objects.get(user=self.owner)
        self.assertIsNotNone(trainee.base_earned)
        self.assertEqual(trainee.base_earned, trainee.reviewed)
        first = trainee.base_earned
        self.finish()
        self.assertEqual(Trainee.objects.get(user=self.owner).base_earned, first)

    def test_a_module_that_does_not_count_for_the_pass_does_not_hold_it_back(self):
        extra = {**EXAMPLE["modules"][2], "id": "bonus", "base": False}
        with configured(changed(modules=[*EXAMPLE["modules"], extra])):
            self.finish("wallet", "paid", "problems")
            self.assertTrue(training.standing(self.owner).base)

    def test_the_conditions_of_the_help_are_the_first_declaration_of_the_path(self):
        with configured(EXAMPLE, help_terms=TERMS):
            state = training.standing(self.owner)
            self.assertEqual([item["id"] for item in state.modules][:2], ["terms", "wallet"])
            self.finish("wallet", "paid", "problems")
            self.assertFalse(training.standing(self.owner).base)
            page = self.client.get(self.module_page("terms"))
            self.assertContains(page, "I read the conditions of the help")
            self.assertContains(page, 'href="/en/about/"')
            self.client.post(self.module_page("terms"), {"action": "declare", "ticked": ["accept"]})
            self.assertTrue(training.standing(self.owner).base)
            self.assertEqual(Declaration.objects.get(key="terms:accept").user, self.owner)

    def test_a_shop_of_another_account_is_never_the_shop_of_a_declaration(self):
        other = Shop.objects.create(owner=get_user_model().objects.create(username="other"), name="Theirs",
                                    lightning_address="t@ln.example.org")
        self.quiz("paid")
        self.client.post(self.module_page("paid") + f"?shop={other.id}", {"action": "declare", "ticked": ["paid-in-wallet"],
                                                                         "shop": str(other.id)})
        self.assertEqual(Declaration.objects.get().shop, self.shop)

    def test_a_merchant_with_no_shop_yet_can_do_the_path(self):
        Shop.objects.all().delete()
        self.finish()
        self.assertEqual(Declaration.objects.filter(shop__isnull=True).count(), Declaration.objects.count())
        self.assertTrue(training.standing(self.owner).base)

    def test_the_rows_outlive_the_shop_and_the_account(self):
        self.quiz("paid")
        self.declare("paid")
        row = Declaration.objects.get()
        Shop.objects.filter(pk=self.shop.pk).delete()
        row.refresh_from_db()
        self.assertEqual((row.shop_id, row.shop_name, row.who), (None, "Bar", "owner"))
        get_user_model().objects.filter(pk=self.owner.pk).delete()
        row.refresh_from_db()
        self.assertEqual((row.user_id, row.who), (None, "owner"))
        self.assertFalse(Trainee.objects.filter(user_id=self.owner.pk).exists())

    def test_the_shops_of_one_owner_can_all_be_deleted_after_the_same_declaration(self):
        """The declarations outlive the shop, with its name and without the link. Two shops of one owner with the same
        step declared leave two such rows: the second deletion must not find the first one in its way."""
        second = Shop.objects.create(owner=self.owner, name="Second", lightning_address="u@ln.example.org")
        for shop in (self.shop, second):
            self.assertTrue(training.declare_step(self.owner, shop, "backup"))
        for shop in (self.shop, second):
            self.assertRedirects(self.client.post(f"/en/shops/remove/{shop.id}/"), "/en/shops/", fetch_redirect_response=False)
        self.assertFalse(Shop.objects.exists())
        self.assertEqual(sorted(Declaration.objects.values_list("shop", "shop_name")), [(None, "Bar"), (None, "Second")])

    def test_an_account_with_two_shops_and_the_same_declaration_can_be_deleted(self):
        second = Shop.objects.create(owner=self.owner, name="Second", lightning_address="u@ln.example.org")
        for shop in (self.shop, second):
            training.declare_step(self.owner, shop, "backup")
        self.owner.delete()
        self.assertEqual(sorted(Declaration.objects.values_list("user", "shop", "who", "shop_name")),
                         [(None, None, "owner", "Bar"), (None, None, "owner", "Second")])

    def test_a_declaration_without_a_shop_is_still_one_row_beside_those_of_deleted_shops(self):
        """Made without a shop it is once per person, statement and version; the rows two deleted shops left are not it."""
        second = Shop.objects.create(owner=self.owner, name="Second", lightning_address="u@ln.example.org")
        for shop in (self.shop, second):
            training.record(self.owner, shop, "k", "v1", "text")
        Shop.objects.all().delete()
        self.assertTrue(training.record(self.owner, None, "k", "v1", "text"))
        self.assertFalse(training.record(self.owner, None, "k", "v1", "text"))
        self.assertEqual(Declaration.objects.filter(key="k").count(), 3)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Declaration.objects.create(user=self.owner, key="k", version="v1", who="owner", text="text")


class Badges(Base):
    def test_steps_are_declared_after_a_guide_for_one_shop(self):
        response = self.client.post("/en/shops/training/declaration/", {"step": "backup", "shop": str(self.shop.id), "agree": "1"})
        self.assertEqual(response.status_code, 302)
        found = Declaration.objects.get()
        self.assertEqual((found.key, found.shop), ("step:backup", self.shop))
        state = training.standing(self.owner)
        self.assertEqual([badge["key"] for badge in state.badges if badge["earned"]], ["backup"])

    def test_a_step_needs_the_tick_and_a_known_step_and_a_shop_of_the_account(self):
        other = Shop.objects.create(owner=get_user_model().objects.create(username="other"), name="Theirs")
        for data in ({"step": "backup", "shop": str(self.shop.id)}, {"step": "magic", "shop": str(self.shop.id), "agree": "1"},
                     {"step": "", "agree": "1"}):
            self.client.post("/en/shops/training/declaration/", data)
        self.assertEqual(Declaration.objects.count(), 0)
        # A shop of somebody else falls back to the account's own, never to the other's
        self.client.post("/en/shops/training/declaration/", {"step": "withdrawal", "shop": str(other.id), "agree": "1"})
        self.assertEqual(Declaration.objects.get().shop, self.shop)

    def test_the_steps_count_for_the_shop_they_were_declared_for(self):
        second = Shop.objects.create(owner=self.owner, name="Second", lightning_address="u@ln.example.org")
        training.declare_step(self.owner, self.shop, "practice")
        self.assertTrue(training.standing(self.owner, self.shop).steps["practice"]["earned"])
        self.assertFalse(training.standing(self.owner, second).steps["practice"]["earned"])
        self.assertFalse(training.declare_step(self.owner, Shop.objects.create(name="Not theirs"), "practice"))

@override_settings(NETWORK="mainnet")
class Unlock(Base):
    def setUp(self):
        super().setUp()
        self.url = f"/en/shops/register/{self.shop.id}/"

    def opens(self):
        return self.client.get(self.url).status_code == 200

    def test_without_the_pass_the_real_register_does_not_open_or_sell(self):
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 403)
        self.assertContains(page, "The register opens soon", status_code=403)
        self.assertContains(page, "Start the path", status_code=403)
        self.assertNotContains(page, "register-data", status_code=403)

    def test_with_the_pass_it_sells(self):
        self.pass_the_basics()
        self.assertTrue(self.opens())

    def test_the_practice_register_always_opens(self):
        self.assertEqual(self.client.get(f"/en/shops/practice/{self.shop.id}/").status_code, 200)

    def test_the_signet_never_locks(self):
        with override_settings(NETWORK="signet"):
            self.assertFalse(training.locked(self.shop))
            self.assertTrue(self.opens())

    def test_only_reminders_when_the_community_chooses_them(self):
        with configured(changed(gate="reminder")):
            self.assertFalse(training.locked(self.shop))
            self.assertTrue(self.opens())
            self.assertContains(self.client.get("/en/shops/"), "not required")

    def test_nothing_changes_where_the_training_is_off(self):
        with configured(None):
            self.assertFalse(training.locked(self.shop))
            self.assertTrue(self.opens())

    def test_a_shop_without_an_owner_and_a_training_site_are_never_locked(self):
        self.assertFalse(training.locked(Shop.objects.create(name="Nobody's", lightning_address="z@ln.example.org")))
        self.assertFalse(training.locked(Shop.objects.create(owner=self.owner, name="Demo", lightning_address="z@ln.example.org", external_id="bitmentor-demo:1")))

    def test_the_day_the_community_starts_counting_nothing_is_locked_before(self):
        # The business day, not the calendar one: until 05:00 it is still the day before
        tomorrow = (business_day.shift_day() + timedelta(days=1)).isoformat()
        with configured(changed(since=tomorrow)):
            self.assertFalse(training.locked(self.shop))
        with configured(changed(since=business_day.shift_day().isoformat())):
            self.assertTrue(training.locked(self.shop))

    def test_preflight_says_which_registers_wait_when_no_day_is_written(self):
        from .management.commands.preflight import training_notes

        notes = training_notes()
        self.assertEqual(len(notes), 1)
        self.assertIn("1 shops with a register", notes[0])
        self.assertIn("training.since", notes[0])
        # Nothing to say with a day written, with reminders only, without the training, or once the owner has the pass
        for block in (changed(since="2026-12-02"), changed(gate="reminder"), None):
            with configured(block):
                self.assertEqual(training_notes(), [])
        self.pass_the_basics()
        self.assertEqual(training_notes(), [])

    def test_the_first_day_begins_with_the_business_day(self):
        # A till open through the night is never locked in the middle of a shift: the day starts at 05:00
        with configured(changed(since="2026-12-02")):
            self.assertFalse(training.locked(self.shop, datetime(2026, 12, 2, 3, 59, tzinfo=UTC)))
            self.assertTrue(training.locked(self.shop, datetime(2026, 12, 2, 5, 0, tzinfo=UTC)))

    def test_an_account_that_met_the_training_without_the_pass_is_locked(self):
        Trainee.objects.create(user=self.owner)
        self.assertTrue(training.locked(self.shop))

    def test_the_pass_ends_it_all_and_a_late_review_or_a_changed_text_never_lock(self):
        self.pass_the_basics(when=timezone.now() - timedelta(days=900))
        self.assertFalse(training.locked(self.shop))
        edited = json.loads(json.dumps(EXAMPLE))
        edited["modules"][0]["declarations"][0]["text"]["en"] = "New words."
        with configured(edited):
            self.assertFalse(training.locked(self.shop))
            self.assertTrue(self.opens())

    def test_a_staff_register_follows_its_owner(self):
        register = StaffRegister.objects.create(shop=self.shop, name="Anna")
        training.accept_staff_rules(register)
        phone = Client()
        page = phone.get(f"/en/shops/register/p/{register.id}/?k={register.key}")
        self.assertEqual(page.status_code, 403)
        self.assertContains(page, "The owner has not finished the training yet", status_code=403)
        self.pass_the_basics()
        self.assertEqual(phone.get(f"/en/shops/register/p/{register.id}/?k={register.key}").status_code, 200)

    def test_the_phones_copy_goes_with_the_register(self):
        gone = (settings.BASE_DIR / "static" / "register-sw-gone.js").read_bytes()
        # The script of a locked register takes the old copy off the phone; the usual one keeps it
        self.assertEqual(self.client.get(f"/en/shops/register/{self.shop.id}/sw.js").content, gone)
        self.pass_the_basics()
        self.assertNotEqual(self.client.get(f"/en/shops/register/{self.shop.id}/sw.js").content, gone)

    def test_the_panel_says_why_and_what_to_do(self):
        page = self.client.get("/en/shops/")
        self.assertContains(page, "The register is waiting for you.")
        self.assertContains(page, "Register (waiting for the pass)")

class Certificate(Base):
    def test_the_panel_path_says_the_badge_also_needs_the_pass(self):
        shop = Shop.objects.get(pk=self.shop.pk)
        from .journey import certificate_step

        self.assertTrue(certificate_step(shop)["needs_pass"])
        self.pass_the_basics()
        self.assertFalse(certificate_step(shop)["needs_pass"])

    def test_the_basic_pass_certifies_a_shop_whose_address_already_works(self):
        Shop.objects.filter(pk=self.shop.pk).update(lightning_status=LightningStatus.OK,
                                                    lightning_checked_address="s@ln.example.org")
        self.finish("wallet", "paid")
        self.assertIsNone(Shop.objects.get(pk=self.shop.pk).certified_month)
        self.finish("problems")
        self.assertEqual(Shop.objects.get(pk=self.shop.pk).certified_month, certified.month_of(timezone.now()))

    def test_without_the_pass_a_working_address_is_not_enough(self):
        Shop.objects.filter(pk=self.shop.pk).update(lightning_status=LightningStatus.OK,
                                                    lightning_checked_address="s@ln.example.org")
        self.assertEqual(certified.refresh(), 0)
        self.pass_the_basics()
        self.assertEqual(certified.refresh(), 1)


class Sign(Base):
    def setUp(self):
        super().setUp()
        self.shop.latitude, self.shop.longitude = 41.9, 12.5
        self.shop.save()

    def test_the_sign_is_on_the_page_the_map_and_the_json_when_the_owner_has_the_pass(self):
        page = f"/en/shops/s/{self.shop.id}/"
        self.assertNotContains(self.client.get(page), "Trained merchant")
        self.assertFalse(json.loads(self.client.get("/shops.json").content)["shops"][0]["trained"])
        self.pass_the_basics()
        self.assertContains(self.client.get(page), "Trained merchant")
        self.assertContains(self.client.get("/en/shops/map/"), "Trained merchant")
        self.assertTrue(json.loads(self.client.get("/shops.json").content)["shops"][0]["trained"])

    def test_the_community_can_leave_the_sign_off(self):
        self.pass_the_basics()
        with configured(changed(public_sign=False)):
            self.assertNotContains(self.client.get(f"/en/shops/s/{self.shop.id}/"), "Trained merchant")
            self.assertFalse(json.loads(self.client.get("/shops.json").content)["shops"][0]["trained"])

    def test_the_sign_waits_for_the_review_and_comes_back_with_it(self):
        self.pass_the_basics(when=timezone.now() - timedelta(days=366 + 31))
        self.assertTrue(training.is_trained(Shop.objects.get(pk=self.shop.pk)) is False)
        self.pass_the_basics(when=timezone.now() - timedelta(days=366))
        self.assertTrue(training.is_trained(Shop.objects.get(pk=self.shop.pk)))

    def test_the_sign_costs_one_query_for_the_whole_map(self):
        others = [Shop.objects.create(owner=get_user_model().objects.create(username=f"o{n}"), name=f"S{n}") for n in range(5)]
        self.pass_the_basics(others[0].owner)
        shops = list(Shop.objects.all())
        with self.assertNumQueries(1):
            training.mark_trained(shops)
        self.assertEqual([shop.trained for shop in shops if shop.pk == others[0].pk], [True])

    def test_a_suspended_shop_shows_no_sign(self):
        Shop.objects.filter(pk=self.shop.pk).update(suspended=True)
        self.pass_the_basics()
        self.assertNotContains(self.client.get(f"/en/shops/s/{self.shop.id}/"), "Trained merchant")


class Review(Base):
    def setUp(self):
        super().setUp()
        self.finish()
        Trainee.objects.filter(user=self.owner).update(reviewed=timezone.now() - timedelta(days=380))

    def test_the_review_asks_one_question_for_each_module(self):
        page = self.client.get("/en/shops/training/review/")
        self.assertEqual(page.content.decode().count("<fieldset"), 3)
        self.assertContains(page, "No penalty")

    def test_it_is_due_after_a_year_and_late_after_the_refresh_window(self):
        state = training.review_state(training.parse(), Trainee.objects.get(user=self.owner))
        self.assertEqual(state["state"], "due")
        Trainee.objects.filter(user=self.owner).update(reviewed=timezone.now() - timedelta(days=340))
        self.assertEqual(training.review_state(training.parse(), Trainee.objects.get(user=self.owner))["state"], "soon")
        Trainee.objects.filter(user=self.owner).update(reviewed=timezone.now() - timedelta(days=500))
        self.assertEqual(training.review_state(training.parse(), Trainee.objects.get(user=self.owner))["state"], "late")
        self.assertContains(self.client.get("/en/shops/"), "Time for the short review")

    def post(self, right=True):
        """One question of each module, answered rightly or not: the form sends the index of the option, from 0."""
        picks = {"wallet": 1, "paid": 0, "problems": 1}
        data = {}
        for name, index in picks.items():
            question = training.find_module(training.parse(), name).questions[index]
            data[f"pick-{name}"] = index
            data[f"a-{name}"] = question.answer if right else (question.answer + 1) % len(question.options)
        return self.client.post("/en/shops/training/review/", data)

    def test_right_answers_renew_the_review(self):
        before = Trainee.objects.get(user=self.owner).reviewed
        response = self.post()
        self.assertEqual(response.status_code, 302)
        self.assertGreater(Trainee.objects.get(user=self.owner).reviewed, before)
        self.assertEqual(training.review_state(training.parse(), Trainee.objects.get(user=self.owner))["state"], "ok")

    def test_wrong_answers_are_explained_and_change_nothing(self):
        before = Trainee.objects.get(user=self.owner).reviewed
        response = self.post(right=False)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Not this one.")
        self.assertEqual(Trainee.objects.get(user=self.owner).reviewed, before)

    def test_a_question_that_was_not_asked_does_not_count(self):
        response = self.client.post("/en/shops/training/review/", {"pick-wallet": 99, "a-wallet": 0})
        self.assertEqual(response.status_code, 200)

    def test_without_the_pass_there_is_nothing_to_review(self):
        Trainee.objects.all().delete()
        self.assertEqual(self.client.get("/en/shops/training/review/").status_code, 302)


class StaffRules(Base):
    def setUp(self):
        super().setUp()
        self.register = StaffRegister.objects.create(shop=self.shop, name="Anna")
        self.phone = Client()
        self.page = f"/en/shops/register/p/{self.register.id}/?k={self.register.key}"
        self.rules = f"/en/shops/register/p/{self.register.id}/rules/?k={self.register.key}"

    def test_a_new_register_reads_the_three_rules_before_the_first_sale(self):
        response = self.phone.get(self.page)
        self.assertRedirects(response, self.rules, fetch_redirect_response=False)
        rules = self.phone.get(self.rules)
        self.assertContains(rules, "Three rules of the counter")
        self.assertContains(rules, "A sale is paid only when the register says so.")
        self.assertContains(rules, "Never give the recovery words of a wallet to anyone.")
        self.assertContains(rules, "If in doubt, call the owner.")
        self.assertEqual(rules["Referrer-Policy"], "same-origin")
        self.assertEqual(rules["Cache-Control"], "no-store")

    def test_i_understood_is_kept_like_a_declaration_for_the_register(self):
        response = self.phone.post(f"/en/shops/register/p/{self.register.id}/rules/", {"k": self.register.key, "agree": "1"})
        self.assertRedirects(response, self.page, fetch_redirect_response=False)
        found = Declaration.objects.get()
        self.assertEqual((found.register, found.user, found.shop, found.who, found.key), (self.register, None, self.shop, "Anna", "staff:rules"))
        self.assertIn("1. A sale is paid only when the register says so.", found.text)
        self.assertIn("3. If in doubt, call the owner.", found.text)
        self.assertEqual(self.phone.get(self.page).status_code, 200)

    def test_reading_without_pressing_the_button_keeps_nothing(self):
        self.phone.post(f"/en/shops/register/p/{self.register.id}/rules/", {"k": self.register.key})
        self.assertEqual(Declaration.objects.count(), 0)
        self.assertEqual(self.phone.get(self.page).status_code, 302)

    def test_twice_is_one_row(self):
        for _ in range(2):
            self.phone.post(f"/en/shops/register/p/{self.register.id}/rules/", {"k": self.register.key, "agree": "1"})
        self.assertEqual(Declaration.objects.count(), 1)

    def test_a_register_that_accepted_an_older_text_is_never_stopped_by_a_new_one(self):
        mine = [{"en": "One.", "it": "Uno."}, {"en": "Two.", "it": "Due."}, {"en": "Three.", "it": "Tre."}]
        with configured(changed(staff_rules=mine)):
            self.phone.post(f"/en/shops/register/p/{self.register.id}/rules/", {"k": self.register.key, "agree": "1"})
        # The software's own rules are another text: a register already at work keeps working
        self.assertEqual(self.phone.get(self.page).status_code, 200)
        # A register paired after the change reads the new text
        other = StaffRegister.objects.create(shop=self.shop, name="Bea")
        self.assertEqual(self.phone.get(f"/en/shops/register/p/{other.id}/?k={other.key}").status_code, 302)

    def test_the_rules_are_the_communitys_when_it_wrote_them_and_a_new_text_is_a_new_version(self):
        mine = [{"en": "One.", "it": "Uno."}, {"en": "Two.", "it": "Due."}, {"en": "Three.", "it": "Tre."}]
        with configured(changed(staff_rules=mine)):
            self.assertContains(self.phone.get(self.rules), "Two.")
            self.phone.post(f"/en/shops/register/p/{self.register.id}/rules/", {"k": self.register.key, "agree": "1"})
            self.assertEqual(Declaration.objects.get().text, "1. One.\n2. Two.\n3. Three.")
            self.assertEqual(self.phone.get(self.page).status_code, 200)
        # Back to the software's own rules: another text and version, kept as its own row when read again
        self.assertNotEqual(Declaration.objects.get().version, training.staff_version(training.parse()))
        self.phone.post(f"/en/shops/register/p/{self.register.id}/rules/", {"k": self.register.key, "agree": "1"})
        self.assertEqual(Declaration.objects.count(), 2)

    def test_the_rules_come_in_the_language_of_the_page(self):
        mine = [{"en": "One.", "it": "Uno."}, {"en": "Two.", "it": "Due."}, {"en": "Three.", "it": "Tre."}]
        with configured(changed(staff_rules=mine)):
            self.assertContains(self.phone.get(self.rules.replace("/en/", "/it/")), "Due.")

    def test_nothing_changes_without_the_training(self):
        with configured(None):
            self.assertEqual(self.phone.get(self.page).status_code, 200)
            self.assertEqual(self.phone.get(self.rules).status_code, 404)

    def test_the_key_is_needed_and_a_register_turned_off_has_no_rules(self):
        self.assertEqual(self.phone.get(f"/en/shops/register/p/{self.register.id}/rules/").status_code, 404)
        self.assertEqual(self.phone.get(f"/en/shops/register/p/{self.register.id}/rules/?k=wrong").status_code, 404)
        self.assertEqual(self.phone.post(f"/en/shops/register/p/{self.register.id}/rules/", {"k": "x" * 30, "agree": "1"}).status_code, 404)
        StaffRegister.objects.filter(pk=self.register.pk).update(retired=timezone.now())
        self.assertEqual(self.phone.get(self.rules).status_code, 404)
        self.assertEqual(Declaration.objects.count(), 0)

    def test_the_phone_needs_the_csrf_token_to_say_it_understood(self):
        strict = Client(enforce_csrf_checks=True)
        response = strict.post(f"/en/shops/register/p/{self.register.id}/rules/", {"k": self.register.key, "agree": "1"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Declaration.objects.count(), 0)
        page = strict.get(self.rules)
        token = strict.cookies["csrftoken"].value
        ok = strict.post(f"/en/shops/register/p/{self.register.id}/rules/", {"k": self.register.key, "agree": "1",
                                                                          "csrfmiddlewaretoken": token})
        self.assertEqual(ok.status_code, 302)
        self.assertContains(page, "csrfmiddlewaretoken")

    def test_the_practice_is_one_tap_away(self):
        self.assertContains(self.phone.get(self.rules), f"/en/shops/register/p/{self.register.id}/practice/?k={self.register.key}")


@override_settings(NETWORK="mainnet")
class QuickStart(Base):
    """Before the pass the owner's register takes payments up to the community's `quick_start`; the count is on the phone
    (shops/test_register_flow.py), here the page, the lock for everyone else and the configuration."""
    block = changed(quick_start=20)

    def setUp(self):
        super().setUp()
        self.url = f"/en/shops/register/{self.shop.id}/"

    def data(self, page):
        return json.loads(re.search(r'<script id="register-data" type="application/json">(.*?)</script>', page.content.decode(), re.S).group(1))

    def test_the_example_has_it(self):
        self.assertEqual(EXAMPLE["quick_start"], 20)

    def test_the_amount_is_kept_in_cents_and_is_off_when_missing_zero_or_wrong(self):
        self.assertEqual(training.parse().quick_start, 2000)
        for value, cents in ((7.5, 750), (0, 0), (False, 0), (None, 0)):
            with configured(changed(quick_start=value)):
                self.assertEqual(training.parse().quick_start, cents, value)
        with configured(EXAMPLE_WITHOUT_QUICK):
            self.assertEqual(training.parse().quick_start, 0)
        for value in (-1, 5000, "20", True, [1], 0.004):
            found = []
            with configured(changed(quick_start=value)):
                self.assertEqual(training.parse(found.append).quick_start, 0, value)
            self.assertTrue(any("quick_start" in line for line in found), value)

    def test_the_owner_s_register_opens_with_the_limit_and_the_words(self):
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'id="quick-start"')
        self.assertContains(page, "/en/shops/training/")
        data = self.data(page)
        self.assertEqual(data["quick_start"], {"cents": 2000, "currency": "EUR"})
        self.assertIn("{left}", data["texts"]["quick_left"])
        self.assertEqual(data["texts"]["quick_done"], "Quick start used up. Finish the training to keep taking payments.")

    def test_the_words_come_in_italian_and_german(self):
        from django.utils import translation

        from .views import quick_texts

        # The example community writes in English and Italian; German is for the communities that add it
        self.assertIn("Avvio rapido", self.data(self.client.get(self.url.replace("/en/", "/it/")))["texts"]["quick_left"])
        with translation.override("de"):
            self.assertIn("Schnellstart", str(quick_texts()["quick_left"]))
            self.assertIn("Schulung", str(quick_texts()["quick_done"]))

    def test_nobody_but_the_owner_has_it(self):
        stranger = Client()
        self.assertEqual(stranger.get(self.url).status_code, 403)
        other = get_user_model().objects.create(username="other")
        stranger.force_login(other)
        self.assertEqual(stranger.get(self.url).status_code, 403)
        register = StaffRegister.objects.create(shop=self.shop, name="Anna")
        training.accept_staff_rules(register)
        # Not even with the owner signed in on that phone
        for phone in (Client(), self.client):
            page = phone.get(f"/en/shops/register/p/{register.id}/?k={register.key}")
            self.assertEqual(page.status_code, 403)
            self.assertNotContains(page, "register-data", status_code=403)

    def test_the_pass_ends_it_and_the_practice_is_untouched(self):
        self.assertEqual(self.client.get(f"/en/shops/practice/{self.shop.id}/").status_code, 200)
        self.assertNotContains(self.client.get(f"/en/shops/practice/{self.shop.id}/"), "quick-start")
        self.pass_the_basics()
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("quick_start", self.data(page))
        self.assertNotContains(page, 'id="quick-start"')

    def test_without_it_the_lock_is_the_same_as_before(self):
        with configured(changed(quick_start=0)):
            self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_the_signet_and_a_gate_of_reminders_need_none(self):
        with override_settings(NETWORK="signet"):
            self.assertEqual(training.quick_start(self.shop, self.owner), 0)
            self.assertNotIn("quick_start", self.data(self.client.get(self.url)))
        with configured(changed(gate="reminder")):
            self.assertEqual(training.quick_start(self.shop, self.owner), 0)

    def test_the_phones_copy_stays_for_the_owner_only(self):
        gone = (settings.BASE_DIR / "static" / "register-sw-gone.js").read_bytes()
        worker = f"/en/shops/register/{self.shop.id}/sw.js"
        self.assertNotEqual(self.client.get(worker).content, gone)
        self.assertEqual(Client().get(worker).content, gone)

    def test_the_panel_says_so(self):
        page = self.client.get("/en/shops/")
        self.assertContains(page, "Open register (quick start)")
        self.assertContains(page, "Quick start.")
        self.assertNotContains(page, "waiting for the pass")


class Permissions(Base):
    URLS = ["/en/shops/training/", "/en/shops/training/wallet/", "/en/shops/training/review/"]

    def test_the_pages_are_for_a_signed_in_merchant(self):
        stranger = Client()
        for url in self.URLS:
            response = stranger.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertTrue(response["Location"].startswith("/en/shops/?next="), url)
        for url in (*self.URLS, "/en/shops/training/declaration/"):
            self.assertEqual(stranger.post(url, {"action": "quiz", "checked": "1"}).status_code, 302, url)
        self.assertEqual((ModuleResult.objects.count(), Declaration.objects.count()), (0, 0))

    def test_every_post_needs_the_csrf_token(self):
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.owner)
        for url in ("/en/shops/training/wallet/", "/en/shops/training/review/", "/en/shops/training/declaration/"):
            response = strict.post(url, {"action": "quiz", "q0": 0, "q1": 0, "step": "backup", "agree": "1", "checked": "1"})
            self.assertEqual(response.status_code, 403, url)
        self.assertEqual((ModuleResult.objects.count(), Declaration.objects.count()), (0, 0))

    def test_the_staff_is_a_visitor_here_as_everywhere(self):
        chief = get_user_model().objects.create_superuser("chief")
        Shop.objects.create(owner=chief, name="Chief's", lightning_address="b@ln.example.org")
        client = Client()
        admin_session(client)
        client.force_login(chief)
        session = client.session
        session["admin_password"] = True
        session.save()
        stranger = Client()
        for url in (*self.URLS, "/en/shops/training/terms/"):
            theirs, ours = client.get(url), stranger.get(url)
            self.assertEqual((theirs.status_code, theirs.get("Location")), (ours.status_code, ours.get("Location")), url)
        client.post("/en/shops/training/wallet/", {"action": "quiz", **RIGHT["wallet"]})
        client.post("/en/shops/training/declaration/", {"step": "backup", "agree": "1"})
        self.assertEqual((ModuleResult.objects.count(), Declaration.objects.count(), Trainee.objects.count()), (0, 0, 0))

    def test_another_account_has_its_own_path(self):
        self.finish()
        other = Client()
        other.force_login(get_user_model().objects.create(username="other"))
        self.assertFalse(training.standing(get_user_model().objects.get(username="other")).base)
        self.assertContains(other.get("/en/shops/training/"), "Towards the basic pass: 0 of")

    def test_a_module_cannot_be_declared_by_an_id_it_does_not_have(self):
        self.quiz("wallet")
        self.declare("wallet", ids=["paid-in-wallet", "../x"])
        self.assertEqual(Declaration.objects.count(), 0)


class Panel(Base):
    def test_the_card_shows_the_bar_the_badges_and_the_next_step(self):
        page = self.client.get("/en/shops/")
        self.assertContains(page, "Your merchant pass")
        # Before the pass the bar counts only the modules it needs, and no badge is shown yet
        self.assertContains(page, '<progress id="training-bar-value" value="0" max="3">')
        self.assertContains(page, "No basic pass yet.")
        self.assertContains(page, "Start the path")
        self.quiz("wallet")
        self.assertContains(self.client.get("/en/shops/"), "Go on with the path")

    def test_the_bar_moves_with_the_modules_and_the_badges(self):
        self.finish()
        training.declare_step(self.owner, self.shop, "practice")
        state = training.standing(self.owner)
        # After the pass the bar counts the steps towards the next badges, and goes away when all are earned
        self.assertEqual(state.progress, {"goal": "badges", "value": 1, "max": 3})
        page = self.client.get("/en/shops/")
        self.assertContains(page, '<progress id="training-bar-value" value="1" max="3">')
        self.assertContains(page, "Basic pass since")
        self.assertContains(page, "Trained merchant")
        for step in ("backup", "withdrawal"):
            training.declare_step(self.owner, self.shop, step)
        self.assertIsNone(training.standing(self.owner).progress)
        self.assertNotContains(self.client.get("/en/shops/"), "<progress")
        self.assertContains(page, "Your path and badges")

    def test_the_text_that_changed_is_told_with_a_link(self):
        self.finish()
        edited = json.loads(json.dumps(EXAMPLE))
        edited["modules"][1]["declarations"][0]["text"]["en"] = "Paid means paid in my wallet."
        with configured(edited):
            page = self.client.get("/en/shops/")
            self.assertContains(page, "Some texts changed.")
            self.assertContains(page, "/en/shops/training/paid/")

    def test_the_path_lists_the_modules_with_their_state(self):
        self.quiz("wallet")
        page = self.client.get("/en/shops/training/")
        self.assertContains(page, "Your wallet and your recovery words")
        self.assertContains(page, "To confirm")
        self.assertContains(page, "0 of 2 statements")
        self.assertContains(page, "Open the guide")
        self.assertContains(page, f"/en/shops/practice/{self.shop.id}/")

    def test_no_card_where_the_training_is_off(self):
        with configured(None):
            self.assertIsNone(training.panel(self.owner, [self.shop]))


class Export(Base):
    def setUp(self):
        super().setUp()
        self.finish()
        self.client = Client()
        admin_session(self.client)

    def export(self, **extra):
        ids = list(Declaration.objects.values_list("pk", flat=True))
        return self.client.post("/admin/shops/declaration/", {"action": "export_csv", "_selected_action": ids, **extra})

    def test_the_association_sees_and_exports_the_proof(self):
        listing = self.client.get("/admin/shops/declaration/")
        self.assertContains(listing, "wallet:words-mine")
        self.assertContains(listing, "Bar")
        response = self.export()
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("attachment", response["Content-Disposition"])
        rows = list(csv.reader(io.StringIO(response.content.decode())))
        self.assertEqual(rows[0], ["made", "who", "shop", "shop_id", "register_id", "statement", "version", "language", "text"])
        self.assertEqual(len(rows) - 1, Declaration.objects.count())
        words = next(row for row in rows if row[5] == "wallet:words-mine")
        self.assertEqual((words[1], words[2], words[3]), ("o***", "Bar", str(self.shop.id)))
        self.assertEqual(len(words[6]), 64)
        self.assertTrue(words[8].startswith("The recovery words of my wallet"))
        self.assertNotIn("ip", [name.lower() for name in rows[0]])

    def test_the_export_masks_the_name_of_who_declared(self):
        Declaration.objects.create(who="Mario Rossi", shop_name="Bar", key="y", version="v" * 64, text="t")
        content = self.export().content.decode()
        self.assertNotIn("Mario", content)
        self.assertNotIn("Rossi", content)
        self.assertEqual(next(row for row in csv.reader(io.StringIO(content)) if row[5] == "y")[1], "M***")

    def test_a_text_a_spreadsheet_would_read_as_a_formula_is_defused(self):
        Declaration.objects.create(who="=HYPERLINK(1)", shop_name="+cmd", key="x", version="v" * 64, text="@SUM(1)")
        rows = list(csv.reader(io.StringIO(self.export().content.decode())))
        bad = next(row for row in rows if row[5] == "x")
        self.assertEqual((bad[1], bad[2], bad[8]), ("'=***", "'+cmd", "'@SUM(1)"))

    def test_nobody_edits_or_deletes_the_proof_from_the_admin(self):
        row = Declaration.objects.first()
        self.assertEqual(self.client.get(f"/admin/shops/declaration/{row.pk}/change/").status_code, 200)
        self.assertEqual(self.client.post(f"/admin/shops/declaration/{row.pk}/change/", {"text": "changed"}).status_code, 403)
        self.assertEqual(self.client.get(f"/admin/shops/declaration/{row.pk}/delete/").status_code, 403)
        self.assertEqual(self.client.get("/admin/shops/declaration/add/").status_code, 403)
        self.assertEqual(Declaration.objects.get(pk=row.pk).text, row.text)

    def test_a_merchant_does_not_reach_the_admin_list(self):
        merchant = Client()
        merchant.force_login(self.owner)
        self.assertEqual(merchant.get("/admin/shops/declaration/").status_code, 302)

    def test_the_trainees_are_listed_read_only(self):
        self.assertContains(self.client.get("/admin/shops/trainee/"), "owner")
        self.assertEqual(self.client.get("/admin/shops/trainee/add/").status_code, 403)


@override_settings(NETWORK="mainnet")
class Diagnosis(Base):
    def run_check(self, owner_view=False):
        from . import diagnosis

        return [finding for finding in diagnosis.diagnose(self.shop, only={diagnosis.check_training}, owner_view=owner_view)]

    def test_the_helper_is_told_the_register_waits_for_the_pass_and_what_to_say(self):
        found = self.run_check()
        self.assertEqual([(finding.code, finding.level) for finding in found], [("training_locked", "warning")])
        self.assertIn("opens soon", found[0].do)
        self.assertIn("no basic pass; since=-", found[0].detail)

    def test_the_days_before_the_start_and_the_pass_are_told_too(self):
        tomorrow = (business_day.shift_day() + timedelta(days=1)).isoformat()
        with configured(changed(since=tomorrow)):
            found = self.run_check()
        self.assertEqual([finding.code for finding in found], ["training_missing"])
        self.assertIn(f"since={tomorrow}", found[0].detail)
        self.pass_the_basics()
        self.assertEqual([finding.code for finding in self.run_check()], ["training_done"])

    def test_nothing_when_the_training_is_off_and_nothing_in_the_owners_own_view(self):
        self.assertEqual(self.run_check(owner_view=True), [])
        with configured(None):
            self.assertEqual(self.run_check(), [])


@skipUnless(connection.vendor == "postgresql", "the foreign keys follow the models in the database on PostgreSQL only")
class DeletedByThePreviousImage(TestCase):
    """The previous image deletes an account or a shop the way it knows; the rows of the training's tables must not stop it."""

    def test_an_account_and_a_shop_with_training_rows_can_be_deleted_in_the_database(self):
        owner = get_user_model().objects.create(username="gone")
        shop = Shop.objects.create(owner=owner, name="Gone")
        Trainee.objects.create(user=owner)
        ModuleResult.objects.create(user=owner, module="wallet")
        row = Declaration.objects.create(user=owner, shop=shop, who="gone", key="k", version="v" * 64, text="t")
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cursor.execute("DELETE FROM shops_shop WHERE id = %s", [shop.pk])
            cursor.execute("DELETE FROM auth_user WHERE id = %s", [owner.pk])
        row.refresh_from_db()
        self.assertEqual((row.user_id, row.shop_id), (None, None))
        self.assertEqual((Trainee.objects.count(), ModuleResult.objects.count()), (0, 0))
