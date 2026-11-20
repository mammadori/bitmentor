"""The owner's panel by stages: on top, for each shop, only the stage it is at, with one step and one button; the rest of
the panel stays below, closed under «Everything, if you want» (templates/shops/panel.html). A community turns it off
with `features: {stages: false}`: the panel is then exactly the one without stages.

Nothing here is stored: a shop's stage is worked out at each visit from facts the site already has (a Lightning
address, the first sale the owner ticked, the till's lock of the training, the approval, the sign ticked as printed, the
days since the first sale). The site never counts sales: «sales or days, whichever comes first» is counted on the phone,
by static/stages.js, from the register's own ledger in that browser; here only the days are known.

The stages, in order:
1. WALLET   the shop and the wallet: until the shop has a Lightning address that answers;
2. TRIAL    the trial: the practice register, then a first real payment by the owner (the first day's `first_sale`);
3. TAKINGS  the first takings: the real register, within the training's `quick_start` when there is one;
4. PASS     the basic pass of the training: the only step while the training locks the till; proposed from 3;
5. MAP      on the map: the shop approved and shown, its sign to print (until it is ticked as printed);
6. ROUTINE  the routine, `routine_days` after the first sale (or `routine_sales` sales on the phone);
7. REST     the rest, always there and closed: on-chain, holidays, OpenStreetMap, Nostr copies, the discount, the address;
8. GAME     going further with the training, `game_days` after the first sale (or `game_sales` sales on the phone).

A stage whose facts do not exist is skipped, never shown empty: without the `training` block there are no 4 and 8; 5
follows the public lists (approved, listed, not suspended, with a position), so with the approval off a new shop has it at
once and a shop still waiting from before does not; a historic shop has no stage at all. A shop the community set
aside (shops/approval.py, `set_aside`) is not approved, so it never gets 5: it walks the others as it is (the trial, the
takings, the routine), and the panel says above the stage that it will not go on the map, instead of telling it to wait.

The numbers, in `community.yml`:

    stages:
      routine_days: 7
      routine_sales: 5
      game_days: 30
      game_sales: 20
"""

from django.utils import timezone
from django.utils.formats import number_format
from django.utils.translation import gettext_lazy as _

from config import features
from config.community import community

from . import address_change

WALLET, TRIAL, TAKINGS, PASS, MAP, ROUTINE, REST, GAME = (
    "wallet",
    "trial",
    "takings",
    "pass",
    "map",
    "routine",
    "rest",
    "game",
)
ORDER = (WALLET, TRIAL, TAKINGS, PASS, MAP, ROUTINE, REST, GAME)
TITLES = {
    WALLET: _("The shop and the wallet"),
    TRIAL: _("The trial"),
    TAKINGS: _("The first takings"),
    PASS: _("The basic pass"),
    MAP: _("On the map"),
    ROUTINE: _("The routine"),
    REST: _("More, whenever you like"),
    GAME: _("Going further"),
}
DEFAULTS = {"routine_days": 7, "routine_sales": 5, "game_days": 30, "game_sales": 20}
LIMITS = {"routine_days": (1, 365), "routine_sales": (1, 1000), "game_days": (1, 365), "game_sales": (1, 1000)}
# The lines of the rest (stage 7), in the order they are shown
REST_LINES = ("onchain", "nostr", "vacation", "osm", "discount", "address")


def enabled():
    return features.enabled(features.STAGES)


def currency():
    """The currency of the register's amounts, with the register's own default (shops/register.py): the quick start the
    stage counts is the one the register counts."""
    return community().get("currency", "EUR")


def options():
    data = community().get("stages")
    return data if isinstance(data, dict) else {}


def number(name, warn=None):
    """A number of the `stages` block, or its default when it is missing or cannot be used (and why, to `warn`)."""
    value = options().get(name, DEFAULTS[name])
    low, high = LIMITS[name]
    if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
        return value
    if warn:
        warn(f"`stages.{name}` is a whole number from {low} to {high}: {value!r}; {DEFAULTS[name]} counts instead")
    return DEFAULTS[name]


def numbers(warn=None):
    return {name: number(name, warn) for name in DEFAULTS}


def problems():
    """What is wrong in the `stages` block, for `manage.py check`: a wrong value counts as its default."""
    found = []
    data = community().get("stages")
    if data is not None and not isinstance(data, dict):
        return [f"`stages` is a block with {', '.join(DEFAULTS)}: {data!r}; the defaults count instead"]
    for name in options():
        if name not in DEFAULTS:
            found.append(f"`stages.{name}` is not a setting of the stages (they are: {', '.join(DEFAULTS)}): left out")
    numbers(found.append)
    return found


