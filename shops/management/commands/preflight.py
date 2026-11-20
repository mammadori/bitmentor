"""Checks before opening real payments: exits with an error if something is not ready."""

import ipaddress
import re
import smtplib
import socket
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.mail import get_connection
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError
from django.db.models import Q
from django.utils import timezone

from config import features, limits, mail
from config.community import community, page_path
from shops import lightning_watch, training, vacation
from shops.models import HAS_REGISTER, Closure, Shop, real, still_trading
from shops.notifications import chats, moderators_emails

YEAR = 31536000
MIN_SECRET = 50


def settings_problems():
    problems = []
    if settings.NETWORK != "mainnet":
        problems.append(f"NETWORK is {settings.NETWORK}")
    if settings.DEBUG:
        problems.append("DEBUG is on")
    if len(settings.SECRET_KEY) < MIN_SECRET:
        problems.append("SECRET_KEY is too short")
    # A mail backend of the community (EMAIL_REAL_BACKEND) sends without EMAIL_HOST (config.mail.leaves)
    if not mail.leaves():
        problems.append("EMAIL_HOST is missing: the sign-in links will not go out")
    # Also in the form «Name <address>»
    _, at, domain = settings.DEFAULT_FROM_EMAIL.rstrip(">").rpartition("@")
    if not at or domain.lower() in ("", "localhost", "example.org", "example.com"):
        problems.append("DEFAULT_FROM_EMAIL must use the domain that has SPF and DKIM")
    if settings.SECURE_HSTS_SECONDS < YEAR:
        problems.append("HSTS_SECONDS is under a year")
    return problems


def mail_problems():
    """The mail server accepts the connection and, if there are any, the credentials. It sends nothing."""
    if not settings.EMAIL_HOST:
        return []
    connection = get_connection(fail_silently=False)
    try:
        connection.open()
        connection.close()
    except (smtplib.SMTPException, OSError) as error:
        return [f"the mail server does not accept the connection: {type(error).__name__}"]
    return []


