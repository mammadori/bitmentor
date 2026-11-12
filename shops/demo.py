"""A training site in one command: a small, made-up community in every state a merchant or the association meets.

An installation starts empty: no shops, no certificates, an empty dashboard. Nearly everything the site does for the
people who run it (the dashboard `/admin/today/`, the staff registers, the moved shops, the shops without a wallet, the
guardian of the Lightning address, the certificate and its steps) can only be understood with data in front of you. `manage.py demo_data` fills a test installation with eleven
shops and their owners, in different states, so that a volunteer can learn on them and a merchant can see the site
before signing up. `manage.py demo_data --remove` takes away all of it and only it. Guide: docs/features/demo.md.

What it never does:
- it refuses to run on mainnet or in production mode, in any mode but development unless BITMENTOR_DEMO=1 says so, and
  always on a site that has a shop, a merchant's account it did not make (`refuse`): the
  installation must start empty, because the network and the mode alone do not tell a training site from a real one;
- it asks nothing of a Lightning provider, of an explorer or of Telegram, and sends no email. What the periodic
  commands would write (the guardian's probes) is written here directly, in the same fields, as of the moment it runs.
  The Lightning address of a demo shop ends in `.invalid` and its owners' emails too (RFC 2606): no register can take a
  real payment. Because of that the periodic commands skip demo shops (`models.real`), or they would overwrite the data
  with their failures; the dashboard leaves their dates out of the «not checked for a while» checks, the morning
  summary and the export of the shops leave them out too;
- nobody signs in with a password: the owners are plain accounts with an unusable password, entered with the same
  one-time email link as any merchant. `--links` prints those links (for a development site, where the email goes to
  the console and not to a mailbox).

Everything it makes is marked: a shop by an `external_id` that starts with PREFIX, an owner by the user name
`demo:<name>` (the names of the demo owners, and no others). Running it again puts all of it back as it was, with the dates moved to today. `--remove` touches nothing
else."""
import hashlib
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.db import transaction
from django.db.models.signals import pre_delete
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import salted_hmac

from config.community import community

from . import certified, guard
from .models import DEMO_PREFIX, Profile, Shop, StaffRegister, Trainee, claim_digest

PREFIX = DEMO_PREFIX
USERNAME_PREFIX = "demo:"
# RFC 2606: nobody owns it, no address there ever receives anything
MAIL_DOMAIN = "demo.invalid"

LOOKS = {"town": "Sample Town", "street": "Demo Street"}


class Refused(Exception):
    """The installation is not one where demo data may be made."""


def foreign():
    """What is on this site that the demo did not make, as {what: how many}: shops, merchants' accounts (staff accounts
    are fine: somebody has to look at the dashboard). An installation with any of it is a real
    site, or a real site's copy, and not one to train on."""
    from django.contrib.auth import get_user_model

    demo_owners = get_user_model().objects.filter(username__in=usernames())
    found = {"shops": Shop.objects.exclude(external_id__startswith=PREFIX).count(),
             "accounts": get_user_model().objects.filter(is_staff=False, is_superuser=False)
             .exclude(pk__in=demo_owners.values("pk")).count()}
    return {name: count for name, count in found.items() if count}


def refuse():
    """Raises Refused where demo data must never be made: on mainnet, in production mode, in any mode but development
    unless BITMENTOR_DEMO=1 says so on purpose, and always beside anything real (`foreign`): the installation must start
    empty. The variable never lifts the first two or the last: a site with a real shop is refused whatever is set.
    `SITE_MODE=production` already implies mainnet in the settings of a site that follows them, but a site on the public
    signet that serves real customers does not, and it is not protected by the network alone."""
    if settings.NETWORK == "mainnet":
        raise Refused("NETWORK is mainnet: demo shops never go next to real money.")
    if settings.SITE_MODE == "production":
        raise Refused("SITE_MODE is production: demo shops never go on the site the customers see.")
    others = foreign()
    if others:
        what = ", ".join(f"{count} {name}" for name, count in others.items())
        raise Refused(f"This site is not empty: {what} that are not demo ones. Demo shops go only on an installation "
                      "made for training, which starts empty (staff accounts are fine).")
    if settings.SITE_MODE != "development" and not settings.DEMO_ALLOWED:
        raise Refused(f"SITE_MODE is {settings.SITE_MODE}: demo shops go on a development site, or here only with "
                      "BITMENTOR_DEMO=1 in the environment, on a site that is empty.")


def claim_code(slug):
    """The one-time code of a demo shop waiting for its owner. Not a secret kept anywhere: it comes from the site's
    SECRET_KEY, so that `--links` can print it again, and nobody else can make it."""
    return salted_hmac("bitmentor.demo.claim", slug, algorithm="sha256").hexdigest()[:32]


