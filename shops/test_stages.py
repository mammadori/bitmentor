"""The owner's panel by stages (shops/stages.py, templates/shops/stages.html, static/stages.js): on top each shop's
current stage, the rest of the panel closed below; off, the panel exactly as without stages."""

import json
import re
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.template import engines
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone, translation

from config.community import community

from . import stages
from .models import DEMO_PREFIX, Profile, Shop
from .test_scripts import assert_talks_to_nobody, node_or_skip
from .test_training import EXAMPLE_WITHOUT_QUICK, changed, configured

PANEL = "/en/shops/"
# A fixed moment, at noon: the stages count days of the community's calendar (stages.days_since), and "thirty days
# before now" taken near midnight, in the weeks after the clocks change, is twenty-nine of those
NOW = datetime(2026, 6, 15, 12, tzinfo=UTC)
NUMBERS = dict(stages.DEFAULTS)
FIXTURE = Path(__file__).parent / "testdata" / "panel_without_stages.html"


def features_off(*names):
    return patch("config.features.community", new=lambda: {**community(), "features": {name: False for name in names}})


def numbers_in(block):
    return patch("shops.stages.community", new=lambda: {**community(), "stages": block})


def fake(**fields):
    """A shop as the panel sees it, with the attributes the view sets."""
    shop = {
        "lightning_address": "a@wallet.example",
        "external_id": "",
        "has_register": True,
        "closed": False,
        "first_sale": None,
        "needs_approval": False,
        "listed": True,
        "suspended": False,
        "latitude": 41.9,
        "longitude": 12.5,
        "sign_printed": None,
        "lightning_watch": None,
        "till_locked": False,
        "till_quick": 0,
        "needs_wallet": False,
        "waits_for_wallet": False,
    }
    shop.update(fields)
    return SimpleNamespace(**shop)


def current(shop, training_on=True, has_pass=False, numbers=None):
    return stages.current(shop, now=NOW, numbers=numbers or NUMBERS, training_on=training_on, has_pass=has_pass)


def key(shop, **kwargs):
    found = current(shop, **kwargs)
    return found and found["key"]


