"""Tests run in series in the same thread: a request to /it/ leaves Italian active for the tests that follow,
and those that expect the default language fail or pass depending on the order. After each test it goes back
to the default language."""
import multiprocessing
import unittest

from django.conf import settings
from django.test.runner import DiscoverRunner, ParallelTestSuite, RemoteTestResult, RemoteTestRunner
from django.utils import translation


# Python 3.14 starts the workers with forkserver on Linux, and Django clones a SQLite test database only for fork: the
# tests keep fork, as up to 3.13. Here, when the runner is imported, and not in its __init__: `manage.py test --parallel`
# with no number reads the start method while it parses its arguments, before the runner exists, and with forkserver it
# chose one process, so the whole suite ran in series (internal/ci-checks.sh: 389 s instead of 105 s)
if multiprocessing.get_start_method(allow_none=True) is None:
    multiprocessing.set_start_method("fork")


class CleanTextResult(unittest.TextTestResult):
    def stopTest(self, test):
        translation.deactivate()
        super().stopTest(test)


class CleanRemoteResult(RemoteTestResult):
    # With --parallel the tests run in other processes, with this result class instead of the one above
    def stopTest(self, test):
        translation.deactivate()
        super().stopTest(test)


class CleanRemoteRunner(RemoteTestRunner):
    resultclass = CleanRemoteResult


class CleanParallelSuite(ParallelTestSuite):
    runner_class = CleanRemoteRunner


class Runner(DiscoverRunner):
    parallel_test_suite = CleanParallelSuite

    def setup_test_environment(self, **kwargs):
        # The test client comes from 127.0.0.1, the default of DEV_ALLOWED_IPS in docker-compose.yml: the tests start
        # from a visitor without the private view, and those about it choose the addresses (website/test_visibility.py)
        super().setup_test_environment(**kwargs)
        settings.DEV_ALLOWED_IPS = ""
        # The emails leave before the request answers: the tests read the outbox right after it (config/test_mail.py tests the thread)
        settings.MAIL_IN_BACKGROUND = False
        # A shop made in a test is approved, as one imported or written in /admin/ is: the tests about the map do not
        # each approve their shops. Those about the approval make it wait (approved_on=None, shops/test_approval.py),
        # and /shops/new/ sets it itself (shops/views.py)
        from django.utils import timezone

        from shops.models import Shop

        # On the field's get_default, not its default: the check of the migrations still reads the model's own (none)
        Shop._meta.get_field("approved_on").get_default = timezone.now

    def get_resultclass(self):
        return super().get_resultclass() or CleanTextResult
