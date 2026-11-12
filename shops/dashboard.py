"""The association's dashboard: what to do today about the shops' registers, in one page of /admin/ (and in text with
`manage.py dashboard`, for a daily message).

One item per thing to do: the shop, what happened in plain words, since when, what to do now (the shop in /admin/, the
command to run, the words for the merchant). Problems first, then what to look at, then what to know; inside each
level the oldest first. On top, two numbers: the registers whose Lightning address works, and the shops to look at.

Only reads of the database, and few of them: two queries for the shops whatever their number, one for the moved shops.
Nothing here asks a Lightning provider: the periodic command already writes what is needed on the shop
(`check_lightning`). When it has not run for a while, the dashboard says so, since its items would be old. The site
sees no sale, so nothing here is about sales or money.

The rules are the ones of the modules that own them, never copied here: `lightning_watch.panel` for the Lightning
address, `first_day.steps` for the first day."""
import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from django.conf import settings
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from django.utils.text import capfirst
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from config import hooks, limits, mailbudget

from . import certified, first_day, lightning_watch, osm, vacation
from .models import HAS_REGISTER, register_kept, HelpRequest, LightningStatus, OsmRequest, Shop, is_demo
from .notifications import plain  # noqa: F401 (the command's and the tests' name for it)

PROBLEM = "problem"
WARNING = "warning"
INFO = "info"
LEVELS = (PROBLEM, WARNING, INFO)
# The guardian probes every address every CHECK_EVERY: twice that without a probe, it is not running
GUARDIAN_LATE = 2 * lightning_watch.CHECK_EVERY
COMMAND = "docker compose exec web python manage.py {command}"
logger = logging.getLogger(__name__)


@dataclass
class Item:
    level: str
    code: str
    what: str
    do: str
    shop: Shop | None = None
    since: object = None
    say: str = ""
    command: str = ""
    links: list = field(default_factory=list)
    # `since` is the last time something worked (a run, a visit), not when the trouble started
    last: bool = False
    # What the merchant wrote in a request for help (plain text, one line): shown, never put in a message
    wrote: str = ""

    @property
    def when(self):
        return lightning_watch.when(self.since) if self.since else ""

    @property
    def days(self):
        """Whole days since `since` in the community's calendar, None without a date."""
        if not self.since:
            return None
        return max((certified.local_day(timezone.now()) - certified.local_day(self.since)).days, 0)


def trading(now):
    """Shops with a register that still works, as lightning_watch.watched: a historic shop keeps it until
    REGISTER_AFTER_CLOSING after its closing day."""
    return Shop.objects.filter(HAS_REGISTER).filter(register_kept(timezone.localdate(now)))


def shops(now):
    """The trading shops, with their closures."""
    # `shared`: the address is also on a shop of another owner, historic ones too (certified.shared_with_another_owner),
    # read in the same query
    return list(trading(now).defer("description").annotate(shared=certified.shared_with_another_owner()).prefetch_related("closures").order_by("name", "pk"))


def shop_link(shop):
    return {"url": reverse("admin:shops_shop_change", args=[shop.pk]), "text": _("The shop in the administration")}


def diagnose(shop):
    return COMMAND.format(command=f"diagnose_shop {shop.pk}")


def lightning_item(shop):
    watch = lightning_watch.panel(shop)
    if not watch or not watch["broken"]:
        return None
    if watch["status"] == lightning_watch.LightningStatus.NO_CONFIRMATION:
        what = _("The Lightning address receives payments but does not tell the register: sales stay waiting.")
    elif watch["never"]:
        what = _("The Lightning address has never answered: customers cannot pay with Lightning.")
    else:
        what = _("The Lightning address stopped answering: customers cannot pay with Lightning.")
    return Item(PROBLEM, f"lightning_{watch['status']}", what, shop=shop, since=shop.lightning_since,
                do=_("Call the merchant: the panel already shows them what to do. If the wallet changed, only they "
                     "can change the address, from their panel. Never change it for them."),
                say=" ".join(filter(None, [watch["customer"], _("Open your wallet and check that your Lightning "
                                                              "address is still the one in your shop page.")])),
                command=diagnose(shop), links=[shop_link(shop)])


