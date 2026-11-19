"""The settings watch: when the site starts with a security setting different from the previous start, the moderators
get one message that says which setting, the value before and the value now.

It is there for the mistake and the unannounced change (a level lowered «for a moment» and forgotten, a deploy with an
old `.env`), not against whoever controls the machine: they can turn it off or change the database.

**The list.** `WATCHED` below, one line per setting: its name, how to read it, and whether it is secret. A setting
that is not secret is stored as it is and shown in the message; a secret is never stored nor shown, only a keyed
fingerprint (`fingerprint`), and the message says only that it changed.

**When.** Once per start of the site: gunicorn's master runs `manage.py settings_watch compare` in a process of its own
when it is ready (config/gunicorn.py), so the start waits for nothing. Two containers that start together compare
once: the start that claims the row of `SettingsWatch` does it, the other does nothing. Whatever goes wrong is a warning
in the log, and the site starts all the same; a message that did not leave is tried again at the next start, because
the stored values are the ones the moderators were last told of.

**Quiet.** The first start, and a setting a later release adds to the list, are recorded without a message; a setting
taken off the list is forgotten. With no moderators' channel only the old recipients of a change of the channel are
tried. `settings_watch: {enabled: false}` in community.yml turns it off: the moderators are told once, then what was
stored is deleted and nothing is compared. The chats and addresses of the moderators are stored, to reach the old ones,
but the message names none: it counts them. The user and password in an address are kept only as a fingerprint.

Not a defence against whoever controls the machine or edits the `.env`: they can turn it off, change the database, or
redirect the messages (the moderators' chat and the bot token changed together)."""
import hashlib
import hmac
import json
import logging
import re
from collections import namedtuple
from datetime import timedelta
from smtplib import SMTPException

from django.conf import settings
from django.db import connection
from django.db.models import Q
from django.utils import formats, timezone, translation
from django.utils.translation import gettext as _

from config.community import community

logger = logging.getLogger(__name__)

# The block of community.yml and its one key
OPTIONS = "settings_watch"
ENABLED = "enabled"
# How long a start may take to compare and send before another start may do it in its place
CLAIM = timedelta(minutes=5)
# The longest value a message shows of a setting
SHOWN_LENGTH = 200
# What a fingerprint is keyed with, besides SECRET_KEY: these fingerprints are of no use as anything else
FINGERPRINT_LABEL = b"bitmentor settings watch v1"

# A setting of the list: `read` gives its value now (text, a number, true/false or a list of texts); `secret` keeps
# only its fingerprint; `lowers(before, now)` says whether the change lowers a defence, and puts it first in the
# message; `reaches` is "telegram" or "mail" for the settings that say where the moderators' messages go: a change of
# them is told to the old recipients too; `counted` is for a list of network addresses (the team's homes and offices):
# the message, which every moderator reads, says how many were removed and added, never the addresses.
Watched = namedtuple("Watched", "name read secret lowers reaches counted", defaults=(False, None, None, False))


def setting(name):
    return lambda: getattr(settings, name)


def moderators_chats():
    from shops.notifications import chats

    return chats(settings.TELEGRAM_MODERATION_CHAT)


def moderators_mail():
    """Where the moderators' messages go by email: `moderators_emails`, else the community's `email`
    (shops.notifications.email_moderators)."""
    from shops.notifications import moderators_emails

    found = moderators_emails()
    if found:
        return found
    address = community().get("email")
    return [address.strip()] if isinstance(address, str) and "@" in address else []


SECOND_FACTOR_RANKS = {"optional": 0, "required": 1, "passkey": 2}


def second_factor_lowered(before, now):
    return SECOND_FACTOR_RANKS.get(now, 0) < SECOND_FACTOR_RANKS.get(before, 0)


def emptied(before, now):
    return bool(str(before or "").strip()) and not str(now or "").strip()


def turned_on(before, now):
    return now is True and before is not True


