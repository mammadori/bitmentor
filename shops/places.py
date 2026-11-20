"""The other places of a shop (ShopPlace) between their owner, the community and the public.

The owner writes them (the page of `views.shop_places`); the public sees the ones that are approved, wanted on the map
and not taken away (`ShopPlace.shown`, `shown` here). What waits for someone of the community:

- a new place the owner writes;
- a new address or position of a place already approved: it is kept beside the approved one, which the public keeps
  reading until the new one is approved.

Nothing else waits: the phone, taking a place off the map or putting it back, taking it away. A community that turned
the approval of shops off (`features: {approval: false}`) has none for the places either. The places of a shop that
is not approved yet are approved with it (shops/approval.py). What the same owner asked piles up, and one approval
takes all of it (`approve`): the page of the day lists a shop once, with everything it asks.

A place taken away is hidden at once and kept for `ShopPlace.TRASH_DAYS`: until then its owner takes it back as it
was, approved if it was. After that it is deleted (`purge`): when its owner opens the page of the places, and
for every shop each time the page of the day is built (shops/dashboard.py), which the daily `dashboard` command does
too."""

import hashlib
from datetime import timedelta

from django.contrib.admin.models import CHANGE, LogEntry
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import approval, public_cache
from .details import INVISIBLE_CONTROLS
from .models import Shop, ShopPlace

WHERE = ("address", "city", "latitude", "longitude")
ASKED = ("asked_address", "asked_city", "asked_latitude", "asked_longitude", "asked_on")


def shown(shop):
    """The places of `shop` the public sees. `shop.places` may be prefetched: nothing is filtered in the database."""
    return [place for place in shop.places.all() if place.shown]


def kept(shop):
    """The places of `shop` its owner has: not taken away. A queryset."""
    return shop.places.filter(removed_on=None)


def trash(shop):
    """The places the owner took away and can still take back, the latest first."""
    return shop.places.filter(removed_on__gt=timezone.now() - timedelta(days=ShopPlace.TRASH_DAYS)).order_by(
        "-removed_on"
    )


def purge(shop=None):
    """Deletes the places taken away more than TRASH_DAYS ago, of one shop or of all. Returns how many."""
    old = ShopPlace.objects.filter(removed_on__lte=timezone.now() - timedelta(days=ShopPlace.TRASH_DAYS))
    if shop is not None:
        old = old.filter(shop=shop)
    return old.delete()[0]


def where(place):
    return tuple(getattr(place, name) for name in WHERE)


def for_form(place):
    """Before the owner's form is built on a place: returns the approved address (`where`), and shows in the place, in
    memory only, the address that waits, if one does: the form starts from what the owner last asked."""
    approved = where(place)
    if place.asked_on is not None:
        place.address, place.city = place.asked_address, place.asked_city
        place.latitude, place.longitude = place.asked_latitude, place.asked_longitude
    return approved


def tidy(spot):
    """A `where` to compare: the texts as the owner's form would save them, whatever wrote the approved ones."""
    return tuple(INVISIBLE_CONTROLS.sub("", part).strip() if isinstance(part, str) else part for part in spot)


def write(place, shop, approved=None):
    """Saves a place as its owner wrote it in the form. `approved` is what `for_form` returned when the form was built,
    None for a new place. Returns "waits" when the place now waits for an approval, "saved" otherwise.

    For a place that is there, the row is read again under a lock before deciding: whether it is approved, and which
    address is the approved one, are what the database says now, not what the form started from. An approval that
    arrives in between is neither undone nor used to publish an address nobody read."""
    waits = approval.enabled()
    if approved is None:
        place.shop = shop
        # With its shop when the shop still waits; at once where the community approves nothing; otherwise by a person
        place.approved_on, place.approved_by = (
            (None, "") if waits or shop.approved_on is None else (timezone.now(), "-")
        )
        place.save()
        return "waits" if place.waits else "saved"
    asked = where(place)
    with transaction.atomic():
        now = ShopPlace.objects.select_for_update().get(pk=place.pk)
        approved = where(now)
        place.approved_on, place.approved_by = now.approved_on, now.approved_by
        if now.approved_on is not None and waits and tidy(asked) != tidy(approved):
            # The public keeps the approved address; the new one waits beside it. Asked again with nothing changed,
            # it keeps its place in the queue
            same = asked == (now.asked_address, now.asked_city, now.asked_latitude, now.asked_longitude)
            place.asked_on = now.asked_on if same and now.asked_on else timezone.now()
            place.asked_address, place.asked_city, place.asked_latitude, place.asked_longitude = asked
            place.address, place.city, place.latitude, place.longitude = approved
        else:
            if now.approved_on is not None and waits:
                # The approved address written again, give or take a space: it stays as it is, and what waited is
                # withdrawn
                place.address, place.city, place.latitude, place.longitude = approved
            # Otherwise not approved yet, or no approval in this community: the address is the one written
            place.asked_address, place.asked_city, place.asked_latitude, place.asked_longitude = "", "", None, None
            place.asked_on = None
        place.save(update_fields=[*WHERE, "phone", "on_map", *ASKED])
    return "waits" if place.waits else "saved"


