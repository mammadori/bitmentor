"""When a merchant calls: everything that matters about a shop's register, in plain words, with the technical detail
for whoever helps.

`diagnose(shop)` returns a list of `Finding`s, problems first: each has a stable `code`, a `level` (ok, warning,
problem), `say`, a sentence for the merchant in the active language (what happened and what to do now), and
`detail`, for the helper (ids, states, HTTP codes, times), in English. The findings that come from what the site keeps
about the shop (closures, staff registers, a moved shop, a changed account, the certificate, the customer's path) also have `do`: what the helper does, in the active language. It only returns data:
the command `diagnose_shop` prints it, and a page can show it.

Read-only towards the database, always: no `save`, no `update`, no `create`. One thing is not a pure read: the
shop's Lightning address is probed as the register would use it, through `lnaddress.refusal` and its protections
(HTTPS only, public hosts only, no redirects, time and size limits): the merchant's provider issues a real invoice,
which nobody pays.

The other checks only read the database, through the functions of the modules that own the rules (`vacation`,
`staff`, `guard`, `journey`, `certified`, `customer`, `moving`): no rule is copied here, and they ask nothing of any
provider. The site keeps no sales, so no check is about them.

Slow probes run in a thread waited for a bounded time, a check that fails does not stop the others, and the whole run
has a time limit.

No secrets in the output: no API keys, and the recovery email masked.
"""
import logging
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass

from django.conf import settings
from django.db.models import Q, prefetch_related_objects
from django.utils import formats, timezone, translation
from django.utils.translation import gettext as _

from config import features
from config.community import community, customer_links

from . import certified, customer, directory, first_day, guard, journey, lnaddress, moving, staff, training, vacation
from .models import AccountChange, LightningStatus, Profile, Shop
from .notifications import plain
from .register import options, register_data
from .views import profile_for

logger = logging.getLogger(__name__)

OK = "ok"
WARNING = "warning"
PROBLEM = "problem"
LEVELS = (PROBLEM, WARNING, OK)

# Seconds: the Lightning address probe (up to three requests of the provider) and the whole run
LIGHTNING_SECONDS = 15
TOTAL_SECONDS = 40
# Below this, a probe is not started: it would only time out
MIN_CALL = 0.5


@dataclass(frozen=True)
class Finding:
    code: str
    level: str
    say: str
    detail: str = ""
    do: str = ""

    def as_dict(self):
        return asdict(self)


class TimeUp(Exception):
    """The run is out of time: the checks left are reported as skipped."""


class Busy(Exception):
    """`bounded` found no free place for its thread: nothing was asked, which is not the same as a slow answer. The
    check that met it is reported as skipped («run it again in a minute»), never as a problem of the shop."""


def worst(findings):
    """The most serious level among the findings, OK for none."""
    return next((level for level in LEVELS if any(finding.level == level for finding in findings)), OK)


def masked(email):
    """m***@e***.org: enough to recognise it on the phone, not to read it."""
    local, at, domain = email.partition("@")
    if not at:
        return "***"
    name, dot, top = domain.rpartition(".")
    return f"{local[:1]}***@{name[:1]}***{dot}{top}" if dot else f"{local[:1]}***@***"


def when(moment):
    """The short date of the language and a 24-hour time: "a.m." would end a sentence with two dots."""
    local = timezone.localtime(moment)
    return f"{formats.date_format(local, 'SHORT_DATE_FORMAT')} {local:%H:%M}"


# Threads of bounded() running at once, per process. One left behind lives until what it calls gives up (up to three
# lnaddress.fetch of 10 s each): from a page, repeated clicks would pile them up and take the site's few threads, as
# reserve.CHECKS says for the backup register. Without a free place the check is skipped (Busy, then TimeUp: «run it again in a minute»), not called slow.
THREADS = threading.BoundedSemaphore(4)