def turned_off(before, now):
    return now == "off" and before != "off"


def raised(before, now):
    try:
        return int(now) > int(before)
    except (TypeError, ValueError):
        return False


def trusted_more(before, now):
    """Whose forwarded address is believed: any other value than before, unless emptied, may let someone pass for an
    allowed address."""
    return bool(str(now or "").strip()) and now != before


WATCHED = (
    # Who reaches /admin/, how and for how long
    Watched("ADMIN_SECOND_FACTOR", setting("ADMIN_SECOND_FACTOR"), lowers=second_factor_lowered),
    Watched("ADMIN_ALLOWED_IPS", setting("ADMIN_ALLOWED_IPS"), lowers=emptied, counted=True),
    Watched("ADMIN_SESSION_HOURS", setting("ADMIN_SESSION_HOURS"), lowers=raised),
    Watched("ADMIN_WALLET_SIGN_IN", setting("ADMIN_WALLET_SIGN_IN"), lowers=turned_on),
    # Who else signs in, and who sees the site and its private part
    Watched("TEAM_ACCESS", setting("TEAM_ACCESS"), lowers=turned_on),
    Watched("TEAM_AREA", setting("TEAM_AREA"), lowers=turned_on),
    Watched("SITE_MODE", setting("SITE_MODE")),
    Watched("DEV_ALLOWED_IPS", setting("DEV_ALLOWED_IPS"), counted=True),
    # Caddy: the robots' probes, and whose word on the visitor's address is believed
    Watched("REFUSE_PROBES", setting("REFUSE_PROBES"), lowers=turned_off),
    Watched("TRUSTED_PROXIES", setting("TRUSTED_PROXIES"), lowers=trusted_more),
    # Where the moderators' messages go, these ones included
    Watched("TELEGRAM_MODERATION_CHAT", moderators_chats, reaches="telegram"),
    Watched("moderators_emails", moderators_mail, reaches="mail"),
    # The names the site answers to, its links, its scheme, the onion address Tor Browser is sent to, HTTPS only
    Watched("ALLOWED_HOSTS", setting("ALLOWED_HOSTS")),
    Watched("PUBLIC_URL", setting("PUBLIC_URL")),
    Watched("SITE_SCHEME", setting("SITE_SCHEME")),
    Watched("ONION_LOCATION", setting("ONION_LOCATION")),
    Watched("HSTS_SECONDS", setting("SECURE_HSTS_SECONDS")),
    Watched("DEBUG", setting("DEBUG"), lowers=turned_on),
    # Who receives the sign-in links and the moderators' messages on their way
    Watched("EMAIL_HOST", setting("EMAIL_HOST")),
    Watched("EMAIL_REAL_BACKEND", setting("EMAIL_REAL_BACKEND")),
    Watched("TELEGRAM_API", setting("TELEGRAM_API")),
    # Secrets: only a fingerprint
    Watched("SECRET_KEY", setting("SECRET_KEY"), secret=True),
    Watched("TELEGRAM_BOT_TOKEN", setting("TELEGRAM_BOT_TOKEN"), secret=True),
    Watched("NOSTR_SECRET_KEY", setting("NOSTR_SECRET_KEY"), secret=True),
)


# --- The switch -------------------------------------------------------------------------------------------------------


def enabled(warn=None):
    """True unless community.yml says `settings_watch: {enabled: false}`. What cannot be used counts as on, and is told
    to `warn`: the watch never goes quiet by mistake."""
    say = warn or (lambda problem: None)
    data = community().get(OPTIONS)
    if data is None:
        return True
    if not isinstance(data, dict):
        say(f"`{OPTIONS}` is a block with the key {ENABLED}, true or false: {data!r}; it stays on")
        return True
    for name in data:
        if name != ENABLED:
            say(f"`{OPTIONS}.{name}` is not a setting of the settings watch (it has: {ENABLED}): it does nothing")
    value = data.get(ENABLED, True)
    if not isinstance(value, bool):
        say(f"`{OPTIONS}.{ENABLED}` must be true or false, not {value!r}: it stays on")
        return True
    return value