def on_the_map(shop):
    """Whether the shop shows on the map now, as the public lists read it (directory.public_shops): approved, whatever
    `features.approval` says now, listed, not suspended, with a position. The caller has already set aside a historic
    shop and one without its wallet."""
    return (
        not shop.needs_approval
        and shop.listed
        and not shop.suspended
        and shop.latitude is not None
        and shop.longitude is not None
    )


def days_since(moment, now):
    """Whole days of the community's calendar between `moment` and `now`."""
    return (timezone.localdate(now) - timezone.localdate(moment)).days


def stage(key, **found):
    return {"key": key, "number": ORDER.index(key) + 1, "title": TITLES[key], **found}


def current(shop, *, now, numbers, training_on, has_pass):
    """The stage `shop` is at, as a dict ({"key", "number", "title", ...}), or None for a shop with nothing to walk (a
    historic one, one without a register that cannot get one here). Reads the attributes the panel already set on the
    shop: `needs_wallet`, `waits_for_wallet`, `lightning_watch`, `till_locked`, `till_quick`."""
    watch = getattr(shop, "lightning_watch", None)
    if shop.closed:
        # A historic shop is long past its stages, with or without a register
        return None
    if not shop.has_register:
        return (
            stage(WALLET, waits=bool(getattr(shop, "waits_for_wallet", False)))
            if getattr(shop, "needs_wallet", False)
            else None
        )
    if watch and watch.get("broken"):
        return stage(WALLET, broken=True)
    if training_on and getattr(shop, "till_locked", False):
        # The training locks the real till (gate `unlock`, no quick start): the basic pass is the only step, before
        # the trial too, since the register takes no real payment until then
        return stage(PASS)
    if not shop.first_sale:
        return stage(TRIAL, tick={"key": "sale", "state": "todo"})
    if on_the_map(shop) and not shop.sign_printed:
        return stage(MAP, tick={"key": "sign", "state": "todo"})
    days = days_since(shop.first_sale, now)
    if training_on and days >= numbers["game_days"]:
        return stage(GAME, days=days)
    if days >= numbers["routine_days"]:
        found = stage(ROUTINE, days=days)
    else:
        found = stage(
            TAKINGS, days=days, quick=getattr(shop, "till_quick", 0) or 0, propose_pass=training_on and not has_pass
        )
    # On a phone with enough sales in the register's ledger (static/stages.js), a later stage opens before its days
    found["promote"] = [
        stage(key, sales=numbers[f"{key}_sales"])
        for key in (ROUTINE, GAME)
        if ORDER.index(key) > ORDER.index(found["key"]) and (key != GAME or training_on)
    ]
    return found


def rest(shop, *, onchain, nostr):
    """The lines of stage 7 for `shop`, among REST_LINES: only those that exist for it (a feature turned on, a register,
    the attributes the panel set: `break_eligible`, `osm_enabled`)."""
    lines = {
        "onchain": onchain and shop.has_register,
        "nostr": nostr and shop.has_register,
        "vacation": bool(getattr(shop, "break_eligible", False)),
        "osm": bool(getattr(shop, "osm_enabled", False)),
        "discount": not shop.closed,
        # Where the page of the change opens (a suspended or demo shop takes no new destination)
        "address": address_change.changeable(shop),
    }
    return [line for line in REST_LINES if lines[line]]


def panel(shops, *, training_state, onchain, nostr, now=None):
    """Sets `stage` and `stage_rest` on each shop of the panel. `training_state` is training.panel's `state` (None
    without the training block)."""
    now = now or timezone.now()
    found = numbers()
    training_on = training_state is not None
    has_pass = bool(training_state and training_state.base)
    for shop in shops:
        shop.stage = current(shop, now=now, numbers=found, training_on=training_on, has_pass=has_pass)
        shop.stage_rest = rest(shop, onchain=onchain, nostr=nostr) if shop.stage else []
        if shop.stage and shop.stage["key"] == TAKINGS and shop.stage.get("quick"):
            # The amount as the register says it: 20.00 EUR
            shop.stage["quick_currency"] = currency()
            shop.stage["quick_amount"] = f"{number_format(shop.stage['quick'] / 100, 2)} {shop.stage['quick_currency']}"
    return shops