def first_day_items(shop):
    """The first day, with the rules of first_day: what the merchant left halfway that the association can help with."""
    if not first_day.applies(shop):
        return []
    found = []
    steps = {step["key"]: step for step in first_day.steps(shop)}
    if steps["map"]["state"] == first_day.TODO:
        found.append(Item(INFO, "no_position", _("The shop has no position, so it is not on the map."), shop=shop,
                          do=_("Ask the merchant to add the address in their shop page, or ask an administrator to "
                               "add the position."),
                          links=[shop_link(shop)]))
    return found


def closed_lightning_items(shop):
    """A shop kept quiet by a closure whose Lightning address does not work: one line to know, never an alarm. Also with
    the closures turned off (`features.vacation`): a closure written before keeps the alarms quiet until its day, and the
    fault must not stay unknown until then; the association is told the day it ends (vacation.welcome_back)."""
    watch = lightning_watch.panel(shop)
    if not (watch and watch["broken"]):
        return []
    # Nobody is told while it is closed: the line is here for whoever looks, and the day the shop opens again the
    # merchant (if closures are on) and the association are told
    if vacation.enabled():
        do = _("Nothing to do now: the merchant and the association hear about it the day the shop opens again.")
    else:
        do = _("Nothing to do now: the association hears about it the day the closure ends.")
    return [Item(INFO, "closed_lightning",
                 _("Closed for a while, and the Lightning address does not work: nobody is told until the shop opens "
                   "again."), shop=shop, since=shop.lightning_since, do=do, links=[shop_link(shop)])]


def closed_items(shop, today):
    """A shop closed for a while for more than three months: ask whether it is coming back (the icons stop forgiving the
    days after that, and no alarm reaches the association for it)."""
    found = closed_lightning_items(shop)
    first = vacation.long_closure(shop, today)
    if first is not None:
        found.append(Item(INFO, "closed_long", _("Closed for a while for over three months."), shop=shop,
                          since=timezone.make_aware(datetime.combine(first, time.min), timezone.get_default_timezone()),
                          do=_("Ask the merchant whether the shop is coming back: an administrator can make a shop "
                               "closed for good historic."),
                          say=_("Is your shop coming back? If it is closed for good, tell us and we mark it as history on "
                                "the map."),
                          links=[shop_link(shop)]))
    return found


def moved_items(now):
    """Shops moved here from another site that still wait for their owner, or for the owner's wallet. A moved shop
    that has a register again is among the trading ones, with their items, and not here a second time."""
    found = []
    waiting = (Shop.objects.filter(moved_in__isnull=False).filter(Q(owner__isnull=True) | Q(lightning_address=""))
               .filter(register_kept(timezone.localdate(now)))
               .exclude(pk__in=trading(now).values("pk"))
               .only("pk", "name", "owner", "lightning_address", "claim_hash", "moved_in", "moved_register")
               .order_by("moved_in", "pk"))
    for shop in waiting:
        if shop.owner_id is None and shop.claim_hash:
            found.append(Item(INFO, "moved_unclaimed", _("Moved here: the owner has not taken the shop yet."),
                              shop=shop, since=shop.moved_in,
                              do=_("Send the owner again the one-time link from the list of owners the import wrote. "
                                   "If that link is lost, the command below makes a new one for this shop only, from "
                                   "the same file of the import, in a new list of owners: the old link stops working, "
                                   "the other shops keep theirs."),
                              say=_("Your shop has moved to this site: open the link we sent you to take it back."),
                              command=COMMAND.format(command=f"import_moved_shops <file> --only {shop.pk} --new-codes "
                                                             "--apply --owners <new-list>.csv"),
                              links=[shop_link(shop)]))
        elif shop.owner_id is None:
            found.append(Item(INFO, "moved_no_owner", _("Moved here without an owner to tell."), shop=shop,
                              since=shop.moved_in,
                              do=_("Find the merchant: once they have signed in here, ask an administrator to assign "
                                   "the shop to their account in this administration."),
                              links=[shop_link(shop)]))
        else:
            found.append(Item(INFO, "moved_no_wallet",
                              _("Moved here: the owner took the shop but has not connected a wallet, so there is no "
                                "register.") if shop.moved_register else
                              _("Moved here: the owner took the shop, which has no register yet."),
                              shop=shop, since=shop.moved_in,
                              do=_("Help the merchant connect a wallet from their panel: only they can."),
                              say=_("To take payments again, open your panel and connect your wallet."),
                              links=[shop_link(shop)]))
    return found


