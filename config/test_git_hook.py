"""The example hook examples/git-hooks/commit-msg: where the «Block height» line goes, and that nothing waits for
the network. No source is asked here."""

import importlib.machinery
import importlib.util
import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase

HOOK = Path(settings.BASE_DIR) / "examples" / "git-hooks" / "commit-msg"


def load():
    loader = importlib.machinery.SourceFileLoader("commit_msg_hook", str(HOOK))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


class BlockHeight(SimpleTestCase):
    def setUp(self):
        self.hook = load()

    def test_the_file_can_be_run(self):
        self.assertTrue(HOOK.stat().st_mode & stat.S_IXUSR)

    def test_the_line_goes_at_the_end(self):
        self.assertEqual(self.hook.with_height("Title\n", 900000), "Title\n\nBlock height: 900000\n")
        self.assertEqual(
            self.hook.with_height("Title\n\nA body.\n\n\n# a comment of git\n", 900000),
            "Title\n\nA body.\n\nBlock height: 900000\n",
        )

    def test_before_the_trailers(self):
        message = "Title\n\nA body.\n\nSigned-off-by: A <a@example.org>\nReviewed-by: B <b@example.org>\n"
        self.assertEqual(
            self.hook.with_height(message, 900000),
            "Title\n\nA body.\n\nBlock height: 900000\n\n"
            "Signed-off-by: A <a@example.org>\nReviewed-by: B <b@example.org>\n",
        )
        # a title that looks like a trailer is the title, and a list is not a block of trailers
        self.assertEqual(self.hook.with_height("Fix: the thing\n", 1), "Fix: the thing\n\nBlock height: 1\n")
        self.assertEqual(
            self.hook.with_height("Title\n\n- one: a\n- two\n", 1), "Title\n\n- one: a\n- two\n\nBlock height: 1\n"
        )

    def test_a_message_with_the_line_or_empty_is_left_alone(self):
        self.assertIsNone(self.hook.with_height("Title\n\nblock height: 5\n", 900000))
        self.assertIsNone(self.hook.with_height("\n# only comments\n", 900000))

    def test_without_an_answer_the_message_stays_as_it_is(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "MSG"
            path.write_text("Title\n")
            with patch.object(self.hook, "height", return_value=None):
                self.hook.main(str(path))
            self.assertEqual(path.read_text(), "Title\n")
            with patch.object(self.hook, "height", return_value=900001):
                self.hook.main(str(path))
            self.assertEqual(path.read_text(), "Title\n\nBlock height: 900001\n")

    def test_the_sources_are_the_environments_when_given(self):
        with patch.dict(os.environ, {"BLOCK_HEIGHT_URLS": "https://a.example.org/h  https://b.example.org/h"}):
            self.assertEqual(self.hook.sources(), ("https://a.example.org/h", "https://b.example.org/h"))
        with patch.dict(os.environ, {"BLOCK_HEIGHT_URLS": ""}):
            self.assertEqual(self.hook.sources(), self.hook.SOURCES)
        # an address that is not http(s) is never opened, and no answer is no height
        with (
            patch.dict(os.environ, {"BLOCK_HEIGHT_URLS": "file:///etc/hostname ftp://x.example.org"}),
            patch.object(self.hook.urllib.request, "urlopen") as opened,
        ):
            self.assertIsNone(self.hook.height())
            opened.assert_not_called()

    def test_a_command_of_the_user_is_asked_first(self):
        with patch.object(self.hook.urllib.request, "urlopen") as opened:
            with patch.dict(os.environ, {"BLOCK_HEIGHT_COMMAND": "echo 900123"}):
                self.assertEqual(self.hook.height(), 900123)
            with patch.dict(os.environ, {"BLOCK_HEIGHT_COMMAND": "echo ' 900124 ' | cat"}):
                self.assertEqual(self.hook.height(), 900124)
            opened.assert_not_called()
        # a command that fails, prints something else or is not there gives way to the addresses
        for command in ("false", "echo not-a-number", "echo 900125; exit 3", "no-such-command-here", "echo 1234567890"):
            with patch.dict(os.environ, {"BLOCK_HEIGHT_COMMAND": command, "BLOCK_HEIGHT_URLS": "ftp://x.example.org"}):
                self.assertIsNone(self.hook.height(), command)
