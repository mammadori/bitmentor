"""What the site's logging (settings.LOGGING) hands to its handler: the warnings and errors of the site's apps and of
any other code, a community's included, the lines Django always wrote, and only the errors of the libraries."""

import logging

from django.conf import settings
from django.test import SimpleTestCase


def written(name, level):
    """Would a record of this logger at this level reach the console handler of the configuration?"""
    logger = logging.getLogger(name)
    if not logger.isEnabledFor(level):
        return False
    handler_level = logging.getLevelName(settings.LOGGING["handlers"]["console"]["level"])
    return level >= handler_level


class Levels(SimpleTestCase):
    def test_a_community_s_code_writes_its_warnings_with_no_configuration(self):
        for name in ("community", "community.notices", "anything.else", "shops.guard", "config.mail", "team.area"):
            self.assertTrue(written(name, logging.WARNING), name)
            self.assertTrue(written(name, logging.ERROR), name)
            self.assertFalse(written(name, logging.INFO), name)

    def test_django_writes_what_it_wrote_before(self):
        """A 404 is a warning of django.request: it was written when the root was at ERROR too (Django gives its own
        logger a level), and team/tests.py checks that such lines never carry a secret."""
        self.assertTrue(written("django.request", logging.WARNING))
        self.assertNotIn("django", settings.QUIET_LOGGERS)

    def test_the_libraries_write_only_their_errors(self):
        for name in (
            "py.warnings",
            "asyncio",
            "urllib3.connectionpool",
            "requests",
            "websockets.client",
            "psycopg.pool",
            "MARKDOWN",
            "PIL.Image",
        ):
            self.assertFalse(written(name, logging.WARNING), name)
            self.assertTrue(written(name, logging.ERROR), name)

    def test_the_handler_still_hides_the_paths_that_open_something(self):
        self.assertEqual(settings.LOGGING["handlers"]["console"]["filters"], ["quiet_paths"])
        self.assertEqual(settings.LOGGING["root"], {"handlers": ["console"], "level": "WARNING"})
