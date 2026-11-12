"""One count of on-chain addresses per shop and chain, so that every register of the shop hands out the addresses of the
owner's receive chain (…/0/n, or the path the owner set) and no two registers ever use the same index.

Why a count on the site: the payments must show in the owner's ordinary wallet, which follows one receive chain and
stops looking after 20 unused addresses in a row (the gap limit). Blocks of indexes reserved for each register would
leave holes of unused addresses between them, past that limit as soon as a register is idle; one counter hands the
indexes out in order, so the only holes are the sales started and never paid, as with a single register (the register
warns about those, static/register.js). The register asks for its index when it prepares a sale (`claim`); an index
never goes back (a register that cancels leaves a hole of one: giving it back would let anyone with a register hand out
an address that is still on another screen).

The site learns only how many on-chain sales the shop started, never an address nor the key: the chain is named by a
SHA-256 of its extended key (`chain`), and an index alone derives nothing. A new path, or a new key, is another chain
with its own count, and the sales made before keep their addresses. The owner's register asks as the signed-in owner
(the shop's id is public), a staff register with its key, and only while it is turned on.

The register still asks the explorer about every index it gets (the wallet may have given that address elsewhere) and
asks again, at least one further on, when it is used. Only the owner opens a new chain (the first claim of the owner's
register, or the owner's phone when it draws a pairing code); a staff register only counts on one that exists.

On-chain at the register is an experimental feature (config/features.py): while the community has not turned it on, the
addresses of these views answer 404 to everybody (shops/urls.py), and the counts already made stay in the database."""
import json
import re

from django.db import transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from config import ratelimit
from .models import HAS_REGISTER, ChainCount, Shop

CHAIN = re.compile(r"[0-9a-f]{64}\Z")
# The last index of a chain (BIP32: from 2^31 on they are hardened)
MAX_INDEX = 0x7fffffff
# How far past the count a staff register may ask to start (its own count, or used addresses the explorer showed): within
# the wallet's gap limit of 20. The owner may set any start (the settings)
STAFF_AHEAD = 19
# The most chains a shop has: a key or a path changed now and then, not one per request
CHAINS_PER_SHOP = 20
# Requests per register and hour (the owner's, and each staff one apart): a till takes a sale a minute at most, and asks
# again only for used addresses
CLAIMS = 300
CLAIM_WINDOW = 3600


class Far(Exception):
    pass


class Unknown(Exception):
    pass


def open_chain(shop, chain):
    """The count of a chain, made if missing (the owner only). Raises Far past CHAINS_PER_SHOP."""
    with transaction.atomic():
        # The shop's row as the lock: two first requests make the count once, and the number of chains holds
        Shop.objects.select_for_update().filter(pk=shop.pk).first()
        if not ChainCount.objects.filter(shop=shop, chain=chain).exists():
            if ChainCount.objects.filter(shop=shop).count() >= CHAINS_PER_SHOP:
                raise Far
            ChainCount.objects.create(shop=shop, chain=chain)


def claim(shop, chain, at_least, ahead=None, owner=True):
    """The next index of the shop's chain, at least `at_least`, never given before. Raises Far when `at_least` is more
    than `ahead` past the count, or past the last index; Unknown for a chain that a staff register (`owner` false) asks
    and the owner never opened."""
    if owner:
        open_chain(shop, chain)
    with transaction.atomic():
        count = ChainCount.objects.select_for_update().filter(shop=shop, chain=chain).first()
        if count is None:
            raise Unknown
        if ahead is not None and at_least > count.next + ahead:
            raise Far
        index = max(count.next, at_least)
        if index >= MAX_INDEX:
            raise Far
        count.next = index + 1
        count.save(update_fields=["next", "changed"])
    return index


def read(request):
    try:
        body = json.loads(request.body.decode("utf-8")) if len(request.body) <= 1000 else None
    except (UnicodeDecodeError, ValueError):
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("chain"), str) or not CHAIN.fullmatch(body["chain"]):
        return None
    value = body.get("at_least")
    if body.get("open") is True and value is None:
        return body
    if not (type(value) is int and 0 <= value < MAX_INDEX):
        return None
    return body


def answer(shop, body, who, owner):
    if ratelimit.over_limit(f"chain:{shop.pk}:{who}", CLAIMS, CLAIM_WINDOW):
        return JsonResponse({"error": "busy"}, status=429)
    try:
        if body.get("open") is True:
            if not owner:
                return JsonResponse({"error": "owner"}, status=403)
            open_chain(shop, body["chain"])
            return JsonResponse({})
        return JsonResponse({"index": claim(shop, body["chain"], body["at_least"], None if owner else STAFF_AHEAD, owner)})
    except Far:
        return JsonResponse({"error": "far"}, status=409)
    except Unknown:
        return JsonResponse({"error": "unknown"}, status=412)


@require_POST
def owner_claim(request, shop):
    """The owner's register, and the owner's phone drawing a pairing code (`open`): only the signed-in owner (the page
    gives it the CSRF token)."""
    shop = get_object_or_404(Shop.objects.filter(HAS_REGISTER), pk=shop)
    if not request.user.is_authenticated or request.user.pk != shop.owner_id:
        return JsonResponse({"error": "owner"}, status=403)
    body = read(request)
    if body is None:
        return JsonResponse({"error": "format"}, status=400)
    return answer(shop, body, "owner", True)


@require_POST
def owner_explorer(request, shop):
    """The owner's own explorer (the settings' «Advanced»): saved on the shop so that the register page's content security
    policy lets the browser ask it, and so that the staff registers get it too. The site never fetches it. Empty clears it."""
    from .register import own_explorer_url

    shop = get_object_or_404(Shop.objects.filter(HAS_REGISTER), pk=shop)
    if not request.user.is_authenticated or request.user.pk != shop.owner_id:
        return JsonResponse({"error": "owner"}, status=403)
    if ratelimit.over_limit(f"explorer:{shop.pk}", 30, 3600):
        return JsonResponse({"error": "limit"}, status=429)
    try:
        body = json.loads(request.body.decode("utf-8")) if len(request.body) <= 1000 else None
    except (UnicodeDecodeError, ValueError):
        body = None
    value = body.get("explorer") if isinstance(body, dict) else None
    if not isinstance(value, str):
        return JsonResponse({"error": "format"}, status=400)
    url = own_explorer_url(value) if value.strip() else ""
    if url is None:
        return JsonResponse({"error": "explorer"}, status=400)
    shop.own_explorer = url
    shop.save(update_fields=["own_explorer"])
    return JsonResponse({"explorer": url})


@csrf_exempt  # no cookie counts here: the staff register's key, in the body, is the only proof
@require_POST
def staff_claim(request, register):
    from .staff import staff_register

    body = read(request)
    if body is None:
        return JsonResponse({"error": "format"}, status=400)
    try:
        staff = staff_register(register, body.get("k") if isinstance(body.get("k"), str) else "")
    except Http404:
        return JsonResponse({"error": "key"}, status=403)
    # Turned off: no new sale (a sale on its screen goes on with the address it has)
    if staff.retired or not Shop.objects.filter(HAS_REGISTER, pk=staff.shop_id).exists():
        return JsonResponse({"error": "retired"}, status=403)
    return answer(staff.shop, body, staff.pk, False)