def certificate_items(found_shops):
    """The association's two jobs about certified shops: a certified shop waiting for a person to open the private part
    to its owner (shops/certified.py, `waiting_approval`), and a Lightning address found on shops of different owners,
    written before the site refused it (`shared_with_another_owner`): none of those shops is certified while it lasts.
    Only the trading shops are listed, each with its own line; the other shop may be historic. A share between two
    shops that both no longer trade is not listed: neither takes payments."""
    found = []
    # No query of their own: the trading shops the dashboard already read (a historic shop is only in /admin/'s filter)
    if certified.enabled():
        for shop in found_shops:
            # The same rule as certified.waiting_approval, on shops already in memory
            if shop.certified_month is None or shop.private_approved is not None or shop.suspended or not shop.owner_id:
                continue
            found.append(Item(WARNING, "private_waiting", _("Certified: the private part waits for your approval."),
                              shop=shop, do=_("Look at the shop and its owner; if all is well, open the private part with "
                                              "the action «Open the private part to the owner»."),
                              links=[shop_link(shop)]))
    for shop in found_shops:
        if shop.owner_id and shop.shared:
            found.append(Item(PROBLEM, "lightning_shared", _("Its Lightning address is also on a shop of another owner."),
                              shop=shop, do=_("Find out whose wallet it is: the other owner's shop must change its address. "
                                              "Until then the site certifies neither shop."),
                              links=[shop_link(shop)]))
    return found


def freshness(found_shops, now):
    """The periodic commands that have not run for a while: their items here would be old. The shops of the training site
    (manage.py demo_data) are left out: their dates are made by hand, and would hide that nothing reads the real ones."""
    items = []
    found_shops = [shop for shop in found_shops if not is_demo(shop)]
    watched = [shop for shop in found_shops if lightning_watch.is_watched(shop)]
    if watched:
        checked = [shop.lightning_checked for shop in watched if shop.lightning_checked]
        last = max(checked, default=None)
        if last is None or now - last > GUARDIAN_LATE:
            items.append(Item(WARNING, "guardian_late", _("The Lightning addresses have not been checked for a while: "
                                                          "what this page says about them may be old.")
                              if last else _("The Lightning addresses have never been checked."), since=last, last=True,
                              do=_("Check the timer that runs check_lightning."),
                              command=COMMAND.format(command="check_lightning")))
    return items


def help_items(now):
    """The merchants' requests for help (shops/helpdesk.py) not yet marked solved in /admin/, the oldest first: they are
    people waiting, so they go on top of everything else. Only what the request holds: the diagnosis is in the request."""
    asking = {
        HelpRequest.Channel.EMAIL: _("They asked to be called back by email."),
        HelpRequest.Channel.TELEGRAM: _("They asked to be called back on Telegram."),
        HelpRequest.Channel.PHONE: _("They asked to be called back by phone: the number in their shop's details."),
        HelpRequest.Channel.PAGE: _("They asked for no call: they wait on their panel."),
    }
    found = []
    for row in HelpRequest.objects.filter(resolved__isnull=True).select_related("shop").order_by("created", "pk"):
        found.append(Item(PROBLEM, "help_request", _("Asked for help: %(symptom)s.") % {"symptom": row.get_symptom_display()},
                          shop=row.shop, since=row.created,
                          do=" ".join([asking[row.channel], _("Start from the diagnosis in the request, and run the "
                                                              "command below to probe the Lightning address again. When it is "
                                                              "done, mark the request solved: the merchant sees "
                                                              "«Solved» in their panel.")]),
                          say=_("We have your request and we are looking at it now."), wrote=plain(row.note),
                          command=diagnose(row.shop),
                          links=[{"url": reverse("admin:shops_helprequest_change", args=[row.pk]),
                                  "text": _("The request and its diagnosis"), "perm": "shops.view_helprequest"},
                                 shop_link(row.shop)]))
    return found


