"""The register: the phone asks the shop's Lightning address for each invoice (LNURL-pay), works out the sats with the rate
of public price sources and checks the payment with the provider's verify URL (LUD-21), all in the browser
(static/register.js). Only what the page needs to know is here: the address, the currency, the exchange-rate sources and
the servers the register's CSP lets it talk to. All of it is data that whoever has the register link can already know: no
key, no token. The sales stay on the phone: the site never receives them."""
import math
import re
import time
from urllib.parse import urlsplit

from django.conf import settings
from django.utils.translation import gettext as _

from config import features, weburl
from config.community import community

# Where the phone asks for the exchange rate. Each has a different operator and answers the browser with open CORS.
# register.js knows how to read each answer, and has the same list of names.
PRICE_SOURCES = {
    "kraken": "https://api.kraken.com",
    "coinbase": "https://api.coinbase.com",
    "bitstamp": "https://www.bitstamp.net",
    "mempool": "https://mempool.space",
    "coingecko": "https://api.coingecko.com",
    "blockchain": "https://blockchain.info",
}
# The first two are asked together, the others one at a time if one of the two does not answer or the two disagree
DEFAULT_SOURCES = ["kraken", "coinbase", "bitstamp", "mempool", "coingecko", "blockchain"]
# Two sources further apart than this are not averaged: the register asks the next one, and when no two agree the
# merchant decides
TOLERANCE = 1.0
# The most `register.tolerance` can be: wider, two sources far apart would still make one rate
MAX_TOLERANCE = 20.0
# Minutes after which the last good rate is no longer enough to take a payment
MAX_RATE_AGE = 120
# A domain name and nothing else: it ends up in a header, and a space or a semicolon would open the CSP to other things
DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z")
USER = re.compile(r"[a-z0-9._+-]+\Z", re.I)
# Where the register asks whether an on-chain payment arrived: mempool.space's API, or an instance of the same API that
# the community runs (`register.explorers` in community.yml). Only these hosts enter the register's CSP
EXPLORERS = {"mainnet": ["https://mempool.space/api"], "signet": ["https://mempool.space/signet/api"]}
EXPLORER_PATH = re.compile(r"(?:/[A-Za-z0-9._~-]+)*/?\Z")
# The discount for paying in bitcoin the register suggests to a shop that has none (`register.discount`), and the amount,
# in the register's currency, above which an on-chain sale waits for a confirmation (`register.confirm_above`; 0: always)
DISCOUNT = 0
CONFIRM_ABOVE = 0
# Where the registers keep their encrypted copies on Nostr (docs/features/nostr-copies.md): `register.relays`, the
# community's relays, always used, and `register.public_relays`, public relays an owner may add on the phone. Only these
# enter the register's CSP: the phone cannot write to any other relay. Without `register.relays` there is no copy on Nostr
MAX_RELAYS = 8
RELAY_PATH = re.compile(r"(?:/[A-Za-z0-9._~-]+)*/?\Z")


def register_block():
    data = community().get("register", {})
    return data if isinstance(data, dict) else {}


def read_tolerance(data):
    """`register.tolerance` as a number from 0 to MAX_TOLERANCE, and whether it was usable as written. «1%» is read as 1;
    a word, a negative number, inf or nan would reach the page as NaN or Infinity and break every register: the default
    applies. Above the most, the most."""
    try:
        value = float(str(data.get("tolerance", TOLERANCE)).rstrip("% "))
    except ValueError:
        return TOLERANCE, False
    if not math.isfinite(value) or value < 0:
        return TOLERANCE, False
    return min(value, MAX_TOLERANCE), value <= MAX_TOLERANCE


def read_max_age(data):
    """`register.max_rate_age` in whole minutes from 0, and whether it was usable as written."""
    try:
        value = int(data.get("max_rate_age", MAX_RATE_AGE))
    except (ValueError, TypeError, OverflowError):
        return MAX_RATE_AGE, False
    return (value, True) if 0 <= value <= 10**6 else (MAX_RATE_AGE, False)


def options():
    """`register` in community.yml: price sources, tolerance in % and age in minutes."""
    data = register_block()
    listed = data.get("price_sources") or DEFAULT_SOURCES
    sources = [name for name in dict.fromkeys(listed if isinstance(listed, list) else [listed]) if name in PRICE_SOURCES]
    # «1%» or «two hours» written in community.yml must not give a 500 to every register: the default value applies
    return {"sources": sources, "tolerance": read_tolerance(data)[0], "max_age": read_max_age(data)[0]}


