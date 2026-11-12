"""The network layer of the Lightning address checks (shops/lnaddress.py): the connection that goes only to the addresses
already checked, the watchdog that cuts a slow answer, and the places that keep slow servers from holding the site's
threads. Nothing leaves the machine: the servers are sockets on 127.0.0.1."""
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase
from urllib3.exceptions import NewConnectionError
from urllib3.util.timeout import _DEFAULT_TIMEOUT

from config.network import SlowName

from . import lnaddress


def listening():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    return server


def address_of(server):
    return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", server.getsockname())


def closed_port():
    server = listening()
    found = address_of(server)
    server.close()
    return found


class PinnedSocket(SimpleTestCase):
    def connection(self, **options):
        return SimpleNamespace(socket_options=options.get("socket_options"), timeout=options.get("timeout", _DEFAULT_TIMEOUT))

    def test_it_goes_to_the_first_address_that_answers_with_the_options_and_the_timeout(self):
        server = listening()
        self.addCleanup(server.close)
        connection = self.connection(socket_options=[(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)], timeout=3)
        sock = lnaddress.pinned_socket(connection, [closed_port(), address_of(server)])
        self.addCleanup(sock.close)
        self.assertEqual(sock.getpeername(), server.getsockname())
        self.assertEqual(sock.gettimeout(), 3)
        self.assertTrue(sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY))

    def test_when_none_answers_it_is_a_failed_connection_not_a_crash(self):
        with self.assertRaises(NewConnectionError) as raised:
            lnaddress.pinned_socket(self.connection(), [closed_port(), closed_port()])
        self.assertIn("Failed to establish a new connection", str(raised.exception))


class Watched(SimpleTestCase):
    def test_a_connection_made_after_the_time_is_up_is_cut_at_once(self):
        server = listening()
        self.addCleanup(server.close)
        watchdog = lnaddress.Watchdog(60, public_only=False, pinned=[address_of(server)])
        self.addCleanup(watchdog.close)
        watchdog.cut()
        with requests.Session() as session:
            session.mount("http://", watchdog)
            with self.assertRaises(requests.RequestException):
                session.get(f"http://localhost:{server.getsockname()[1]}/", timeout=2)
        self.assertTrue(watchdog.fired)
        self.assertTrue(watchdog.sockets)

    def test_a_socket_that_cannot_be_shut_down_does_not_stop_the_cut(self):
        watchdog = lnaddress.Watchdog(60)
        broken, fine = MagicMock(), MagicMock()
        broken.shutdown.side_effect = OSError("already closed")
        watchdog.sockets.extend([broken, fine])
        watchdog.cut()
        self.assertTrue(watchdog.fired)
        fine.shutdown.assert_called_once()
        watchdog.close()
        fine.close.assert_called_once()

    def test_a_connection_to_an_address_that_is_not_public_is_refused_before_a_byte_is_sent(self):
        server = listening()
        self.addCleanup(server.close)
        accepted = []
        threading.Thread(target=lambda: accepted.append(server.accept()), daemon=True).start()
        watchdog = lnaddress.Watchdog(60, pinned=[address_of(server)])
        self.addCleanup(watchdog.close)
        with requests.Session() as session:
            session.mount("http://", watchdog)
            with self.assertRaises(requests.ConnectionError):
                session.get(f"http://localhost:{server.getsockname()[1]}/", timeout=2)
        self.assertEqual(watchdog.sockets, [])


class PublicAddresses(SimpleTestCase):
    def test_a_name_that_does_not_resolve_is_a_connection_error_with_the_cause(self):
        error = socket.gaierror(socket.EAI_NONAME, "not known")
        with patch("shops.lnaddress.resolve", side_effect=error), self.assertRaises(requests.ConnectionError) as raised:
            lnaddress.public_addresses("nowhere.example.org", 5)
        self.assertIs(raised.exception.__cause__, error)

    def test_a_resolver_that_fails_in_another_way_or_a_name_that_cannot_be_encoded_is_a_wrong_host(self):
        for failure in (OSError("network down"), UnicodeError("label too long")):
            with patch("shops.lnaddress.resolve", side_effect=failure), self.assertRaises(ValueError):
                lnaddress.public_addresses("x.example.org", 5)

    def test_a_slow_name_is_not_a_wrong_host(self):
        with patch("shops.lnaddress.resolve", side_effect=SlowName("late")), self.assertRaises(SlowName):
            lnaddress.public_addresses("slow.example.org", 5)

    def test_one_private_address_among_public_ones_refuses_the_name(self):
        info = lambda ip: (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))  # noqa: E731
        with patch("shops.lnaddress.resolve", return_value=[info("93.184.216.34"), info("10.0.0.1")]):
            with self.assertRaises(ValueError):
                lnaddress.public_addresses("rebind.example.org", 5)
        with patch("shops.lnaddress.resolve", return_value=[]), self.assertRaises(ValueError):
            lnaddress.public_addresses("empty.example.org", 5)