def bounded(seconds, function, *args):
    """function(*args) in a thread of its own, waited for at most `seconds`: (True, its value), or (False, None) if it
    is still running. Busy if THREADS has no free place within `seconds`: the call was never made. The thread is left to
    end on its own: what it calls has its own time limits, and it gives its place back when it ends."""
    started = time.monotonic()
    if not THREADS.acquire(timeout=max(seconds, 0)):
        raise Busy
    box = {}

    def work():
        try:
            box["value"] = function(*args)
        except Exception as error:  # noqa: BLE001 - raised again in the caller's thread
            box["error"] = error
        finally:
            THREADS.release()

    seconds -= time.monotonic() - started
    thread = threading.Thread(target=work, daemon=True, name="diagnosis")
    thread.start()
    thread.join(max(seconds, 0))
    if thread.is_alive():
        return False, None
    if "error" in box:
        raise box["error"]
    return True, box.get("value")


def english_refusal(address):
    """lnaddress.refusal in English, whatever the thread: its sentence tells the two refusals apart."""
    with translation.override("en"):
        return refusal_reason(lnaddress.refusal(address))


def refusal_reason(text):
    if text is None:
        return None
    return "no_confirmation" if "does not tell the register" in text else "no_answer"


class Run:
    """One diagnosis: the shop, the time left, and the findings."""

    def __init__(self, shop, seconds):
        self.shop = shop
        self.deadline = time.monotonic() + seconds
        self.findings = []
        # The page of the shop's owner (shops/helpdesk.py) tells only what concerns the signed-in owner: not the rows of
        # the register of account changes that only share the shop's name, which is not unique
        self.owner_view = False

    def left(self):
        return self.deadline - time.monotonic()

    def add(self, code, level, say, detail="", do=""):
        self.findings.append(Finding(code, level, str(say), detail, str(do)))


# The checks, in the order they run. Each adds its findings to the run; an error in one does not stop the others.

def check_shop(run):
    shop = run.shop
    if shop.suspended:
        run.add("shop_suspended", WARNING,
                _("The association has taken your shop off the map and the lists for now. The register still works."),
                "suspended=True")
    if shop.closed_on:
        run.add("shop_historic", WARNING,
                _("Your shop is marked as historic ({note}): it stays on the map but does not count among the places "
                  "to pay. The register and the sign still work.").format(note=shop.historic_note),
                f"closed_on={shop.closed_on.isoformat()}")
    if not shop.listed:
        run.add("shop_unlisted", WARNING,
                _("Your shop is not shown on the map or in the lists, as you chose. The register works the same."),
                "listed=False")
    elif not shop.suspended and (shop.latitude is None or shop.longitude is None):
        run.add("shop_no_position", WARNING,
                _("Your shop has no position, so it is not on the map. Add it in your shop page."),
                "latitude or longitude empty")
    elif not shop.suspended and not shop.closed_on and not directory.waits_for_wallet(shop):
        run.add("shop_visible", OK, _("Your shop is on the map."), f"id={shop.id}")


def check_owner(run):
    shop = run.shop
    if not shop.owner_id:
        # A shop moved here waits for its owner in a way check_waiting explains better
        if not moving.waiting_for(shop):
            run.add("shop_no_owner", WARNING,
                    _("Nobody has claimed this shop yet: it is on the map, but it has no register on this site."),
                    f"no owner; external_id={shop.external_id or '-'}")
        return
    user = shop.owner
    email = Profile.objects.filter(user=user).values_list("email", flat=True).first() or ""
    if user.username.startswith("email:"):
        how = "email link"
    elif re.fullmatch(r"[0-9a-f]{66}", user.username):
        how = f"wallet key {user.username[:8]}…"
    else:
        how = "other"
    seen = f"last login {user.last_login:%Y-%m-%d %H:%M} UTC" if user.last_login else "never logged in"
    detail = f"signs in with: {how}; {seen}"
    if user.is_staff or user.is_superuser:
        run.add("owner_is_staff", WARNING,
                _("This shop belongs to a staff account, which signs in only from the administration and does not open the pages of a shop: the shop needs an ordinary account."),
                f"{detail}; staff account")
    if email:
        run.add("owner_recovery_email", OK,
                _("You can sign in with a link sent to your email, even without the wallet."),
                f"{detail}; recovery email {masked(email)}")
    else:
        run.add("owner_no_recovery_email", WARNING,
                _("There is no email to get back in: if you lose or change the wallet you sign in with, you cannot "
                  "open your shop any more. Add an email in your profile."),
                f"{detail}; no recovery email")