def problems():
    """What is wrong in the `settings_watch` block, for `manage.py check` (website/apps.py)."""
    found = []
    enabled(found.append)
    return found


# --- Values and fingerprints ------------------------------------------------------------------------------------------


def fingerprint(name, value):
    """A secret's fingerprint: HMAC-SHA256 keyed with K = HMAC-SHA256(SECRET_KEY, FINGERPRINT_LABEL), of the setting's
    name and its value. Without SECRET_KEY, which is not in the database, a fingerprint of another secret tests no
    guess. SECRET_KEY's own is keyed with what it derives from itself: a change of it changes both the key and the
    value, so it is seen; and testing a guess of it against this fingerprint costs what testing it against any value the
    site signs for anyone (a session cookie) costs. An empty secret has no fingerprint: "" (it is shown as removed or
    set, which preflight says anyway)."""
    if not value:
        return ""
    key = hmac.new(settings.SECRET_KEY.encode(), FINGERPRINT_LABEL, hashlib.sha256).digest()
    return hmac.new(key, f"{name}\0{value}".encode(), hashlib.sha256).hexdigest()


# The user and password in an address (https://user:pass@host/…): never stored nor shown
USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/?#@\s]*)@")


def without_credentials(text):
    """(the text with the user and password of every address in it taken out, what was taken out)."""
    taken = []

    def cut(found):
        taken.append(found[2])
        return found[1]

    return USERINFO.sub(cut, text), taken


def normalized(value):
    """(the value as it is stored and shown, the credentials taken out of it)."""
    if isinstance(value, (bool, int)):
        return value, []
    if isinstance(value, (list, tuple)):
        items, taken = [], []
        for item in value:
            text, cut = without_credentials(str(item))
            items.append(text)
            taken += cut
        return items, taken
    return without_credentials("" if value is None else str(value))


def current(warn):
    """{name: (value as JSON, fingerprint)} of every setting of the list that can be read now. One that cannot is left
    out and told to `warn` by name: its record stays as it is."""
    found = {}
    for entry in WATCHED:
        try:
            value = entry.read()
            if entry.secret:
                found[entry.name] = ("", fingerprint(entry.name, value))
            else:
                # The credentials of an address are kept only as a fingerprint: a change of them alone is still seen
                value, taken = normalized(value)
                found[entry.name] = (json.dumps(value), fingerprint(entry.name, "\0".join(taken)))
        except Exception as error:  # noqa: BLE001 (a setting that cannot be read never stops the start)
            warn(f"{entry.name} cannot be read ({type(error).__name__})")
    return found


def loaded(value):
    try:
        return json.loads(value) if value else ""
    except ValueError:
        return value


def shown(value):
    """A value as the message and `show` write it: one line, no control characters, at most SHOWN_LENGTH characters."""
    from shops.notifications import plain

    if value is True or value is False:
        text = "on" if value else "off"
    elif isinstance(value, list):
        text = ", ".join(value)
    else:
        text = str(value)
    text = plain(" ".join(without_credentials(text)[0].split()))
    if not text:
        return _("(empty)")
    return text if len(text) <= SHOWN_LENGTH else text[:SHOWN_LENGTH - 1] + "…"


# --- The database -----------------------------------------------------------------------------------------------------


def tables_ready():
    """Whether migrate created the tables: before the first one nothing happens."""
    from .models import SettingsWatch, WatchedSetting

    names = connection.introspection.table_names()
    return WatchedSetting._meta.db_table in names and SettingsWatch._meta.db_table in names