class Fetch(SimpleTestCase):
    def test_only_https_to_a_public_name_on_the_standard_port_within_a_budget(self):
        for url in ("http://a.example.org/x", "https://a.example.org:8443/x", "https:///x", "https://localhost/x",
                    "https://printer.local/x", "ftp://a.example.org/x"):
            with self.assertRaises(ValueError, msg=url):
                lnaddress.fetch(url)
        for kwargs in ({"seconds": 0}, {"seconds": -1}, {"size": 0}):
            with self.assertRaises(ValueError, msg=str(kwargs)):
                lnaddress.fetch("https://a.example.org/x", **kwargs)

    def test_the_name_that_took_all_the_time_leaves_none_for_the_connection(self):
        with patch("shops.lnaddress.public_addresses", return_value=[("fake",)]), \
                patch("shops.lnaddress.time.monotonic", side_effect=[0, 1000]), self.assertRaises(SlowName):
            lnaddress.fetch("https://a.example.org/x", seconds=5)

    def test_an_answer_cut_by_the_watchdog_is_never_taken_as_complete(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.iter_content.return_value = [b'{"tag": "payRequest"}']
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.return_value = response
        cut = SimpleNamespace(fired=True)
        with patch("shops.lnaddress.public_addresses", return_value=[("fake",)]), \
                patch("shops.lnaddress.requests.Session", return_value=session), \
                patch("shops.lnaddress.Watchdog", return_value=cut), self.assertRaises(ValueError):
            lnaddress.fetch("https://a.example.org/x")
        # Not cut, the same bytes are the answer
        cut.fired = False
        with patch("shops.lnaddress.public_addresses", return_value=[("fake",)]), \
                patch("shops.lnaddress.requests.Session", return_value=session), \
                patch("shops.lnaddress.Watchdog", return_value=cut):
            self.assertEqual(lnaddress.fetch("https://a.example.org/x"), {"tag": "payRequest"})

    def test_an_answer_too_nested_to_read_is_a_wrong_answer(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.iter_content.return_value = [b"[" * 50000]
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.return_value = response
        with patch("shops.lnaddress.public_addresses", return_value=[("fake",)]), \
                patch("shops.lnaddress.requests.Session", return_value=session), \
                patch("shops.lnaddress.Watchdog", return_value=SimpleNamespace(fired=False)):
            with self.assertRaises(ValueError):
                lnaddress.fetch("https://a.example.org/x", size=100000)


class Missing(SimpleTestCase):
    def test_a_resolver_that_breaks_is_not_a_typo(self):
        for failure in (OSError("network down"), UnicodeError("label too long")):
            with patch("shops.lnaddress.resolve", side_effect=failure):
                self.assertFalse(lnaddress.missing("x.example.org"), failure)

    def test_only_the_dns_saying_the_name_does_not_exist_is_a_typo(self):
        with patch("shops.lnaddress.resolve", side_effect=socket.gaierror(socket.EAI_NONAME, "unknown")):
            self.assertTrue(lnaddress.missing("typo.example.org"))
        with patch("shops.lnaddress.resolve", side_effect=socket.gaierror(socket.EAI_AGAIN, "try later")):
            self.assertFalse(lnaddress.missing("typo.example.org"))
        with patch("shops.lnaddress.resolve", return_value=[("x",)]):
            self.assertFalse(lnaddress.missing("exists.example.org"))


class Bounded(SimpleTestCase):
    def test_a_pool_that_was_shut_down_is_busy_and_gives_the_place_back(self):
        pool = ThreadPoolExecutor(max_workers=1)
        pool.shutdown()
        slots = threading.BoundedSemaphore(1)
        self.assertEqual(lnaddress.bounded(slots, pool, 1, lambda: "x"), lnaddress.BUSY)
        # The place is free: it can be taken again
        self.assertTrue(slots.acquire(blocking=False))

    def test_every_place_taken_is_busy_at_once_and_a_late_work_keeps_its_place_until_it_ends(self):
        pool = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(pool.shutdown)
        slots = threading.BoundedSemaphore(1)
        go, started = threading.Event(), threading.Event()

        def slow():
            started.set()
            go.wait(5)
            return "done"

        self.assertEqual(lnaddress.bounded(slots, pool, 0.05, slow), lnaddress.LATE)
        started.wait(5)
        self.assertEqual(lnaddress.bounded(slots, pool, 0.05, lambda: "other"), lnaddress.BUSY)
        go.set()
        pool.submit(lambda: None).result(5)
        self.assertEqual(lnaddress.bounded(slots, pool, 5, lambda: "again"), "again")