class Conditions(SimpleTestCase):
    def test_each_stage(self):
        sold = NOW - timedelta(days=1)
        self.assertEqual(key(fake(has_register=False, needs_wallet=True)), stages.WALLET)
        self.assertEqual(key(fake(lightning_watch={"broken": True})), stages.WALLET)
        self.assertEqual(key(fake()), stages.TRIAL)
        self.assertEqual(key(fake(first_sale=sold, till_locked=True)), stages.PASS)
        self.assertEqual(key(fake(first_sale=sold)), stages.MAP)
        self.assertEqual(key(fake(first_sale=sold, sign_printed=sold)), stages.TAKINGS)
        self.assertEqual(key(fake(first_sale=NOW - timedelta(days=7), sign_printed=sold)), stages.ROUTINE)
        self.assertEqual(key(fake(first_sale=NOW - timedelta(days=30), sign_printed=sold)), stages.GAME)

    def test_the_days_are_the_ones_of_the_calendar(self):
        # 23:50 in Rome on 19 November; thirty times 24 hours before it is 00:50 on 21 October there (summer time)
        late = datetime(2026, 11, 19, 22, 50, tzinfo=UTC)
        with override_settings(TIME_ZONE="Europe/Rome"):
            self.assertEqual(stages.days_since(late - timedelta(days=30), late), 29)
            self.assertEqual(stages.days_since(datetime(2026, 10, 20, 21, 59, tzinfo=UTC), late), 30)

    def test_nothing_to_walk(self):
        self.assertIsNone(current(fake(has_register=False)))
        self.assertIsNone(current(fake(closed=True, first_sale=NOW)))
        # A historic shop has no stage, also without a register it could still connect
        self.assertIsNone(current(fake(closed=True, has_register=False, needs_wallet=True)))

    def test_a_locked_till_has_only_the_pass_from_the_start(self):
        self.assertEqual(key(fake(till_locked=True)), stages.PASS)
        self.assertEqual(key(fake(till_locked=True), training_on=False), stages.TRIAL)

    def test_the_trial_and_the_map_carry_their_tick(self):
        self.assertEqual(current(fake())["tick"], {"key": "sale", "state": "todo"})
        self.assertEqual(current(fake(first_sale=NOW))["tick"], {"key": "sign", "state": "todo"})

    def test_a_waiting_or_hidden_shop_has_no_map_stage(self):
        for fields in ({"needs_approval": True}, {"listed": False}, {"suspended": True}, {"latitude": None}):
            with self.subTest(fields):
                self.assertEqual(key(fake(first_sale=NOW, **fields)), stages.TAKINGS)

    def test_the_map_follows_the_public_lists_whatever_the_switch(self):
        # approved_on decides, as in directory.public_shops: a shop still waiting is not on the map with the switch off
        with features_off("approval"):
            self.assertEqual(key(fake(first_sale=NOW, needs_approval=True)), stages.TAKINGS)
            self.assertEqual(key(fake(first_sale=NOW)), stages.MAP)

    def test_without_the_training_no_pass_and_no_game(self):
        old = NOW - timedelta(days=90)
        self.assertEqual(
            key(fake(first_sale=old, sign_printed=old, till_locked=True), training_on=False), stages.ROUTINE
        )
        takings = current(fake(first_sale=NOW, sign_printed=NOW, till_quick=2000), training_on=False)
        self.assertFalse(takings["propose_pass"])
        self.assertEqual([later["key"] for later in takings["promote"]], [stages.ROUTINE])

    def test_the_pass_is_proposed_from_the_takings(self):
        found = current(fake(first_sale=NOW, sign_printed=NOW, till_quick=2000))
        self.assertTrue(found["propose_pass"])
        self.assertEqual(found["quick"], 2000)
        self.assertFalse(current(fake(first_sale=NOW, sign_printed=NOW), has_pass=True)["propose_pass"])

    def test_the_phone_may_open_the_later_stages(self):
        found = current(fake(first_sale=NOW, sign_printed=NOW))
        self.assertEqual(
            [(later["key"], later["sales"]) for later in found["promote"]], [(stages.ROUTINE, 5), (stages.GAME, 20)]
        )
        routine = current(fake(first_sale=NOW - timedelta(days=8), sign_printed=NOW))
        self.assertEqual([later["key"] for later in routine["promote"]], [stages.GAME])
        # Nothing to promote over the steps that need doing first
        self.assertNotIn("promote", current(fake()))
        self.assertNotIn("promote", current(fake(first_sale=NOW)))

    def test_the_days_come_from_the_community(self):
        numbers = {**NUMBERS, "routine_days": 2, "game_days": 3}
        self.assertEqual(
            key(fake(first_sale=NOW - timedelta(days=2), sign_printed=NOW), numbers=numbers), stages.ROUTINE
        )
        self.assertEqual(key(fake(first_sale=NOW - timedelta(days=3), sign_printed=NOW), numbers=numbers), stages.GAME)

    def test_the_rest_has_only_what_exists(self):
        shop = fake(break_eligible=True, osm_enabled=True)
        self.assertEqual(stages.rest(shop, onchain=True, nostr=True), list(stages.REST_LINES))
        self.assertEqual(stages.rest(fake(), onchain=False, nostr=False), ["discount", "address"])
        self.assertEqual(
            stages.rest(fake(has_register=False, lightning_address=""), onchain=True, nostr=True), ["discount"]
        )

    def test_the_address_change_only_where_its_page_opens(self):
        for fields in ({"suspended": True}, {"external_id": f"{DEMO_PREFIX}1"}):
            with self.subTest(fields):
                self.assertNotIn("address", stages.rest(fake(**fields), onchain=False, nostr=False))
        self.assertIn("address", stages.rest(fake(), onchain=False, nostr=False))


