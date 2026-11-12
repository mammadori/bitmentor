"""A temporary password is changed at the first sign-in (config/temporary.py): the mark, the redirect, the change, the
command. On SQLite and on PostgreSQL (with POSTGRES_HOST set): the mark is a row of the Profile, written and read in
the same transactions as the rest."""
import logging
import re
import uuid
from io import StringIO
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.management import CommandError, call_command
from django.http import HttpResponse
from django.test import Client, RequestFactory, TestCase, override_settings

from shops.models import Profile, RequestCount, Shop
from shops.tests import reset_limits, steady_limits

from .guard import ADMIN_SESSION
from .temporary import TemporaryPassword, exempt, is_temporary

User = get_user_model()

OLD = "handed-over-in-person 2026"
NEW = "a brand new secret 8421 z"
CHANGE = "/admin/password_change/"
DONE = "/admin/password_change/done/"
EN = {"headers": {"accept-language": "en"}}


def create_staff(username="volunteer", password=OLD, **extra):
    return User.objects.create_user(username, password=password, is_staff=True, **extra)


def admin_password_session(client, user):
    """A session born from the admin's password, for any account: as /admin/login/ makes it."""
    client.force_login(user)
    session = client.session
    session[ADMIN_SESSION] = True
    session.save()


def mark(username="volunteer"):
    call_command("temporary_password", username, stdout=StringIO(), stderr=StringIO())


def marked_hash(user):
    return Profile.objects.get(user=user).temporary_password


class Base(TestCase):
    def setUp(self):
        reset_limits()


# ---------------------------------------------------------------------------------------------------------------------
# The redirect
# ---------------------------------------------------------------------------------------------------------------------

