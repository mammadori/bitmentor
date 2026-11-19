"""Events: what the software tells the community's own code when something happens.

Hooks (config/hooks.py) are questions the software asks. These are Django signals the software sends, so that the
community's code is told, without wrapping views or watching responses. A community connects its receivers in its
own app's `ready()`:

    from django.dispatch import receiver
    from shops import events

    @receiver(events.shop_created)
    def welcome(sender, shop, user, source, **kwargs):
        ...

The arguments are keyword-only and their names are stable; a receiver takes `**kwargs` too, so that an argument added
later breaks nothing. The sender is the `Shop` class, or None for `theme_file_served`. No event carries an email, an
address of the connection or a request: a receiver reads from the model what it needs.

- `shop_created(shop, user, source)`: a shop came to exist. `source` is "site" (an owner's form, /shops/new/),
  "admin" (written in /admin/), "import" (`manage.py import_shops`, one event for each new shop of the list), "move"
  (`manage.py import_moved_shops`, a shop that was not here: a moved shop that takes the place of an unclaimed shop of
  the list is not new) or "demo" (`manage.py demo_data`). `user` is the account that did it (site, admin), else None.
- `shop_claimed(shop, user, via)`: a shop that waited without an owner got one. `via` is "claim_link" (the one-time
  link, `claim_shop`: made for shops moved from another site, and for the demo's) or "move" (`import_moved_shops` puts
  a moved shop in the place of an unclaimed shop of the list and gives it to the account made for its owner's email).
  `shop` is the row as the database has it after the change. A moved shop that is new here comes with `shop_created`
  (source "move", maybe with an owner) and no `shop_claimed`; an owner given to an unclaimed shop in /admin/ sends
  no event.
- `shop_approved(shop, by)`: a waiting shop was approved by `approval.approve_or_hold` (/admin/today/, the action of
  /admin/, the volunteers' area). `by` is what the shop's `approved_by` says: a username, or `[team key N] name`.
  Never for a shop held for a check, nor for one somebody else approved in the meantime. Not sent for the shops
  approved as they are brought (`approval.stamp`: imports, moves, the demo, a shop written in /admin/), nor for a new
  shop of a community that turned the approval off: those come with `shop_created`.
- `lightning_address_broken(shop)`, `lightning_address_working(shop)`: the guardian (shops/lightning_watch.py) decided
  that a shop's address stopped working (after the failed probes it waits for), or works again after that. Once for
  each change of the status, not at every probe; also while the shop is closed for a holiday, when its people are not
  told. "Working" is sent only when the guardian sees the same address work again: a receiver that keeps a list of
  broken shops also ends a shop's state on `lightning_address_changed` (the owner replaced the address: the status
  starts again) and when the shop is no longer watched (suspended, historic, without an address), which sends nothing.
- `lightning_address_changed(shop, before, after)`: a shop's Lightning address changed, wherever from (the owner's
  panel, a command, a shell: everything shops/guard.py sees, `save()`, `update()` and `bulk_update()`), the first
  connection of a wallet too (`before` is ""). Not for a new shop. `before` and `after` are the two addresses, "" for
  none. Do not write them whole in logs or messages: the software's own notices use the short form
  (`guard.short_address`).
- `theme_file_served(path, first_byte, client_kind)`: a file of the community's theme is being sent, one of those
  `announced_files` lists in community.yml (shell patterns, relative to the theme). `path` is relative to the theme;
  `first_byte` is True when the answer starts at the beginning of the file (no Range, or a Range from byte 0), so
  that a download read in pieces usually counts once. Only roughly: Safari asks `bytes=0-1` before the file, and a
  player that seeks back to the start asks from 0 again; a receiver that needs exact counts cannot rely on it.
  `client_kind` is "browser" or "program" (`client_kind`). Without `announced_files` it is never sent; a value it
  cannot use is a warning of `manage.py check` (shops.W014).

Every event but the last is sent after the transaction commits (`transaction.on_commit`; when there is no transaction,
at once): a change rolled back says nothing. `theme_file_served` has no transaction, and is sent before the answer
leaves. A receiver that raises is written in the log, with its name and the exception's class, and stops nothing: not
the page, not the command, not the other receivers. With no receivers an event costs nothing."""
import logging