def site_ipv6(host):
    """The public IPv6 addresses (AAAA) of the site's name: the ones IPv6 visitors will connect to."""
    try:
        found = socket.getaddrinfo(host, 443, socket.AF_INET6, socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return set()
    addresses = {ipaddress.ip_address(info[4][0].split("%")[0]) for info in found}
    return {str(address) for address in addresses if address.is_global}


def own_ipv6(path=Path("/proc/net/if_inet6")):
    """Whether this container has an IPv6 address on its Docker network, loopback and link-local left out. None when
    the system does not say (no /proc)."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        fields = line.split()
        if not fields:
            continue
        address = ipaddress.IPv6Address(int(fields[0], 16))
        if not (address.is_loopback or address.is_link_local):
            return True
    return False


def ipv6_problems():
    """With an AAAA record and a Docker network without IPv6, docker-proxy hands every IPv6 visitor to Caddy from the
    network's gateway: one address for all of them, which shares the request limits and that DEV_ALLOWED_IPS and
    ADMIN_ALLOWED_IPS cannot tell apart (README, "IPv6"). An IPv6 address in the container does not tests the daemon
    translates with ip6tables: that one is checked by hand."""
    if settings.SITE_SCHEME != "https":
        return []  # behind another proxy the AAAA record is the proxy's, and the visitor's address comes from it
    addresses = site_ipv6(settings.SITE_HOST)
    if addresses and own_ipv6() is False:
        return [
            f"{settings.SITE_HOST} has an IPv6 address ({', '.join(sorted(addresses))}) but the Docker network does not: "
            "every IPv6 visitor would come from the same address. Turn on compose.ipv6.yml, with ip6tables "
            'in the Docker daemon, and check by hand with ACCESS_LOG=on (README, "IPv6"), or remove the AAAA record'
        ]
    return []


def lightning_notes():
    """The Lightning addresses that do not work, and a guardian that has stopped: worth knowing, not a reason to stop. A
    merchant's wallet is the merchant's, and the register keeps taking the other payments."""
    watched = lightning_watch.watched()
    notes = []
    broken = watched.filter(lightning_status__in=lightning_watch.BROKEN).count()
    if broken:
        notes.append(f"{broken} shops with a Lightning address that does not work: /admin/shops/shop/?lightning=broken")
    recent = timezone.now() - lightning_watch.CHECK_EVERY * 2
    if watched.exists() and not watched.filter(lightning_checked__gte=recent).exists():
        notes.append("no Lightning address checked in the last 12 hours: is bitmentor-sentinel@<project>.timer on?")
    return notes


def feature_notes():
    """The features the community turned off in community.yml (`features:`), so that whoever opens the site knows what
    the pages, the timers and the notices will not do, and the experimental ones it turned on (`experimental:`). Worth
    knowing, not a reason to stop. With the closures off, the ones still running: what they do to the alarms is not obvious
    (shops/vacation.py, `is_quiet`)."""
    off = features.switched_off()
    notes = [f"features turned off in community.yml (features): {', '.join(off)}"] if off else []
    if features.VACATION in off:
        try:
            notes += closure_notes()
        except DatabaseError as error:
            notes.append(f"the closures running cannot be read ({type(error).__name__}): is migrate missing?")
    on = features.experimental_on()
    if on:
        notes.append(f"experimental features turned on in community.yml (experimental): {', '.join(on)}")
    return notes + team_notes()


def team_notes():
    """Team access (TEAM_ACCESS in the .env, team/access.py) is on: how many wallets it lets in, that outside a site
    under construction it does nothing, and that a pass reaches /admin/'s sign-in page when ADMIN_ALLOWED_IPS is empty.
    Whether anybody hears of new keys and passes is said with the other moderators' messages (moderators_notes). Worth
    knowing, not reasons to stop."""
    from team import access
    from team.models import TeamInvite, TeamKey

    if not access.on():
        if settings.TEAM_AREA:
            return [
                (
                    "the volunteers' area is on (TEAM_AREA=on) but team access is off: it does nothing until TEAM_ACCESS=on "
                    "(docs/features/team-access.md)"
                )
            ]
        return []
    try:
        counts = f"{TeamKey.active().count()} team keys, {TeamInvite.open().count()} open invites (/admin/team/)"
    except DatabaseError as error:
        counts = f"its keys cannot be read ({type(error).__name__}): is migrate missing?"
    if access.active():
        where = ""
    elif access.area_active():
        where = f"; with SITE_MODE {settings.SITE_MODE} it only signs the volunteers in to their area (/team/)"
    else:
        where = f"; with SITE_MODE {settings.SITE_MODE} it does nothing: it works only on a site under construction (development)"
    notes = [f"team access is on (TEAM_ACCESS=on in the .env): {counts}{where}"]
    if access.area_on():
        try:
            keys = TeamKey.active().filter(role=TeamKey.Role.MODERATOR)
            moderators = (
                f"{keys.count()} moderator keys can open it, {keys.filter(approves=True).count()} of them may "
                "approve new shops"
            )
        except DatabaseError as error:
            moderators = f"its keys cannot be read ({type(error).__name__})"
        closed = "" if access.area_active() else f"; with SITE_MODE {settings.SITE_MODE} it is closed"
        notes.append(f"the volunteers' area is on (TEAM_AREA=on): {moderators} (/team/){closed}")
    if not settings.ADMIN_ALLOWED_IPS.strip():
        notes.append(
            "team access: ADMIN_ALLOWED_IPS is empty, so a browser with a team pass reaches the sign-in page of "
            "/admin/ like an allowed address does (the password is still asked). If the team is not the staff, "
            "set ADMIN_ALLOWED_IPS (docs/features/team-access.md)"
        )
    return notes


def moderators_channel():
    """How the moderators' messages travel (shops.notifications.deliver_to_moderators): "telegram" with a bot and a
    moderators' chat, else "mail" with a mail server and an address to write to (`moderators_emails`, else the
    community's `email`), else None: they reach nobody. Mail counts only when it leaves the machine (config.mail.leaves:
    EMAIL_HOST, or a mail backend of the community): a test network's console only writes it to the log of web."""
    if settings.TELEGRAM_BOT_TOKEN and chats(settings.TELEGRAM_MODERATION_CHAT):
        return "telegram"
    address = community().get("email")
    # The moderators' mail goes to `moderators_emails` when it has an address, else to `email` (shops/notifications.py)
    if mail.leaves() and (moderators_emails() or (isinstance(address, str) and "@" in address)):
        return "mail"
    return None


