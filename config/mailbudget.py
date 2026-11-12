"""The emails the site sends, counted, against the quota of the mail provider (limits `emails_per_month`,
`emails_per_day`, `emails_per_address_day`, `emails_per_sender_day`).

The provider allows a fixed number a month. Without a budget a botnet asking for sign-in links could use up the month in
a day, and then nobody could sign in by email and the owners would get no security notice. So:
- every email goes through CountingBackend (settings.EMAIL_BACKEND), which hands it to the real backend
  (settings.EMAIL_REAL_BACKEND) and writes a row in the database for each one that was sent: the count survives a restart;
- over the month's or the day's limit nothing is sent, whatever it is: a warning in the log, never an error for the request;
- the staff and the allowed addresses (`factor`) have `emails_staff_factor` times the per-recipient cap and the per-address
  rate limits, and are not stopped by the reserve, only by the total;
- what a visitor makes the site send (sign-in and confirmation links) has to pass `visitor_allowed` first: it stops at
  VISITOR_SHARE of the month and of the day, and at `emails_per_address_day` for the same recipient. The rest is kept for
  the security notices and for the owners' (guard, Lightning watch, address changes, the staff's dashboard);
- one visitor cannot spend the visitors' share alone: `sender_over` counts the emails each network address makes the site
  send in a day (`emails_per_sender_day`), whoever they are for. Without it one address asking for sign-in links to
  made-up recipients would turn email sign-in off for everybody until midnight.
A month and a day are those of the site's time zone."""
import hashlib
import logging
from datetime import datetime, time, timedelta

from django.conf import settings
from django.core.mail import get_connection
from django.core.mail.backends.base import BaseEmailBackend
from django.db import DatabaseError
from django.utils import timezone
from django.utils.translation import gettext as _

logger = logging.getLogger(__name__)
# Visitors can use this share of the month and of the day; the rest is for security and owner notices
VISITOR_SHARE = 0.8
# The window of `emails_per_sender_day`: a day of the limits' own clock (config/ratelimit.py)
SENDER_WINDOW = 86400


def digest(address):
    return hashlib.sha256(f"{settings.SECRET_KEY}:email-budget:{address.strip().lower()}".encode()).hexdigest()


def starts():
    """(first instant of the month, first instant of the day), local time."""
    today = timezone.localdate()
    make = lambda day: timezone.make_aware(datetime.combine(day, time.min))  # noqa: E731
    return make(today.replace(day=1)), make(today)


def sent():
    from shops.models import EmailSent
    return EmailSent.objects


def month_count():
    return sent().filter(sent__gte=starts()[0]).count()


def day_count():
    return sent().filter(sent__gte=starts()[1]).count()


def address_count(address):
    return sent().filter(sent__gte=starts()[1], recipient=digest(address)).count()


def crowded():
    """How many addresses received more than `emails_per_address_day` emails today, notices included: a shop in trouble, or
    the site used against someone. Only the staff can tell, so the dashboard says it (the addresses are kept as prints)."""
    from django.db.models import Count

    from config import limits

    cap = limits.get("emails_per_address_day")
    if not cap:
        return 0
    return (sent().filter(sent__gte=starts()[1]).values("recipient").annotate(n=Count("pk")).filter(n__gt=cap).count())


def over(used, limit, share=1.0):
    return bool(limit) and used >= limit * share


def blocked(share=1.0):
    """The name of the budget that is used up (to `share` of it), or ""."""
    from config import limits
    if over(month_count(), limits.get("emails_per_month"), share):
        return "emails_per_month"
    if over(day_count(), limits.get("emails_per_day"), share):
        return "emails_per_day"
    return ""


def factor(request):
    """How many times the per-recipient and per-network-address limits count for this request: `emails_staff_factor` for a
    staff account (config/guard.py keeps it in request.staff_user) or an allowed address, who may be testing; 1 for all the
    others. 0 in the settings counts as 1."""
    from config import limits
    from website.visibility import from_allowed_address
    if getattr(request, "staff_user", None) is not None or from_allowed_address(request):
        return max(1, limits.get("emails_staff_factor"))
    return 1