def number_problems():
    """What `manage.py check` says of `register.tolerance` and `register.max_rate_age`: a value the register does not use
    as written."""
    data = register_block()
    found = []
    if not read_tolerance(data)[1]:
        found.append(f"register.tolerance: {data.get('tolerance')!r} is not a percentage from 0 to {MAX_TOLERANCE:g}: the "
                     f"register uses {read_tolerance(data)[0]:g}")
    if not read_max_age(data)[1]:
        found.append(f"register.max_rate_age: {data.get('max_rate_age')!r} is not a whole number of minutes from 0: the "
                     f"register uses {MAX_RATE_AGE}")
    return found


def explorer_url(value):
    """A well-written explorer API (https, a domain name, a plain path, no port, query or fragment) without the final
    slash, or None. It ends up in a header too."""
    found = weburl.parts(value, "https")
    if found is None:
        return None
    parts, host, port = found
    if (not DOMAIN.match(host) or port is not None or parts.query or parts.fragment or parts.netloc.lower() != host
            or not EXPLORER_PATH.match(parts.path)):
        return None
    return f"https://{host}{parts.path.rstrip('/')}"


# The longest address of an owner's own explorer
OWN_EXPLORER_MAX = 200


def own_explorer_url(value):
    """An owner's own explorer (an Esplora or mempool API on the owner's node, asked by the owner's browser): like a
    community's, https only (the register is an https page, a plain http address would be blocked as mixed content), a
    domain name, no credentials, query or fragment, but a port is fine (a node's web app often has one). Without the
    final slash, or None."""
    if not isinstance(value, str) or len(value.strip()) > OWN_EXPLORER_MAX:
        return None
    found = weburl.parts(value, "https")
    if found is None:
        return None
    parts, host, port = found
    if (not DOMAIN.match(host) or parts.query or parts.fragment or port == 0 or not EXPLORER_PATH.match(parts.path)
            or parts.netloc.lower() != f"{host}{f':{port}' if port else ''}"):
        return None
    return f"https://{host}{f':{port}' if port else ''}{parts.path.rstrip('/')}"


def explorers():
    """The explorers of this network, the first one first. `register.explorers` is a list, or one list per network
    ({mainnet: [...], signet: [...]}); without it, or with nothing valid in it, mempool.space. With the fake explorer
    (shops/explorer.py, never on mainnet) the site itself."""
    from . import explorer

    if explorer.enabled():
        return [explorer.api_base()]
    data = community().get("register", {})
    listed = data.get("explorers") if isinstance(data, dict) else None
    if isinstance(listed, dict):
        listed = listed.get(settings.NETWORK)
    if isinstance(listed, str):
        listed = [listed]
    found = [url for url in map(explorer_url, listed if isinstance(listed, list) else []) if url]
    return list(dict.fromkeys(found)) or EXPLORERS[settings.NETWORK]


def explorer_problems():
    """What `manage.py check` says of `register.explorers`: a value the register will not use."""
    data = community().get("register", {})
    listed = data.get("explorers") if isinstance(data, dict) else None
    values = [item for value in (listed.values() if isinstance(listed, dict) else [listed]) if value is not None
              for item in (value if isinstance(value, list) else [value])]
    return [f"register.explorers: {value!r} is not an https address of an explorer's API (https://host/path): it is "
            "not used" for value in values if not explorer_url(value)]


def relay_url(value):
    """A well-written relay (wss, a domain name, a plain path, no port, query or fragment) without the final slash, or
    None. It ends up in a header too. A path alone, `/relay`, is a relay of the site itself: wss://SITE_HOST/relay, so
    one community.yml serves a development site and the real one."""
    if not isinstance(value, str):
        return None
    if value.strip().startswith("/") and not value.strip().startswith("//"):
        value = f"wss://{settings.SITE_HOST}{value.strip()}"
    found = weburl.parts(value, "wss")
    if found is None:
        return None
    parts, host, port = found
    if (not DOMAIN.match(host) or port is not None or parts.query or parts.fragment or parts.netloc.lower() != host
            or not RELAY_PATH.match(parts.path)):
        return None
    return f"wss://{host}{parts.path.rstrip('/')}"


