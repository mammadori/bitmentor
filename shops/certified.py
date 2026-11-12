"""Certified shops, and how recently their Lightning address worked.

A certified shop is a shop whose Lightning address answers the guardian (`lightning_status` OK on the address it has now,
shops/lightning_watch.py) and whose owner has earned the basic pass of the merchant's training (`Trainee.base_earned`,
shops/training.py), unless the association revoked its certificate. The site sees no sale: the certificate rests on
what it can check, the address, and on the merchant's commitment, the training. Its page and its card on the map carry a
small "Certified shop". The private part of the pages (website/visibility.py, through `sees_private_part`) is not
automatic: its owner sees it once a person of the association approved the shop by hand in /admin/
(`private_approved`, `private_approved_by`).

- `certified_month`: the first calendar month, in the community's time zone, in which all of it held. Once certified,
  always certified: it is a fact of the past. `refresh` writes it, after a probe that worked, after a basic pass, and at
  every run of the guardian (manage.py check_lightning).
- A Lightning address the owner changed from the panel (shops/address_change.py, `ADDRESS_PENDING`) holds the badge and
  the private part back, without touching `certified_month` or the approval, until the guardian sees the new address work.
- `certified_revoked`: the association took the certificate away from /admin/; the site does not give it back by itself.

One Lightning address belongs to one owner: several shops of the same owner may share it, shops of different owners
never (`address_taken`). The forms refuse it, and a shop whose address is also on a shop of another owner is not
certified (`qualifies`): data written before the rule is listed in the association's dashboard and gives no private part (`shared_with_another_owner`).

The map's icons say how recently the Lightning address last answered (`lightning_worked`), in bands of whole days of the
community's calendar (`bands`): never the date itself. Only the days the shop was open count (`vacation.open_days`).
Outside the site only `certified` and the band leave, never the month or the dates.
"""
import logging

from django.db import DatabaseError
from django.db.models import Exists, F, OuterRef
from django.db.models.functions import Lower, Trim
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from config import features

from . import vacation
from .models import ADDRESS_PENDING, LightningStatus, Shop

logger = logging.getLogger(__name__)

# The bands of the map, from the most recent, in whole days of the community's calendar: up to 7 days ago, up to 31,
# up to 366, then older. Whole days: a band computed to the second would tell, asked every minute, when a probe ran
WEEK, MONTH, YEAR, OLDER = "week", "month", "year", "older"
BANDS = ((WEEK, 7), (MONTH, 31), (YEAR, 366))

# What the icon says, for screen readers and next to it in the map card. The map gets them as a whole (map.html,
# #paid-texts), never a date
TEXTS = {
    "lightning": {WEEK: _("Lightning, active in the last week"), MONTH: _("Lightning, active in the last month"),
                  YEAR: _("Lightning, active in the last year"), OLDER: _("Lightning, not active in the last year")},
}
# On-chain has no band: the site sees no on-chain payment, only that the owner says the till takes them (Shop.onchain)
ONCHAIN = _("On-chain payments too")
CERTIFIED = _("Certified shop")
# What the badge means, for the public pages: true of every certified shop, without dates, which never leave the site
MEANING = _("Certified shop: its Lightning address works, and its owner has done the community's training.")
# The rule, for the panel of a shop not certified yet
RULE = _("A shop is certified when its Lightning address works and its owner has earned the basic pass of the training.")


def texts():
    """The words of the badge and of the icons in the page's language, for map.js."""
    return {"certified": str(CERTIFIED), "rule": str(MEANING),
            **({"onchain": str(ONCHAIN)} if features.enabled(features.ONCHAIN) else {}),
            **{kind: {name: str(text) for name, text in by_band.items()} for kind, by_band in TEXTS.items()}}


def marks(shop, now=None):
    """The icons of a shop for a template: [{"kind", "band", "label"}]. None for a historic shop. On-chain, said by the
    owner, has the band «said»; not while on-chain at the register, an experimental feature, is off."""
    if shop.closed_on:
        return []
    found = bands(shop, now) or {}
    icons = [{"kind": kind, "band": found[kind], "label": TEXTS[kind][found[kind]]} for kind in TEXTS if kind in found]
    if getattr(shop, "onchain", False) and features.enabled(features.ONCHAIN):
        icons.append({"kind": "onchain", "band": "said", "label": ONCHAIN})
    return icons


def enabled():
    return features.enabled(features.CERTIFIED)