def point(slug_index):
    """Where on the map: around the community's centre, on a small grid. Without a centre, around 0, 0."""
    centre = community().get("map") or {}
    row, column = divmod(slug_index, 4)
    return (float(centre.get("latitude", 0)) + (row - 1) * 0.0035,
            float(centre.get("longitude", 0)) + (column - 1.5) * 0.0045)


class Plan:
    """The moments of one run, as days and hours before `now`, so that every run puts the dates back to today."""

    def __init__(self, now):
        self.now = now

    def ago(self, days=0, hours=0, minutes=0):
        return self.now - timedelta(days=days, hours=hours, minutes=minutes)

    def month_start(self, months_back):
        """The first day of the month `months_back` months ago, as the certificate keeps it (a date)."""
        day = certified.month_of(self.now)
        for _month in range(months_back):
            day = (day - timedelta(days=1)).replace(day=1)
        return day


# One entry per shop. `owner` is the name of the account (several shops may share one); `story` is what the shop is
# for in the training, in a line, for the summary the command prints and for the guide.
SHOPS = [
    {"slug": "loaf", "name": "The Daily Loaf", "story": "certified, with a staff register",
     "description": "Bread baked every morning, and a counter where bitcoin has been welcome for months."},
    {"slug": "door", "name": "Blue Door Cafe", "owner": "door",
     "story": "the Lightning address works, the certificate waits for the owner's basic pass; owner of two shops",
     "description": "Coffee, cake and a blue door. Pay with bitcoin at the till."},
    {"slug": "books", "name": "Paper & Ink Books", "owner": "door",
     "story": "first day half done: the register is ready, the sign printed, the first sale not ticked yet",
     "description": "New and second-hand books, a corner for children."},
    {"slug": "petals", "name": "Petal Flowers", "story": "just signed up, no wallet yet: the page is ready, the shop is not on the map",
     "description": "Flowers for every day and for the days that count."},
    {"slug": "hardware", "name": "Hardware Hub", "story": "certified long ago",
     "description": "Screws, paint and good advice."},
    {"slug": "gelato", "name": "Moonlight Gelato", "story": "the Lightning address stopped answering three days ago",
     "description": "Ice cream made on the spot, open until late."},
    {"slug": "tailor", "name": "Needle & Thread Tailor", "story": "a staff register in use, and one turned off",
     "description": "Repairs and alterations while you wait."},
    {"slug": "herbs", "name": "Chamomile Herbalist", "story": "everything in order, without the certificate",
     "description": "Teas, herbs and natural remedies."},
    {"slug": "kitchen", "name": "Grandma's Kitchen", "owner": None, "story": "moved here from another site, waiting for its owner to take it",
     "description": "Home cooking, three tables and a lot of patience."},
    {"slug": "pencil", "name": "Pencil Stationery", "story": "moved here, the owner took it but has not connected a wallet",
     "description": "Notebooks, pens and everything for school."},
    {"slug": "records", "name": "The Old Record Shop", "story": "historic: closed, certified when it traded",
     "description": "Vinyl and stories. Closed now; the page stays as a memory."},
]


def owner_name(spec):
    return spec.get("owner", spec["slug"])


def usernames():
    """The user names of the demo owners, all of them known: an account a visitor opens is never called like this, and a
    volunteer who types an email of their own in an owner's panel does not stop being one."""
    return {f"{USERNAME_PREFIX}{owner_name(spec)}" for spec in SHOPS if owner_name(spec) is not None}


def email_of(name):
    return f"{name}@{MAIL_DOMAIN}"


def ensure_user(name):
    """The owner's account: a plain user with an unusable password, and a recovery email nobody can read. Signs in with
    the email link (`--links`), as any merchant does."""
    User = get_user_model()
    user, created = User.objects.get_or_create(username=f"{USERNAME_PREFIX}{name}")
    if created or user.has_usable_password() or user.is_staff or user.is_superuser or not user.is_active:
        # Back to what it was made as. A demo account is never staff: staff accounts cannot sign in from the site
        User.objects.filter(pk=user.pk).update(password="!" + hashlib.sha256(name.encode()).hexdigest(), is_staff=False,
                                               is_superuser=False, is_active=True)
        user.refresh_from_db()
    # update(), not save(): the recovery email is what the guardian watches over
    profile, _made = Profile.objects.get_or_create(user=user, defaults={"email": email_of(name)})
    Profile.objects.filter(pk=profile.pk).update(email=email_of(name), account_changed=None)
    return user


def lightning_ok(plan, spec_hours=2):
    return {"lightning_status": "ok", "lightning_checked": plan.ago(hours=spec_hours),
            "lightning_worked": plan.ago(hours=spec_hours), "lightning_since": plan.ago(days=60),
            "lightning_failures": 0, "lightning_failing_since": None}