def listed_relays(name):
    data = community().get("register", {})
    listed = data.get(name) if isinstance(data, dict) else None
    return [listed] if isinstance(listed, str) else listed if isinstance(listed, list) else []


def relays():
    """The community's relays and the public ones an owner may add, each at most MAX_RELAYS, without repeats."""
    community_relays = list(dict.fromkeys(url for url in map(relay_url, listed_relays("relays")) if url))[:MAX_RELAYS]
    public = [url for url in dict.fromkeys(map(relay_url, listed_relays("public_relays")))
              if url and url not in community_relays][:MAX_RELAYS]
    return {"relays": community_relays, "public_relays": public}


def relay_problems():
    """What `manage.py check` says of `register.relays` and `register.public_relays`: a value the register will not use."""
    return [f"register.{name}: {value!r} is not a wss address of a relay (wss://host/path): it is not used"
            for name in ("relays", "public_relays") for value in listed_relays(name) if not relay_url(value)]


def number(data, name, default, least, most):
    """A whole number of community.yml within bounds, or the default: a mistake there must not stop every register."""
    try:
        value = int(str(data.get(name, default)).rstrip("% "))
    except (TypeError, ValueError):
        return default
    return value if least <= value <= most else default


def preferences(shop):
    """What the owner's register starts from, before the owner changes anything (it keeps the changes on the phone): the
    discount of the shop, or the community's suggestion; the threshold for a confirmation, in cents; who the shop is."""
    data = community().get("register", {})
    data = data if isinstance(data, dict) else {}
    colour = community().get("theme_color", "")
    return {
        "discount": shop.discount or number(data, "discount", DISCOUNT, 0, 100),
        "confirm_above": number(data, "confirm_above", CONFIRM_ABOVE, 0, 10**7) * 100,
        "shop": {"name": shop.name[:120], "town": shop.city[:100],
                 "colour": colour if re.fullmatch(r"#[0-9a-fA-F]{6}", str(colour)) else ""},
    }


def lightning_parts(shop):
    """User and domain of the Lightning address, if it is well written."""
    user, at, domain = shop.lightning_address.strip().lower().rpartition("@")
    if not at or not USER.match(user) or not DOMAIN.match(domain):
        return None
    return user, domain


def register_data(shop):
    """What the register page carries to take payments, or None if this shop cannot have one (no well-written address,
    or fewer than two price sources in community.yml)."""
    from .address_change import previous_domain  # it imports this module

    parts = lightning_parts(shop)
    choices = options()
    if not parts or len(choices["sources"]) < 2:
        return None
    return {
        "address": f"{parts[0]}@{parts[1]}", "domain": parts[1], "currency": community().get("currency", "EUR"),
        **choices, "network": settings.NETWORK, **onchain_data(shop), **relays(), **preferences(shop),
        # The site's time, for the phone to see how far off its own clock is (static/register.js, measureClock)
        "now": int(time.time() * 1000), "texts": texts(),
        # The old address's provider after the owner changed it, for a few days: the sales shown on it are still checked
        **({"previous_domain": previous} if (previous := previous_domain(shop)) else {})}


def onchain_data(shop):
    """What the page carries for on-chain, an experimental feature (config/features.py): `chain_on`, which the register
    reads before it offers on-chain at all (static/register.js, CHAIN_ON), the explorers, and the shop's own explorer,
    asked before the community's by the owner's and the staff registers (the browser asks it). Off, nothing: the
    register takes Lightning only, and no explorer enters its CSP (connect_origins)."""
    if not features.enabled(features.ONCHAIN):
        return {}
    return {"chain_on": True, "explorers": explorers(),
            **({"own_explorer": own} if (own := own_explorer_url(shop.own_explorer)) else {})}


def connect_origins(data):
    """The servers the register talks to besides the site: the Lightning provider, with its subdomains, the rate sources,
    the explorers of the community (only their origin: the site's own fake explorer is already 'self') and the relays
    of community.yml (only wss://, by host)."""
    if not data:
        return []
    chain = [f"https://{urlsplit(url).hostname}" for url in data.get("explorers", []) if url.startswith("https://")]
    # The shop's own explorer: its origin with the port (a CSP source without one means the default port only)
    chain += [f"https://{urlsplit(data['own_explorer']).netloc}"] if data.get("own_explorer") else []
    nostr = [f"wss://{urlsplit(url).hostname}" for url in [*data.get("relays", []), *data.get("public_relays", [])]
             if url.startswith("wss://")]
    previous = [f"https://{data['previous_domain']}", f"https://*.{data['previous_domain']}"] if data.get("previous_domain") else []
    return list(dict.fromkeys([f"https://{data['domain']}", f"https://*.{data['domain']}", *previous,
                               *(PRICE_SOURCES[name] for name in data["sources"]), *chain, *nostr]))


