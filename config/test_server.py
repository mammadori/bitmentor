"""The server side: headers, request limits, gunicorn and Caddy agreeing with each other."""
import os
import re
import runpy
import tempfile
from pathlib import Path
from unittest.mock import patch

import yaml
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase

ROOT = Path(__file__).resolve().parent.parent
CADDYFILE = (ROOT / "Caddyfile").read_text()
MIB = {"KiB": 1024, "MiB": 1024 * 1024}


def compose_seconds(text):
    """A duration written in docker-compose.yml, in seconds: `35s`, `1m30s`."""
    minutes, seconds = re.fullmatch(r"(?:(\d+)m)?(?:(\d+)s)?", text).groups()
    return int(minutes or 0) * 60 + int(seconds or 0)


def caddy_size(name):
    """A size written in the Caddyfile, in bytes: `max_size 1MiB`."""
    number, unit = re.search(rf"\b{name} (\d+)(KiB|MiB)\b", CADDYFILE).groups()
    return int(number) * MIB[unit]


class ResourcePolicy(TestCase):
    """Cross-Origin-Resource-Policy: pages and data only from the site, public files from anywhere."""

    def test_pages_and_data_stay_on_the_site(self):
        for path in ("/it/", "/health/", "/shops.json", "/it/no-such-page/"):
            self.assertEqual(self.client.get(path)["Cross-Origin-Resource-Policy"], "same-origin", path)

    def test_public_files_can_be_shown_elsewhere(self):
        # the theme logo is the Nostr profile image: a Nostr client shows it from another site
        self.assertEqual(self.client.get("/theme/logo.svg")["Cross-Origin-Resource-Policy"], "cross-origin")
        self.assertEqual(self.client.get("/static/base.css")["Cross-Origin-Resource-Policy"], "cross-origin")

    def test_nostr_names_are_still_readable_by_clients(self):
        # clients read it with CORS, which the policy does not touch
        self.assertEqual(self.client.get("/.well-known/nostr.json")["Access-Control-Allow-Origin"], "*")


class RequestBodies(TestCase):
    def test_files_are_dropped_not_stored(self):
        upload = SimpleUploadedFile("grande.bin", b"\0" * 4 * 1024 * 1024)
        request = RequestFactory().post("/it/shops/recovery/", {"email": "a@example.org", "file": upload})
        with tempfile.TemporaryDirectory() as folder, self.settings(FILE_UPLOAD_TEMP_DIR=folder):
            self.assertEqual(request.POST["email"], "a@example.org")
            self.assertEqual(len(request.FILES), 0)
            self.assertEqual(os.listdir(folder), [])

    def test_a_form_over_the_limit_is_refused(self):
        with self.assertLogs("django.security.RequestDataTooBig", "ERROR"):
            response = self.client.post("/it/shops/recovery/", "email=" + "x" * settings.DATA_UPLOAD_MAX_MEMORY_SIZE,
                                        content_type="application/x-www-form-urlencoded")
        self.assertEqual(response.status_code, 400)

    def test_caddy_and_django_have_the_same_limit(self):
        self.assertEqual(caddy_size("max_size"), settings.DATA_UPLOAD_MAX_MEMORY_SIZE)
        # Caddy holds the whole body before passing it on: a slow client does not occupy a gunicorn thread
        self.assertEqual(caddy_size("request_buffers"), caddy_size("max_size"))


class Gunicorn(TestCase):
    def load(self, **environment):
        with patch.dict(os.environ, environment):
            return runpy.run_path(str(ROOT / "config" / "gunicorn.py"))

    def test_defaults_fit_a_four_cpu_machine(self):
        config = self.load(WEB_CONCURRENCY="", WEB_THREADS="")
        self.assertEqual((config["workers"], config["threads"]), (3, 8))

    def test_workers_and_threads_come_from_the_environment(self):
        config = self.load(WEB_CONCURRENCY="5", WEB_THREADS="2")
        self.assertEqual((config["workers"], config["threads"]), (5, 2))

    def test_gunicorn_keeps_connections_longer_than_caddy(self):
        # if gunicorn closed first, Caddy would sometimes send a request on an already closed connection
        caddy = int(re.search(r"\bkeepalive (\d+)s\b", CADDYFILE)[1])
        self.assertGreater(self.load()["keepalive"], caddy)

    def test_docker_waits_for_the_requests_in_progress(self):
        # on stop Docker sends SIGTERM and, after stop_grace_period (10 seconds if missing), SIGKILL: gunicorn must have
        # closed the requests in progress before
        web = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["web"]
        self.assertGreater(compose_seconds(web.get("stop_grace_period", "10s")), self.load()["graceful_timeout"])


