"""A short cache for the public list of shops: /shops.json and the list under the map.

Both are the same for every visitor: they read only the public shops (directory.public_shops), never the account of who
asks, so a body built for one visitor can be given to the next. With a few thousand shops each one costs a megabyte and a
fifth of a second; a burst of requests would hold every worker. What is cached is only the body, per language (the
links and the sentences change with it), for PUBLIC_LIST_SECONDS: never the page around the map, which says who is
signed in.

Saving or deleting a shop, a closure or a trainee changes the generation once the transaction commits (`changed`), so
the next request builds the body again,
except a shop saved with `update_fields` that are all in NOT_PUBLIC (the guardian's probes, the ticks of the first day),
or marked with `same_list` (a probe that moves the last time the address worked within the same day). So does a Shop
queryset.update() (ShopQuerySet: admin actions, the Lightning guardian, the certificates), unless it only touches the
bookkeeping in NOT_PUBLIC, and the updates of a trainee's pass (shops/training.py). Other updates that skip the signals
show within PUBLIC_LIST_SECONDS."""
import secrets

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.utils.translation import get_language

GENERATION = "public-shops:generation"

# Shop columns that neither /shops.json nor the map read: an update of these alone keeps the cached list (the
# OpenStreetMap sync writes them for thousands of shops in a row, the guardian at every probe). Not
# `lightning_worked`: the icons' bands come from it (shops/certified.py)
NOT_PUBLIC = frozenset({
    "osm_notes", "osm_seen",
    "lightning_status", "lightning_since", "lightning_checked", "lightning_failures", "lightning_failing_since",
    "lightning_checked_address",
    # The merchant's own ticks of the first day (shops/first_day.py)
    "first_sale", "sign_printed", "register_shared"})

# The attribute same_list() sets on a shop about to be saved
SAME_LIST = "_public_list_same"


def seconds():
    return getattr(settings, "PUBLIC_LIST_SECONDS", 60)


def browser_seconds():
    """How long a browser may keep /shops.json: shorter than the server's cache, since nothing renews it there when a
    shop changes."""
    return max(0, min(seconds(), 30))


def generation():
    found = cache.get(GENERATION)
    if found is None:
        found = secrets.token_hex(4)
        cache.add(GENERATION, found, None)
        found = cache.get(GENERATION, found)
    return found


def cached(name, build):
    """The value of build() for `name` in the active language, from the cache when it is there. build() must not read
    the request: whatever it returns is given to everybody."""
    timeout = seconds()
    if timeout <= 0:
        return build()
    key = f"public-shops:{name}:{get_language()}:{generation()}"
    found = cache.get(key)
    if found is None:
        found = build()
        cache.set(key, found, timeout)
    return found


def same_list(shop):
    """Says that the next save of `shop` changes nothing the public list shows, whatever its fields: for a caller that
    knows it, like a probe that moves `lightning_worked` within the same day (the bands count whole days)."""
    setattr(shop, SAME_LIST, True)
    return shop


def saved(sender, instance, update_fields=None, **kwargs):
    """post_save: a save that wrote only NOT_PUBLIC columns of a shop, or one marked with same_list, keeps the list."""
    from .models import Shop

    if instance.__dict__.pop(SAME_LIST, False):
        return
    if sender is Shop and update_fields and not set(update_fields) - NOT_PUBLIC:
        return
    changed()


def changed(**kwargs):
    """The list must be built again: after the commit, when the change is there for the next request to read. Renewed
    inside the transaction, a request in between would build the old list and keep it under the new generation."""
    transaction.on_commit(renew)


def renew():
    cache.set(GENERATION, secrets.token_hex(4), None)


def connect():
    from .models import Closure, Shop, Trainee

    for model in (Shop, Closure, Trainee):
        post_save.connect(saved, sender=model, dispatch_uid=f"public-shops-save-{model.__name__}")
        post_delete.connect(changed, sender=model, dispatch_uid=f"public-shops-delete-{model.__name__}")