def osm_items(now):
    """OpenStreetMap (shops/osm.py): the merchants who asked the association to do their entry (people waiting, on top with
    the requests for help), the changes proposed that a person has not sent yet, and a reading that failed or is late.
    Nothing here is sent by the site: the proposals wait for `manage.py osm_upload`, run by hand. That command is an
    experimental feature (config/features.py): while it is off nothing here names it, and the changes are for a person to
    make by hand on openstreetmap.org."""
    from config import features

    found = []
    upload = features.enabled(features.OSM_UPLOAD)
    for row in OsmRequest.objects.filter(state=OsmRequest.State.OPEN).select_related("shop").order_by("created", "pk"):
        adding = row.kind == OsmRequest.Kind.ADD
        found.append(Item(WARNING, "osm_request", _("Asked the association to put the shop on OpenStreetMap.") if adding else
                          _("Asked the association to update the shop's entry on OpenStreetMap."),
                          shop=row.shop, since=row.created,
                          do=(_("Add the shop in the editor with the lines of the request, or tell the merchant it cannot be "
                                "done. Then mark the request done in the administration: the merchant sees it in the panel. "
                                "The site never writes to OpenStreetMap by itself.") if adding else
                              _("Run the command below: it shows what would change, asks before it sends anything, and marks "
                                "the request done. Or edit by hand with the lines of the request, then mark it done in the "
                                "administration.") if upload else
                              _("Update the shop's entry in the editor with the lines of the request, or tell the merchant it "
                                "cannot be done. Then mark the request done in the administration: the merchant sees it in "
                                "the panel. The site never writes to OpenStreetMap by itself.")),
                          say=_("We have your request about OpenStreetMap: a person of the association does it by hand."),
                          command=COMMAND.format(command="osm_upload") if upload and not adding else "",
                          links=[{"url": reverse("admin:shops_osmrequest_change", args=[row.pk]),
                                  "text": _("The request"), "perm": "shops.view_osmrequest"}, shop_link(row.shop)]))
    if osm.settings() is None:
        return found
    run = osm.last_run(ok=False)
    good = run if run is not None and run.ok else osm.last_run()
    if run is not None and not run.ok:
        found.append(Item(INFO, "osm_failed", _("The last reading of OpenStreetMap failed: %(reason)s.") % {"reason": plain(run.error)},
                          since=run.created, do=_("Nothing was changed. The monthly timer tries again; to try now run the command below."),
                          command=COMMAND.format(command="osm_sync")))
    if good is None or now - good.created > timedelta(days=osm.LATE_DAYS):
        found.append(Item(INFO, "osm_late", _("The shops have not been compared with OpenStreetMap for a while.")
                          if good else _("The shops have never been compared with OpenStreetMap."),
                          since=good.created if good else None, last=good is not None,
                          do=_("Check the timer that runs osm_sync."), command=COMMAND.format(command="osm_sync --send")))
    proposed = Shop.objects.exclude(osm_proposed="").count()
    if proposed:
        found.append(Item(INFO, "osm_links", ngettext("%(count)d shop has a link to OpenStreetMap proposed, waiting for you to confirm it.",
                                                      "%(count)d shops have a link to OpenStreetMap proposed, waiting for you to confirm it.",
                                                      proposed) % {"count": proposed},
                          do=_("Open the shops with a link proposed, look at the element in each shop's page, then use the action "
                               "«Confirm the OpenStreetMap link proposed» (or «Refuse»). Nothing is read from an element, and nothing "
                               "is written for it, until you confirm it."),
                          links=[{"url": f"{reverse('admin:shops_shop_changelist')}?osm=proposed", "text": _("The shops"),
                                  "perm": "shops.view_shop"}]))
    offered = sum(1 for notes in Shop.objects.exclude(osm_ref="").values_list("osm_notes", flat=True)
                  if isinstance(notes, dict) and any(isinstance(entry, dict) and entry.get("state") == osm.OFFERED
                                                     for entry in (notes.get("fields") or {}).values()))
    if offered:
        found.append(Item(INFO, "osm_contacts", ngettext("%(count)d shop has a website or a phone offered by OpenStreetMap, waiting for you to accept it.",
                                                         "%(count)d shops have a website or a phone offered by OpenStreetMap, waiting for you to accept it.",
                                                         offered) % {"count": offered},
                          do=_("They are never written by the reading: a stranger can write a website on OpenStreetMap. Look at the "
                               "offer in the shop's page, then use the action «Accept the website and phone offered by OpenStreetMap»."),
                          links=[{"url": reverse("admin:shops_shop_changelist"), "text": _("The shops"), "perm": "shops.view_shop"}]))
    count = sum(len(item["changes"]) for item in osm.waiting(good))
    if count:
        found.append(Item(INFO, "osm_proposals", (ngettext("%(count)d change proposed for OpenStreetMap waits for a person to send it.",
                                                           "%(count)d changes proposed for OpenStreetMap wait for a person to send them.",
                                                           count) if upload else
                                                  ngettext("%(count)d tag of our shops on OpenStreetMap is not what this site knows.",
                                                           "%(count)d tags of our shops on OpenStreetMap are not what this site knows.",
                                                           count)) % {"count": count},
                          since=good.created,
                          do=_("Read them in the administration. To send them, run the command below with the OpenStreetMap "
                               "account that signs the edits: it asks before it sends anything. The site never sends them by itself.")
                          if upload else
                          _("Read them in the administration: they are what OpenStreetMap should say of each shop. A person "
                            "can change them by hand on openstreetmap.org, with their own account. The site sends nothing."),
                          command=COMMAND.format(command="osm_upload") if upload else "",
                          links=[{"url": reverse("admin:shops_osmrun_change", args=[good.pk]), "text": _("The reading and its proposals"),
                                  "perm": "shops.view_osmrun"}]))
    return found


