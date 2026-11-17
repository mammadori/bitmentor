"""New shops wait for an approval: a shop its owner writes on the site (/shops/new/) stays off the maps, the lists and
the counts until someone of the community looks at it and approves it, on /admin/today/ or with the action in /admin/.
Its register works from the first minute: the approval is about what the public sees, never about the payments.

The shops imported from a list (`import_shops`, with `external_id`) and then taken by their owner never wait: the
community already put them on its list. A community can turn the approval off (`features: {approval: false}`): new
shops then appear at once, and the shops already waiting keep waiting until someone approves them."""
import logging
import threading

from django.conf import settings
from django.contrib.admin.models import CHANGE, LogEntry
from django.db import transaction
from django.urls import reverse
from django.utils import translation
from django.utils.translation import gettext as _

from config import features

from .models import Shop

logger = logging.getLogger(__name__)


def enabled():
    return features.enabled(features.APPROVAL)


def waiting():
    return Shop.objects.filter(awaiting_approval=True)


def approve(shops, user):
    """Approves the shops of a queryset that wait; each approval goes in the admin's history of the shop. Returns how many."""
    count = 0
    for shop in shops.filter(awaiting_approval=True):
        # Only if it still waits: two people approving at once write one history line
        if Shop.objects.filter(pk=shop.pk, awaiting_approval=True).update(awaiting_approval=False):
            LogEntry.objects.log_actions(user.pk, [shop], CHANGE, change_message="Shop approved",
                                         single_object=True)
            count += 1
    return count


def tell_moderators(shop):
    """Tells the people who look after the site that a shop waits, once the shop is saved, from a thread of its own: the
    owner's page waits neither for Telegram nor for the mail server, and a message that cannot go is a line in the log."""
    shop_id, name = shop.pk, shop.name

    def deliver():
        from .management.commands.dashboard import site_label
        from .notifications import alert_text, deliver_to_moderators

        try:
            with translation.override(settings.LANGUAGE_CODE):
                text = _("A new shop waits for your approval: %(name)s") % {"name": alert_text(name)}
                link = f"{settings.PUBLIC_URL}{reverse('admin_dashboard')}#approve-{shop_id}"
                where, problems = deliver_to_moderators(str(_("New shop")), f"{site_label()}\n{text}\n{link}")
            if where is None:
                logger.warning("new shop: the moderators were not told (%s)", "; ".join(problems))
        except Exception:
            logger.exception("new shop: telling the moderators failed")

    transaction.on_commit(lambda: threading.Thread(target=deliver, daemon=True).start())
