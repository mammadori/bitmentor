"""Optional notifications: with no token nothing is sent."""
import logging
import re
import unicodedata

from smtplib import SMTPException

import requests
from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


class NotificationFailed(Exception):
    """Without the original error: its text contains the URL, and the URL contains the bot's token."""


def telegram(chat, text):
    """True when Telegram took the message, False when it refused it (a wrong chat, a blocked bot: trying again does
    not help), None when there is no bot or no chat. NotificationFailed when trying again later may work."""
    if not settings.TELEGRAM_BOT_TOKEN or not chat:
        return None
    try:
        response = requests.post(f"{settings.TELEGRAM_API}/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
                                 json={"chat_id": chat, "text": text}, timeout=10)
    except requests.RequestException as error:
        raise NotificationFailed(type(error).__name__) from None
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


def deliver_to_moderators(subject, text):
    """A message for the people who look after the site, the way the morning summary goes (manage.py dashboard --send):
    to TELEGRAM_MODERATION_CHAT, else, when the site has no Telegram or Telegram refuses it, to the community's email.
    Returns (where, problems): where it went ("TELEGRAM_MODERATION_CHAT" or "the community's email") or None when it
    went nowhere, and a line for each way that failed. Never raises for a delivery that failed."""
    from config.community import community

    problems = []
    if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_MODERATION_CHAT:
        try:
            if telegram(settings.TELEGRAM_MODERATION_CHAT, text):
                return "TELEGRAM_MODERATION_CHAT", problems
            problems.append("Telegram refused it (a wrong chat, or a bot that is not in it)")
        except NotificationFailed as error:
            problems.append(f"Telegram did not take it ({error})")
    address = community().get("email")
    if isinstance(address, str) and "@" in address:
        try:
            send_mail(subject, text, None, [address.strip()])
            return "the community's email", problems
        except (OSError, SMTPException, ValueError) as error:
            problems.append(f"the community's email failed ({type(error).__name__})")
    else:
        problems.append("no `email` in community.yml to fall back on")
    return None, problems