def probe(run, address):
    """The address asked now as the register would ask it: (seconds allowed, verdict, seconds taken), the verdict
    "ok", "no_answer", "no_confirmation" or "slow"."""
    seconds = min(LIGHTNING_SECONDS, run.left())
    if seconds < MIN_CALL:
        raise TimeUp
    started = time.monotonic()
    try:
        finished, reason = bounded(seconds, english_refusal, address)
    except Busy:
        raise TimeUp from None
    verdict = "slow" if not finished else reason or "ok"
    return seconds, verdict, time.monotonic() - started


def check_lightning(run):
    """The address the register really uses, asked now as the register would ask it."""
    address = run.shop.lightning_address
    if not address or "@" not in address:
        return
    seconds, verdict, took = probe(run, address)
    finished, reason = verdict != "slow", None if verdict == "ok" else verdict
    detail = f"{address}; {took:.1f} s"
    if not finished:
        run.add("lightning_slow", PROBLEM,
                _("Your Lightning wallet takes too long to answer: the customer would wait too long. Try again in a "
                  "few minutes; if it stays so, check the wallet app."),
                f"{detail}: no verdict within {seconds:.0f} s")
    elif reason == "no_confirmation":
        run.add("lightning_no_confirmation", PROBLEM,
                _("Your Lightning wallet receives, but it does not tell the register when a payment arrives: check "
                  "each payment in your wallet app before you confirm it on the register."),
                f"{detail}: no LUD-21 verify URL, or it does not answer with settled")
    elif reason == "no_answer":
        run.add("lightning_no_answer", PROBLEM,
                _("Your Lightning wallet is not answering: the register cannot show the Lightning payment code. Open "
                  "the wallet app and check that it works."),
                f"{detail}: LNURL-pay metadata or invoice not obtained")
    else:
        run.add("lightning_ok", OK,
                _("Your Lightning address answers and tells the register when a payment arrives."),
                f"{detail}: LNURL-pay, invoice and LUD-21 verify all answered")


def check_register(run):
    """Whether the register page can take payments: a well-written address and enough price sources."""
    shop = run.shop
    if not shop.has_register:
        return
    data = register_data(shop)
    if not data:
        why = ("fewer than two price sources in `register` in community.yml" if len(options()["sources"]) < 2
               else "the Lightning address in the site database cannot be used by the register")
        run.add("register_unavailable", PROBLEM, _("The register cannot take payments: write to the association."), why)
        return
    run.add("register_ready", OK, _("The register is ready: it asks your Lightning wallet for each payment."),
            f"register to {data['address']}, price sources {', '.join(data['sources'])}")


def first_day_names():
    return {"sale": _("a first paid sale"), "map": _("the position on the map"),
            "sign": _("the sign printed"), "staff": _("the register shown to the staff")}


def check_first_day(run):
    shop = run.shop
    if not first_day.applies(shop):
        return
    steps = first_day.summary(shop)["steps"]
    counted = [step for step in steps if step["state"] != first_day.OFF]
    done = sum(step["state"] == first_day.DONE for step in counted)
    todo = [step["key"] for step in counted if step["state"] != first_day.DONE]
    detail = ", ".join(f"{step['key']}={step['state']}" for step in steps)
    if not todo:
        run.add("first_day_done", OK, _("The first day is complete."), detail)
        return
    names = first_day_names()
    run.add("first_day_todo", WARNING,
            _("The first day: {done} of {total} done. Still to do: {steps}.").format(
                done=done, total=len(counted), steps=", ".join(names[key] for key in todo)), detail)