def texts():
    return {
        "rate": _("Getting the exchange rate..."),
        # A part of the register loaded at its first use (static/register.js, need) that the phone could not get
        "not_loaded": _("This part of the register did not load: check the connection and try again."),
        "invoice": _("Asking your Lightning wallet for the invoice..."),
        "agreed": _("Exchange rate: {rate} ({sources})."),
        "to": _("Paid into {address}"),
        "disagree": _("The two sources do not agree: {first} and {second}. Which rate do you want to use?"),
        "disagree_many": _("The sources do not agree: {rates}. Which rate do you want to use?"),
        "single": _("Only one source answers: {first}. Use this rate?"),
        "stale": _("No source answers. The last good rate is {first}, from {age} ago. Use it?"),
        "too_old": _("No source answers and the last good rate is too old: the register cannot charge."),
        "no_rate": _("No source answers: the register cannot calculate the amount in sats."),
        "use": _("Use {rate}"),
        "no_answer": _("Your Lightning wallet does not answer: the register cannot charge."),
        "limits": _("Your Lightning wallet does not accept this amount: too high or too low."),
        "elsewhere": _("Your Lightning wallet answers from another address: the register cannot use it."),
        "bad_invoice": _("Your Lightning wallet sent a wrong invoice: do not use it."),
        "old_invoice": _("Your Lightning wallet sent an invoice that is old or already used for another sale: do not use "
                         "it. Try again in a moment."),
        "no_check": _("Careful: your Lightning wallet does not tell the register when a payment arrives. Hand over the "
                      "goods only after you see the payment in your wallet, then confirm here."),
        "check_failed": _("Cannot check the payment with your Lightning wallet right now: still trying."),
        "unverifiable": _("Your Lightning wallet says an invoice was paid, but the register cannot check that it is this "
                          "sale's. Look in your wallet: if you see the payment, tap «I see the payment in my wallet»."),
        "clock_off": _("This phone's clock is {minutes} min away from the site's: set the date and time right in the "
                       "phone's settings."),
        "seen": _("I see the payment in my wallet"),
        "paid": _("Payment received"),
        "confirmed": _("Marked as paid by you"),
        "expired": _("Payment expired"),
        "cancelled": _("Payment cancelled"),
        "pending": _("Waiting"),
        "unchecked": _("Not checked: look in your wallet"),
        "confirmed_by": _("Marked as paid by {who}"),
        "confirmed_elsewhere": _("Marked as paid by hand on another phone"),
        "method_onchain": _("On-chain"),
        "log": _("Sales on this phone: {count}"),
        "minutes": _("{count} min"),
        "hours": _("{count} h"),
        **history_texts(),
        **onchain_texts(),
    }