class Claim:
    """The comparison of this start, taken atomically: one start only among those running at once has it. A claim left
    behind by a start that died ends after CLAIM. `until` is also the token: holding it longer or letting it go
    changes only the claim this start took, never one another start took after it ended."""

    def __init__(self):
        self.until = None

    def take(self, now):
        from .models import SettingsWatch

        SettingsWatch.objects.bulk_create([SettingsWatch(pk=1)], ignore_conflicts=True)
        if SettingsWatch.objects.filter(pk=1).filter(Q(claimed_until__isnull=True) | Q(claimed_until__lt=now)).update(
                claimed_until=now + CLAIM):
            self.until = now + CLAIM
        return self.until is not None

    def hold(self):
        """Another CLAIM from now, before each sending that may be slow (a mail server that takes its time)."""
        from .models import SettingsWatch

        until = timezone.now() + CLAIM
        if self.until and SettingsWatch.objects.filter(pk=1, claimed_until=self.until).update(claimed_until=until):
            self.until = until

    def release(self):
        from .models import SettingsWatch

        if self.until:
            SettingsWatch.objects.filter(pk=1, claimed_until=self.until).update(claimed_until=None)


def forget():
    """Deletes what was recorded: the next start records again, without a message."""
    from .models import SettingsWatch, WatchedSetting

    WatchedSetting.objects.all().delete()
    SettingsWatch.objects.all().delete()


def last_told():
    """When the moderators were last told of a change, or None."""
    from .models import SettingsWatch

    return SettingsWatch.objects.filter(pk=1).values_list("told", flat=True).first()


# --- The message ------------------------------------------------------------------------------------------------------


Change = namedtuple("Change", "entry before after lowered")


def changes_of(stored, now):
    """The settings of the list whose record differs from now: (changes, lowerings first, then in the order of the
    list; whether SECRET_KEY changed). A secret that differs while SECRET_KEY changed cannot be compared: its
    fingerprint was keyed with the old one, so it is not a change, only recorded again."""
    key_changed = "SECRET_KEY" in stored and "SECRET_KEY" in now and stored["SECRET_KEY"][1] != now["SECRET_KEY"][1]
    found = []
    for entry in WATCHED:
        if entry.name not in stored or entry.name not in now or stored[entry.name] == now[entry.name]:
            continue
        if entry.secret:
            before, after = stored[entry.name][1], now[entry.name][1]
            if key_changed and entry.name != "SECRET_KEY" and before and after:
                continue
            found.append(Change(entry, before, after, False))
        else:
            before, after = loaded(stored[entry.name][0]), loaded(now[entry.name][0])
            if before == after and key_changed and stored[entry.name][1] and now[entry.name][1]:
                # Only the fingerprint of the credentials in an address, keyed with the old SECRET_KEY
                continue
            found.append(Change(entry, before, after, bool(entry.lowers and entry.lowers(before, after))))
    found.sort(key=lambda change: not change.lowered)
    return found, key_changed


def recipients(value):
    return {str(item).strip().lower() for item in value} if isinstance(value, list) else set()


def addresses(value):
    """The addresses and networks of a setting written as text, separated by spaces or commas."""
    return set(str(value or "").lower().replace(",", " ").split())


def line(change):
    name = change.entry.name
    if change.entry.secret:
        if not change.before:
            text = _("{name}: set").format(name=name)
        elif not change.after:
            text = _("{name}: removed").format(name=name)
        else:
            text = _("{name}: changed").format(name=name)
    elif change.entry.reaches or change.entry.counted:
        # Where the messages go is said by counts only: the old recipients and the new read the same message, and
        # neither learns the other's addresses or chats (a typo would send them to a stranger). The same for the
        # network addresses of who reaches /admin/: every moderator reads this, and they are people's homes
        read = recipients if change.entry.reaches else addresses
        old, new = read(change.before), read(change.after)
        if change.entry.counted and old and not new:
            text = _("{name}: removed: {removed}, now empty").format(name=name, removed=len(old))
        else:
            text = _("{name}: removed: {removed}, added: {added}").format(
                name=name, removed=len(old - new), added=len(new - old))
    elif change.before == change.after:
        # Only the user or the password in the address changed: neither is ever stored nor shown
        text = _("{name}: the credentials in the address changed").format(name=name)
    else:
        text = f"{name}: {shown(change.before)} → {shown(change.after)}"
    return f"⚠ {_('lowered')}: {text}" if change.lowered else f"· {text}"


