"""The first day of a shop with a register: a few steps in the panel, each with its state. What the site knows (the shop
on the map) it says; the rest the merchant ticks: the first sale, the sign printed, the register handed over. The site
never sees a sale: the sales stay on the merchant's phone."""
DONE = "done"
TODO = "todo"
# Not up to the merchant, and it does not hold the first day back: a shop taken off the map by the community
OFF = "off"
# Steps the merchant ticks, and the field that keeps each one
TICKED = {"sale": "first_sale", "sign": "sign_printed", "staff": "register_shared"}


def applies(shop):
    """A shop with a register, still trading: a historic shop is long past its first day."""
    return shop.has_register and not shop.closed_on


def steps(shop):
    """The steps with their state, in order; `why` tells apart the texts of the same state."""
    found = [{"key": "sale", "state": DONE if shop.first_sale else TODO}]
    if shop.suspended or not shop.listed:
        found.append({"key": "map", "state": OFF, "why": "suspended" if shop.suspended else "unlisted"})
    elif shop.latitude is None or shop.longitude is None:
        found.append({"key": "map", "state": TODO})
    else:
        found.append({"key": "map", "state": DONE})
    for key, field in TICKED.items():
        if key == "sale":
            continue
        found.append({"key": key, "state": DONE if getattr(shop, field) else TODO})
    return found


def summary(shop):
    """The steps, how many are done out of those that count, and whether the first day is complete."""
    found = steps(shop)
    counted = [step for step in found if step["state"] != OFF]
    done = sum(step["state"] == DONE for step in counted)
    return {"steps": found, "done": done, "total": len(counted), "complete": done == len(counted)}