def moved(count):
    """After an `update()` that changes what the public sees: it sends no signal, so the public list is told here."""
    if count:
        public_cache.changed()
    return bool(count)


def remove(shop, pk):
    """The owner takes a place away: hidden at once, kept in the trash. Returns whether it did."""
    return moved(kept(shop).filter(pk=pk).update(removed_on=timezone.now()))


def restore(shop, pk):
    """The owner takes a place back from the trash, as it was: approved if it was. False when it is not there any
    more, or the shop already has all the places it can have."""
    with transaction.atomic():
        # One at a time for a shop: two taken back together would both find room for one
        Shop.objects.select_for_update().filter(pk=shop.pk).first()
        if kept(shop).count() >= ShopPlace.MAX:
            return False
        return moved(trash(shop).filter(pk=pk).update(removed_on=None))


def waiting():
    """What waits, as [(shop, [places])], one entry for each approved shop that asks something, by the shop's name; the
    places of a shop the oldest first. A shop not approved yet is asked about as a shop, and its places go with it.
    One query, whatever the number of shops."""
    found = (
        ShopPlace.objects.filter(Q(approved_on=None) | Q(asked_on__isnull=False), removed_on=None)
        .filter(shop__approved_on__isnull=False)
        .select_related("shop")
        .order_by("shop__name", "shop_id", "pk")
    )
    by_shop = {}
    for place in found:
        by_shop.setdefault(place.shop_id, (place.shop, []))[1].append(place)
    return list(by_shop.values())


def spot(text, latitude, longitude):
    """An address with its point, for whoever approves: the point is what goes on the map, and the text alone would
    hide a place moved somewhere else under the same words."""
    point = f"{latitude:.5f}, {longitude:.5f}"
    return f"{text} ({point})" if text and text != point else point


def described(place):
    """What a place that waits asks, for the page of the day: (the address and point asked, the approved ones or "")."""
    if place.approved_on is None:
        return spot(str(place), place.latitude, place.longitude), ""
    return (
        spot(place.asked, place.asked_latitude, place.asked_longitude),
        spot(str(place), place.latitude, place.longitude),
    )


def seal(found):
    """A short code of what some places ask, as it was read: the approval carries it back, and approves only if
    nothing changed meanwhile (`approve`)."""
    lines = sorted(f"{place.pk}|{'|'.join(described(place))}" for place in found)
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]


def approve(shop, by, user=None, seen=None):
    """Approves everything the places of `shop` wait for, in one go: the new places, and the new addresses, which
    become the addresses. `by` is who approves, as `ShopPlace.approved_by` keeps it; with `user`, one line goes in the
    admin's history of the shop. Returns how many places were approved.

    `seen` is the `seal` of what the person who approves read: if the owner changed or added something since, nothing
    is approved and None is returned, so that nobody approves what they have not read. Without it (a shop approved
    with its places, shops/approval.py) everything that waits is approved."""
    count = 0
    with transaction.atomic():
        found = [place for place in kept(shop).select_for_update() if place.waits]
        if seen is not None and found and seal(found) != seen:
            return None
        for place in found:
            if place.asked_on is not None:
                place.address, place.city = place.asked_address, place.asked_city
                place.latitude, place.longitude = place.asked_latitude, place.asked_longitude
                place.asked_address, place.asked_city, place.asked_latitude, place.asked_longitude = "", "", None, None
                place.asked_on = None
            place.approved_on, place.approved_by = timezone.now(), by[:150]
            place.save(update_fields=[*WHERE, *ASKED, "approved_on", "approved_by"])
            count += 1
        if count and user is not None:
            LogEntry.objects.log_actions(
                user.pk, [shop], CHANGE, change_message=f"Places approved: {count}", single_object=True
            )
    return count