# What the site keeps about the shop: only the database, through the functions of the modules that own each rule. Each finding says what to tell the merchant (`say`) and what the helper does (`do`).

def connect_help():
    if not features.enabled(features.PRACTICE):
        return _("Help the merchant connect a wallet from their own panel: only they can. Never type an address or a "
                 "key for them.")
    return _("Help the merchant connect a wallet from their own panel: only they can. Never type an address or a key "
             "for them. «Practise without money» works meanwhile.")


def check_waiting(run):
    """A shop that has no register because it waits: for its owner (moved here, the link not opened, nobody to give it
    to) or for the owner's wallet (moved here, or saved before having one)."""
    shop = run.shop
    state = moving.waiting_for(shop)
    moved = f"moved here {shop.moved_in:%Y-%m-%d}; had a register there: {'yes' if shop.moved_register else 'no'}" \
        if shop.moved_in else ""
    if state == "unclaimed":
        run.add("moved_unclaimed", WARNING,
                _("Your shop has moved to this site: open the link we sent you to take it back."),
                f"{moved}; no owner; the one-time link exists and was not used; "
                f"command: python manage.py import_moved_shops <file> --only {shop.pk} --new-codes --apply "
                "--owners <new-list>.csv",
                _("Send the owner again the one-time link from the list of owners the import wrote. If that link is "
                  "lost, the command below makes a new one for this shop only, from the same file of the import, in a "
                  "new list of owners: the old link stops working, the other shops keep theirs."))
    elif state == "no_owner":
        run.add("moved_no_owner", WARNING,
                _("Your shop is here, but nobody has taken it yet: tell us who you are and we help you take it."),
                f"{moved}; no owner and no one-time link",
                _("Find the merchant: once they have signed in here, ask an administrator to assign the shop to their "
                  "account in this administration."))
    elif state == "no_wallet":
        run.add("moved_no_wallet", WARNING,
                _("To take payments again, open your panel and connect your wallet."),
                f"{moved}; the owner took the shop, which has no register"
                + ("; off the map until the wallet is connected" if directory.waits_for_wallet(shop) else ""),
                connect_help())
    elif directory.waits_for_wallet(shop):
        run.add("shop_waiting_wallet", WARNING,
                _("You saved your shop before having a wallet, so it has no register yet and it is not on the map. "
                  "Open your panel and connect your wallet: it takes a few minutes."),
                "awaiting_wallet: the shop's page is saved, there is no register", connect_help())


def hidden_from_map(shop, now):
    """Not on the map: suspended, unlisted, without a position (as the merchant's path says it, `journey.card`), or
    waiting for its wallet."""
    return not journey.card(shop, now)["on_map"] or directory.waits_for_wallet(shop)


def closure_line(closure):
    return f"{closure.first_day} to {closure.reopens_on or 'until the owner says'} ({closure.reason})"


def check_closure(run):
    """A shop closed for a while (holidays, works…): what the map says, and whether the register's alarms are quiet.
    The closures are read once for all the questions of `vacation`, and not kept on the shop, which the caller may use
    again."""
    shop = run.shop
    prefetch_related_objects([shop], "closures")
    try:
        closure_lines(run, shop)
    finally:
        getattr(shop, "_prefetched_objects_cache", {}).pop("closures", None)