def message(changes, key_changed, now):
    """The text, in the site's language: the site, the time of this start, one line a change, and what to do. Nothing
    about the machine, and never a secret's value or fingerprint."""
    from shops.management.commands.dashboard import site_label

    with translation.override(settings.LANGUAGE_CODE):
        when = formats.date_format(timezone.localtime(now), "DATETIME_FORMAT")
        lines = [site_label(), _("Security settings changed since the previous start of the site ({time}):").format(time=when)]
        lines += [line(change) for change in changes]
        if key_changed:
            lines.append(_("The other secrets cannot be compared this time, because SECRET_KEY changed: they are "
                           "recorded again."))
        lines.append(_("If nobody of the team did this, look at the machine's .env."))
        return str(_("Settings changed")), "\n".join(lines)


# --- Sending ----------------------------------------------------------------------------------------------------------


def deliver(subject, text, changes, hold=lambda: None):
    """Sends the message to the moderators as their other messages go, and to the old recipients of a change of where
    they go. Returns (done, sent, problems): `sent` when someone got it; `done` is False when it must be tried again at
    the next start: with a moderators' channel, when the message did not reach it; without one, when an old recipient
    could not be reached for a reason that may pass. With nobody to tell at all, it is done: nothing is sent."""
    from config import mail
    from shops.management.commands.preflight import moderators_channel
    from shops.notifications import (
        NotificationFailed,
        chats,
        deliver_to_moderators,
        telegram,
    )

    channel = moderators_channel()
    problems, reached = [], set()
    done = sent = True
    if channel:
        hold()
        where, problems = deliver_to_moderators(subject, text)
        done = sent = where is not None
        if where == "TELEGRAM_MODERATION_CHAT":
            reached = {f"telegram:{chat}" for chat in chats(settings.TELEGRAM_MODERATION_CHAT)}
        elif where is not None:
            reached = {f"mail:{address.lower()}" for address in moderators_mail()}
    # A change of where the messages go: the old recipients hear of it too, and the new ones by email
    extra_chats, extra_mail = [], []

    def listed(value):
        return [str(item) for item in value] if isinstance(value, list) else []

    for change in changes:
        if change.entry.reaches == "telegram":
            extra_chats += [chat for chat in listed(change.before) if f"telegram:{chat}" not in reached]
        elif change.entry.reaches == "mail":
            extra_mail += [address for address in [*listed(change.before), *listed(change.after)]
                           if f"mail:{address.lower()}" not in reached]
    extra_chats = list(dict.fromkeys(extra_chats))
    # Each address once, as first written
    seen = {}
    for address in extra_mail:
        seen.setdefault(address.lower(), address)
    extra_mail = list(seen.values())
    again = False
    if not channel:
        sent = False
    if extra_chats and settings.TELEGRAM_BOT_TOKEN:
        hold()
        try:
            if telegram(",".join(extra_chats), text):
                sent = True
            else:
                problems.append("the old moderators' chats refused it")
        except NotificationFailed as error:
            again = True
            problems.append(f"the old moderators' chats did not take it ({error})")
    if extra_mail and mail.leaves():
        for place, address in enumerate(extra_mail, 1):
            hold()
            try:
                if mail.send_mail(subject, text, None, [address], kind="staff"):
                    sent = True
                else:
                    again = True
                    problems.append(f"address {place} of {len(extra_mail)} of the old and new moderators' emails: "
                                    f"{mail.NOT_SENT}")
            except (OSError, SMTPException, ValueError) as error:
                again = True
                problems.append(f"address {place} of {len(extra_mail)} of the old and new moderators' emails failed "
                                f"({type(error).__name__})")
    if not channel:
        done = not again
    return done, sent, problems


