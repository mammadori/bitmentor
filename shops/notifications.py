"""Optional notifications: with no token nothing is sent."""
import logging
import re
import unicodedata

from smtplib import SMTPException

import requests
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from config.mail import NOT_SENT, send_mail

logger = logging.getLogger(__name__)
# `moderators_emails` in community.yml: each address gets its own copy of the moderators' messages, so few
MAX_MODERATORS_EMAILS = 5


class NotificationFailed(Exception):
    """Without the original error: its text contains the URL, and the URL contains the bot's token."""


class Unreachable(NotificationFailed):
    """Telegram itself did not answer: the next chat would wait as long for nothing."""


def chats(value):
    """The chats written in a setting: one, or several separated by commas."""
    return [chat.strip() for chat in str(value or "").split(",") if chat.strip()]


def telegram(chat, text):
    """True when Telegram took the message, False when it refused it (a wrong chat, a blocked bot: trying again does
    not help), None when there is no bot or no chat. NotificationFailed when trying again later may work.

    `chat` can be several chats separated by commas (TELEGRAM_MODERATION_CHAT: each moderator in a chat of their own):
    every one is tried and the answer is True when at least one took the message, so a second try never repeats it to
    those who have it. The ones that failed are in the log, by their place in the list. When none took it the answer
    is the one of a single chat: NotificationFailed if trying again may work for one of them, else False. When
    Telegram cannot be reached the rest of the list is not tried: the wait stays the one of a single chat."""
    several = chats(chat)
    if not settings.TELEGRAM_BOT_TOKEN or not several:
        return None
    if len(several) == 1:
        return _telegram(several[0], text)
    taken, later = False, None
    for place, one in enumerate(several, 1):
        try:
            if _telegram(one, text):
                taken = True
            else:
                logger.error("Telegram: chat %s of %s refused the message", place, len(several))
        except NotificationFailed as error:
            later = error
            logger.error("Telegram: chat %s of %s did not take the message (%s)", place, len(several), error)
            if isinstance(error, Unreachable):
                break
    if later and not taken:
        raise later
    return taken


def _telegram(chat, text):
    try:
        response = requests.post(f"{settings.TELEGRAM_API}/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
                                 json={"chat_id": chat, "text": text}, timeout=10)
    except requests.RequestException as error:
        raise Unreachable(type(error).__name__) from None
    if response.status_code == 429 or response.status_code >= 500:
        raise NotificationFailed(str(response.status_code))
    if response.status_code != 200:
        # Wrong chat or blocked bot: trying again does not help, but the owner receives nothing
        logger.error("Telegram refused the message: %s", response.status_code)
        return False
    return True


def alert_name(shop):
    """The shop's name in Telegram alerts: one line, no web addresses. The owner chooses it, and the community's bot
    signs the message."""
    return alert_text(shop.name)


def plain(text):
    """A text as a terminal or a message shows it: control and format characters (an escape sequence, a direction
    override) become "?", so a name cannot rewrite the lines around it. The joiner of emoji stays."""
    return "".join("?" if unicodedata.category(char).startswith("C") and char != "\u200d" else char for char in str(text))


def alert_text(text, limit=60):
    """A name chosen by someone else (a shop's, a staff register's) as an alert shows it: one line, no web address, no
    control characters, and nothing Telegram would turn into a mention of someone (@) or a bot command (/ in front).
    `limit` characters at most: a name is short, a line a merchant writes to the association (shops/helpdesk.py) longer."""
    name = plain(re.sub(r"\s+", " ", str(text)))
    # Any web address or link to an app (tg://, ftp://…), the tg: and www. forms, and whatever looks like a domain
    name = re.sub(r"(?i)\b[a-z][a-z0-9+.-]*://\S*|\b(?:tg:|www\.)\S*|\S+\.[a-z]{2,}(?:/\S*)?", "…", name)
    name = name.strip().replace("@", "\uff20")
    if name.startswith("/"):
        name = "\u2215" + name[1:]
    return name[:limit] or "?"