def closure_lines(run, shop):
    closures = vacation.closures(shop)
    if not closures:
        return
    today = vacation.day_of()
    detail = "; ".join(closure_line(closure) for closure in closures[-5:])
    if shop.closed_on:
        run.add("closed_ignored", WARNING,
                _("Your closure dates are saved, but your shop is marked as historic: the map says nothing about "
                  "them."), f"historic since {shop.closed_on}; {detail}",
                _("A closure only counts for a shop that still trades: a historic shop is closed for good."))
        return
    current = vacation.current(shop, today)
    if current:
        note = vacation.note(shop, today)
        run.add("closed_now", OK,
                _("On the map and on your shop page it says: «{note}». The register keeps working, and no alarm "
                  "goes off for these days.").format(note=note), closure_line(current),
                _("Nothing to fix. To change the dates the merchant uses «Change the dates» in their panel; the "
                  "association edits the closure under the shop in the administration."))
        if hidden_from_map(shop, timezone.now()):
            run.add("closed_hidden", WARNING,
                    _("You are marked as closed, but your shop is not on the map, so nobody reads the notice. The "
                      "register works the same."), "the shop is suspended, unlisted, without a position, or waiting",
                    _("Look at the shop_ lines: the notice shows only for a shop that is on the map."))
        first = vacation.long_closure(shop, today)
        if first is not None:
            run.add("closed_long", WARNING,
                    _("Is your shop coming back? If it is closed for good, tell us and we mark it as history on the "
                      "map."), f"closed since {first} ({(today - first).days} days); the icons forgive at most "
                               f"{vacation.MAX_CREDIT} closed days in {vacation.BUDGET_DAYS}",
                    _("Ask the merchant whether the shop is coming back: an administrator can make a shop closed for "
                      "good historic."))
    else:
        upcoming = vacation.upcoming(shop, today)
        if upcoming:
            if vacation.info(shop, today):
                run.add("closed_soon", OK,
                        _("The map and your shop page already say: «{note}».").format(
                            note=vacation.note(shop, today)), closure_line(upcoming))
            else:
                run.add("closed_planned", OK,
                        _("A closure is saved from {date}: the map starts to say it {days} days before.").format(
                            date=vacation.long_date(upcoming.first_day), days=vacation.SOON_DAYS),
                        closure_line(upcoming))


def names(registers):
    """The names the owner gave, as a terminal shows them: a name is someone else's text."""
    return ", ".join(plain(register.name) for register in registers)


def check_staff(run):
    """The registers the owner gave to the people who work with them: which ones are on, off or never opened."""
    shop = run.shop
    registers = staff.summary(shop)
    if not registers:
        return
    active = [register for register in registers if not register.retired]
    off = [register for register in registers if register.retired]
    never = [register for register in active if register.opened is None]
    waiting = [register for register in never if register.code_alive]
    unopened = [register for register in never if not register.code_alive]
    working = [register for register in active if register.opened is not None]
    if active and not shop.has_register:
        run.add("staff_no_register", PROBLEM,
                _("The registers for your staff cannot work: your shop has no register of its own yet."),
                f"{len(active)} staff register(s) turned on, the shop has no Lightning address: every link answers 404",
                _("Connect the wallet first (see the lines about the register): the staff registers open as soon as "
                  "the shop has one."))
    if off:
        run.add("staff_off", WARNING,
                _("These registers are turned off, so they take no new payment: {names}. If the person still needs "
                  "one, add a new one in «Registers for your staff».").format(names=names(off)),
                "; ".join(f"{plain(register.name)} turned off {register.retired:%Y-%m-%d %H:%M} UTC"
                          for register in off[:10]),
                _("Nothing is broken: the owner turned them off. A turned-off register cannot be turned on again; "
                  "the owner makes a new one."))
    if unopened:
        run.add("staff_unopened", WARNING,
                _("These registers were never opened on a phone, and their code is no longer valid: {names}. The "
                  "code on the card works once and lasts 24 hours: make a new one in «Registers for your staff».")
                .format(names=names(unopened)),
                "; ".join(f"{plain(register.name)} made {register.created:%Y-%m-%d %H:%M} UTC, code used or expired"
                          for register in unopened[:10]),
                _("The owner opens «Registers for your staff», chooses the register and makes a new code, then the "
                  "person scans it with their phone. The code is never read out on the phone."))
    if waiting:
        run.add("staff_code_waiting", WARNING,
                _("These registers wait for the person to scan their code: {names}. The code works once and lasts 24 "
                  "hours.").format(names=names(waiting)),
                "; ".join(f"{plain(register.name)} made {register.created:%Y-%m-%d %H:%M} UTC, code valid until "
                          f"{register.code_until:%Y-%m-%d %H:%M} UTC" for register in waiting[:10]),
                _("Nothing is broken: the person scans the code with their phone before it expires. If it has "
                  "expired, the owner makes a new one. The code is never read out on the phone."))
    # A register of a shop with no register of its own opens nothing: it is not «working»
    if working and shop.has_register:
        run.add("staff_ok", OK, _("These registers are working: {names}.").format(names=names(working)),
                "; ".join(f"{plain(register.name)} opened {register.opened:%Y-%m-%d}" for register in working[:10]))