def visitor_allowed(address, factor=1):
    """False when a visitor cannot make the site send one more email now: the reserve for security notices is reached, or
    this address received enough today. Warns in the log; the caller tells the visitor to try later."""
    from config import limits
    try:
        # The staff is not stopped by the reserve: it still stops at the total, in the backend
        full = blocked(VISITOR_SHARE) if factor == 1 else ""
        if full:
            logger.warning("email budget: %s reached %d%% (limits.%s in community.yml); sign-in and confirmation links are "
                           "refused, the rest is kept for security notices", full, VISITOR_SHARE * 100, full)
            return False
        if over(address_count(address), limits.get("emails_per_address_day") * factor):
            logger.warning("email budget: one address received limits.emails_per_address_day emails today; no more links for it")
            return False
    except DatabaseError:
        logger.exception("email budget: the count cannot be read")
    return True


def from_onion(request):
    """Every visitor of the onion entrance arrives with the same address, the onion service's own (Caddy writes
    `X-Entrance: onion`): there one address is everybody."""
    from website.visibility import ENTRANCE
    return request.headers.get(ENTRANCE) == "onion"


def sender_over(request, factor=1):
    """Counts one more email that this visitor's network address makes the site send today, and says whether that is
    over `emails_per_sender_day` (three times as many for the address's wider network, as in the other limits by
    address). The visitors of the onion share one address: together they have what a wider network has, counted apart, so
    that they can use up only their own. Call it last, when the email would otherwise go (a request refused for another
    reason is not counted), and `sender_give_back` if the email then does not leave. The day is the one of the limits'
    clock (UTC), not the site's."""
    from config import limits, ratelimit
    cap = limits.rate("emails_per_sender_day") * factor
    if from_onion(request):
        over = ratelimit.over_limit("mail-day:onion", cap * 3, SENDER_WINDOW)
    else:
        over = ratelimit.over_address_limits(request, "mail-day", cap, cap * 3, SENDER_WINDOW)
    if over:
        logger.warning("email budget: one network address asked for more than limits.emails_per_sender_day emails today; "
                       "no more links from it")
    return over


def sender_give_back(request):
    """An email `sender_over` counted did not leave (the mail server refused it): it does not use up the address's day."""
    from config import ratelimit
    if from_onion(request):
        ratelimit.give_back("mail-day:onion", SENDER_WINDOW)
    else:
        ratelimit.give_back_address(request, "mail-day", SENDER_WINDOW)


def message():
    # An account made with an email has no wallet key: "sign in with your wallet" would open a new, empty account
    return _("This site cannot send more emails for now. Please try again later. If you made your account with a "
             "Lightning wallet, you can sign in with it.")


def sender_message():
    return _("Too many emails asked from your connection today: try again tomorrow.")


def record(addresses):
    from shops.models import EmailSent
    now = timezone.now()
    EmailSent.objects.bulk_create([EmailSent(sent=now, recipient=digest(address)) for address in addresses])
    from shops import privacy

    kept = privacy.days("email_prints_days")
    if kept is not None:
        EmailSent.objects.filter(sent__lt=now - timedelta(days=kept)).delete()


class CountingBackend(BaseEmailBackend):
    """Hands each message to settings.EMAIL_REAL_BACKEND and counts it. Above the month's or the day's limit the message is
    dropped with a warning: sending returns fewer than asked, it does not raise."""

    def __init__(self, fail_silently=False, **kwargs):
        super().__init__(fail_silently=fail_silently, **kwargs)
        self.real = get_connection(settings.EMAIL_REAL_BACKEND, fail_silently=fail_silently, **kwargs)

    def open(self):
        return self.real.open()

    def close(self):
        return self.real.close()

    def send_messages(self, email_messages):
        done = 0
        for message in email_messages:
            try:
                full = blocked()
            except DatabaseError:
                # Better one email too many than no sign-in: a database that cannot be read has bigger problems
                logger.exception("email budget: the count cannot be read, sending without it")
                full = ""
            if full:
                logger.warning("email budget: %s reached, nothing is sent (\"%s\" to %d recipients dropped)",
                               full, message.subject, len(message.recipients()))
                continue
            if self.real.send_messages([message]):
                done += 1
                try:
                    record(message.recipients())
                except DatabaseError:
                    logger.exception("email budget: an email sent could not be counted")
        return done