# --- The start --------------------------------------------------------------------------------------------------------


def compare(now=None):
    """Compares the settings of the list with the last record and tells the moderators of the changes. Returns what
    happened, for the command and the tests: "no tables", "off", "off, told", "off, not told", "busy", "recorded",
    "unchanged", "told", "not told".
    Exceptions go to the caller (`at_start`)."""
    from .models import WatchedSetting

    now = now or timezone.now()
    if not tables_ready():
        return "no tables"
    if not enabled():
        return switched_off(now)
    warnings = []
    values = current(warnings.append)
    for warning in warnings:
        logger.warning("settings watch: %s", warning)
    claim = Claim()
    if not claim.take(now):
        return "busy"
    try:
        WatchedSetting.objects.exclude(name__in=[entry.name for entry in WATCHED]).delete()
        stored = {row.name: (row.value, row.fingerprint) for row in WatchedSetting.objects.all()}
        changes, key_changed = changes_of(stored, values)
        outcome = "recorded" if any(name not in stored for name in values) else "unchanged"
        if changes:
            subject, text = message(changes, key_changed, now)
            done, sent, problems = deliver(subject, text, changes, claim.hold)
            if not done:
                # The record stays the one the moderators know: the next start tries again
                logger.warning("settings watch: the moderators were not told of %s changed settings, the next start "
                               "tries again (%s)", len(changes), "; ".join(problems))
                record(values, stored, now, keep=stored)
                return "not told"
            if problems:
                logger.warning("settings watch: %s", "; ".join(problems))
            if sent:
                from .models import SettingsWatch

                SettingsWatch.objects.filter(pk=1).update(told=now)
                outcome = "told"
            else:
                outcome = "recorded"
        record(values, stored, now)
        return outcome
    finally:
        claim.release()


def switched_off(now):
    """The watch turned off in community.yml: the moderators are told once, as of a change, then what was recorded is
    deleted. Not told (a channel that did not take it), the record stays and the next start tries again; with nobody to
    tell, it is deleted at once. Nothing recorded: nothing to say."""
    from .models import WatchedSetting

    if not WatchedSetting.objects.exists():
        forget()
        return "off"
    claim = Claim()
    if not claim.take(now):
        return "busy"
    try:
        from shops.management.commands.dashboard import site_label

        with translation.override(settings.LANGUAGE_CODE):
            when = formats.date_format(timezone.localtime(now), "DATETIME_FORMAT")
            text = "\n".join([
                site_label(),
                _("The settings watch was turned off in community.yml ({time}): the moderators no longer hear when a "
                  "security setting changes.").format(time=when),
                _("If nobody of the team did this, look at community.yml on the machine.")])
            subject = str(_("Settings watch turned off"))
        done, sent, problems = deliver(subject, text, [], claim.hold)
        if not done:
            logger.warning("settings watch: the moderators were not told that it was turned off, the next start tries "
                           "again (%s)", "; ".join(problems))
            return "off, not told"
        forget()
        return "off, told" if sent else "off"
    finally:
        claim.release()


def record(values, stored, now, keep=()):
    """Writes the values of now, except those in `keep` that have a record already (a change not told yet)."""
    from .models import WatchedSetting

    secret = {entry.name: entry.secret for entry in WATCHED}
    for name, (value, print_) in values.items():
        if name in keep or stored.get(name) == (value, print_):
            continue
        WatchedSetting.objects.update_or_create(name=name, defaults={
            "secret": secret[name], "value": value, "fingerprint": print_, "recorded": now})


def at_start():
    """`compare` at the start of the site: whatever goes wrong is a warning in the log, by the kind of error only (its
    text might quote a value), and the start goes on."""
    try:
        return compare()
    except Exception as error:  # noqa: BLE001 (the site starts all the same)
        logger.warning("settings watch: the comparison failed (%s); the site starts all the same", type(error).__name__)
        return "failed"
