"""«The association does it for me»: a merchant asks, from the panel, for someone of the staff to bring the shop's entry on
OpenStreetMap up to date, or to put the shop there (`OsmRequest`, shops/osm_views.py).

Only a queue. The site never writes to OpenStreetMap by itself: a person of the staff does it by hand, in the editor or with
`manage.py osm_upload`, and marks the request done in /admin/. One request is open at a time for a shop, there are few per
day, and a telegram message tells the moderators, as for the requests for help. Telegram that does not answer never loses
the request: it stays in /admin/ and on top of the dashboard (shops/dashboard.py). Nothing a merchant writes is kept: the
request carries only the lines the site itself worked out."""
import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.translation import gettext as _

from . import certified, lightning_watch, osm
from .models import HelpRequest, OsmRequest, Shop, is_demo
from .notifications import NotificationFailed, alert_name, telegram

logger = logging.getLogger(__name__)

# Requests per shop in a rolling day: a person does them by hand, and few are enough for a bad day
ASK_PER_DAY = 3
ASK_WINDOW = timedelta(hours=24)
# What a merchant sees of a request done or declined, in the panel, before it is only history
SHOWN = timedelta(days=7)
State = OsmRequest.State
Kind = OsmRequest.Kind


class TooMany(Exception):
    """The shop has asked ASK_PER_DAY times in the last day."""


def lines_for(shop, now=None):
    """The lines to write for a shop, one tag a line, as the person who does the request reads them. From the community's
    settings and what the site knows: a linked shop gets the changes last found (and every tag, if nothing was found yet)."""
    config = osm.settings()
    if config is None:
        return ""
    today = certified.local_day(now or timezone.now())
    state = osm.page(shop, now)
    if state and state["changes"]:
        return "\n".join(f"{change['tag']} = {change['new']}" if change["new"] is not None else f"{change['tag']}: remove"
                         for change in state["changes"])
    return osm.request_summary(osm.wanted_tags(shop, config, today))


def create(shop, user, now=None):
    """(request, True) a new one, or (the open one, False) when the shop already has one waiting; TooMany at the limit.
    Under a lock on the shop, so that two taps together make one request: the database also refuses a second open one."""
    now = now or timezone.now()
    kind = Kind.UPDATE if shop.osm_ref else Kind.ADD
    try:
        with transaction.atomic():
            Shop.objects.select_for_update().filter(pk=shop.pk).first()
            # A request of the owner before this one: the shop changed hands, and the new owner did not ask. It is closed
            # (the staff sees who did), and does not block the new owner, nor does it pass for theirs
            shop.osm_requests.filter(state=State.OPEN).exclude(asked_by_id=user.pk).update(
                state=State.DECLINED, done=now, done_by="site: the shop changed owner")
            open_one = shop.osm_requests.filter(state=State.OPEN).first()
            if open_one:
                return open_one, False
            if shop.osm_requests.filter(created__gte=now - ASK_WINDOW).count() >= ASK_PER_DAY:
                raise TooMany
            return OsmRequest.objects.create(shop=shop, asked_by=user, kind=kind, created=now,
                                             summary=lines_for(shop, now)[:4000]), True
    except IntegrityError:
        return shop.osm_requests.filter(state=State.OPEN).first(), False


def message(row):
    """The text for the moderators' chat, in the association's language: the site, the shop, what is asked, the lines the
    site worked out (no more than a few) and the link. Never anything the merchant wrote: nothing is asked of them."""
    with translation.override(settings.LANGUAGE_CODE):
        from .management.commands.dashboard import site_label

        link = f"{settings.PUBLIC_URL}{reverse('admin:shops_osmrequest_change', args=[row.pk])}"
        lines = [site_label(), _("{shop} asks the association to {what}.").format(shop=alert_name(row.shop), what=row.get_kind_display())]
        if row.shop.osm_ref:
            lines.append(osm.element_url(row.shop.osm_ref))
        lines.append(_("The request: {link}").format(link=link))
        return "\n".join(lines)


def tell(row, notify=True):
    """Tell the moderators' chat; the state of it, as HelpRequest.Delivery. A made-up shop (a training site) never writes
    to the chat of the people who look after the real site. Without `notify` (the caller's limits are spent) nothing is
    sent: the request is only in /admin/ and on the dashboard."""
    if is_demo(row.shop):
        return HelpRequest.Delivery.OFF
    if not notify:
        return HelpRequest.Delivery.FAILED
    try:
        answer = telegram(settings.TELEGRAM_MODERATION_CHAT, message(row))
    except NotificationFailed:
        return HelpRequest.Delivery.FAILED
    except Exception as error:  # noqa: BLE001 - the request is saved; nothing here may lose it
        logger.error("OpenStreetMap request %s: the message to the moderators failed: %s", row.pk, type(error).__name__)
        return HelpRequest.Delivery.FAILED
    if answer is None:
        return HelpRequest.Delivery.OFF
    return HelpRequest.Delivery.SENT if answer else HelpRequest.Delivery.REFUSED


def finish(row, notify=True):
    row.delivery = tell(row, notify)
    row.save(update_fields=["delivery"])
    return row


def own(shop):
    """The requests the shop's owner now made: after a change of owner the new one does not read the old one's."""
    if shop.owner_id is None:
        return OsmRequest.objects.none()
    return shop.osm_requests.filter(asked_by_id=shop.owner_id)


def state(row):
    return {"request": row, "open": row.state == State.OPEN, "done": row.state == State.DONE, "declined": row.state == State.DECLINED,
            "when": lightning_watch.when(row.created), "done_when": lightning_watch.when(row.done) if row.done else "",
            "delivered": row.delivery == HelpRequest.Delivery.SENT, "kind": row.get_kind_display()}


def panel(shop, now=None):
    """For the merchant's pages: None, or the request still open, or the one finished in the last SOLVED days. One query."""
    now = now or timezone.now()
    rows = list(own(shop)[:5])
    chosen = next((row for row in rows if row.state == State.OPEN), None) or next(
        (row for row in rows if row.done and row.done >= now - SHOWN), None)
    return state(chosen) if chosen else None