from django.db import transaction
from django.dispatch import Signal

logger = logging.getLogger(__name__)
# `announced_files`: how many patterns, and how long each, at most
MAX_ANNOUNCED = 50
PATTERN_LENGTH = 200

shop_created = Signal()
shop_claimed = Signal()
shop_approved = Signal()
lightning_address_broken = Signal()
lightning_address_working = Signal()
lightning_address_changed = Signal()
theme_file_served = Signal()

NAMES = {shop_created: "shop_created", shop_claimed: "shop_claimed", shop_approved: "shop_approved",
         lightning_address_broken: "lightning_address_broken", lightning_address_working: "lightning_address_working",
         lightning_address_changed: "lightning_address_changed", theme_file_served: "theme_file_served"}


def dispatch(signal, sender, kwargs):
    """The receivers, each on its own: what one raises is logged and goes no further. Django writes the traceback too
    (logger `django.dispatch`); this line names the event."""
    for receiver, outcome in signal.send_robust(sender, **kwargs):
        if isinstance(outcome, Exception):
            name = getattr(receiver, "__qualname__", None) or repr(receiver)
            logger.error("Event %s: the receiver %s.%s raised %s", NAMES.get(signal, "?"),
                         getattr(receiver, "__module__", "?"), name, type(outcome).__name__)


def send(signal, sender=None, *, now=False, **kwargs):
    """Sends `signal` with these arguments once the transaction commits (at once outside one, or with `now`). Never
    raises because of a receiver. Nothing at all happens without receivers."""
    if not signal.has_listeners(sender):
        return
    if now:
        dispatch(signal, sender, kwargs)
    else:
        transaction.on_commit(lambda: dispatch(signal, sender, kwargs), robust=True)


def announced_patterns():
    """(patterns, problems) of `announced_files` in community.yml: shell patterns of files of the theme, up to
    MAX_ANNOUNCED of at most PATTERN_LENGTH characters each. What is not a pattern, or beyond the limits, is left out,
    and `manage.py check` says so (shops.W014). No key is ([], [])."""
    from config.community import community

    value = community().get("announced_files")
    if value is None:
        return [], []
    if not isinstance(value, list):
        return [], [(f"`announced_files` is a list of up to {MAX_ANNOUNCED} shell patterns of theme files, not "
                     f"{type(value).__name__}: no file is announced")]
    found, problems = [], []
    for place, pattern in enumerate(value, 1):
        if not isinstance(pattern, str) or not pattern.strip():
            problems.append(f"`announced_files`: entry {place} is not a pattern, left out")
        elif len(pattern) > PATTERN_LENGTH:
            problems.append(f"`announced_files`: entry {place} is longer than {PATTERN_LENGTH} characters, left out")
        else:
            found.append(pattern.strip())
    if len(found) > MAX_ANNOUNCED:
        problems.append(f"`announced_files` has {len(found)} patterns: only the first {MAX_ANNOUNCED} are used")
        found = found[:MAX_ANNOUNCED]
    return found, problems


def client_kind(request):
    """"browser" or "program", for `theme_file_served`. Every browser of today sends `Sec-Fetch-Mode` with each request,
    and programs (curl, wget, a podcast app, a robot) as a rule do not; a User-Agent that names a robot or a tool
    (website/blossom_visits.py, BOTS) is a program even with the header. Nothing of the request is kept or passed on."""
    from website.blossom_visits import BOTS

    if request.headers.get("Sec-Fetch-Mode") and not BOTS.search(request.headers.get("User-Agent", "")):
        return "browser"
    return "program"
