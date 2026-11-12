"""The error paths of the training (shops/training.py): a community.yml with mistakes in it must leave out what cannot be
used and say so, a database that fails must never lock a till, and a review form written by hand must not pass."""
from datetime import date, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.db import DatabaseError, IntegrityError
from django.test import SimpleTestCase, TestCase, override_settings

from . import training
from .models import Shop, Trainee
from .test_training import Base, configured


def told(function, *args):
    problems = []
    result = function(*args, problems.append)
    return result, problems


class Questions(SimpleTestCase):
    def test_a_question_needs_text_options_an_answer_and_an_explanation(self):
        good = {"text": "Who holds the keys?", "options": ["you", "the bank"], "answer": 1, "explain": "You do."}
        question, problems = told(lambda warn: training.parse_question(good, "q", warn))
        self.assertEqual((question.answer, problems), (0, []))
        for broken, saying in (
                ("not a dict", "needs `text`"), ({**good, "text": ""}, "needs `text`"), ({**good, "explain": None}, "needs `text`"),
                ({**good, "options": "ab"}, "2 to"), ({**good, "options": ["only one"]}, "2 to"),
                ({**good, "options": ["a", 2]}, "2 to"), ({**good, "options": ["x"] * 7}, "2 to"),
                ({**good, "answer": 0}, "from 1 to 2"), ({**good, "answer": 3}, "from 1 to 2"),
                ({**good, "answer": True}, "from 1 to 2"), ({**good, "answer": "1"}, "from 1 to 2")):
            question, problems = told(lambda warn: training.parse_question(broken, "q", warn))
            self.assertIsNone(question, broken)
            self.assertIn(saying, problems[0], broken)


class Declarations(SimpleTestCase):
    def test_a_declaration_needs_a_new_id_and_a_text_and_there_are_not_too_many(self):
        entries = [{"id": "one", "text": "I read it"}, {"id": "one", "text": "again"}, {"id": "Bad Id", "text": "x"},
                   {"text": "no id"}, "not a dict", {"id": "two", "text": ""}, {"id": "three", "text": "ok"}]
        found, problems = told(lambda warn: training.parse_statements("wallet", entries, "module", warn))
        self.assertEqual([item.id for item in found], ["one", "three"])
        self.assertEqual(len(problems), 5)
        self.assertTrue(all("it needs a new `id`" in problem for problem in problems))
        many = [{"id": f"d{n}", "text": "x"} for n in range(training.MAX_STATEMENTS + 3)]
        found, problems = told(lambda warn: training.parse_statements("wallet", many, "module", warn))
        self.assertEqual(len(found), training.MAX_STATEMENTS)
        self.assertEqual(problems, [])

    def test_declarations_that_are_not_a_list_are_none(self):
        self.assertEqual(training.parse_statements("wallet", {"id": "x"}, "module", print), ())
        self.assertEqual(training.parse_statements("wallet", None, "module", print), ())


class Modules(SimpleTestCase):
    def parse(self, entry, taken=()):
        return told(lambda warn: training.parse_module(entry, 1, set(taken), warn))

    def test_a_module_that_cannot_stand_is_left_out_and_told(self):
        question = {"text": "T", "options": ["a", "b"], "answer": 1, "explain": "E"}
        for entry in ("text", {"id": 5, "title": "x"}, {"id": "taken", "title": "x", "questions": [question]},
                      {"id": "ok", "title": "", "questions": [question]}, {"id": "ok", "title": "T"}):
            module, problems = self.parse(entry, taken=["taken"])
            self.assertIsNone(module, entry)
            self.assertEqual(len(problems), 1, entry)

    def test_a_page_that_is_not_a_page_is_dropped_but_the_module_stays(self):
        question = {"text": "T", "options": ["a", "b"], "answer": 1, "explain": "E"}
        module, problems = self.parse({"id": "ok", "title": "T", "page": "http://elsewhere.example/", "questions": [question]})
        self.assertIsNotNone(module)
        self.assertIsNone(module.page)
        self.assertIn("`page`", problems[0])

    def test_base_is_a_yes_or_no_and_anything_else_is_yes(self):
        question = {"text": "T", "options": ["a", "b"], "answer": 1, "explain": "E"}
        for written, read in ((False, False), (True, True), ("no", True), (0, True), (None, True)):
            module, _ = self.parse({"id": "ok", "title": "T", "base": written, "questions": [question]})
            self.assertIs(module.base, read, written)