def owns_certified_shop(user):
    """True if `user` owns a certified shop the community has not suspended. Never an error, one small query. Never true
    when the community turned the badge off. The private part of the pages asks more: `sees_private_part`."""
    if not enabled() or not getattr(user, "is_authenticated", False):
        return False
    try:
        # A shop whose owner just changed its address waits for the guardian, as its badge (Shop.certified)
        return Shop.objects.filter(owner=user, suspended=False, certified_month__isnull=False).exclude(ADDRESS_PENDING).exists()
    except DatabaseError:
        return False


def sees_private_part(user):
    """True if `user` owns a certified shop the community has not suspended and that the association approved by hand
    for the private part. Never an error, one small query; never true when the badge is turned off."""
    if not enabled() or not getattr(user, "is_authenticated", False):
        return False
    try:
        # A shop whose address is also another owner's opens nothing until that is resolved, as for the certificate
        return (Shop.objects.filter(owner=user, suspended=False, certified_month__isnull=False,
                                    private_approved__isnull=False).exclude(shared_with_another_owner())
                .exclude(ADDRESS_PENDING).exists())
    except DatabaseError:
        return False


def waiting_approval(shops):
    """The certified shops of a queryset, with an owner and not suspended, whose private part nobody approved yet."""
    return shops.filter(certified_month__isnull=False, private_approved__isnull=True, owner__isnull=False,
                        suspended=False)


def same_address(address):
    """A filter for the shops with this Lightning address, however it is written (case, spaces around it)."""
    return Shop.objects.annotate(_address=Lower(Trim("lightning_address"))).filter(_address=address.strip().lower())


def address_taken(address, owner, shop=None):
    """True if `address` is the Lightning address of a shop of another owner. The shop being edited and the shops of the
    same owner do not count; a shop with no owner yet (imported, not claimed) does not either."""
    if not address.strip():
        return False
    found = same_address(address).filter(owner__isnull=False)
    if owner is not None:
        found = found.exclude(owner=owner)
    if shop is not None and shop.pk:
        found = found.exclude(pk=shop.pk)
    return found.exists()


def shared_with_another_owner():
    """An expression for a queryset of shops: true when a shop of another owner, any shop, historic or suspended too,
    has the same Lightning address however it is written. For data from before the rule: the forms refuse it now."""
    others = (Shop.objects.annotate(_address=Lower(Trim("lightning_address"))).filter(owner__isnull=False)
              .exclude(lightning_address="").filter(_address=Lower(Trim(OuterRef("lightning_address"))))
              .exclude(owner=OuterRef("owner")))
    return Exists(others)


def local_day(moment):
    return timezone.localtime(moment, timezone.get_default_timezone()).date()


def month_of(moment):
    return local_day(moment).replace(day=1)


def qualifies(shops):
    """The shops of a queryset that meet the rule now: the Lightning address that the guardian found working is the one
    they have today, the owner has the basic pass, the certificate was not revoked, and no shop of another owner has the
    same address."""
    return (shops.exclude(shared_with_another_owner()).filter(certified_revoked=False, owner__trainee__base_earned__isnull=False,
                         lightning_status=LightningStatus.OK, lightning_checked_address=F("lightning_address"))
            .exclude(lightning_address=""))


def meets_rule(shop):
    """Whether one shop meets the rule now (the panel's words), with the same checks as `qualifies`."""
    return qualifies(Shop.objects.filter(pk=shop.pk)).exists()


def refresh(shops=None, now=None):
    """Certifies the shops of `shops` (all of them by default) that meet the rule now and are not certified yet, with
    this month. Returns how many. Never takes a certificate away: only the association does, from /admin/."""
    shops = Shop.objects.all() if shops is None else shops
    month = month_of(now or timezone.now())
    try:
        return qualifies(shops.filter(certified_month__isnull=True)).update(certified_month=month)
    except DatabaseError as error:
        logger.warning("Certificates not refreshed: %s", type(error).__name__)
        return 0


def band(moment, now, shop=None):
    """The band of a moment's age in whole days of the community's calendar. With the shop, only the days it was open
    count (shops/vacation.py): a fortnight of holidays does not make the icons fade."""
    if moment is None:
        return OLDER
    days = (local_day(now) - local_day(moment)).days
    if shop is not None:
        days = vacation.open_days(shop, local_day(moment), local_day(now))
    return next((name for name, limit in BANDS if days <= limit), OLDER)


def bands(shop, now=None):
    """{"lightning": band} for a shop whose Lightning address has worked at least once, None for the others: for one that
    never did, "not active" would say something untrue."""
    if not shop.lightning_worked:
        return None
    return {"lightning": band(shop.lightning_worked, now or timezone.now(), shop)}