class Redirect(Base):
    def setUp(self):
        super().setUp()
        self.user = create_staff()
        mark()
        admin_password_session(self.client, self.user)

    def test_every_page_of_the_admin_goes_to_the_change(self):
        for url in ("/admin/", "/admin/today/", "/admin/shops/shop/", "/admin/shops/shop/add/", "/admin/auth/user/",
                    "/admin/shops/accountchange/", "/admin/jsi18n/", "/admin/nothing/here/"):
            response = self.client.get(url)
            self.assertEqual((response.status_code, response["Location"]), (302, CHANGE), url)

    def test_so_does_a_form_sent_to_the_admin(self):
        response = self.client.post("/admin/shops/shop/", {"action": "delete_selected"})
        self.assertEqual((response.status_code, response["Location"]), (302, CHANGE))
        self.assertEqual(Shop.objects.count(), 0)

    def test_the_pages_of_the_site_are_left_alone(self):
        """Outside /admin/ the staff is a visitor already (config/guard.py): a staff member who reaches the site from a
        network the admin is closed to (ADMIN_ALLOWED_IPS) would be sent to a page that answers 403, and could not use the
        public pages at all. The merchants' pages answer as to a stranger, and nothing sends them to the change."""
        for url in ("/en/", "/it/", "/en/shops/map/", "/shops.json", "/robots.txt", "/en/shops/",
                    f"/en/shops/edit/{uuid.uuid4()}/", f"/en/shops/register/{uuid.uuid4()}/", "/en/shops/new/"):
            response = self.client.get(url)
            self.assertNotEqual(response.headers.get("Location"), CHANGE, url)
            self.assertIn(response.status_code, (200, 302, 404), url)
        self.assertEqual(self.client.get("/en/").status_code, 200)
        self.assertEqual(self.client.get("/shops.json").status_code, 200)
        response = self.client.post("/en/shops/new/", {"name": "x"})
        self.assertNotEqual(response.headers.get("Location"), CHANGE)
        self.assertEqual(Shop.objects.count(), 0)

    def test_the_redirect_is_plain(self):
        """Nothing in it but the place to go: no page asked for, no message, nothing about the password."""
        response = self.client.get("/admin/shops/shop/?q=x")
        self.assertEqual(response["Location"], CHANGE)
        self.assertEqual(response.content, b"")
        self.assertNotIn(self.user.password, str(response.headers))
        self.assertNotIn(OLD, str(response.headers))

    def test_the_change_and_its_exits_stay_open(self):
        page = self.client.get(CHANGE, **EN)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Your password is temporary. Choose a new one to go on.")
        # The way out, for whoever cannot change it now: a form (the sign-out is a POST) to the sign-out, and the sentence
        self.assertContains(page, "Not now? Log out: the next sign-in asks again.")
        self.assertContains(page, '<form method="post" action="/admin/logout/">')
        self.assertEqual(self.client.get(DONE).status_code, 200)
        # The sign-out is not sent back to the change: the account is signed out
        response = self.client.post("/admin/logout/")
        self.assertNotEqual(response.headers.get("Location"), CHANGE)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_only_the_change_and_its_exits_are_exempt(self):
        for path in (CHANGE, DONE, "/admin/logout/"):
            self.assertTrue(exempt(path), path)
        for path in ("/admin/", "/en/", "/admin/password_change/other/", "/admin/jsi18n/", "/static/admin/css/base.css"):
            self.assertFalse(exempt(path), path)
        # The files of the site are not in /admin/: nothing to exempt, nothing sent
        for url in ("/static/admin/css/not-there.css", "/theme/not-there.css"):
            self.assertEqual(self.client.get(url).status_code, 404, url)

    def test_the_explanation_is_in_the_language(self):
        page = self.client.get(CHANGE, headers={"accept-language": "it"})
        self.assertContains(page, "La tua password è temporanea. Scegline una nuova per andare avanti.")
        # German is the other languages' business (the example community has two): the catalogue has the sentences
        from .test_translations import untranslated

        untranslated(self, {"Your password is temporary. Choose a new one to go on.",
                            "The new password must be different from the old one."})

    def test_a_staff_member_with_an_ordinary_password_is_not_told_so(self):
        create_staff("other")
        client = Client()
        admin_password_session(client, User.objects.get(username="other"))
        self.assertEqual(client.get("/admin/").status_code, 200)
        self.assertNotContains(client.get(CHANGE, **EN), "Your password is temporary")

    def test_it_sits_after_the_staff_is_set_apart(self):
        order = settings.MIDDLEWARE
        self.assertEqual(order.index("config.temporary.TemporaryPassword"), order.index("config.guard.StaffApart") + 1)