def limit_items(rows):
    """The totals of config/limits.py near their limit (80%) or at it: at the limit the site refuses what would add one more
    (never deleting anything), so it is a problem; before, something to look at. The staff raises the limit in
    `community.yml` (`limits`) or clears what nobody needs."""
    found = []
    for row in rows:
        if not row["level"]:
            continue
        full = row["level"] == "full"
        if row["key"].startswith("emails_"):
            what = (_("%(what)s: %(used)d of %(limit)d. The limit is reached: only the security notices still go out, "
                      "up to the total.") if full else _("%(what)s: %(used)d of %(limit)d, close to the limit."))
            found.append(Item(PROBLEM if full else WARNING, f"limit_{row['key']}",
                              what % {"what": capfirst(row["what"]), "used": row["used"], "limit": row["limit"]},
                              do=_("Look in the web log for who asks for so many links (limits.emails_per_month, "
                                   "limits.emails_per_day): many addresses at once is an attack, and the quota of the "
                                   "mail provider is what it uses up. Raise the limit only if the provider allows more.")))
            continue
        what = (_("%(what)s: %(used)d of %(limit)d. The limit is reached: the site refuses new ones, and keeps all it has.")
                if full else _("%(what)s: %(used)d of %(limit)d, close to the limit."))
        found.append(Item(PROBLEM if full else WARNING, f"limit_{row['key']}",
                          what % {"what": capfirst(row["what"]), "used": row["used"], "limit": row["limit"]},
                          do=_("Clear what nobody needs any more in the administration, or raise the limit: "
                               "`limits.%(key)s` in community.yml, then restart the site. Spam and a site that grows "
                               "are the two reasons for a full limit: look before raising it.") % {"key": row["key"]}))
    many = mailbudget.crowded()
    if many:
        cap = limits.get("emails_per_address_day")
        found.append(Item(WARNING, "emails_one_address",
                          ngettext("%(count)d address received more than %(cap)d emails today.",
                                   "%(count)d addresses received more than %(cap)d emails today.", many)
                          % {"count": many, "cap": cap},
                          do=_("A shop with many notices may be in trouble (its Lightning address, a change of "
                               "address): look at its page in the administration. Many sign-in links to one address "
                               "are someone using the site against it.")))
    return found


