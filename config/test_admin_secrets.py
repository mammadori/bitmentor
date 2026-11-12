"""The admin does not give away, by a filter in the address, what its pages do not show (config/admin_secrets.py)."""
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from config.admin_secrets import HidesSecrets, names
from shops.test_dashboard import register
from shops.models import Shop
from shops.test_guard import sign_in_as_staff_session

User = get_user_model()
APPS = ("shops", "meetups", "team")
LOOKUPS = ("", "__startswith", "__contains", "__regex", "__in", "__isnull", "__exact", "__gte")

# What the pages of the admin keep to themselves and must not give away one character at a time: the claim of a shop
NAMED = {
    "Shop": ("claim_hash",),
    # Team access: the hash of an invite's token, and what a key's passes are bound to
    "TeamInvite": ("token_hash",),
    "TeamKey": ("stamp",),
}


def admins():
    return [(model, model_admin) for model, model_admin in admin.site._registry.items() if model._meta.app_label in APPS]


def url_of(model):
    return reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_changelist")


def page_shows(model_admin, request):
    """What the pages show, as Django itself says it (the form and the read-only fields of the page of one row), and what
    the list shows of its own: the columns, the filters, the search and the date."""
    found = set(model_admin.get_fields(request)) | set(model_admin.get_readonly_fields(request))
    return found | names(model_admin.list_display) | names(model_admin.list_filter) | names(model_admin.search_fields) | names(
        [model_admin.date_hierarchy] if model_admin.date_hierarchy else [])