def moderators_notes():
    """(warnings, notes) about the way the site reaches the people who look after it. No way at all is a warning that
    cannot be missed, never a reason to stop: whoever tries the software, or runs a small site alone, is their own
    moderator and reads /admin/today/ in person. Telegram and the moderators' mail stay optional. Mail without Telegram
    is a plain note. The same in every SITE_MODE: it is about the installation."""
    from team import access

    channel = moderators_channel()
    if channel == "telegram":
        return [], []
    if channel == "mail":
        return [], [
            (
                "the moderators' messages go by email (`moderators_emails`, else `email` in community.yml): there "
                "is no Telegram moderators' chat (TELEGRAM_BOT_TOKEN, TELEGRAM_MODERATION_CHAT)"
            )
        ]
    lost = [
        "the morning summary",
        "the new shops waiting for an approval",
        "the notices about the staff accounts' second factor",
    ]
    if access.on():
        lost.append("the team access notices of new keys and passes")
    missing = []
    if not settings.TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN is empty")
    if not chats(settings.TELEGRAM_MODERATION_CHAT):
        missing.append("TELEGRAM_MODERATION_CHAT is empty")
    missing.append(
        "EMAIL_HOST is empty"
        if not mail.leaves()
        else "community.yml has no `moderators_emails` and no `email` to write to"
    )
    return [
        (
            f"the moderators cannot be reached ({'; '.join(missing)}): {', '.join(lost)} will reach nobody. "
            "Fine for a site you look after alone: read /admin/today/ yourself. Otherwise set TELEGRAM_BOT_TOKEN and "
            "TELEGRAM_MODERATION_CHAT, or EMAIL_HOST with `moderators_emails` or `email` in community.yml, and "
            "try it with manage.py notify_test --moderators (docs/operations.md)"
        )
    ], []


def admin_notes():
    """What defends the staff accounts (config/second_factor.py): /admin/ open to every address without a second factor
    required, and how many staff accounts have none the mode accepts. Worth knowing, not reasons to stop; and nothing at
    all on a local try (DEBUG, or SITE_HOST unset or localhost), where `optional` is simply right."""
    from django.contrib.auth import get_user_model

    from config import second_factor

    if settings.DEBUG or settings.SITE_HOST in ("", "localhost", "127.0.0.1"):
        return []
    notes = []
    if not settings.ADMIN_ALLOWED_IPS.strip() and not second_factor.required():
        notes.append(
            "/admin/ is open to every address (ADMIN_ALLOWED_IPS is empty) and ADMIN_SECOND_FACTOR is optional: "
            "right for trying the site with the team. For a community with real shops, a stricter level "
            "(required, or passkey) or ADMIN_ALLOWED_IPS defends the staff accounts beyond their password "
            "(docs/configuration.md)"
        )
    if settings.ADMIN_WALLET_SIGN_IN and second_factor.required():
        notes.append(
            f"ADMIN_WALLET_SIGN_IN is on and ADMIN_SECOND_FACTOR is {second_factor.mode()}: at this level no "
            "wallet signs in to /admin/, and the linked ones stay unused. Set ADMIN_WALLET_SIGN_IN=off "
            "(docs/configuration.md)"
        )
    elif settings.ADMIN_WALLET_SIGN_IN:
        notes.append(
            "ADMIN_WALLET_SIGN_IN is on: a linked wallet alone opens /admin/ for a staff account without a "
            "second factor, one step as the password alone. Right for trying the site with a few friends; a "
            "community with real shops moves to ADMIN_SECOND_FACTOR=required or passkey, where wallets no "
            "longer sign in (manage.py admin_wallet list, docs/configuration.md)"
        )
    staff = list(get_user_model().objects.filter(is_active=True, is_staff=True))
    # In `passkey` mode a TOTP does not count: it only lets an account without a passkey add one
    factors = [second_factor.accepted_factors(user) for user in staff]
    without = sum(1 for found in factors if found.totp is None and not found.passkeys)
    if without:
        what = (
            "they are sent to set one up at their next sign-in"
            if second_factor.required()
            else "each may add one in /admin/second-factor/, if they wish"
        )
        kind = "passkey" if second_factor.mode() == "passkey" else "second factor"
        notes.append(
            f"{without} of {len(staff)} staff accounts have no {kind}: {what} "
            "(manage.py admin_totp list, manage.py admin_passkey list)"
        )
    return notes


def settings_watch_notes():
    """Whether the settings watch is on (website/settings_watch.py) and when the moderators last heard of a change. A
    note, never a reason to stop."""
    from website import settings_watch

    if not settings_watch.enabled():
        return [
            (
                "the settings watch is off (`settings_watch: {enabled: false}` in community.yml): nobody hears of a "
                "security setting changed between two starts"
            )
        ]
    told = settings_watch.last_told() if settings_watch.tables_ready() else None
    when = f"the last change was told on {timezone.localtime(told):%Y-%m-%d %H:%M}" if told else "no change told yet"
    return [f"the settings watch is on: the moderators hear of a security setting changed between two starts ({when})"]


PLACEHOLDER = re.compile(r"\[[A-Z][A-Z0-9 /_]{3,}")


def privacy_notes():
    """The privacy page of the community still holds the template's placeholders ([CONTROLLER NAME] and so on): worth knowing
    before opening the site, not a reason to stop (docs/privacy.md)."""
    found = []
    for language in community()["languages"]:
        path = page_path(language, "privacy")
        if path.exists() and PLACEHOLDER.search(path.read_text()):
            found.append(language)
    return (
        [f"the privacy page still has placeholders to fill in ({', '.join(found)}): see docs/privacy.md"]
        if found
        else []
    )