# What in a change of the account is more than a change of access: who can sign in, who owns the shop, who is told
RISKY_CHANGES = {"username", "shop_owner", "shop_chat", "is_staff", "is_superuser", "deleted",
                 # Where the money goes (shops/guard.py): the owner's own first connection is not in this list of rows
                 "shop_lightning"}


def check_account(run):
    """What whoever runs the site changed in the owner's account in the last days (shops/guard.py): the email the
    owner got, and the register of /admin/ it comes from. The rows of the account that signs in now are told to its
    owner as the email did; those of another account (the owner before, an account deleted) are told as a fact about
    the shop, and are never a problem for the owner who has it now."""
    shop = run.shop
    since = timezone.now() - guard.RECENT
    owner = shop.owner_id
    query = Q(shops__icontains=shop.name)
    if owner is not None:
        query |= Q(user_id=owner)
    named = shop.name.lower()

    def of_owner(row):
        return owner is not None and row.user_id == owner

    def about_this_shop(row):
        """The owner's own rows, and those that name this shop exactly: the register keeps the names of the shops, not
        their ids."""
        names_in_row = {part.strip().lower() for part in row.shops.split(",")} | {row.shops.lower()}
        return of_owner(row) or named in names_in_row

    # The most recent ones: the cut must never drop the latest, and the most serious is often that
    # Without the owner's own first connection of a wallet (Via.OWNER): it is not something whoever runs the site did
    rows = [row for row in AccountChange.objects.filter(query, created__gte=since).exclude(via=AccountChange.Via.OWNER)
            .order_by("-created")[:50]
            if about_this_shop(row)][:20]
    rows.reverse()
    own = [row for row in rows if of_owner(row)]
    others = [row for row in rows if not of_owner(row)]

    def lines(found):
        return " | ".join(f"{row.created:%Y-%m-%d %H:%M} UTC {row.changed} via {row.via}"
                          f"{' by ' + plain(row.actor) if row.actor else ''}; told: {row.told or '-'}"
                          f"{'; could not tell: ' + row.not_told if row.not_told else ''}" for row in found)

    def changes(found):
        return {code for row in found for code in row.changed.split(",") if code}

    if own:
        codes = changes(own)
        run.add("account_changed", PROBLEM if codes & RISKY_CHANGES else WARNING,
                " ".join([*guard.words(codes, sentence=True), guard.advice(community().get("email", ""))]),
                f"{len(own)} change(s) of the owner's account in the last {guard.RECENT.days} days: {lines(own)}",
                _("Ask the merchant whether they expected it. If not, change nothing and tell whoever runs the site: "
                  "«Account changes» in the administration says who changed what, from where, and who was told. "
                  "Never ask for a password."))
    if others and not run.owner_view:
        run.add("account_changed_other", WARNING,
                _("The administration changed something that concerns this shop on {date}: {what}. It was done to "
                  "another account, not to the one signed in now.").format(
                    date=when(others[-1].created), what=", ".join(guard.words(changes(others)))),
                f"{len(others)} change(s) naming this shop, of another or deleted account, in the last "
                f"{guard.RECENT.days} days: {lines(others)}",
                _("If the person on the phone is the one who had the shop before and got the email, ask whether they "
                  "know why. If not, change nothing and tell whoever runs the site: «Account changes» in the "
                  "administration says who did it. Never ask for a password."))


