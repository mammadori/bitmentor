import socket
import threading
import time
from unittest.mock import patch

from django.test import SimpleTestCase

from config import network
from config.network import is_public, resolve


class PublicAddress(SimpleTestCase):
    def test_inside_whatever_the_spelling(self):
        for address in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "100.64.0.1", "0.0.0.0", "::1", "::", "fd00::1",
                        "fe80::1", "::ffff:10.0.0.1", "::ffff:127.0.0.1",
                        # IPv4-compatible (deprecated): ipaddress calls them global
                        "::127.0.0.1", "::10.0.0.1", "::8.8.8.8",
                        # NAT64: behind a gateway these are 127.0.0.1 and 10.0.0.1
                        "64:ff9b::7f00:1", "64:ff9b::a00:1", "64:ff9b::127.0.0.1"):
            with self.subTest(address):
                self.assertFalse(is_public(address))

    def test_public(self):
        # A public IPv4 address through NAT64 stays reachable, for a server on an IPv6-only network with DNS64
        for address in ("93.184.216.34", "2606:2800:220:1::1", "::ffff:93.184.216.34", "64:ff9b::5db8:d822"):
            with self.subTest(address):
                self.assertTrue(is_public(address))


def wait_for(condition, seconds=5):
    stop = time.monotonic() + seconds
    while not condition() and time.monotonic() < stop:
        time.sleep(0.01)
    return condition()


class Resolve(SimpleTestCase):
    """Each test has places of its own: lookups left running by other tests in the same process do not count here."""

    def setUp(self):
        self.slots = threading.BoundedSemaphore(network.DNS_AT_ONCE)
        self.enterContext(patch("config.network.DNS_SLOTS", self.slots))

    def free(self):
        return self.slots._value

    def test_a_slow_name_holds_a_thread_of_its_own_not_the_caller(self):
        answer = threading.Event()
        with patch("socket.getaddrinfo", side_effect=lambda *args, **kwargs: answer.wait(5) and []):
            started = time.monotonic()
            with self.assertRaises(network.SlowName):
                resolve("slow.example.org", seconds=0.2)
            self.assertLess(time.monotonic() - started, 0.8)
            # Its place stays taken until the lookup really ends
            self.assertEqual(self.free(), network.DNS_AT_ONCE - 1)
            answer.set()
            self.assertTrue(wait_for(lambda: self.free() == network.DNS_AT_ONCE))

    def test_never_longer_than_dns_seconds_nor_than_the_time_left(self):
        answer = threading.Event()
        self.addCleanup(answer.set)
        with patch("config.network.DNS_SECONDS", 0.2), \
                patch("socket.getaddrinfo", side_effect=lambda *args, **kwargs: answer.wait(5) and []) as asked:
            started = time.monotonic()
            with self.assertRaises(network.SlowName):
                resolve("slow.example.org", seconds=30)
            self.assertLess(time.monotonic() - started, 0.8)
            answer.set()
            self.assertTrue(wait_for(lambda: self.free() == network.DNS_AT_ONCE))
            asked.reset_mock()
            # No time left: nothing is asked
            for left in (0, -1):
                with self.assertRaises(network.SlowName):
                    resolve("example.org", seconds=left)
            asked.assert_not_called()

    def test_a_thread_that_cannot_start_gives_its_place_back(self):
        with patch("config.network.threading.Thread.start", side_effect=RuntimeError("can't start new thread")), \
                patch("socket.getaddrinfo") as asked, self.assertRaises(network.SlowName):
            resolve("example.org")
        asked.assert_not_called()
        self.assertEqual(self.free(), network.DNS_AT_ONCE)

    def test_too_many_lookups_at_once_are_refused_without_waiting(self):
        for _place in range(network.DNS_AT_ONCE):
            self.slots.acquire()
        with patch("socket.getaddrinfo") as asked, self.assertRaises(network.SlowName):
            resolve("example.org")
        asked.assert_not_called()

    def test_answers_and_errors_reach_the_caller(self):
        found = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch("socket.getaddrinfo", return_value=found) as asked:
            self.assertEqual(resolve("example.org", 443, proto=socket.IPPROTO_TCP), found)
        self.assertEqual(asked.call_args.kwargs, {"proto": socket.IPPROTO_TCP})
        with patch("socket.getaddrinfo", side_effect=socket.gaierror(socket.EAI_NONAME, "x")), \
                self.assertRaises(socket.gaierror):
            resolve("nowhere.example.org")
        # The thread gives its place back as it ends, a moment after the caller has the answer
        self.assertTrue(wait_for(lambda: self.free() == network.DNS_AT_ONCE))