class Numbers(SimpleTestCase):
    def test_defaults(self):
        with numbers_in(None):
            self.assertEqual(
                stages.numbers(), {"routine_days": 7, "routine_sales": 5, "game_days": 30, "game_sales": 20}
            )
            self.assertEqual(stages.problems(), [])

    def test_the_community_s_numbers(self):
        with numbers_in({"routine_days": 3, "game_sales": 50}):
            self.assertEqual(stages.numbers()["routine_days"], 3)
            self.assertEqual(stages.numbers()["game_sales"], 50)
            self.assertEqual(stages.problems(), [])

    def test_a_wrong_value_counts_as_the_default_and_check_says_so(self):
        for value in (0, -1, 366, "7", True, 1.5, None):
            with self.subTest(value), numbers_in({"routine_days": value}):
                self.assertEqual(stages.number("routine_days"), 7)
                self.assertTrue(any("stages.routine_days" in line for line in stages.problems()))
        with numbers_in({"game_sales": 1001}):
            self.assertEqual(stages.number("game_sales"), 20)
        with numbers_in({"weeks": 2}):
            self.assertIn("`stages.weeks` is not a setting", stages.problems()[0])
        with numbers_in([1]):
            self.assertEqual(len(stages.problems()), 1)
            self.assertEqual(stages.numbers(), NUMBERS)

    def test_the_example_documents_the_block(self):
        import yaml

        text = (Path(settings.BASE_DIR) / "examples" / "community" / "community.yml").read_text()
        block = text[text.index("# stages:") :].split("\n# The stops")[0]
        found = yaml.safe_load("\n".join(line[2:] for line in block.split("\n") if line.startswith("# ")))
        self.assertEqual(found, {"stages": stages.DEFAULTS})

    def test_manage_py_check_warns(self):
        from .apps import wrong_stages

        with numbers_in({"routine_days": "x"}):
            self.assertEqual([warning.id for warning in wrong_stages(None)], ["shops.W015"])