def check_certificate(run):
    """The certificate: given, revoked, or what it still waits for (shops/certified.py). Nothing when the community turned
    the badge off."""
    shop = run.shop
    if not certified.enabled() or not journey.applies(shop):
        return
    if shop.certified_month:
        run.add("certificate_done", OK,
                _("Your shop is certified, since {month}.").format(
                    month=formats.date_format(shop.certified_month, "YEAR_MONTH_FORMAT")),
                f"certified_month={shop.certified_month}")
        return
    if shop.certified_revoked:
        run.add("certificate_revoked", WARNING,
                _("The association has taken the certificate away from your shop: ask them why."),
                "certified_revoked=True",
                _("It was revoked in the administration and the site will not give it again until that is undone. "
                  "Ask the association before saying more."))
        return
    address = shop.lightning_status == LightningStatus.OK and shop.lightning_checked_address == shop.lightning_address
    trained = training.has_pass(shop)
    detail = f"lightning_status={shop.lightning_status or '-'}; basic pass {'yes' if trained else 'no'}"
    if address and trained:
        run.add("certificate_ready", OK,
                _("You have everything for the certificate: the site gives it at its next check of your Lightning address."),
                detail, str(certified.RULE))
    else:
        missing = [text for text, ok in ((_("a Lightning address that works"), address),
                                         (_("the owner's basic pass of the training"), trained)) if not ok]
        run.add("certificate_waiting", OK,
                _("The certificate still waits for: {missing}.").format(missing=", ".join(missing)),
                detail, str(certified.RULE))


def check_customer_path(run):
    """The customer at the till without a wallet: whether the guide leads somewhere, and the practice register."""
    shop = run.shop
    if not shop.has_register:
        return
    methods = customer.payable(shop)
    if methods is None:
        run.add("customer_path_off", WARNING,
                _("Customers who scan the card for people without a wallet see only the shop page, because nobody "
                  "can pay at this shop now."),
                f"suspended={shop.suspended}, closed_on={shop.closed_on or '-'}",
                _("It follows from the lines about the shop: the guide opens again when the shop can take payments."))
    else:
        wallets = customer.usable(customer_links(translation.get_language())["wallets"], methods)
        if not wallets:
            run.add("customer_wallets_empty", WARNING,
                    _("The guide for customers without a wallet has no wallet to suggest for the way you get paid."),
                    f"the shop takes {methods}; no wallet of shop_page.wallets sends it",
                    _("The list is in the community's settings (shop_page.wallets): manage.py check says what it left "
                      "out."))
        else:
            run.add("customer_path_ok", OK,
                    _("A customer without a wallet scans the card «Card for customers without a wallet» (in your "
                      "panel) and the phone guides them in three steps."),
                    f"the shop takes {methods}; {len(wallets)} wallet(s) offered")
    if features.enabled(features.PRACTICE) and first_day.applies(shop) and not shop.first_sale:
        run.add("practice_offer", OK,
                _("Before the first real sale you can try the register with a pretend customer: «Practise without "
                  "money» in your panel. Nothing is charged."), f"practice page for {shop.id}")