def onchain_texts():
    """The words of on-chain, of the discount, of the owner's settings and of a staff register's pairing
    (static/register.js)."""
    return {
        "new_address": _("Preparing a new on-chain address..."),
        "to_chain": _("On-chain address …/{path} of your key: {address}"),
        "to_fixed": _("Your fixed on-chain address: {address}"),
        "to_staff": _("On-chain, to the shop: {address}"),
        "chain_seen": _("Payment seen: waiting for a confirmation in a block. It can take ten minutes or more."),
        "chain_partial": _("Less than asked has arrived: {seen} of {sats}. Do not hand over the goods: ask the customer to "
                           "pay the rest, or cancel."),
        "chain_later": _("Next customer: check this sale later"),
        "chain_down": _("The explorer does not answer: the register cannot see the payment. Look in your wallet."),
        "chain_fixed_down": _("The explorer does not answer: with a fixed address the register cannot tell this sale's "
                              "payment from earlier ones. Use Lightning, or try again in a moment."),
        "chain_no_key": _("The on-chain key on this phone is not valid any more: check it in the settings."),
        "chain_unchecked": _("The explorer does not answer, and this phone has no count of its own of the on-chain "
                             "addresses (a history brought back from a copy): the register gives an address only when "
                             "the explorer says it is unused. Take this sale with Lightning, or try again in a moment."),
        "chain_used_up": _("The register asked the explorer about many addresses of this chain and did not find one "
                           "that is surely unused: the history on this phone may miss many sales. Take this sale with "
                           "Lightning, or try again later."),
        "chain_far": _("Many addresses after the last one this phone knows look used (by the explorer's answers). If "
                       "your wallet really used them, set the starting number in Settings; otherwise ask the community "
                       "about its explorer. Take this sale with Lightning."),
        "chain_far_staff": _("Many addresses after the last one this register knows look used (by the explorer's "
                             "answers). Tell the owner: if the wallet really used them, the owner adds this register "
                             "again and pairs it (a new path of addresses); otherwise ask the community about its "
                             "explorer. Take this sale with Lightning."),
        "chain_pairing_key": _("The on-chain key of this register's pairing does not give addresses any more: ask the "
                               "owner for a new pairing code, and take this sale with Lightning."),
        "chain_sign_in": _("To take on-chain sales here, sign in on this phone as the shop's owner: all the shop's "
                           "registers share one count of the addresses, kept by the site, so that none is given twice. "
                           "Take this sale with Lightning."),
        "chain_claim_down": _("The site does not answer, and the shop's registers share one count of the on-chain "
                              "addresses through it: no address without it. Take this sale with Lightning, or try again "
                              "in a moment."),
        "chain_not_opened": _("The shop's on-chain addresses are not ready for this register yet: ask the owner to "
                              "open this register's code page on the owner's phone once more. Take this sale with Lightning."),
        "path_now": _("On-chain addresses in use: …/{path} of your key."),
        "path_bad": _("Not a path: whole numbers separated by «/», at most four, none hardened (no ' or h)."),
        "path_no_key": _("A path needs an extended public key, not a fixed address."),
        "path_first": _("First address of this key at …/{path}: {address}. Check that your wallet shows the same one, "
                        "then tick the box."),
        "chain_small": _("Too little for on-chain: the least is {sats}, below it wallets refuse the payment or its fee "
                         "eats it. Take this amount with Lightning."),
        "seen_state": _("Seen, waiting for a confirmation"),
        "discount_on": _("Bitcoin discount −{percent}%: {amount} to pay. Tap to remove it for this sale."),
        "discount_off": _("No discount on this sale. Tap to give the −{percent}% bitcoin discount."),
        "discount_fixed": _("Bitcoin discount −{percent}%: {amount} to pay."),
        "bad_discount": _("Write the discount as a whole number from 0 to 100."),
        "scan_checking": _("Looking for the first unused address of your wallet…"),
        "scan_far": _("Your wallet has used many addresses: the register would start at number {index}."),
        "scan_down": _("The explorer did not answer: the register looks for the first unused address at the first "
                       "on-chain sale."),
        "unpaired": _("This register is not paired yet: ask the owner to show you the pairing code of this register, "
                      "and scan it with this phone's camera."),
        "ln_changed": _("The shop's Lightning address changed after this register was paired: ask the owner for a new "
                        "pairing code."),
        "paired": _("Paired with the owner's code on {date}."),
        "paired_chain": _("On-chain: yes."),
        "paired_now": _("Register paired. Payments go to {address}."),
        "pairing_wrong": _("This pairing code cannot be used: ask the owner for a new one."),
        "pairing_unreadable": _("This register was paired, but its pairing no longer matches the site's settings: it "
                                "cannot take payments. Ask the owner for a new pairing code."),
        "pin_changed": _("The shop's Lightning address changed: before it was {before}, now it is {after}. If you did not "
                         "change it, do not take payments and tell the association."),
        "key_first": _("First receive address of this key: {address}. Check that your wallet shows the same one, then tick "
                       "the box and save."),
        "confidence_sure": _("Confidence: sure ({why})."),
        "confidence_likely": _("Confidence: probable ({why})."),
        "confidence_wait": _("Confidence: low, wait for a confirmation ({why})."),
        "why_confirmed": _("confirmed in a block"),
        "why_confirmations": _("confirmations: {count}"),
        "why_fee_next": _("fee {rate} sat/vB, at least the next block's {target}"),
        "why_fee_good": _("fee {rate} sat/vB, at least the half hour's {target}"),
        "why_fee_hour": _("fee {rate} sat/vB, below the half hour's {target}: it may take up to an hour"),
        "why_fee_low": _("fee {rate} sat/vB, below the next hour's {target}: it may wait long, or be dropped or replaced"),
        "why_rbf": _("the sender marked it as replaceable (RBF)"),
        "why_no_rbf": _("not marked as replaceable (no RBF)"),
        "why_parent": _("it spends a payment not confirmed yet: if that one is dropped, this goes with it"),
        "why_no_parent": _("no unconfirmed parent"),
        "why_parents_unknown": _("the explorer does not say whether it spends unconfirmed payments"),
        "level_sure": _("Confidence: sure"),
        "level_likely": _("Confidence: probable"),
        "level_wait": _("Confidence: low"),
        "why_fee_unknown": _("the explorer does not say the fee level"),
        "why_source_own": _("answered by your own explorer"),
        "why_source_community": _("answered by {host}: your own explorer did not answer"),
        "why_source_public": _("answered by {host}"),
        "own_explorer_saved": _("Saved. Reload the register for the new explorer to be used."),
        "own_explorer_bad": _("Your own explorer must be an https address (https://host/api): no password in it, no query."),
        "own_explorer_failed": _("The site could not save your own explorer: try again."),
        "chain_owner_down": _("The explorer does not answer: the register gives an address of your wallet only after "
                              "the explorer says it is unused. Take this sale with Lightning, or try again in a moment."),
        "chain_over": _("More than asked has arrived: {seen} of {sats}. It may be a payment for something else to the "
                        "same address: look in your wallet, and confirm by hand only if it is this sale's."),
        "chain_outside": _("A payment of {seen} reached this address before this sale or long after it: it is not "
                           "counted for this sale. Look in your wallet."),
        "chain_gap": _("On-chain addresses of this register not paid in a row: {count}."),
        "chain_gap_hint": _("A wallet stops looking after 20 unused addresses in a row (the gap limit): raise it in your "
                            "wallet's settings, or the next payments may not show there. The money is safe all the same."),
        "fixed_warning": _("A fixed address: every payment goes to the same address, so two sales of the same amount at "
                           "the same time cannot be told apart, a payment arriving late for a cancelled sale or an earlier "
                           "payment replaced by its sender can be taken for the sale on the screen, and anyone who sees "
                           "it sees all your sales. An extended public key (zpub) is better. Tick the box to use it "
                           "anyway."),
        "tick": _("Tick the box to confirm."),
        "bad_destination": _("This is neither an extended public key nor an address of this network."),
        "wrong_network": _("This key or address is of another network."),
        "private_key": _("This is a private key: never write it anywhere. Use the public one (zpub, xpub…)."),
        "few_sources": _("Choose at least two price sources."),
        "bad_currency": _("Write the currency as three letters, like EUR."),
        "bad_amount": _("Write the amount as a number, like 50 or 49.90."),
        "settings_saved": _("Saved on this phone. Staff registers get the new settings only with a new pairing code."),
        "start_first": _("On-chain address …/{path} of your key: {address}. Check that your wallet shows the same one at "
                         "that number, then tick the box and save."),
        "start_set": _("The next on-chain sale uses the address …/{path}."),
        "start_bad": _("Write the starting number as a whole number, like 120."),
        "start_no_key": _("The starting number needs an extended public key of your wallet (zpub, xpub…)."),
        **nostr_texts(),
        "imported_foreign": _("Careful: {count} on-chain sales of this copy were paid or asked for payment to addresses "
                              "that are not of your current key: {times}. Check them in your wallet."),
        "imported_check": _("Check the staff registers' on-chain sales in your wallet too: only the wallet shows that a "
                            "payment really arrived."),
    }