@override_settings(NETWORK="signet")
class Panel(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create(username="owner")
        Profile.objects.create(user=self.owner, email="mario@example.org")
        self.client.force_login(self.owner)
        self.shop = Shop.objects.create(
            name="Bar", owner=self.owner, latitude=41.9, longitude=12.5, lightning_address="mario@wallet.example"
        )

    def page(self, url=PANEL):
        return self.client.get(url).content.decode()

    def split(self, page):
        """What is above the fold, and what is inside it."""
        top, _sep, rest = page.partition('<details class="everything" id="everything">')
        self.assertTrue(rest, "no fold")
        return top, rest.partition('<details class="account"')[0]

    def test_the_current_stage_on_top_and_everything_closed_below(self):
        top, fold = self.split(self.page())
        self.assertIn(f'id="stage-{self.shop.id}"', top)
        self.assertIn('data-stage="trial"', top)
        self.assertRegex(top, r'<h4 class="stage-step">Try the register</h4>')
        self.assertIn(f'href="/en/shops/register/{self.shop.id}/"', top)
        self.assertIn("<summary>Everything, if you want</summary>", fold)
        # Today's panel is all there, inside the fold
        for piece in (
            f'id="first-day-{self.shop.id}"',
            f'id="handover-{self.shop.id}"',
            'class="panel-row"',
            f"/en/shops/sign/{self.shop.id}/",
            "Add a shop",
        ):
            self.assertIn(piece, fold)
        # The staff registers are not folded a second time inside the fold
        self.assertIn(f'<details class="handover" id="handover-{self.shop.id}" open>', fold)
        # in the image the static files carry a fingerprint in their name (stages.<hash>.js)
        self.assertRegex(top, r'src="/static/stages(\.[0-9a-f]+)?\.js"')

    def test_each_stage_in_the_page(self):
        Shop.objects.filter(pk=self.shop.pk).update(first_sale=timezone.now())
        self.assertIn('data-stage="map"', self.split(self.page())[0])
        Shop.objects.filter(pk=self.shop.pk).update(sign_printed=timezone.now())
        top = self.split(self.page())[0]
        self.assertIn('data-stage="takings"', top)
        self.assertIn('data-stage="routine" data-stage-sales="5" hidden', top)
        Shop.objects.filter(pk=self.shop.pk).update(first_sale=timezone.now() - timedelta(days=8))
        self.assertIn('data-stage="routine" data-stage-now', self.split(self.page())[0])

    def test_the_quick_start_without_a_currency_counts_as_the_register_does(self):
        from .register import register_data

        Shop.objects.filter(pk=self.shop.pk).update(first_sale=timezone.now(), sign_printed=timezone.now())
        without = {key: value for key, value in community().items() if key != "currency"}
        with (
            configured(changed(quick_start=20)),
            override_settings(NETWORK="mainnet"),
            patch("shops.stages.community", new=lambda: without),
            patch("shops.register.community", new=lambda: without),
        ):
            top = self.split(self.page())[0]
            shop = Shop.objects.get(pk=self.shop.pk)
            used = register_data(shop)["currency"]
        self.assertEqual(used, "EUR")
        self.assertIn(f'data-quick-cents="2000" data-quick-currency="{used}"', top)
        self.assertIn(f"up to 20.00 {used} in total", top)

    def test_with_the_training_the_pass_and_the_game(self):
        Shop.objects.filter(pk=self.shop.pk).update(first_sale=timezone.now(), sign_printed=timezone.now())
        with configured(changed(quick_start=20)), override_settings(NETWORK="mainnet"):
            top = self.split(self.page())[0]
            self.assertIn('data-stage="takings"', top)
            self.assertIn("the register takes payments up to 20.00 EUR in total", top)
            self.assertIn("it adds up the payments taken on the phone", top)
            self.assertIn(f'data-quick-shop="{self.shop.id}" data-quick-cents="2000" data-quick-currency="EUR"', top)
            self.assertIn("Meanwhile, the basic pass", top)
            self.assertIn("Open register (quick start)", top)
            self.assertIn('data-stage="game" data-stage-sales="20" hidden', top)
        with configured(EXAMPLE_WITHOUT_QUICK), override_settings(NETWORK="mainnet"):
            top = self.split(self.page())[0]
            self.assertIn('data-stage="pass" data-stage-now', top)
            self.assertIn("The register waits for your basic pass: until you have it, it takes no real payments.", top)
            self.assertNotIn(f'href="/en/shops/register/{self.shop.id}/"', top)
            # A new shop too: the pass before the trial, never «Open register» on a till that refuses
            Shop.objects.filter(pk=self.shop.pk).update(first_sale=None)
            top = self.split(self.page())[0]
            self.assertIn('data-stage="pass" data-stage-now', top)
            self.assertNotIn("Try the register", top)
        Shop.objects.filter(pk=self.shop.pk).update(first_sale=timezone.now() - timedelta(days=31))
        with configured(EXAMPLE_WITHOUT_QUICK):
            self.assertIn('data-stage="game" data-stage-now', self.split(self.page())[0])

    def test_the_pass_card_is_in_the_fold_in_every_state(self):
        for block, network in (
            (changed(quick_start=20), "mainnet"),
            (EXAMPLE_WITHOUT_QUICK, "mainnet"),
            (EXAMPLE_WITHOUT_QUICK, "signet"),
        ):
            with (
                self.subTest(block.get("quick_start"), network=network),
                configured(block),
                override_settings(NETWORK=network),
            ):
                top, fold = self.split(self.page())
                self.assertNotIn('id="training"', top)
                self.assertIn('id="training"', fold)
        # With the stages off the card stays where it was: on top while it holds the till
        with configured(EXAMPLE_WITHOUT_QUICK), override_settings(NETWORK="mainnet"), features_off("stages"):
            page = self.page()
            self.assertLess(page.index('id="training"'), page.index('class="panel-list"'))

    def test_a_shop_still_waiting_with_the_approval_off_has_no_map_stage(self):
        Shop.objects.filter(pk=self.shop.pk).update(first_sale=timezone.now(), approved_on=None)
        with features_off("approval"):
            top = self.split(self.page())[0]
        self.assertIn('data-stage="takings"', top)
        self.assertIn(f'id="approval-waits-{self.shop.id}"', top)

    def test_a_historic_shop_without_a_register_has_no_stage(self):
        old = Shop.objects.create(
            name="Old", owner=self.owner, closed_on=timezone.localdate() - timedelta(days=3), awaiting_wallet=True
        )
        top = self.split(self.page())[0]
        card = top[top.index(f'id="stage-{old.id}"') :]
        card = card[: card.index("</article>")]
        self.assertNotIn("data-stage-now", card)
        self.assertNotIn("Connect your wallet", card)

    def test_the_lightning_notes_once_on_top(self):
        from django.contrib.messages.storage.base import Message

        Shop.objects.filter(pk=self.shop.pk).update(first_sale=timezone.now(), sign_printed=timezone.now())
        note = Message(20, "The address answered just now.", extra_tags=f"lightning lightning-{self.shop.id}")
        with patch("shops.views.messages.get_messages", return_value=[note]):
            response = self.client.get(PANEL)
        page = response.content.decode()
        self.assertIsNotNone(response.context["shops"][0].journey)
        self.assertEqual(page.count("The address answered just now."), 1)
        self.assertIn("The address answered just now.", self.split(page)[0])

    def test_a_shop_waiting_for_its_wallet_shows_its_notice_as_the_step(self):
        waiting = Shop.objects.create(owner=self.owner, name="New", awaiting_wallet=True)
        top, fold = self.split(self.page())
        self.assertIn(f'id="wallet-{waiting.id}"', top)
        self.assertNotIn(f'id="wallet-{waiting.id}"', fold)

    def test_several_shops_each_with_its_stage(self):
        other = Shop.objects.create(
            name="Café",
            owner=self.owner,
            latitude=41.9,
            longitude=12.5,
            lightning_address="cafe@wallet.example",
            first_sale=timezone.now(),
        )
        top = self.split(self.page())[0]
        self.assertIn(f'id="stage-{self.shop.id}"', top)
        self.assertIn(f'id="stage-{other.id}"', top)
        self.assertEqual(top.count("data-stage-now"), 2)

    def test_the_safety_notices_stay_outside_the_fold(self):
        Shop.objects.filter(pk=self.shop.pk).update(suspended=True)
        broken = Shop.objects.create(
            name="Broken",
            owner=self.owner,
            lightning_address="b@wallet.example",
            lightning_status="no_answer",
            lightning_checked_address="b@wallet.example",
            lightning_since=timezone.now(),
            lightning_checked=timezone.now(),
        )
        waiting = Shop.objects.create(
            name="Waiting", owner=self.owner, lightning_address="w@wallet.example", approved_on=None
        )
        change = {"when": timezone.now(), "before": "a@x.example", "after": "b@x.example", "pending": False}
        account = {"when": timezone.now(), "what": ["The recovery email"], "advice": "Write to us."}
        with (
            patch("shops.views.address_change.panel", return_value=change),
            patch("shops.views.guard.notice", return_value=account),
        ):
            top, fold = self.split(self.page())
        for piece in (
            "Suspended: not on the map or in the lists.",
            f'id="lightning-{broken.id}"',
            f'id="address-changed-{self.shop.id}"',
            'id="account-changed"',
            f'id="approval-waits-{waiting.id}"',
            f'id="help-{self.shop.id}"',
        ):
            self.assertIn(piece, top)
            self.assertNotIn(piece, fold)

    def test_works_without_javascript(self):
        top = self.split(self.page())[0]
        form = re.search(r'<form method="post" action="/en/shops/first-day/[^"]+/" class="step-tick">.*?</form>', top)
        self.assertIn('name="back" value="stage"', form[0])
        self.assertIn("csrfmiddlewaretoken", form[0])
        self.assertNotIn("<script>", top)
        response = self.client.post(
            f"/en/shops/first-day/{self.shop.id}/", {"step": "sale", "done": "1", "back": "stage"}
        )
        self.assertRedirects(response, f"/en/shops/#stage-{self.shop.id}", fetch_redirect_response=False)

    def test_the_sign_ticked_on_the_stage_ticks_the_card_too(self):
        """The stage shows the sign and the card as one thing to print; the first day has them apart."""
        url = f"/en/shops/first-day/{self.shop.id}/"
        self.client.post(url, {"step": "sign", "done": "1", "back": "stage"})
        self.shop.refresh_from_db()
        self.assertIsNotNone(self.shop.card_printed)
        self.assertEqual(self.shop.card_printed, self.shop.sign_printed)
        # From the first day each is its own: ticking the sign there leaves the card as it is
        Shop.objects.filter(pk=self.shop.pk).update(card_printed=None, sign_printed=None)
        self.client.post(url, {"step": "sign", "done": "1"})
        self.shop.refresh_from_db()
        self.assertIsNone(self.shop.card_printed)

    def test_the_rest_is_closed_under_each_shop(self):
        top = self.split(self.page())[0]
        self.assertIn(f'<details class="stage-rest" id="stage-rest-{self.shop.id}">', top)
        self.assertIn(f'href="/en/shops/lightning-address/{self.shop.id}/"', top)
        self.assertIn(f'href="/en/shops/edit/{self.shop.id}/"', top)

    def test_no_shops_no_stages(self):
        Shop.objects.all().delete()
        page = self.page()
        self.assertNotIn('id="stages"', page)
        self.assertNotIn('id="everything"', page)

    def test_in_italian_and_german(self):
        page = self.page("/it/shops/")
        self.assertIn("Tutto, se vuoi", page)
        self.assertIn("Adesso: La prova", page)
        # German, for a community that has it, with «Sie»
        with translation.override("de"):
            self.assertEqual(translation.gettext("Everything, if you want"), "Alles, wenn Sie möchten")
            self.assertEqual(str(stages.TITLES[stages.TRIAL]), "Die Probe")

    def render_before(self, response):
        """The same context through the panel's template as it was without stages (testdata/panel_without_stages.html)."""
        context = response.context[0].flatten()
        return engines["django"].from_string(FIXTURE.read_text()).render(context, response.wsgi_request)

    def test_off_the_panel_is_byte_for_byte_the_one_without_stages(self):
        """testdata/panel_without_stages.html is the panel before the stages (e0b4c4cf), plus exactly the lines later
        commits changed on purpose, all in «To print»: the sign line that says the card for customers waits too and the
        card's button hidden while the shop waits (ad605163), and the sign line of a shop set aside (38dd10a0); and the
        staff registers under one name, open at once when the panel has stages, and the button of the other places."""
        Shop.objects.create(name="Old", owner=self.owner, closed_on=timezone.localdate() - timedelta(days=3))
        Shop.objects.create(owner=self.owner, name="New", awaiting_wallet=True)
        Shop.objects.create(owner=self.owner, name="Waiting", lightning_address="w@wallet.example", approved_on=None)
        Shop.objects.create(
            owner=self.owner,
            name="Aside",
            lightning_address="a@wallet.example",
            approved_on=None,
            set_aside_on=timezone.now(),
            set_aside_by="staff",
        )
        Shop.objects.filter(pk=self.shop.pk).update(first_sale=timezone.now(), suspended=True)
        tokens = re.compile(r'name="csrfmiddlewaretoken" value="[^"]+"')
        with features_off("stages"):
            for url in (PANEL, f"{PANEL}?saved"):
                response = self.client.get(url)
                now = tokens.sub("", response.content.decode())
                self.assertNotIn('id="stages"', now)
                self.assertIn("come once the community has approved the shop", now)
                self.assertIn("and the community set this one aside", now)
                self.assertEqual(now, tokens.sub("", self.render_before(response)))

    def test_a_community_template_that_extends_a_block_still_renders(self):
        response = self.client.get(PANEL)
        template = engines["django"].from_string(
            '{% extends "shops/panel.html" %}{% block head %}<meta name="community-head">{{ block.super }}{% endblock %}'
            '{% block content %}<div id="community-before"></div>{{ block.super }}<div id="community-after"></div>'
            "{% endblock %}"
        )
        page = template.render(response.context[0].flatten(), response.wsgi_request)
        for piece in (
            'name="community-head"',
            'id="community-before"',
            'id="community-after"',
            'id="stages"',
            'id="everything"',
            f'id="first-day-{self.shop.id}"',
        ):
            self.assertIn(piece, page)
        # And one that replaces the new block keeps the rest
        page = (
            engines["django"]
            .from_string('{% extends "shops/panel.html" %}{% block stages %}{% endblock %}')
            .render(response.context[0].flatten(), response.wsgi_request)
        )
        self.assertNotIn('id="stages"', page)
        self.assertIn(f'id="first-day-{self.shop.id}"', page)


SCRIPT = settings.BASE_DIR / "static" / "stages.js"
HARNESS = r"""
const path = process.argv[2];
const store = {
  "register-ledger:a": JSON.stringify([{ status: "paid" }, { status: "confirmed" }, { status: "pending" },
    { status: "expired" }, { status: "seen" }, { status: "cancelled" }, { status: "paid" }, null, 7]),
  "register-ledger:b": "{broken",
  "register-ledger:c": JSON.stringify({ status: "paid" }),
  "register-ledger:a:staff-1": JSON.stringify(Array(50).fill({ status: "paid" })),
  "register-ledger:practice:a": JSON.stringify(Array(50).fill({ status: "paid" })),
  "register-ledger:d": JSON.stringify(Array(25).fill({ status: "confirmed" })),
  "register-ledger:q": JSON.stringify([{ status: "paid", cents: 500, currency: "EUR" },
    { status: "seen", cents: 250, currency: "EUR" }, { status: "confirmed", cents: 100, currency: "CHF" },
    { status: "pending", cents: 900, currency: "EUR" }, { status: "confirmed", cents: "x", currency: "EUR" }]),
  "register-ledger:r": JSON.stringify([{ status: "paid", cents: 2500, currency: "EUR" }]),
};
const storage = { getItem: (key) => (key in store ? store[key] : null), setItem() { throw new Error("written"); } };
require(path);
const stages = globalThis.panelStages;
const node = (data) => ({ dataset: data, hidden: "stageSales" in data });
function card(shop, needs) {
  const now = { dataset: {}, hidden: false, now: true };
  const offers = needs.map((sales) => node({ stageSales: String(sales) }));
  return { dataset: { stageShop: shop }, now, offers,
    querySelectorAll: (selector) => (selector === "[data-stage-sales]" ? offers : [now, ...offers]) };
}
const cards = [card("a", [3, 20]), card("a", [4]), card("b", [1]), card("d", [5, 20]), card("x", [1]), card("a", ["x"])];
stages.promote({ querySelectorAll: () => cards }, storage);
const shown = (item) => [item.now.hidden, ...item.offers.map((offer) => offer.hidden)];
// The quick start left, filled in a line on the panel
const line = (shop) => ({ dataset: { quickShop: shop, quickCents: "2000", quickCurrency: "EUR",
  text: "On this phone, {left} of it is still left.", done: "Used up." }, hidden: true, textContent: "" });
const lines = ["q", "r", "x", "b"].map(line);
stages.showQuick({ querySelectorAll: () => lines }, storage);
// A fragment that points into closed folds: every closed one around it opens, and it comes into view
function tree() {
  const outer = { tagName: "DETAILS", open: false, parentElement: null };
  const open = { tagName: "DETAILS", open: true, parentElement: outer };
  const div = { tagName: "DIV", parentElement: open };
  const inner = { tagName: "DETAILS", open: false, parentElement: div };
  const target = { tagName: "LI", parentElement: inner, seen: false, scrollIntoView() { this.seen = true; } };
  const loose = { tagName: "P", parentElement: null, seen: false, scrollIntoView() { this.seen = true; } };
  return { outer, inner, target, loose,
    root: { getElementById: (id) => ({ "first-day-1": target, "top": loose })[id] || null } };
}
const deep = tree();
const revealed = stages.reveal(deep.root, "#first-day-1");
const flat = tree();
process.stdout.write(JSON.stringify({
  counts: ["a", "b", "c", "d", "x"].map((shop) => stages.salesOf(storage, shop)),
  cards: cards.map(shown),
  quick: ["q", "r", "x"].map((shop) => stages.quickLeft(storage, shop, 2000, "EUR")),
  lines: lines.map((item) => [item.hidden, item.textContent]),
  reveal: [revealed, deep.outer.open, deep.inner.open, deep.target.seen],
  others: [stages.reveal(flat.root, "#top"), flat.loose.seen, stages.reveal(flat.root, "#nothing"),
    stages.reveal(flat.root, ""), stages.reveal(flat.root, "#%E0%A4%A"), flat.outer.open],
}));
"""


class Script(SimpleTestCase):
    def test_it_reads_only_and_sends_nothing(self):
        assert_talks_to_nobody(self, "stages.js", also=("setItem", "removeItem", "sessionStorage"))

    def test_the_count_on_the_phone(self):
        node = node_or_skip(self)
        with tempfile.TemporaryDirectory() as folder:
            harness = Path(folder) / "harness.js"
            harness.write_text(HARNESS)
            result = subprocess.run(
                [node, str(harness), str(SCRIPT)], capture_output=True, text=True, timeout=60, check=False
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        # Only paid and confirmed, only the owner's register of that shop; a broken ledger, or none, counts nothing
        self.assertEqual(out["counts"], [3, 0, 0, 25, 0])
        self.assertEqual(
            out["cards"],
            [
                [True, False, True],  # 3 sales: the routine (3) shows, the game (20) waits
                [False, True],  # 4 needed, 3 there: as the days say
                [False, True],  # broken ledger
                [True, True, False],  # 25 sales: the game, the latest reached
                [False, True],  # no ledger at all
                [False, True],  # a number that cannot be read
            ],
        )
        # The quick start as the register counts it: seen, paid and confirmed in the community's currency
        self.assertEqual(out["quick"], [1250, 0, None])
        self.assertEqual(
            out["lines"],
            [[False, "On this phone, 12.50 EUR of it is still left."], [False, "Used up."], [True, ""], [True, ""]],
        )
        self.assertEqual(out["reveal"], [True, True, True, True])
        # A target outside any fold, none, an empty or broken fragment: nothing changes
        self.assertEqual(out["others"], [False, False, False, False, False, False])

    def test_it_runs_after_the_fold_and_on_every_change_of_the_fragment(self):
        script = SCRIPT.read_text()
        self.assertIn('addEventListener("hashchange"', script)
        self.assertIn("reveal(document, location.hash)", script)
        self.assertIn(
            """<script src="{% static 'stages.js' %}" defer></script>""",
            (settings.BASE_DIR / "templates" / "shops" / "stages.html").read_text(),
        )