# Team access shows in the admin only when it is on (team/admin.py)
@override_settings(TEAM_ACCESS=True)
class EveryAdmin(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser("boss")
        viewer = User.objects.create_user("reader", password="x", is_staff=True)
        viewer.user_permissions.add(*Permission.objects.filter(content_type__app_label__in=APPS, codename__startswith="view_"))
        self.viewer = User.objects.get(pk=viewer.pk)
        self.request = RequestFactory().get("/admin/")
        self.request.user = self.superuser

    def test_every_admin_of_the_site_has_the_protection(self):
        for model, model_admin in admins():
            self.assertIsInstance(model_admin, HidesSecrets, model.__name__)

    def test_what_a_page_does_not_show_is_refused_in_the_address_to_a_superuser_and_to_who_can_only_see(self):
        for user in (self.superuser, self.viewer):
            sign_in_as_staff_session(self.client, user)
            for model, model_admin in admins():
                self.assertEqual(self.client.get(url_of(model)).status_code, 200, model.__name__)
                pk = model._meta.pk.name
                hidden = {field.name for field in model._meta.concrete_fields if field.name != pk} - page_shows(
                    model_admin, self.request)
                for field in hidden:
                    for lookup in LOOKUPS:
                        response = self.client.get(url_of(model), {f"{field}{lookup}": "a"})
                        self.assertEqual(response.status_code, 400, f"{user.username}: {model.__name__} ?{field}{lookup}")

    def test_no_relation_the_admin_does_not_name_can_be_asked_through(self):
        """What the review of team access found: Django lets a list be filtered across a reverse relation when the lookup
        ends on the related row's primary key (`?teamchallenge__k1__startswith=`), and that key was a secret. Every
        relation of every registered model is walked, forward and reverse: unless the admin names it, the address is
        refused whatever comes after, to a superuser and to whoever can only look."""
        asked = 0
        for user in (self.superuser, self.viewer):
            sign_in_as_staff_session(self.client, user)
            for model, model_admin in admins():
                relations = [(field.name, field.related_model) for field in model._meta.get_fields()
                             if field.is_relation and field.related_model is not None]
                for name, related in relations:
                    if name in model_admin.askable():
                        continue
                    ends = ["", "__isnull", "__pk", "__pk__startswith", f"__{related._meta.pk.name}",
                            f"__{related._meta.pk.name}__startswith", f"__{related._meta.pk.name}__regex"]
                    ends += [f"__{field.name}__startswith" for field in related._meta.concrete_fields]
                    for end in ends:
                        response = self.client.get(url_of(model), {f"{name}{end}": "a"})
                        self.assertEqual(response.status_code, 400, f"{user.username}: {model.__name__} ?{name}{end}")
                        asked += 1
        # Not an empty walk: the shops' registers and the challenges of the team's invites are in it
        self.assertGreater(asked, 200)
        reverse_names = {(model.__name__, relation.name) for model, _admin in admins() for relation in model._meta.related_objects}
        for pair in (("Shop", "staff_registers"), ("TeamInvite", "teamchallenge"), ("TeamKey", "teaminvite")):
            self.assertIn(pair, reverse_names)

    def test_a_name_that_is_nothing_is_refused_too(self):
        """Django lets a lookup on a field that does not exist through, to ignore it later: here it is refused, so that
        a relation added tomorrow under any name is closed until an admin shows it."""
        sign_in_as_staff_session(self.client, self.viewer)
        for model, _model_admin in admins():
            self.assertEqual(self.client.get(url_of(model), {"nothing__startswith": "a"}).status_code, 400, model.__name__)
            # The primary key and Django's own parameters are still fine
            self.assertEqual(self.client.get(url_of(model), {"pk__isnull": "False", "o": "1", "q": ""}).status_code, 200, model.__name__)

    def test_no_model_that_takes_part_in_a_relation_keeps_a_secret_as_its_primary_key(self):
        """The other half of the same finding, for every registered model, Django's own admins of the accounts too
        (they are not HidesSecrets): a lookup across a relation can always end on the other row's primary key, so that
        key is an id, a number or a UUID that opens nothing, never a token or a challenge."""
        from django.db import models

        ids = (models.AutoField, models.UUIDField)
        seen = set()
        for model in admin.site._registry:
            for field in model._meta.get_fields():
                if field.is_relation and field.related_model is not None:
                    seen.add(field.related_model)
                    seen.add(model)
        self.assertGreater(len(seen), 12)
        for model in seen:
            self.assertIsInstance(model._meta.pk, ids, f"{model._meta.label}: primary key {model._meta.pk.name}")
        # The challenge of a team invite, which was the one found, is among them
        self.assertIn("team.TeamChallenge", {model._meta.label for model in seen})

    def test_the_challenge_of_a_team_invite_is_not_read_through_the_admin(self):
        """The attack as the reviewer ran it, with only the right to look at the invites: every guess answers the same."""
        from team.models import TeamChallenge, TeamInvite

        invite = TeamInvite(name="Anna", role="team")
        invite.arm()
        invite.save()
        challenge = TeamChallenge.new(invite)
        looker = User.objects.create_user("looker", is_staff=True)
        looker.user_permissions.add(Permission.objects.get(content_type__app_label="team", codename="view_teaminvite"))
        sign_in_as_staff_session(self.client, looker)
        self.assertEqual(self.client.get(url_of(TeamInvite)).status_code, 200)
        for lookup in ("teamchallenge__k1__startswith", "teamchallenge__k1", "teamchallenge__k1__regex", "teamchallenge__id",
                       "teamchallenge__pk__startswith", "teamchallenge__linking_key__startswith", "teamchallenge__isnull",
                       "token_hash__startswith", "key__stamp__startswith", "key__linking_key__startswith"):
            for guess in (challenge.k1[:1], "z", challenge.k1, str(challenge.pk)):
                self.assertEqual(self.client.get(url_of(TeamInvite), {lookup: guess}).status_code, 400, f"{lookup}={guess[:4]}")
        self.assertNotContains(self.client.get(url_of(TeamInvite)), challenge.k1)
        self.assertNotContains(self.client.get(reverse("admin:team_teaminvite_change", args=[invite.pk])), challenge.k1)

    def test_the_named_secrets_are_among_them(self):
        """Whatever the rule works out, these are refused by name: a shop's webhook secret and claim hash."""
        for user in (self.superuser, self.viewer):
            sign_in_as_staff_session(self.client, user)
            for model, model_admin in admins():
                for field in NAMED.get(model.__name__, ()):
                    self.assertIn(field, model_admin.hidden_fields(), f"{model.__name__}.{field}")
                    for lookup in LOOKUPS:
                        self.assertEqual(self.client.get(url_of(model), {f"{field}{lookup}": "a"}).status_code, 400,
                                         f"{user.username}: {model.__name__} ?{field}{lookup}")

    def test_a_secret_of_a_shop_is_not_asked_through_another_model_either(self):
        for user in (self.superuser, self.viewer):
            sign_in_as_staff_session(self.client, user)
            for model, model_admin in admins():
                if any(field.name == "shop" for field in model._meta.concrete_fields):
                    for path in ("shop__claim_hash__startswith", "shop__claim_hash"):
                        self.assertEqual(self.client.get(url_of(model), {path: "a"}).status_code, 400, f"{model.__name__} {path}")

    def test_the_filters_that_are_shown_still_work(self):
        sign_in_as_staff_session(self.client, self.superuser)
        register("Bar")
        for name, params in (("shops_shop", {"lightning": "broken", "suspended__exact": "0"}),
                             ("shops_shop", {"certified": "yes", "listed__exact": "1"}),
                             ("shops_helprequest", {"state": "open", "symptom__exact": "pay"}),
                             ("shops_accountchange", {"via__exact": "admin"})):
            self.assertEqual(self.client.get(reverse(f"admin:{name}_changelist"), params).status_code, 200, f"{name} {params}")

    def test_the_value_is_not_read_one_character_at_a_time(self):
        """A filter such as `claim_hash__startswith` would give a moderator one row on the right prefix and none on a wrong
        one, and so the secret, a character at a time: both answer the same."""
        shop = register("Bar")
        Shop.objects.filter(pk=shop.pk).update(claim_hash="the-bearer-secret")
        sign_in_as_staff_session(self.client, self.viewer)
        for prefix in ("the-bearer", "zz"):
            response = self.client.get(url_of(Shop), {"claim_hash__startswith": prefix})
            self.assertEqual(response.status_code, 400, prefix)