def training_notes():
    """The registers that wait for their owner's basic pass with nothing to say from which day (`gate: unlock`, no `since`):
    on mainnet they are locked from the moment the training is on. A community that turns it on for shops that already
    sell writes a `since` ahead and tells its merchants first (docs/features/merchant-training.md)."""
    cfg = training.parse()
    if not cfg or "unlock" not in (cfg.gate, cfg.gate_imported) or cfg.since:
        return []
    shops = real(Shop.objects.filter(HAS_REGISTER, owner__isnull=False, owner__trainee__base_earned__isnull=True))
    # The shops of the community's list have their own gate (training.gate_of)
    if cfg.gate != "unlock":
        shops = shops.exclude(external_id="")
    if cfg.gate_imported != "unlock":
        shops = shops.filter(external_id="")
    waiting = shops.count()
    if not waiting:
        return []
    return [
        (
            "the training locks the register without the basic pass (training.gate: unlock) and has no `since` day: "
            f"{waiting} shops with a register wait for their owner's pass at once on mainnet. Write a `training.since` "
            "ahead in community.yml and tell the merchants first"
        )
    ]


def limit_notes():
    """Totals at 80% of their limit or full (config/limits.py), and the limits changed from their defaults: worth knowing, because a
    full limit makes the site refuse new shops or accounts."""
    notes = [f"limit {note}" for note in limits.notes()]
    changed = [f"{key}={value}" for key, value, default in limits.configured() if not default]
    return notes + ([f"limits changed in community.yml (limits): {', '.join(changed)}"] if changed else [])


def closure_notes():
    """The closures running today while the feature is off: one with no day to open again no longer keeps any alarm quiet
    (nobody could end it), one with a day keeps them quiet until that day, and at most MAX_CREDIT days from its first."""
    today = vacation.day_of()
    running = Closure.objects.filter(first_day__lte=today, shop__in=Shop.objects.filter(still_trading(today))).filter(
        Q(reopens_on__isnull=True) | Q(reopens_on__gt=today)
    )
    undated = running.filter(reopens_on__isnull=True).count()
    dated = list(running.filter(reopens_on__isnull=False))
    notes = []
    if undated:
        notes.append(
            f"{undated} closures running with no day to reopen: with the closures off they keep no alarm quiet "
            "any more, those shops raise all the alarms (/admin/ lets you remove or end them)"
        )
    if dated:
        until = max(
            min(closure.reopens_on, closure.first_day + timedelta(days=vacation.MAX_CREDIT)) for closure in dated
        )
        notes.append(
            f"{len(dated)} closures running with a day to reopen: they keep the alarms quiet until that day, "
            f"at most {vacation.MAX_CREDIT} days from the start (the last one until {until})"
        )
    return notes


class Command(BaseCommand):
    help = "Checks the configuration and the mail before going to production"

    def handle(self, *args, **options):
        # First: they are only notes, and must be seen even when a check below stops with an error
        try:
            notes = lightning_notes()
        except DatabaseError as error:
            # A database not migrated yet: the other checks still count
            notes = [
                f"the state of the Lightning addresses cannot be read ({type(error).__name__}): is migrate missing?"
            ]
        try:
            notes += limit_notes()
        except DatabaseError as error:
            notes.append(f"the limits' usage cannot be read ({type(error).__name__}): is migrate missing?")
        try:
            notes += training_notes()
        except DatabaseError as error:
            notes.append(
                f"the registers waiting for the training cannot be read ({type(error).__name__}): is migrate missing?"
            )
        try:
            notes += admin_notes()
        except DatabaseError as error:
            notes.append(
                f"the staff accounts' second factors cannot be read ({type(error).__name__}): is migrate missing?"
            )
        try:
            notes += settings_watch_notes()
        except DatabaseError as error:
            notes.append(f"the settings watch cannot be read ({type(error).__name__}): is migrate missing?")
        warnings, channel_notes = moderators_notes()
        for note in notes + feature_notes() + channel_notes + privacy_notes():
            self.stdout.write(f"good to know: {note}")
        # On stdout, like the notes: a warning never changes the outcome below
        for warning in warnings:
            self.stdout.write(self.style.WARNING(f"WARNING: {warning}"))
        problems = settings_problems() + mail_problems() + ipv6_problems()
        for problem in problems:
            self.stderr.write(problem)
        if problems:
            raise CommandError(f"{len(problems)} problems")
        self.stdout.write("All ready")