def check_training(run):
    """The merchant's training (shops/training.py): whether the register waits for the owner's basic pass, and where the
    path stands. Only where the community has the training; the owner is told in their own panel."""
    shop = run.shop
    cfg = training.parse()
    if not cfg or run.owner_view or not shop.owner_id or not shop.has_register:
        return
    trainee = training.trainee_of_id(shop.owner_id)
    if trainee and trainee.base_earned:
        run.add("training_done", OK, _("The owner has the basic pass of the training."),
                f"base_earned={trainee.base_earned:%Y-%m-%d}")
        return
    detail = f"gate={cfg.gate}; NETWORK={settings.NETWORK}; no basic pass; since={cfg.since or '-'}"
    if training.locked(shop):
        run.add("training_locked", WARNING, _("The register is waiting for the owner's basic pass."), detail,
                _("The register page says «The register opens soon» and no new sale starts, by the community's choice. Tell "
                  "the owner to open «Your merchant pass» in the panel: a few short modules. The practice register works "
                  "meanwhile. Nothing else is wrong: do not change anything."))
    elif training.real_money(shop):
        run.add("training_missing", OK, _("The owner has not done the training yet."), detail,
                _("The register works for now. Remind the owner of the basic pass."))


CHECKS = [check_shop, check_waiting, check_owner, check_lightning, check_register, check_first_day, check_closure,
          check_staff, check_account, check_certificate, check_customer_path, check_training]
# These only read the database, and they are quick: they run even when the slow ones used up the time
LOCAL_CHECKS = {check_shop, check_waiting, check_owner, check_register, check_first_day, check_closure, check_staff,
                check_account, check_certificate, check_customer_path, check_training}


def diagnose(shop, seconds=TOTAL_SECONDS, only=None, owner_view=False):
    """The findings about a shop, problems first, then warnings, then what is fine; within each level, in the order
    of the checks. The sentences are in the active language. `owner_view` leaves out what is told about other accounts
    (the page of the shop's owner). `only`, a collection of checks (the functions of CHECKS),
    runs just those, still in the order of CHECKS: the help page (shops/helpdesk.py) asks the database-only ones
    while it loads, and the others, with their time limits, only when the merchant asks."""
    run = Run(shop, seconds)
    run.owner_view = owner_view
    skipped = []
    run_checks(run, skipped, only)
    if skipped:
        run.add("time_up", WARNING, _("Some checks did not finish in time: run it again in a minute."),
                f"skipped: {', '.join(skipped)}; limit {seconds} s")
    return sorted(run.findings, key=lambda finding: LEVELS.index(finding.level))


def run_checks(run, skipped, only=None):
    for check in CHECKS:
        if only is not None and check not in only:
            continue
        name = check.__name__.removeprefix("check_")
        if run.left() <= 0 and check not in LOCAL_CHECKS:
            skipped.append(name)
            continue
        try:
            check(run)
        except TimeUp:
            skipped.append(name)
        except Exception as error:  # noqa: BLE001 - one broken check must not hide the others
            logger.error("Diagnosis check %s failed for shop %s: %s", name, run.shop.id, type(error).__name__)
            run.add("check_failed", WARNING,
                    _("One check could not be done: run it again; if it repeats, tell whoever runs the server."),
                    f"{name}: {type(error).__name__}")


def find_shops(text):
    """The shops a helper means: the id (or the id on the site it moved from), the owner's recovery email or the shop's
    public email, the number in the imported list, or a piece of the name."""
    text = text.strip()
    if not text:
        return []
    try:
        found = uuid.UUID(text)
    except ValueError:
        pass
    else:
        # The shop's id here, or its id on the site it moved from (the register link the merchant still has)
        return list(Shop.objects.filter(Q(pk=found) | Q(moved_from=str(found))).order_by("name", "pk"))
    if "@" in text:
        profile = profile_for(text)
        query = Q(email__iexact=text)
        if profile:
            query |= Q(owner=profile.user)
        return list(Shop.objects.filter(query).order_by("name", "pk"))
    return list(Shop.objects.filter(Q(name__icontains=text) | Q(external_id=text)).order_by("name", "pk"))
