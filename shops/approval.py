"""No shop goes on the map without an approval: `Shop.approved_on` is empty until someone of the community looks at the
shop and approves it, on /admin/today/ or with the action in /admin/. Until then the shop is off the maps, the lists,
the counts and OpenStreetMap, and its page shows as little as a suspended one's. Its register works from the first
minute: the approval is about what the public sees, never about the payments.

Approved by whoever brings them: the shops imported from a list (`import_shops`, with `external_id`, later taken by
their owner), those moved from another site (`manage.py move_shops`), those written in /admin/, the made-up ones of the
demo. A shop its owner writes on the site (/shops/new/) waits. A community can turn the wait off
(`features: {approval: false}`): new shops are then approved as they are saved, and the shops already waiting keep
waiting until someone approves them."""
import logging
import threading

from django.conf import settings
from django.contrib.admin.models import CHANGE, LogEntry
from django.db import transaction
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.translation import gettext as _

from config import features

from .models import Shop

logger = logging.getLogger(__name__)


def enabled():
    return features.enabled(features.APPROVAL)


def waiting():
    return Shop.objects.filter(approved_on=None)


def stamp(shop, by):
    """Marks a shop being saved as approved, by `by` (a username, or how it came: "import", "move", "demo")."""
    if shop.approved_on is None:
        shop.approved_on, shop.approved_by = timezone.now(), by


def approve(shops, user):
    """Approves the shops of a queryset that wait; each approval goes in the admin's history of the shop. Returns how many."""
    count = 0
    for shop in shops.filter(approved_on=None):
        # Only if it still waits: two people approving at once write one history line
        if Shop.objects.filter(pk=shop.pk, approved_on=None).update(approved_on=timezone.now(),
                                                                       approved_by=user.get_username()):
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