def read_moderators_emails():
    """(addresses, problems) of `moderators_emails` in community.yml: a list of up to MAX_MODERATORS_EMAILS addresses
    that take the moderators' messages by email instead of the community's `email`, when Telegram does not take them.
    An address that is not one, or that is already in the list, is left out, and so are the ones beyond the limit;
    `manage.py check` says so (shops.W013). No key, or no address left, is ([], …): the community's `email`, as before."""
    from config.community import community

    value = community().get("moderators_emails")
    if value is None:
        return [], []
    if not isinstance(value, list):
        return [], [(f"`moderators_emails` is a list of up to {MAX_MODERATORS_EMAILS} email addresses, not "
                     f"{type(value).__name__}: the community's `email` is used")]
    found, problems, seen = [], [], set()
    for place, address in enumerate(value, 1):
        try:
            if not isinstance(address, str):
                raise ValidationError("not text")
            address = address.strip()
            validate_email(address)
        except ValidationError:
            # Not the value: the check's text goes to the log of web
            problems.append(f"`moderators_emails`: entry {place} is not an email address, left out")
            continue
        if address.lower() in seen:
            problems.append(f"`moderators_emails`: entry {place} repeats an address, left out")
            continue
        seen.add(address.lower())
        found.append(address)
    if len(found) > MAX_MODERATORS_EMAILS:
        problems.append(f"`moderators_emails` has {len(found)} addresses: only the first {MAX_MODERATORS_EMAILS} are used")
        found = found[:MAX_MODERATORS_EMAILS]
    if not found:
        problems.append("`moderators_emails` has no address that can be used: the community's `email` is used")
    return found, problems


def moderators_emails():
    """The addresses of `moderators_emails` that can be used (read_moderators_emails), or []."""
    return read_moderators_emails()[0]


def email_moderators(subject, text, problems):
    """The moderators' message by email: to each address of `moderators_emails`, in a message of its own (no address
    sees the others), or, without that key, to the community's `email` as before. True when at least one message left;
    a line in `problems` for each that failed or when there is nowhere to send it."""
    from config.community import community

    addresses = moderators_emails()
    if addresses:
        sent = False
        for place, address in enumerate(addresses, 1):
            try:
                # Sent only when it says so: over the email budget it is dropped without an error (config/mailbudget.py)
                if send_mail(subject, text, None, [address], kind="staff"):
                    sent = True
                else:
                    problems.append(f"`moderators_emails`: address {place} of {len(addresses)}: {NOT_SENT}")
            except (OSError, SMTPException, ValueError) as error:
                problems.append(f"`moderators_emails`: address {place} of {len(addresses)} failed ({type(error).__name__})")
        return sent
    address = community().get("email")
    if isinstance(address, str) and "@" in address:
        try:
            if send_mail(subject, text, None, [address.strip()], kind="staff"):
                return True
            problems.append(f"the community's email: {NOT_SENT}")
        except (OSError, SMTPException, ValueError) as error:
            problems.append(f"the community's email failed ({type(error).__name__})")
    else:
        problems.append("no `email` in community.yml to fall back on")
    return False


def deliver_to_moderators(subject, text):
    """A message for the people who look after the site, the way the morning summary goes (manage.py dashboard --send):
    to TELEGRAM_MODERATION_CHAT, else, when the site has no Telegram or Telegram refuses it, by email (email_moderators:
    the addresses of `moderators_emails`, or the community's `email`).
    Returns (where, problems): where it went ("TELEGRAM_MODERATION_CHAT" or "the community's email", which is said of
    `moderators_emails` too) or None when it went nowhere, and a line for each way that failed. Never raises for a
    delivery that failed."""
    problems = []
    if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_MODERATION_CHAT:
        try:
            if telegram(settings.TELEGRAM_MODERATION_CHAT, text):
                return "TELEGRAM_MODERATION_CHAT", problems
            problems.append("Telegram refused it (a wrong chat, or a bot that is not in it)")
        except NotificationFailed as error:
            problems.append(f"Telegram did not take it ({error})")
    if email_moderators(subject, text, problems):
        return "the community's email", problems
    return None, problems
