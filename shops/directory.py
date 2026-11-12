"""Public list of the shops: a suspended shop, or one without consent, never appears.

A historic shop (with `closed_on`) stays on the map as a place of history, but is not a place to pay:
it does not enter the counts, the discounts or the shop window.
"""
from math import asin, ceil, cos, degrees, radians, sin, sqrt

from django.db.models import Q
from django.db.models.functions import Lower

from .models import Shop

# Nearby shops on a shop page: within a walk, the closest first
NEARBY_SHOPS = 4
NEARBY_METRES = 1200
# Streets are longer than the straight line, and 80 m a minute is an easy pace (4.8 km/h)
DETOUR = 1.3
WALK_METRES_PER_MINUTE = 80
EARTH_METRES = 6371000


# A shop that waits for its owner's wallet, without a register: one saved before having a wallet (Shop.awaiting_wallet)
# or one moved here with a register on the other site (shops/moving.py). Until then it would be a place to pay where
# nobody can pay. Once historic it is a place of the map again, with its own pin, and waits for nothing.
WAITING_FOR_WALLET = Q(lightning_address="", closed_on=None) & (Q(awaiting_wallet=True) | Q(moved_register=True))


def waits_for_wallet(shop):
    """WAITING_FOR_WALLET for one shop already read."""
    return not shop.has_register and not shop.closed_on and (shop.awaiting_wallet or shop.moved_register)


def public_shops():
    """A shop waiting for its owner's wallet (WAITING_FOR_WALLET) is not a place of the map yet."""
    return (Shop.objects.filter(suspended=False, listed=True).exclude(latitude=None).exclude(longitude=None)
            .exclude(WAITING_FOR_WALLET))


def payable_shops():
    """The public ones minus the historic ones, including those that close later on."""
    return public_shops().filter(closed_on=None)


def statistics():
    shops = payable_shops()
    return {
        "shops": shops.count(),
        "discounts": shops.filter(discount__gt=0).count(),
        # «Example» and «example» are one city, as for featured_shops
        "cities": shops.exclude(city="").values(town=Lower("city")).distinct().count(),
    }


def featured_shops(limit=6, city=None):
    shops = payable_shops().filter(discount__gt=0)
    if city:
        shops = shops.filter(city__iexact=city)
    return shops.order_by("-discount", "name")[:limit]


def metres_between(a, b):
    """The distance on the ground between two (latitude, longitude) points, in metres (haversine)."""
    lat1, lon1, lat2, lon2 = map(radians, (*a, *b))
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_METRES * asin(min(1, sqrt(h)))


def walking_minutes(metres):
    return max(1, ceil(metres * DETOUR / WALK_METRES_PER_MINUTE))


def nearby_shops(shop, limit=NEARBY_SHOPS, radius=NEARBY_METRES):
    """Up to `limit` other places to pay within `radius` metres of `shop`, the closest first, each with `metres` and
    `minutes` on foot. Computed here, from the shop's own position: the page asks nothing of the visitor.
    A box around the shop keeps the query small; the circle is drawn in Python. Shops across the 180th meridian are
    not looked for."""
    if shop.latitude is None or shop.longitude is None:
        return []
    here = (shop.latitude, shop.longitude)
    span = degrees(radius / EARTH_METRES)
    wide = span / max(cos(radians(shop.latitude)), 0.01)
    candidates = (payable_shops().exclude(pk=shop.pk)
                  .filter(latitude__range=(here[0] - span, here[0] + span), longitude__range=(here[1] - wide, here[1] + wide))
                  .only("id", "name", "city", "discount", "lightning", "onchain", "latitude", "longitude"))
    found = []
    for other in candidates:
        other.metres = metres_between(here, (other.latitude, other.longitude))
        if other.metres <= radius:
            other.minutes = walking_minutes(other.metres)
            found.append(other)
    return sorted(found, key=lambda other: (other.metres, other.name))[:limit]