# A link of a community's line: an address of this site (one slash, then no slash or backslash) or https, of a sane length
COMMUNITY_LINK = re.compile(r"(?:/(?:[^/\\\s][^\s]*)?|https://[^\s]+)\Z")
COMMUNITY_LINK_LENGTH = 500


def community_items():
    """The lines the community's own code adds to the list (config/hooks.py, `dashboard_items`): a text for what is
    happening and one for what to do, and a level, "info" unless it says otherwise. Texts only: a line with none of the
    two is left out, a level that is not one of ours is "info", and links must be addresses of the site or https."""
    found = []
    for row in hooks.rows("dashboard_items"):
        # One line of plain text each, as the software's own: they also go to a terminal, an email and Telegram
        what, do = (plain(" ".join(str(text).split()))[:300] if type(text) is str else "" for text in (row.get("what"), row.get("do")))
        if not (what and do):
            continue
        given = row.get("links")
        links = [{"text": plain(" ".join(link["text"].split()))[:80], "url": link["url"]}
                 for link in (given if isinstance(given, (list, tuple)) else ())
                 if isinstance(link, dict) and type(link.get("text")) is str and type(link.get("url")) is str
                 and len(link["url"]) <= COMMUNITY_LINK_LENGTH and COMMUNITY_LINK.fullmatch(link["url"])]
        code = row.get("code")
        # Each line needs a name of its own: the morning message tells a line once, by its code. Without one, the
        # text itself names it, so that a new line is told while another one stands
        name = code if type(code) is str and re.fullmatch(r"[a-z0-9_]{1,40}", code) else hashlib.sha256(what.encode()).hexdigest()[:12]
        found.append(Item(row.get("level") if row.get("level") in LEVELS else INFO, f"community_{name}", what, do, links=links))
    return found


def rank(item):
    since = item.since.timestamp() if item.since else float("-inf")
    return LEVELS.index(item.level), since, item.shop.name.lower() if item.shop else ""


def summary(now=None):
    """{"items": [Item] most urgent first, "counts": {"working", "to_look"}, "shops": how many trading registers,
    "closed": how many of them are closed for a while today}. "working" counts the trading registers not suspended
    whose Lightning address the guardian last found working."""
    now = now or timezone.now()
    found_shops = shops(now)
    items = freshness(found_shops, now)
    counts = {"working": 0}
    closed_now = 0
    today = vacation.day_of(now)
    for shop in found_shops:
        # Closed for a while (holidays, works…): no alarm for these days for the Lightning address, which the guardian
        # keeps watching and the shop's reopening tells
        closed = vacation.is_quiet(shop, today)
        lightning = None if closed else lightning_item(shop)
        items += [lightning] if lightning else []
        items += first_day_items(shop)
        if closed:
            # With the feature turned off (features.vacation) a closure written before keeps the alarms quiet until its
            # day (vacation.is_quiet) but is not talked about: no count of closed shops, no "closed for over three
            # months". Only the line about a broken Lightning address stays: the one fault nobody would hear about
            if vacation.enabled():
                closed_now += 1
                items += closed_items(shop, today)
            else:
                items += closed_lightning_items(shop)
            continue
        if not shop.suspended and shop.lightning_status == LightningStatus.OK:
            counts["working"] += 1
    items += moved_items(now)
    items += certificate_items(found_shops)
    limit_rows = limits.rows()
    items += limit_items(limit_rows)
    # The merchants who asked for OpenStreetMap wait like those who asked for help: on top. The rest goes with the others
    osm_found = osm_items(now)
    items += [item for item in osm_found if item.code != "osm_request"]
    try:
        items += community_items()
    except Exception:  # noqa: BLE001 (the community's lines never take the software's own away from the list)
        logger.exception("the community's lines for the list of the day could not be read: left out")
    items.sort(key=rank)
    items = help_items(now) + [item for item in osm_found if item.code == "osm_request"] + items
    counts["to_look"] = len({item.shop.pk for item in items if item.shop and item.level != INFO})
    # The shops closed for a while are in none of the counts: nothing to look at
    return {"items": items, "counts": counts, "shops": len(found_shops), "closed": closed_now, "now": now,
            "limits": limit_rows,
            "telegram": bool(settings.TELEGRAM_BOT_TOKEN)}
