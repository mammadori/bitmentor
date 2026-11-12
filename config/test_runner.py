"""Tests run in series in the same thread: a request to /it/ leaves Italian active for the tests that follow,
and those that expect the default language fail or pass depending on the order. After each test it goes back
to the default language."""
import multiprocessing
import unittest

from django.conf import settings
from django.test.runner import DiscoverRunner, ParallelTestSuite, RemoteTestResult, RemoteTestRunner
from django.utils import translation


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

    def __init__(self, *args, **kwargs):
        # Python 3.14 starts the workers with forkserver on Linux, and Django clones a SQLite test database only for
        # fork: --parallel keeps fork, as up to 3.13
        if multiprocessing.get_start_method(allow_none=True) is None:
            multiprocessing.set_start_method("fork")
        super().__init__(*args, **kwargs)

    def setup_test_environment(self, **kwargs):
        # The test client comes from 127.0.0.1, the default of DEV_ALLOWED_IPS in docker-compose.yml: the tests start
        # from a visitor without the private view, and those about it choose the addresses (website/test_visibility.py)
        super().setup_test_environment(**kwargs)
        settings.DEV_ALLOWED_IPS = ""

    def get_resultclass(self):
        return super().get_resultclass() or CleanTextResult