class WhoIsNotSent(Base):
    def test_a_user_without_a_profile(self):
        user = create_staff()
        admin_password_session(self.client, user)
        self.assertFalse(Profile.objects.exists())
        for url in ("/admin/", "/en/", "/en/shops/map/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertFalse(is_temporary(user))
        # Looking does not make a Profile
        self.assertFalse(Profile.objects.exists())

    def test_a_profile_without_the_mark(self):
        user = create_staff()
        Profile.objects.create(user=user, email="volunteer@example.org")
        admin_password_session(self.client, user)
        self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_a_user_who_is_not_staff_with_the_mark(self):
        """A merchant has no password and no way to the change: a mark on their account does nothing, and the shop opens."""
        merchant = User.objects.create(username="merchant", password=make_password(OLD))
        shop = Shop.objects.create(owner=merchant, name="Pub", lightning_address="pub@ln.example.org")
        mark("merchant")
        self.assertEqual(marked_hash(merchant), merchant.password)
        self.client.force_login(merchant)
        self.assertContains(self.client.get("/en/shops/"), "Your shops")
        self.assertEqual(self.client.get(f"/en/shops/edit/{shop.id}/").status_code, 200)
        self.assertEqual(self.client.get("/en/").status_code, 200)
        # Not even with the flag of the admin's session left from the time they were staff
        session = self.client.session
        session[ADMIN_SESSION] = True
        session.save()
        self.assertEqual(self.client.get("/en/shops/").status_code, 200)

    def test_staff_who_lost_it_after_signing_in(self):
        user = create_staff()
        mark()
        admin_password_session(self.client, user)
        self.assertEqual(self.client.get("/admin/")["Location"], CHANGE)
        User.objects.filter(pk=user.pk).update(is_staff=False)
        # The admin does not let them in any more: there is no change of password for them to be sent to
        self.assertRedirects(self.client.get("/admin/"), "/admin/login/?next=/admin/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/en/").status_code, 200)

    def test_a_staff_session_that_did_not_come_from_the_admins_password(self):
        """A session born from the email link or a wallet does not open the admin: there is no change to be sent to."""
        user = create_staff()
        mark()
        self.client.force_login(user)
        self.assertEqual(self.client.get("/en/").status_code, 200)
        self.assertRedirects(self.client.get("/admin/"), "/admin/login/?next=/admin/", fetch_redirect_response=False)

    def test_another_account_and_a_visitor(self):
        create_staff()
        mark()
        other = create_staff("other")
        admin_password_session(self.client, other)
        self.assertEqual(self.client.get("/admin/").status_code, 200)
        # A browser with no session at all: the site, the data and the register's files answer as ever
        visitor = Client()
        for url in ("/en/", "/shops.json", "/robots.txt", "/en/shops/map/"):
            self.assertEqual(visitor.get(url).status_code, 200, url)

    def test_a_visitor_costs_no_query(self):
        """No cookie of the session, nobody to look for: the middleware does not even touch `request.user`."""
        request = RequestFactory().get("/en/")
        with self.assertNumQueries(0):
            self.assertFalse(TemporaryPassword.must_change(request))


# ---------------------------------------------------------------------------------------------------------------------
# The change
# ---------------------------------------------------------------------------------------------------------------------