class Block(SimpleTestCase):
    def problems(self, **changes):
        block = {"modules": [{"id": "one", "title": "One", "declarations": [{"id": "a", "text": "I read it"}]}], **changes}
        with configured(block):
            return training.problems()

    def test_a_block_that_is_not_a_list_of_fields_is_off_and_told(self):
        with configured("yes"):
            self.assertIsNone(training.parse())
            self.assertEqual(len(training.problems()), 1)
        with configured(False):
            self.assertIsNone(training.parse())
            self.assertEqual(training.problems(), [])

    def test_a_since_that_is_not_a_day_makes_the_training_wait_and_a_datetime_is_not_a_day(self):
        """A day that cannot be read must not mean "from now": the lock would fall at once because of a slip of the keyboard."""
        for since in ("soon", "2026-13-45", "01/12/2026", 20261201, datetime(2026, 12, 1, 9, 0)):
            self.assertTrue(any("training.since" in problem for problem in self.problems(since=since)), since)
            with configured({"modules": [{"id": "one", "title": "One", "declarations": [{"id": "a", "text": "x"}]}],
                             "since": since}):
                self.assertFalse(training.in_force(training.parse()), since)
        with configured({"modules": [{"id": "one", "title": "One", "declarations": [{"id": "a", "text": "x"}]}],
                         "since": "2026-12-01"}):
            self.assertEqual(training.parse().since, date(2026, 12, 1))
            self.assertEqual(training.problems(), [])

    def test_a_guide_that_is_not_an_address_of_the_community_is_left_out(self):
        block = {"modules": [{"id": "one", "title": "One", "declarations": [{"id": "a", "text": "x"}]}],
                 "guides": {"wallet": "http://elsewhere.example/", "ok": "about/"}}
        with configured(block):
            cfg = training.parse()
            self.assertEqual(set(cfg.guides), {"ok"})
            self.assertTrue(any("training.guides.wallet" in problem for problem in training.problems()))
            self.assertEqual(training.guide_url(cfg, "missing"), "")
            self.assertTrue(training.guide_url(cfg, "ok", "en"))
        with configured({"modules": block["modules"], "guides": "not a map"}):
            self.assertEqual(training.parse().guides, {})

    def test_a_guide_that_stopped_being_valid_gives_no_address(self):
        cfg = training.Config("unlock", 12, 30, None, True, {"old": "http://elsewhere.example/"}, (), None, ())
        self.assertEqual(training.guide_url(cfg, "old", "en"), "")

    def test_the_terms_page_that_is_not_an_address_of_the_community_is_no_link(self):
        with configured({"modules": []}, merchant_terms="http://elsewhere.example/"):
            self.assertEqual(training.terms_link("en"), "")
        with configured({"modules": []}, merchant_terms={"path": ""}):
            self.assertEqual(training.terms_link("en"), "")
        with configured({"modules": []}, merchant_terms={"path": "about/"}):
            self.assertTrue(training.terms_link("en"))

    def test_a_module_whose_page_went_bad_has_no_address(self):
        module = training.Module("m", "M", "http://elsewhere.example/", True, (), ())
        self.assertEqual(training.page_url(module, "en"), "")
        self.assertEqual(training.page_url(training.Module("m", "M", None, True, (), ()), "en"), "")


class Accounts(TestCase):
    def test_two_requests_creating_the_same_trainee_end_with_one(self):
        user = get_user_model().objects.create(username="owner")
        existing = Trainee.objects.create(user=user)
        with patch.object(Trainee.objects, "get_or_create", side_effect=IntegrityError("duplicate")):
            self.assertEqual(training.get_trainee(user), existing)

    def test_the_trainee_of_nobody_is_none(self):
        self.assertIsNone(training.trainee_of(None))
        self.assertIsNone(training.trainee_of(AnonymousUser()))

    def test_a_declaration_lost_to_a_race_is_not_new(self):
        user = get_user_model().objects.create(username="owner")
        with patch("shops.training.Declaration.objects.get_or_create", side_effect=IntegrityError("duplicate")):
            self.assertFalse(training.record(user, None, "wallet:a", 1, "text"))


@override_settings(NETWORK="mainnet")
class Locking(Base):
    def test_a_database_that_fails_never_locks_a_till(self):
        self.assertTrue(training.locked(self.shop))
        with patch("shops.training.locked_for", side_effect=DatabaseError("down")):
            self.assertFalse(training.locked(self.shop))

    def test_a_shop_without_an_owner_or_with_the_training_off_is_never_locked(self):
        orphan = Shop.objects.create(name="Orphan", lightning_address="o@ln.example.org")
        self.assertFalse(training.locked(orphan))
        with configured(None):
            self.assertFalse(training.locked(self.shop))


class Review(Base):
    def test_a_review_made_by_hand_passes_only_with_every_question_answered_right(self):
        self.pass_the_basics()
        cfg = training.parse()
        picks = training.review_picks(cfg)
        right = {}
        for module in training.all_modules(cfg):
            if module.questions:
                right[f"pick-{module.id}"] = picks[module.id]
                right[f"a-{module.id}"] = str(module.questions[picks[module.id]].answer)
        marks, passed = training.answer_review(self.owner, cfg, right)
        self.assertTrue(passed, marks)
        # Nothing, a pick that is not a number or out of range: no marks, no pass
        for post in ({}, {**right, "pick-wallet": "x"}, {**right, "pick-wallet": "99"}, {**right, "pick-wallet": "-1"}):
            marks, passed = training.answer_review(self.owner, cfg, post)
            self.assertFalse(passed, post)
        self.assertEqual(training.answer_review(self.owner, cfg, {}), ([], False))

    def test_settling_with_the_training_off_earns_nothing(self):
        with configured(None):
            self.assertFalse(training.settle(self.owner))
        self.assertFalse(Trainee.objects.filter(user=self.owner, base_earned__isnull=False).exists())
        with configured(None):
            self.assertIsNone(training.standing(self.owner))