def nostr_texts():
    """The words of the copies on Nostr (static/register.js)."""
    return {
        "nostr_sent": _("Last copy on Nostr: {date}, on {count} relays."),
        "nostr_never": _("No copy on Nostr yet."),
        "nostr_failed": _("No relay took the copy: the register tries again after the next sale."),
        "nostr_bad_public": _("This is not a Nostr public key (npub…)."),
        "nostr_bad_secret": _("This is not a Nostr private key (nsec…)."),
        "nostr_other_secret": _("This private key is not the one of the public key in the settings."),
        "nostr_need_key": _("{count} copies of staff registers cannot be read on this phone (it no longer holds their "
                            "keys): give your Nostr private key below to read them."),
        "nostr_no_relay": _("No relay answers: try again in a moment."),
        "nostr_pulled": _("From Nostr: {added} new sales, {updated} updated, {refused} copies that are not yours. The "
                          "staff registers' sales keep arriving while this page is open."),
        "nostr_forget_ask": _("Forget the Nostr keys of all the staff registers paired from this phone? Their copies "
                              "will no longer come into this register until you pair them again."),
        "nostr_forgotten": _("The staff registers' Nostr keys are forgotten: pair them again to receive their sales."),
        "nostr_strangers": _("{count} copies from keys this phone does not know were left out."),
        "nostr_strangers_ask": _("{copies} copies on Nostr ({sales} sales) come from {keys} keys this phone does not "
                                 "know. They may be your own, from a phone you no longer have, or written by anyone who "
                                 "knows your public key. Take them, every sale marked as reported, to check in your "
                                 "wallet?"),
        "nostr_strangers_taken": _("From keys this phone does not know: {added} sales taken as reported, {left} left out "
                                   "(bring those back with the encrypted file)."),
    }