class ComposeEnvironment(TestCase):
    # Read by settings.py, and not meant to come from the .env through compose: the version is baked into the image, the
    # others are for development and tests
    # OSM_TOKEN is the OpenStreetMap account's token for manage.py osm_upload: it is passed for that one run (`docker compose exec
    # -e OSM_TOKEN=… web …`, docs/features/openstreetmap.md), never kept in the .env that feeds the container that serves the site
    NOT_FROM_THE_ENV = {"BITMENTOR_VERSION", "SITE_VERSION", "DEBUG", "POSTGRES_DB", "SQLITE_PATH", "TELEGRAM_API", "OSM_TOKEN"}

    def test_web_gets_every_setting_the_env_file_can_set(self):
        # A variable that settings.py reads and compose does not hand to web can be written in the .env and never arrives (the
        # key of the Telegram bot was one: the alerts never went out)
        web = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["web"]["environment"]
        read = set(re.findall(r'os\.environ(?:\.get)?[(\[]"([A-Z0-9_]+)"', (ROOT / "config" / "settings.py").read_text()))
        self.assertEqual(sorted(read - set(web) - self.NOT_FROM_THE_ENV), [])
        self.assertGreater(len(read & set(web)), 20)  # the pattern still finds the settings


class Caddy(TestCase):
    def test_only_reads_wait_for_a_restart(self):
        # on the second attempt Caddy would send a POST with an empty body
        retry = re.search(r"reverse_proxy (@\w+) web:8000 \{[^}]*lb_try_duration", CADDYFILE)
        self.assertIsNotNone(retry)
        self.assertIn(f"{retry[1]} method GET HEAD\n", CADDYFILE)
        self.assertEqual(CADDYFILE.count("lb_try_duration"), 1)

    def test_every_site_hides_the_software(self):
        sites = re.findall(r"^\S[^\n]*\{\n(.*?)^\}", CADDYFILE, re.M | re.S)
        sites = [body for body in sites if "import /etc/caddy/mode/" in body]
        self.assertEqual(len(sites), 2)
        for body in sites:
            self.assertIn("import anonymous", body)


class HeadRequests(TestCase):
    """On HEAD gunicorn also sent the file: with persistent connections the extra bytes ended up in the next response."""

    def reply(self, view, method="head"):
        from django.http import FileResponse, HttpResponse
        from config.middleware import HeadWithoutBody
        request = getattr(RequestFactory(), method)("/theme/x")
        make = {"file": lambda r: FileResponse(open(ROOT / "manage.py", "rb")), "page": lambda r: HttpResponse(b"hello")}[view]
        return HeadWithoutBody(make)(request)

    def test_a_file_answers_head_with_headers_and_no_body(self):
        response = self.reply("file")
        self.assertEqual(b"".join(response.streaming_content), b"")
        self.assertEqual(int(response["Content-Length"]), (ROOT / "manage.py").stat().st_size)
        self.assertIsNone(getattr(response, "file_to_stream", None))

    def test_a_page_answers_head_with_no_body(self):
        self.assertEqual(self.reply("page").content, b"")

    def test_get_keeps_the_body(self):
        self.assertEqual(self.reply("page", "get").content, b"hello")
        self.assertNotEqual(b"".join(self.reply("file", "get").streaming_content), b"")

    def test_it_comes_first(self):
        self.assertEqual(settings.MIDDLEWARE[0], "config.middleware.HeadWithoutBody")


