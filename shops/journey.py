"""The merchant's path in the panel: a few milestones, from a working register to a certified shop, each one with what
to do now and how the shop looks on the map at that moment.

It only reads what the site already knows, from the shop's own fields: the guardian of the Lightning address
(`lightning_watch`), the first day (`first_day`) and the certificate (`certified_month`, shops/certified.py). The site
sees no sale, so no milestone is about payments. Nothing here calls another server, and nothing compares a shop with
another one: no scores.

States: DONE, TODO, OPTIONAL (not done, and fine as it is: it never holds the path back), WAITING (the site has not
looked yet), OFF (not up to the merchant)."""
from django.template import TemplateDoesNotExist
from django.template.loader import get_template
from django.utils import timezone

from . import certified, first_day, lightning_watch, training
from .models import LightningStatus

DONE = "done"
TODO = "todo"
OPTIONAL = "optional"
WAITING = "waiting"
OFF = "off"
# The badge and the icons of the map, as the map itself draws them
MARKS_TEMPLATE = "shops/shop_marks.html"


def applies(shop):
    """A shop with a register made on this site, still trading. An imported shop without a register, or a historic one,
    has no path to walk."""
    return shop.has_register and not shop.closed_on


def marks_template():
    """The map's template for the badge and the icons, or None while the site has none."""
    try:
        get_template(MARKS_TEMPLATE)
    except TemplateDoesNotExist:
        return None
    return MARKS_TEMPLATE


def lightning_step(shop, watch):
    if shop.suspended:
        return {"key": "lightning", "state": OFF}
    if watch and watch["broken"]:
        return {"key": "lightning", "state": TODO, "why": "broken"}
    if watch:
        return {"key": "lightning", "state": DONE, "checked": watch["checked"]}
    return {"key": "lightning", "state": WAITING}


def first_day_step(shop, day):
    return {"key": "first_day", "state": DONE if day["complete"] else TODO}


def certificate_step(shop):
    if shop.certified_month:
        return {"key": "certificate", "state": DONE, "since": shop.certified_month}
    if shop.certified_revoked:
        return {"key": "certificate", "state": OFF}
    return {"key": "certificate", "state": TODO, "rule_text": certified.RULE,
            "needs_lightning": not (shop.lightning_status == LightningStatus.OK
                                    and shop.lightning_checked_address == shop.lightning_address),
            "needs_pass": not training.has_pass(shop)}


def card(shop, now):
    """How the shop shows on the map now: why it is not there, or the name, the place, the discount, the badge and the
    icons. The same rules as the map: a suspended or unlisted shop, or one without a position, is not on it."""
    if shop.suspended:
        return {"on_map": False, "why": "suspended"}
    if not shop.listed:
        return {"on_map": False, "why": "unlisted"}
    if shop.latitude is None or shop.longitude is None:
        return {"on_map": False, "why": "position"}
    found = {"on_map": True, "certified": certified.enabled() and bool(shop.certified_month), "marks": certified.marks(shop, now)}
    found["template"] = marks_template() if found["certified"] or found["marks"] or getattr(shop, "trained", False) else None
    return found


def summary(shop, day=None, watch=None, now=None):
    """The milestones in order, how many count and are done, the next one to walk, and the map card. `day` and `watch`
    are what the panel already computed for the shop (first_day.summary, lightning_watch.panel)."""
    now = now or timezone.now()
    day = day or first_day.summary(shop)
    watch = watch if watch is not None else lightning_watch.panel(shop)
    steps = [lightning_step(shop, watch), first_day_step(shop, day)]
    # The certificate is a milestone only while the community has the badge (features.certified)
    if certified.enabled():
        steps.append(certificate_step(shop))
    # An optional milestone counts only once done: choosing one method only is a fine way to run a shop
    counted = [step for step in steps if step["state"] not in (OFF, OPTIONAL)]
    done = sum(step["state"] == DONE for step in counted)
    # The next milestone is the first one the merchant can walk now: one the site is about to check needs nobody
    following = next((step for step in counted if step["state"] == TODO), None)
    for number, step in enumerate(steps, 1):
        step["number"] = number
        step["next"] = step is following
    return {"steps": steps, "done": done, "total": len(counted), "complete": done == len(counted),
            "next": following["key"] if following else None,
            "card": card(shop, now)}