def history_texts():
    """The words of the history on the phone (static/register.js and register-book.js): the day closing, the copies and
    the CSV for the accountant."""
    return {
        "full": _("The phone has no more room for the register's history: download a copy now."),
        "last_copy": _("Last copy: {date}."),
        "no_copy": _("No copy downloaded yet."),
        "copy_due": _("It is older than the reminder you chose: download a copy now."),
        "copy_now": _("Day closed. Now download the encrypted copy."),
        "closed": _("Closed at {time}."),
        "day_line": _("{method}: {count} paid, {amount} ({sats})"),
        "by_hand": _("Of these, {count} marked as paid by you: check them in your wallet."),
        "verified_count": _("Of these, {count} reported by a staff register and checked by this register with the payment "
                            "provider: no need to look in the wallet."),
        "verified_mark": _("checked with the provider"),
        "reported_count": _("Of these, {count} reported by a staff register: check them in your wallet."),
        "late_count": _("Of these, {count} paid after you cancelled them: settle it with the customer."),
        "late_mark": _("paid after it was cancelled"),
        "no_paid": _("No paid sales on this day."),
        "unchecked_count": _("Not checked: {count}. Look for them in your wallet."),
        "waiting_count": _("Still waiting for a payment or a confirmation: {count}. The register looks again by itself."),
        "not_paid": _("Not paid (expired or cancelled): {count}"),
        "short": _("The passphrase must be at least {count} characters long."),
        "mismatch": _("The two passphrases are different."),
        "busy": _("Just a moment..."),
        "no_crypto": _("This browser cannot encrypt the copy: try another browser."),
        "saved": _("Copy downloaded. Keep it somewhere other than this phone, and keep the passphrase too."),
        "wrong": _("Wrong passphrase, or a damaged file."),
        "newer": _("This copy comes from a newer version of the register: reload the page and try again."),
        "not_copy": _("This file is not a copy of a register."),
        "practice_copy": _("This is a copy of the practice register: its sales are pretend, and it does not come into "
                           "the real register."),
        "real_copy": _("This is a copy of a real register: it does not come into the practice."),
        "imported": _("Copy imported: {added} new sales, {updated} updated."),
        "imported_refused": _("{count} sales of this register's own were left as they were. If the file is a copy of "
                              "this register, and only if you made it yourself on this register, tick «It is a copy of "
                              "this register» and import it again."),
        "imported_unsure": _("Its sales came in as reported, to check in your wallet: this phone already has sales, and the "
                             "box saying it is a copy of this register was not ticked."),
        "csv_columns": [
            _("Date and time"), _("Business day"), _("Amount"), _("Currency"), _("Status"), _("Payment method"),
            _("Satoshis"), _("Exchange rate (per BTC)"), _("How the rate was chosen"), _("Rate sources"), _("Payment hash"),
            _("Staff register"), _("Reported by (check in the wallet)"), _("Discount (%)"), _("Price before the discount"), _("On-chain address"),
            _("Transaction"), _("Sale ID")],
        "hows": {
            "agreed": _("two sources agreed"), "chosen": _("chosen at the register"),
            "stale": _("last good rate, chosen at the register")},
    }