def states(plan):
    """What each shop is: the fields the site, the owner and the periodic commands write, by slug. The numbers are the
    ones of the guide (docs/features/demo.md)."""
    both = lightning = {"lightning": True}
    return {
        "loaf": {
            **both, "created": plan.ago(days=110), "first_sale": plan.ago(days=108), "sign_printed": plan.ago(days=109),
            "register_shared": plan.ago(days=100), "discount": 5, **lightning_ok(plan),
            "certified_month": plan.month_start(2)},
        "door": {
            **lightning, "created": plan.ago(days=40), "first_sale": plan.ago(days=38), "sign_printed": plan.ago(days=39),
            "register_shared": None, **lightning_ok(plan, 3)},
        "books": {
            **lightning, "created": plan.ago(days=2), "sign_printed": plan.ago(days=1), "register_shared": None,
            "lightning_status": "ok", "lightning_checked": plan.ago(minutes=30), "lightning_worked": plan.ago(minutes=30),
            "lightning_since": plan.ago(days=2), "lightning_failures": 0, "lightning_failing_since": None},
        "petals": {"created": plan.ago(days=1), "awaiting_wallet": True},
        "hardware": {
            **both, "created": plan.ago(days=200), "first_sale": plan.ago(days=198), "sign_printed": plan.ago(days=199),
            "register_shared": plan.ago(days=190), **lightning_ok(plan, 4),
            "certified_month": plan.month_start(6)},
        "gelato": {
            **lightning, "created": plan.ago(days=90), "first_sale": plan.ago(days=88), "sign_printed": plan.ago(days=89),
            "register_shared": plan.ago(days=80), "lightning_status": "no_answer", "lightning_since": plan.ago(days=3),
            "lightning_checked": plan.ago(hours=1), "lightning_worked": plan.ago(days=3, hours=2),
            "lightning_failures": 3, "lightning_failing_since": plan.ago(days=3)},
        "tailor": {
            **lightning, "created": plan.ago(days=60), "first_sale": plan.ago(days=57), "sign_printed": plan.ago(days=58),
            "register_shared": plan.ago(days=50), **lightning_ok(plan)},
        "herbs": {
            **both, "created": plan.ago(days=75), "first_sale": plan.ago(days=70), "sign_printed": plan.ago(days=72),
            "register_shared": plan.ago(days=60), **lightning_ok(plan)},
        "kitchen": {"created": plan.ago(days=3), "moved_in": plan.ago(days=3), "moved_register": True,
                    "moved_from": "demo-old-site-kitchen", "claim_hash": claim_digest(claim_code("kitchen"))},
        "pencil": {"created": plan.ago(days=6), "moved_in": plan.ago(days=6), "moved_register": True,
                   "moved_from": "demo-old-site-pencil"},
        "records": {
            **lightning, "created": plan.ago(days=400), "first_sale": plan.ago(days=395), "sign_printed": plan.ago(days=399),
            "register_shared": plan.ago(days=390), "closed_on": plan.now.date() - timedelta(days=70),
            "certified_month": plan.month_start(10)},
    }


def fields_of(spec, index, plan, state):
    """All the columns of the shop, from the spec and its state: what it is, where it is, how it gets paid."""
    slug = spec["slug"]
    latitude, longitude = point(index)
    taking_lightning = state.get("lightning", False)
    base = {
        "name": spec["name"], "description": spec["description"], "address": f"{index + 1} {LOOKS['street']}",
        "city": LOOKS["town"], "latitude": latitude, "longitude": longitude, "website": "", "email": "", "phone": "",
        "legal_name": "", "vat_number": "", "show_website": True, "show_address": True, "show_phone": False,
        "show_email": False, "show_legal_name": False, "show_vat_number": False,
        "discount": 0, "listed": True, "lightning": False, "lightning_address": "",
        "nostr_pubkey": "", "nip05_name": "", "telegram_chat": "",
        "suspended": False, "closed_on": None, "first_sale": None, "sign_printed": None, "register_shared": None,
        "awaiting_wallet": False, "moved_in": None, "moved_from": "",
        "moved_register": False, "claim_hash": "", "lightning_status": "", "lightning_since": None,
        "lightning_checked": None, "lightning_worked": None, "lightning_failures": 0, "lightning_failing_since": None,
        "lightning_checked_address": "", "certified_month": None, "certified_revoked": False,
    }
    base.update(state)
    if taking_lightning:
        base["lightning_address"] = f"{slug}@{slug}.{MAIL_DOMAIN}"
        base["lightning_checked_address"] = base["lightning_address"] if base["lightning_status"] else ""
    return base


def make_shop(spec, index, plan, state, owner):
    external = PREFIX + spec["slug"]
    values = fields_of(spec, index, plan, state)
    shop = Shop.objects.filter(external_id=external).first()
    if shop is None:
        shop = Shop.objects.create(external_id=external, name=values["name"])
    # update(): no signal sees a demo shop change hands or an owner's chat change. Back to the start, on every run
    Shop.objects.filter(pk=shop.pk).update(owner=owner, **values)
    shop.refresh_from_db()
    return shop