class Change(Base):
    def setUp(self):
        super().setUp()
        # The limits count in windows of fixed length (config.ratelimit.limit_key). A test that sends the requests of a limit and
        # expects the refusal at the next one must not see the clock cross a window's end in between: each wrong password
        # costs a second or more of hashing, so eleven of them can easily straddle one. The clock stands still.
        self.enterContext(steady_limits())
        self.user = create_staff()
        mark()
        admin_password_session(self.client, self.user)

    def change(self, old=OLD, new=NEW, again=None):
        return self.client.post(CHANGE, {"old_password": old, "new_password1": new,
                                         "new_password2": new if again is None else again}, **EN)

    def refused(self, response, text):
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, text)
        self.user.refresh_from_db()
        self.assertTrue(is_temporary(self.user))
        self.assertTrue(self.user.check_password(OLD))
        self.assertEqual(self.client.get("/admin/")["Location"], CHANGE)

    def test_a_new_password_ends_the_mark_by_itself(self):
        marked = marked_hash(self.user)
        self.assertEqual(marked, User.objects.get(pk=self.user.pk).password)
        self.assertRedirects(self.change(), DONE, fetch_redirect_response=False)
        self.user.refresh_from_db()
        # The hash is another: that alone ends it, whatever the Profile says
        self.assertNotEqual(self.user.password, marked)
        self.assertFalse(is_temporary(self.user))
        Profile.objects.filter(user=self.user).update(temporary_password=marked)
        self.assertFalse(is_temporary(User.objects.get(pk=self.user.pk)))
        # The session goes on, and so does every page
        for url in ("/admin/", "/admin/password_change/done/", "/en/", "/en/shops/map/", "/en/shops/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertTrue(self.user.check_password(NEW))
        self.assertFalse(self.user.check_password(OLD))

    def test_the_form_also_drops_the_old_hash(self):
        self.change()
        self.assertEqual(marked_hash(self.user), "")

    def test_the_same_password_put_back_does_not_count(self):
        self.refused(self.change(new=OLD), "The new password must be different from the old one.")

    def test_the_old_password_must_be_right(self):
        self.refused(self.change(old="not the one 2026"), "Your old password was entered incorrectly.")

    def test_the_form_does_not_tell_whether_a_guess_is_the_password(self):
        """With a wrong old password, a new one equal to the real password is not answered differently: the form cannot be
        used to try out guesses."""
        response = self.change(old="not the one 2026", new=OLD)
        self.assertNotContains(response, "must be different")
        self.assertContains(response, "Your old password was entered incorrectly.")

    def test_the_validators_of_the_settings_apply(self):
        self.refused(self.change(new="short 1"), "This password is too short.")
        self.refused(self.change(new="volunteer1234"), "too similar to the username")

    def test_the_form_has_the_limits_of_the_sign_in(self):
        for _ in range(10):
            self.assertEqual(self.change(old="not the one 2026").status_code, 200)
        blocked = self.change(old="not the one 2026")
        self.assertEqual(blocked.status_code, 429)
        # A page with a line that says what happened and when to come back, and the way out: not an empty answer
        self.assertContains(blocked, "Too many wrong passwords. Wait a little and try again.", status_code=429)
        self.assertContains(blocked, "Not now? Log out", status_code=429)
        self.assertNotContains(blocked, 'name="old_password"', status_code=429)
        self.assertTrue(1 <= int(blocked["Retry-After"]) <= 900)
        # Not even the right password goes through until the window is over; the pages around it do not count
        self.assertEqual(self.change().status_code, 429)
        self.assertEqual(self.client.get(CHANGE).status_code, 200)
        self.assertTrue(User.objects.get(pk=self.user.pk).check_password(OLD))
        reset_limits()
        self.assertRedirects(self.change(), DONE, fetch_redirect_response=False)

    def test_the_limit_in_the_language(self):
        for _ in range(11):
            response = self.client.post(CHANGE, {"old_password": "not the one 2026", "new_password1": NEW,
                                                 "new_password2": NEW}, headers={"accept-language": "it"})
        self.assertContains(response, "Troppe password sbagliate.", status_code=429)

    def test_only_a_wrong_old_password_counts(self):
        """A new password the validators refuse, two that do not match, a change that goes through: nothing against the
        limit, so that whoever is sent here and makes mistakes while choosing is not shut out of the page they must use."""
        for _ in range(12):
            self.assertEqual(self.change(new="short 1").status_code, 200)
            self.assertEqual(self.change(again=NEW + "x").status_code, 200)
            self.assertEqual(self.change(new=OLD).status_code, 200)
        # All those answered the form; and ten wrong old passwords are still ten
        for _ in range(10):
            self.assertEqual(self.change(old="not the one 2026").status_code, 200)
        self.assertEqual(self.change(old="not the one 2026").status_code, 429)
        reset_limits()
        self.assertRedirects(self.change(), DONE, fetch_redirect_response=False)
        # A change that goes through is given back too
        self.assertEqual(RequestCount.objects.filter(count__gt=0).count(), 0)

    def test_requests_sent_together_are_counted_before_the_form_looks(self):
        """The count is made first (atomic) and given back after, so that requests sent together do not all find it at
        zero: with the form out of the way and nothing given back, the eleventh is the one that is refused."""
        answered = lambda request: HttpResponse("answered")  # noqa: E731 (not a wrong old password: it would be given back)
        with patch("config.temporary.PasswordChangeView.as_view", return_value=answered):
            for _ in range(15):
                self.assertEqual(self.client.post(CHANGE, {}).content, b"answered")
            self.assertEqual(RequestCount.objects.filter(count__gt=0).count(), 0)
            with patch("config.ratelimit.give_back"):
                for _ in range(10):
                    self.assertEqual(self.client.post(CHANGE, {}).status_code, 200)
                self.assertEqual(self.client.post(CHANGE, {}).status_code, 429)

    def test_the_two_new_passwords_must_match(self):
        self.refused(self.change(again=NEW + "x"), "The two password fields didn’t match.")

    def test_the_message_stays_while_the_form_is_refused(self):
        self.assertContains(self.change(new=OLD), "Your password is temporary")

    def test_a_password_set_by_other_means_ends_the_mark_too(self):
        """The admin's user form, `changepassword`, the shell: a new hash. Even the same password (a new salt): to hand it
        over again as temporary, mark it again."""
        self.user.set_password(OLD)
        self.user.save()
        self.assertFalse(is_temporary(self.user))
        # The session of the old hash is no longer valid; a sign-in with the new one is not sent anywhere
        client = Client()
        admin_password_session(client, User.objects.get(pk=self.user.pk))
        self.assertEqual(client.get("/admin/").status_code, 200)
        self.assertEqual(client.get("/en/").status_code, 200)
        mark()
        self.assertTrue(is_temporary(User.objects.get(pk=self.user.pk)))


# ---------------------------------------------------------------------------------------------------------------------
# The sign-in
# ---------------------------------------------------------------------------------------------------------------------

class SignIn(Base):
    def sign_in(self, client, password=OLD):
        return client.post("/admin/login/", {"username": "volunteer", "password": password, "next": "/admin/"})

    def test_the_first_sign_in_leads_to_the_change_and_the_change_to_the_site(self):
        create_staff()
        mark()
        client = Client()
        self.assertEqual(self.sign_in(client).status_code, 302)
        page = client.get("/admin/", follow=True, **EN)
        self.assertEqual(page.redirect_chain, [(CHANGE, 302)])
        self.assertContains(page, "Your password is temporary")
        response = client.post(CHANGE, {"old_password": OLD, "new_password1": NEW, "new_password2": NEW})
        self.assertRedirects(response, DONE, fetch_redirect_response=False)
        self.assertEqual(client.get("/admin/").status_code, 200)
        # The next time: the new password, and straight in
        client = Client()
        self.assertEqual(self.sign_in(client, NEW).status_code, 302)
        self.assertEqual(client.get("/admin/").status_code, 200)
        self.assertEqual(self.sign_in(Client()).status_code, 200)  # the old one signs nobody in

    def test_leaving_without_changing_asks_again_next_time(self):
        create_staff()
        mark()
        for _ in range(2):
            client = Client()
            self.sign_in(client)
            self.assertEqual(client.get("/admin/")["Location"], CHANGE)

    def test_a_wrong_password_is_still_a_wrong_password(self):
        create_staff()
        mark()
        client = Client()
        self.assertEqual(self.sign_in(client, "wrong password 2026").status_code, 200)
        self.assertNotIn("_auth_user_id", client.session)

    def test_a_sign_in_that_stores_the_password_with_a_newer_hash_keeps_the_mark(self):
        """Django re-hashes a password at the sign-in when the hasher changed since it was set: the hash is another and
        the password is not, and the mark must not be lost on the very sign-in it is for."""
        old_first = ["django.contrib.auth.hashers.MD5PasswordHasher", "django.contrib.auth.hashers.PBKDF2PasswordHasher"]
        with override_settings(PASSWORD_HASHERS=old_first):
            user = create_staff()
            self.assertTrue(user.password.startswith("md5$"))
            mark()
            self.assertEqual(marked_hash(user), user.password)
        with override_settings(PASSWORD_HASHERS=old_first[::-1]):
            client = Client()
            self.assertEqual(self.sign_in(client).status_code, 302)
            user.refresh_from_db()
            self.assertTrue(user.password.startswith("pbkdf2_sha256$"))
            self.assertEqual(marked_hash(user), user.password)
            self.assertTrue(is_temporary(user))
            self.assertEqual(client.get("/admin/")["Location"], CHANGE)

    def test_a_sign_in_typed_with_spaces_keeps_the_mark_on_a_rehash_too(self):
        old_first = ["django.contrib.auth.hashers.MD5PasswordHasher", "django.contrib.auth.hashers.PBKDF2PasswordHasher"]
        with override_settings(PASSWORD_HASHERS=old_first):
            user = create_staff()
            mark()
        with override_settings(PASSWORD_HASHERS=old_first[::-1]):
            client = Client()
            response = client.post("/admin/login/", {"username": "  volunteer ", "password": OLD, "next": "/admin/"})
            self.assertEqual(response.status_code, 302)
            user.refresh_from_db()
            self.assertTrue(user.password.startswith("pbkdf2_sha256$"))
            self.assertTrue(is_temporary(user))
            self.assertEqual(client.get("/admin/")["Location"], CHANGE)

    def test_the_same_password_put_back_is_not_marked_again_by_the_sign_in(self):
        """Someone sets the same password again (a new hash): the mark ended, and the next sign-in does not bring it back."""
        user = create_staff()
        mark()
        user.set_password(OLD)
        user.save()
        self.assertFalse(is_temporary(user))
        client = Client()
        self.assertEqual(self.sign_in(client).status_code, 302)
        user.refresh_from_db()
        self.assertFalse(is_temporary(user))
        self.assertEqual(marked_hash(user), "")
        self.assertEqual(client.get("/admin/").status_code, 200)

    def test_nor_when_that_sign_in_stores_a_newer_hash_of_it(self):
        """The sneaky one: the same password set again with an older hasher, then the sign-in re-hashes it. The password
        typed fits the hash that was marked, but the mark was not in force just before this sign-in."""
        old_first = ["django.contrib.auth.hashers.MD5PasswordHasher", "django.contrib.auth.hashers.PBKDF2PasswordHasher"]
        user = create_staff()
        mark()
        with override_settings(PASSWORD_HASHERS=old_first):
            user.set_password(OLD)
            user.save()
            self.assertTrue(user.password.startswith("md5$"))
        with override_settings(PASSWORD_HASHERS=old_first[::-1]):
            client = Client()
            self.assertEqual(self.sign_in(client).status_code, 302)
            user.refresh_from_db()
            self.assertTrue(user.password.startswith("pbkdf2_sha256$"))
            self.assertFalse(is_temporary(user))
            self.assertEqual(marked_hash(user), "")
            self.assertEqual(client.get("/admin/").status_code, 200)

    def test_a_password_changed_after_the_mark_is_dropped_at_the_next_sign_in(self):
        user = create_staff()
        mark()
        old_hash = user.password
        user.set_password(NEW)
        user.save()
        self.assertEqual(marked_hash(user), old_hash)  # nothing cleared it: it only no longer holds
        client = Client()
        self.assertEqual(self.sign_in(client, NEW).status_code, 302)
        self.assertEqual(marked_hash(user), "")
        self.assertEqual(client.get("/admin/").status_code, 200)

    def test_the_sign_in_of_an_unmarked_account_makes_no_profile(self):
        create_staff()
        client = Client()
        self.assertEqual(self.sign_in(client).status_code, 302)
        self.assertEqual(client.get("/admin/").status_code, 200)
        self.assertFalse(Profile.objects.exists())


# ---------------------------------------------------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------------------------------------------------

class Command(Base):
    def run_command(self, *args):
        out, err = StringIO(), StringIO()
        call_command("temporary_password", *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_it_marks_the_password_the_account_has(self):
        user = create_staff()
        out, err = self.run_command("volunteer")
        user.refresh_from_db()
        self.assertEqual(marked_hash(user), user.password)
        self.assertTrue(user.check_password(OLD))  # the account is as it was
        self.assertTrue(is_temporary(user))
        self.assertEqual(err, "")
        self.assertIn("volunteer", out)

    def test_it_says_nothing_secret(self):
        user = create_staff()
        out, err = self.run_command("volunteer")
        for text in (out, err):
            self.assertNotIn(OLD, text)
            self.assertNotIn(user.password, text)
            self.assertNotIn("pbkdf2", text)

    def test_it_makes_the_profile_and_keeps_the_one_there_is(self):
        create_staff()
        self.assertFalse(Profile.objects.exists())
        self.run_command("volunteer")
        self.assertEqual(Profile.objects.count(), 1)
        create_staff("second")
        Profile.objects.create(user=User.objects.get(username="second"), email="second@example.org")
        self.run_command("second")
        self.assertEqual(Profile.objects.get(user__username="second").email, "second@example.org")
        self.assertEqual(Profile.objects.count(), 2)

    def test_it_marks_only_the_account_asked(self):
        create_staff()
        other = create_staff("other")
        Profile.objects.create(user=other)
        self.run_command("volunteer")
        self.assertEqual(marked_hash(other), "")

    def test_twice_is_the_same_as_once(self):
        user = create_staff()
        self.run_command("volunteer")
        self.run_command("volunteer")
        self.assertEqual(Profile.objects.count(), 1)
        self.assertEqual(marked_hash(user), user.password)

    def test_a_new_password_marked_again(self):
        user = create_staff()
        self.run_command("volunteer")
        user.set_password(NEW)
        user.save()
        self.assertFalse(is_temporary(user))
        self.run_command("volunteer")
        self.assertTrue(is_temporary(user))

    def test_clear_takes_the_mark_away(self):
        user = create_staff()
        self.run_command("volunteer")
        out, _err = self.run_command("volunteer", "--clear")
        self.assertIn("no longer", out)
        self.assertEqual(marked_hash(user), "")
        self.assertFalse(is_temporary(user))
        # An account with no Profile, and one never marked: nothing to clear, nothing made
        create_staff("other")
        self.run_command("other", "--clear")
        self.assertFalse(Profile.objects.filter(user__username="other").exists())

    def test_it_refuses_an_account_that_does_not_exist(self):
        with self.assertRaisesMessage(CommandError, "There is no account with that name."):
            self.run_command("nobody")
        with self.assertRaises(CommandError):
            self.run_command("nobody", "--clear")
        self.assertFalse(Profile.objects.exists())

    def test_it_refuses_an_account_without_a_usable_password(self):
        unusable = create_staff("unusable")
        unusable.set_unusable_password()
        unusable.save()
        User.objects.create(username="blank", password="", is_staff=True)
        User.objects.create(username="strange", password="not-a-hash", is_staff=True)
        for username in ("unusable", "blank", "strange"):
            with self.assertRaisesMessage(CommandError, "no password to mark"):
                self.run_command(username)
        self.assertFalse(Profile.objects.exists())

    def test_it_asks_for_the_name_of_an_account(self):
        with self.assertRaises(CommandError):
            call_command("temporary_password")

    def test_an_account_that_is_not_staff_is_marked_and_warned(self):
        user = User.objects.create_user("merchant", password=OLD)
        _out, err = self.run_command("merchant")
        self.assertIn("not a staff account", err)
        self.assertTrue(is_temporary(user))
        self.assertNotIn(OLD, err)

    def test_the_marking_is_not_a_change_of_the_account(self):
        """No `User` is saved, so the register of the changes to the accounts of shops' owners has nothing to say."""
        user = create_staff()
        before = (user.password, user.is_staff, user.is_superuser, user.is_active, user.username)
        self.run_command("volunteer")
        user.refresh_from_db()
        self.assertEqual(before, (user.password, user.is_staff, user.is_superuser, user.is_active, user.username))


class NoLeaks(Base):
    def test_nothing_is_logged_with_a_password_or_a_hash(self):
        user = create_staff()
        mark()
        admin_password_session(self.client, user)
        with self.assertLogs(level="DEBUG") as logged:
            logging.getLogger("test").debug("start")
            self.client.get("/en/")
            self.client.post(CHANGE, {"old_password": OLD, "new_password1": OLD, "new_password2": OLD})
            self.client.post(CHANGE, {"old_password": OLD, "new_password1": NEW, "new_password2": NEW})
        text = "\n".join(logged.output)
        for secret in (OLD, NEW, user.password):
            self.assertNotIn(secret, text)
        self.assertFalse(re.search(r"pbkdf2_sha256\$", text))