class CaddyErrorLog(TestCase):
    """The error log of Caddy writes the whole request: what opens something never stays in its query."""

    def test_the_error_log_drops_the_secrets_of_the_query(self):
        error_log = CADDYFILE.split("log default {")[1].split("\n\t}")[0]
        query = error_log.split("request>uri query {")[1].split("}")[0]
        deleted = re.findall(r"delete (\w+)", query)
        # k: the staff register; c: the code of a register; code: the link that takes an
        # imported shop; token: the links in the emails
        # next: the page to go back to after signing in, which carries the others inside it (next=/…/claim/?code=…)
        for name in ("k", "c", "code", "token", "next"):
            self.assertIn(name, deleted)
        self.assertIn("request>headers>Referer delete", error_log)
        self.assertIn("request>headers>X-Register-Key delete", error_log)

    def test_the_error_log_keeps_only_the_network_of_an_address(self):
        """As the access log: /24 or /48, never the whole address, of the visitor and of the connection."""
        error_log = CADDYFILE.split("log default {")[1].split("\n\t}")[0]
        for field in ("request>client_ip", "request>remote_ip"):
            masked = re.search(field + r" ip_mask \{\s*ipv4 (\S+)\s*ipv6 (\S+)\s*\}", error_log)
            self.assertIsNotNone(masked, field)
            # The defaults, which each installation can change (LOG_IPV4_MASK, LOG_IPV6_MASK)
            self.assertEqual(masked.groups(), ("{$LOG_IPV4_MASK:24}", "{$LOG_IPV6_MASK:48}"))
        self.assertIn("request>remote_port delete", error_log)
        # Behind the entry proxy the whole address is in the forwarding headers: they go too
        for header in ("X-Forwarded-For", "Forwarded", "X-Real-Ip"):
            self.assertIn(f"request>headers>{header} delete", error_log)

    def test_the_entry_proxy_example_says_no_software_nor_proxies(self):
        entry = (ROOT / "deploy" / "examples" / "entry" / "Caddyfile").read_text()
        snippet = entry.split("(anonymous) {")[1].split("\n}")[0]
        self.assertIn("-Server", snippet)
        self.assertIn("-Via", snippet)
        sites = [block for block in entry.split("\n\n") if "\treverse_proxy" in block]
        self.assertTrue(sites)
        for block in sites:
            self.assertIn("import anonymous", block)

    def test_the_secrets_travel_under_the_names_the_log_drops(self):
        """If a link changes the name of its parameter, the Caddyfile must change too: the names are read from the code."""
        source = " ".join((ROOT / "shops" / name).read_text() for name in ("views.py", "staff.py"))
        self.assertIn('request.GET.get("token"', source)
        self.assertIn('urlencode({\'token\': token})', source)
        self.assertIn('request.GET.get("code"', source)
        self.assertIn("request.GET.get(\"c\"", source)

    # Query parameters the site reads that carry nothing that opens something. A new one is a decision: either the
    # Caddyfile drops it from the error log, or it goes here with the reason.
    HARMLESS = {
        "qr": "whether the sign shows the QR code",
        "key": "the public key a wallet signs with (LNURL-auth)",
        "sig": "a signature of a challenge that a QR code shows to anyone, valid for a few minutes",
        "w": "the wallet a customer picked from the community's list, on the path for customers without one",
        "on": "the kind of phone a customer picked on that path",
        "shop": "the shop the merchant's training path is about, one of their own (shops/training_views.py): its id is public anyway",
        "a": "an address of the fake explorer of a test network (shops/explorer.py), never on mainnet",
    }

    def test_every_parameter_of_a_query_the_site_reads_is_dropped_or_harmless(self):
        error_log = CADDYFILE.split("log default {")[1].split("\n\t}")[0]
        deleted = set(re.findall(r"delete (\w+)", error_log.split("request>uri query {")[1].split("}")[0]))
        read = set()
        for folder in ("shops", "website", "config", "team"):
            for path in (ROOT / folder).rglob("*.py"):
                if path.name.startswith("test") or path.name == "tests.py":
                    continue
                read |= set(re.findall(r"request\.GET(?:\.get\(|\[)[\"']([A-Za-z0-9_]+)[\"']", path.read_text()))
        self.assertTrue({"k", "c", "code", "token", "next"} <= read, read)
        self.assertEqual(read - deleted - set(self.HARMLESS), set(),
                         "a query parameter the site reads is neither dropped by the Caddyfile nor known harmless")
        # And the harmless ones are still read: a name nobody reads any more leaves the list
        self.assertEqual(set(self.HARMLESS) - read, set())