def make_staff(shop, name, plan, retired=None, opened=True):
    register = StaffRegister.objects.filter(shop=shop, name=name).first() or StaffRegister.objects.create(shop=shop, name=name)
    # The one-time code of a register is made by the owner when it is handed over: here it starts without one
    StaffRegister.objects.filter(pk=register.pk).update(retired=retired, code="", code_until=None,
                                                         opened=plan.ago(days=25) if opened else None,
                                                         created=plan.ago(days=30))
    register.refresh_from_db()
    return register


def populate(now=None):
    """Makes the demo community, or puts it back as it was: the same shops, owners and registers, the dates moved to
    `now`. Returns {"shops": {slug: Shop}, "owners": [names]}."""
    refuse()
    plan = Plan(now or timezone.now())
    with transaction.atomic():
        users = {}
        for spec in SHOPS:
            name = owner_name(spec)
            if name is not None and name not in users:
                users[name] = ensure_user(name)
        table, shops = states(plan), {}
        for index, spec in enumerate(SHOPS):
            name = owner_name(spec)
            shops[spec["slug"]] = make_shop(spec, index, plan, table[spec["slug"]], users[name] if name else None)
        staff(shops, plan)
        passes(shops, plan)
    return {"shops": shops, "owners": sorted(users)}


def staff(shops, plan):
    """The staff registers that tell each shop's story."""
    loaf, tailor = shops["loaf"], shops["tailor"]
    kept = [make_staff(loaf, "Anna", plan).pk, make_staff(loaf, "Evening shift", plan, retired=plan.ago(days=20)).pk,
            make_staff(tailor, "Sam", plan).pk, make_staff(tailor, "Weekend help", plan, retired=plan.ago(days=6)).pk]
    # The registers the training added are gone; the ones above keep their keys
    StaffRegister.objects.filter(shop__external_id__startswith=PREFIX).exclude(pk__in=kept).delete()


def passes(shops, plan):
    """The basic pass of the owners of the certified shops: a certificate needs it (shops/certified.py)."""
    for slug in ("loaf", "hardware", "records"):
        owner = shops[slug].owner
        if owner is not None:
            Trainee.objects.update_or_create(user=owner, defaults={"base_earned": plan.ago(days=100),
                                                                   "reviewed": plan.ago(days=100)})


def links(now=None):
    """The sign-in links of the owners and the claim link of the shop waiting for its owner, for a development site
    where the email goes to a console. Each sign-in link is the one the recovery page would email (15 minutes, once)."""
    from .views import login_state, site_link

    refuse()
    found = {"owners": [], "claim": []}
    names = []
    for spec in SHOPS:
        name = owner_name(spec)
        if name is not None and name not in names:
            names.append(name)
    for name in names:
        email = email_of(name)
        if not Profile.objects.filter(email=email, user__username=f"{USERNAME_PREFIX}{name}").exists():
            continue
        token = signing.dumps({"email": email, "state": login_state(email), "next": reverse("shops")}, salt="recovery")
        shops = [spec["name"] for spec in SHOPS if owner_name(spec) == name]
        found["owners"].append({"name": name, "shops": shops, "email": email, "url": site_link("recovery_login", token)})
    for spec in SHOPS:
        if owner_name(spec) is not None:
            continue
        shop = Shop.objects.filter(external_id=PREFIX + spec["slug"], owner=None).first()
        if shop is not None and shop.claim_hash:
            from django.utils.http import urlencode

            found["claim"].append({"shop": shop.name, "url": f"{settings.PUBLIC_URL}{reverse('claim_shop')}?"
                                                              f"{urlencode({'code': claim_code(spec['slug'])})}"})
    return found


def remove():
    """Takes away the demo community and only it. Returns what was removed, by name."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    count = {}
    with transaction.atomic():
        shops = Shop.objects.filter(external_id__startswith=PREFIX)
        count["shops"] = shops.count()
        for shop in list(shops):
            shop.delete()
        # An owner goes only if it is one of the demo owners, and never while they own a shop that is not a demo one
        owners = User.objects.filter(username__in=usernames())
        keep = Shop.objects.exclude(external_id__startswith=PREFIX).filter(owner__in=owners).values("owner")
        owners = owners.exclude(pk__in=keep)
        count["owners"] = owners.count()
        # The guardian of the accounts would write down «account deleted» for each of them, and email and write to
        # the moderation chat: these accounts are not anybody's
        pre_delete.disconnect(guard.user_deleted, sender=User)
        try:
            owners.delete()
        finally:
            pre_delete.connect(guard.user_deleted, sender=User)
    return count
