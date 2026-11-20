from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from config import version


class InUse(SimpleTestCase):
    def setUp(self):
        version.in_use.cache_clear()
        self.addCleanup(version.in_use.cache_clear)

    def test_the_site_version_wins(self):
        self.assertEqual(version.in_use("0.1.7-site.1", "0.1.7"), "0.1.7-site.1")

    def test_the_software_version_without_a_site_version(self):
        self.assertEqual(version.in_use("", "0.1.7"), "0.1.7")

    def test_the_commit_without_an_exact_version(self):
        for software in ("", "development"):
            version.in_use.cache_clear()
            with patch.object(version, "commit", return_value="abc1234-dirty"):
                self.assertEqual(version.in_use("", software), "abc1234-dirty")

    def test_no_version_without_git(self):
        with patch.object(version, "commit", return_value=""):
            self.assertEqual(version.in_use("", ""), "no-version")


class Commit(SimpleTestCase):
    def answers(self, top, short="abc1234", changed=""):
        replies = {
            "rev-parse --show-toplevel": top,
            "rev-parse --short HEAD": short,
            "status --porcelain --untracked-files=no": changed,
        }
        return patch.object(version, "_git", side_effect=lambda *a: replies[" ".join(a)])

    def test_clean(self):
        from django.conf import settings

        with self.answers(str(settings.BASE_DIR)):
            self.assertEqual(version.commit(), "abc1234")

    def test_dirty_with_uncommitted_changes(self):
        from django.conf import settings

        with self.answers(str(settings.BASE_DIR), changed=" M config/version.py"):
            self.assertEqual(version.commit(), "abc1234-dirty")

    def test_another_checkout_around_the_sources_does_not_count(self):
        with self.answers("/"):
            self.assertEqual(version.commit(), "")

    def test_no_git(self):
        with self.answers(None):
            self.assertEqual(version.commit(), "")

    def test_a_missing_git_program_is_not_an_error(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(version.commit(), "")


class Footer(TestCase):
    def setUp(self):
        version.in_use.cache_clear()
        self.addCleanup(version.in_use.cache_clear)

    @override_settings(SITE_VERSION="0.1.7-site.1", BITMENTOR_VERSION="0.1.7")
    def test_every_page_shows_the_version_in_its_footer(self):
        self.assertContains(
            self.client.get("/", follow=True), '<span class="version" title="Version in use">0.1.7-site.1</span>'
        )

    @override_settings(SITE_VERSION="", BITMENTOR_VERSION="")
    def test_no_version_is_said_too(self):
        with patch.object(version, "commit", return_value=""):
            self.assertContains(self.client.get("/", follow=True), ">no-version</span>")

    @override_settings(SHOW_VERSION=False, SITE_VERSION="0.1.7-site.1")
    def test_a_site_can_hide_it(self):
        self.assertNotContains(self.client.get("/", follow=True), 'class="version"')
