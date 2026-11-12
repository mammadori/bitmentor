"""The register's tests need node and skip themselves without it. With REQUIRE_NODE=1 (the CI) a missing node is an
error, not a hundred tests silently skipped."""
import os
import shutil
import subprocess
from unittest import skipUnless

from django.test import SimpleTestCase


@skipUnless(os.environ.get("REQUIRE_NODE") == "1", "REQUIRE_NODE=1 only in the CI")
class Node(SimpleTestCase):
    def test_node_is_there(self):
        node = os.environ.get("NODE") or shutil.which("node")
        self.assertTrue(node, "REQUIRE_NODE=1 but node is missing: NODE=/path/to/node or node in the PATH")
        result = subprocess.run([node, "--version"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
