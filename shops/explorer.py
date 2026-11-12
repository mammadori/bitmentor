"""The fake explorer: a few addresses of mempool.space's API, answered by the site itself, for the tests and for
practice sites, where no public explorer sees the payments.

The register asks an explorer whether an on-chain payment arrived (static/register-chain.js). With FAKE_EXPLORER=1 the
register's explorer is this one (shops/register.py, `explorers`): it answers with the answers recorded in
shops/testdata/explorer.json, and with the pretend payments made on its page (/dev/explorer/), kept for a day in the
cache, with limits per visitor and on how many addresses are kept. Nothing here moves any money.

It cannot run where money is real: settings refuse FAKE_EXPLORER on mainnet, so in production too (production runs on
mainnet), and every view answers 404 unless the flag is on and the network is not mainnet, whatever the settings say.
The same 404 while on-chain at the register, an experimental feature, is off (config/features.py)."""
import hashlib
import json
import re
import time
from functools import cache

from django import forms
from django.conf import settings
from django.core.cache import cache as store
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET

from config import features, ratelimit

RECORDED = settings.BASE_DIR / "shops" / "testdata" / "explorer.json"
ADDRESS = re.compile(r"[a-zA-Z0-9]{14,90}\Z")
KEPT = 24 * 3600
MAX_PAYMENTS = 50
# The page needs no login (whoever tries the register on a practice site uses it), so it has limits: pretend
# payments per address of the visitor and per hour for everyone, and at most this many addresses with pretend payments
# kept at once (the oldest is forgotten). The cache holds the site's rate counters too: these must not push them out
PER_VISITOR = 60
PER_HOUR = 600
MAX_ADDRESSES = 200
ADDRESSES = "fake-explorer:addresses"


def enabled():
    """With FAKE_EXPLORER, never on mainnet, and only while on-chain at the register is on (an experimental feature,
    config/features.py): off, no register asks an explorer and these pages do not exist."""
    return (bool(getattr(settings, "FAKE_EXPLORER", False)) and settings.NETWORK != "mainnet"
            and features.enabled(features.ONCHAIN))


def api_base():
    """The explorer's API as the register uses it: on the site itself, so the CSP needs nothing more than 'self'."""
    return reverse("fake_explorer_api")


@cache
def recorded():
    return json.loads(RECORDED.read_text())["addresses"]


def key(address):
    return f"fake-explorer:{address}"


def txs_for(address):
    """The address's transactions as mempool.space gives them: the pretend ones first (newest first), then the recorded."""
    return [*store.get(key(address), []), *recorded().get(address, [])]


def pretend(address, sats, confirmed):
    """A pretend payment of `sats` to `address`, in the mempool or confirmed, shaped as mempool.space's answer: a block
    of now, since the register counts only a payment in the sale's time."""
    payments = store.get(key(address), [])
    txid = hashlib.sha256(f"{address}:{sats}:{len(payments)}:{settings.SECRET_KEY[:8]}".encode()).hexdigest()
    tx = {"txid": txid, "version": 2, "locktime": 0, "vin": [], "size": 222, "weight": 561, "fee": 500,
          "vout": [{"scriptpubkey_type": "v0_p2wpkh", "scriptpubkey_address": address, "value": sats}],
          "status": {"confirmed": True, "block_height": 200000, "block_time": int(time.time())} if confirmed else {"confirmed": False}}
    known = [item for item in store.get(ADDRESSES, []) if item != address]
    for forgotten in known[:max(0, len(known) + 1 - MAX_ADDRESSES)]:
        store.delete(key(forgotten))
    store.set(ADDRESSES, [*known[-(MAX_ADDRESSES - 1):], address], KEPT)
    store.set(key(address), [tx, *payments][:MAX_PAYMENTS], KEPT)
    return txid


def confirm(address):
    """The pretend payments to `address` get their confirmation."""
    payments = store.get(key(address), [])
    for tx in payments:
        tx["status"] = {"confirmed": True, "block_height": 200001, "block_time": int(time.time())}
    store.set(key(address), payments, KEPT)


@require_GET
def address_txs(request, address):
    if not enabled():
        raise Http404
    if not ADDRESS.fullmatch(address):
        return JsonResponse({"error": "Invalid Bitcoin address"}, status=400)
    return JsonResponse(txs_for(address), safe=False, headers={"Cache-Control": "no-store"})


def api_root(request):
    """Only the base of the API, for reverse(): the register adds /address/<address>/txs."""
    raise Http404


class PayForm(forms.Form):
    address = forms.RegexField(regex=ADDRESS, max_length=90)
    sats = forms.IntegerField(min_value=1, max_value=21 * 10**14, required=False)
    action = forms.ChoiceField(choices=[("mempool", "mempool"), ("confirmed", "confirmed"), ("confirm", "confirm")])


def pay(request):
    """The page that pretends: a payment to an address, in the mempool or confirmed, or the confirmation of the ones
    already made. For whoever tries the register on a practice site; the address is the one on the register's screen."""
    if not enabled():
        raise Http404
    if request.method == "POST":
        if ratelimit.over_limit(f"fake-explorer:{ratelimit.client_ip(request)}", PER_VISITOR, 3600) or ratelimit.over_limit("fake-explorer", PER_HOUR, 3600):
            return HttpResponse("Too many pretend payments: try again later.", status=429, content_type="text/plain",
                                headers={"Retry-After": str(ratelimit.seconds_left(3600))})
    form = PayForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        address, action = form.cleaned_data["address"], form.cleaned_data["action"]
        if action == "confirm":
            confirm(address)
        elif form.cleaned_data["sats"]:
            pretend(address, form.cleaned_data["sats"], action == "confirmed")
        return redirect(f"{reverse('fake_explorer')}?a={address}")
    shown = request.GET.get("a", "")
    response = render(request, "shops/fake_explorer.html", {
        "form": form, "address": shown if ADDRESS.fullmatch(shown) else "",
        "txs": txs_for(shown) if ADDRESS.fullmatch(shown) else []})
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response
