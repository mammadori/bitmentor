"""The merchant's training, the «pass»: a path of modules written by the community, with a quiz and a few declarations, that
moves the risk of the money to the merchant who has been informed and accompanied (docs/features/merchant-training.md).

What the community writes, in the `training` block of `community.yml` (off when the block is missing):
- the modules: a title, a link to one of its pages, two to four questions with the right answer and a line that explains,
  and the statements the merchant ticks («the recovery words stay with me only»);
- how the real till is unlocked (`gate`: `unlock` or `reminder`), the `quick_start` (below), the day the training starts
  to count (`since`), how often the short review comes, whether a «trained merchant» sign is shown. The certificate of a
  shop always needs the basic pass (shops/certified.py).

What the site keeps, in the database: the quiz of each module (`ModuleResult`), and for each statement ticked a
`Declaration` row: which statement, the hash of its text (the version), the text as it was shown, the date, the account and
the shop. No address of the connection. A changed text is a new version and is asked again; the old rows stay. The
association reads them in /admin/ and exports them: it is the written proof that the merchant was informed.

The basic pass (`Trainee.base_earned`) is earned once all the modules that count for it (and the community's terms, if it
has any) are done. From then on the merchant is «formed»; the pass is not taken back, only a review is asked every
`review_months` and the sign of the trained merchant waits for it.

The unlock is the only thing here that can stop a till, and it is built so that it never stops one in the middle of a day:
- only when `NETWORK` is `mainnet`, which is where the money is real (signet and the practice register always work);
- only to open a new sale and to show the till, never to follow a sale that has started, never to send the backup register
  sales already made;
- only from the day the community wrote (`since`), which begins with the business day (`business_day.shift_day`: until
  05:00 it is still the day before); without `since`, from the moment the training is turned on. A community that turns
  it on for shops that already sell writes a `since` ahead and tells its merchants first;
- the texts changing, the review being late and the yearly reminders never lock anything;
- a community with a rule of its own can give an owner some days more (`grace_of`), and count the practice as done for
  whoever practised elsewhere: both through its own code (config/hooks.py), never through `community.yml`.
"""
import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import DatabaseError, IntegrityError, transaction
from django.utils import timezone, translation
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from config import features, hooks
from config.community import community, community_link, localized

from . import business_day, certified, public_cache
from .models import Declaration, ModuleResult, Trainee, is_demo

GATES = ("unlock", "reminder")
# The most `quick_start` can be, in the community's currency
QUICK_START_MAX = 1000
DEFAULTS = {"review_months": 12, "refresh_days": 30}
LIMITS = {"review_months": (1, 60), "refresh_days": (0, 365)}
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,39}\Z")
# The pseudo module of the community's terms (`merchant_terms`) and the words the routes use
TERMS = "terms"
RESERVED = (TERMS, "review", "declaration")
MAX_QUESTIONS = 8
MAX_OPTIONS = 6
MAX_STATEMENTS = 6
MAX_TEXT = 600
# The review comes this many days before it is due as a gentle reminder
REVIEW_WARNING = 30
# The steps the merchant declares after a guide, for one shop
STEP_KEYS = ("practice", "backup", "withdrawal")


# --- What the software says, in the pages' language (the msgid is the text the hash of the version comes from) ----------

STEP_TEXTS = {
    "practice": _("I did the exercises of the practice register. I know what the register says when a customer pays, "
                  "says they paid but the payment has not arrived, or does not pay, and when the phone switches off."),
    "backup": _("I checked my wallet's backup. My recovery words are written on paper, in a place only I know, and I "
                "checked them against the wallet."),
    "withdrawal": _("I tried a withdrawal. I sent a small amount from my wallet to another address, and I saw it arrive."),
}
STEP_NAMES = {"practice": _("Practice done"), "backup": _("Backup checked"), "withdrawal": _("Withdrawal tried")}
STEP_HINTS = {
    "practice": _("Do the exercises of the practice register, then say so here."),
    "backup": _("Check your backup with the guide, then say so here."),
    "withdrawal": _("Send a small amount to another address with the guide, then say so here."),
}
STAFF_RULES = (
    _("A sale is paid only when the register says so. The customer's phone and a screenshot do not count."),
    _("Never give the recovery words of a wallet to anyone."),
    _("If in doubt, call the owner."),
)
# The sign on the shop page and on the map
TRAINED = _("Trained merchant")
TERMS_TITLE = _("The conditions of the help")
TERMS_STATEMENT = _("I read the conditions of the help. I know what the volunteers do and what stays with me: my wallet, "
                    "my recovery words, my backup and the count of my sales.")


# --- The block in community.yml ----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Question:
    text: object
    options: tuple
    answer: int
    explain: object


@dataclass(frozen=True)
class Statement:
    id: str
    key: str
    text: object
    version: str


@dataclass(frozen=True)
class Module:
    id: str
    title: object
    page: object
    base: bool
    questions: tuple = ()
    statements: tuple = ()


@dataclass(frozen=True)
class Config:
    gate: str
    review_months: int
    refresh_days: int
    since: date | None
    public_sign: bool
    guides: dict = field(default_factory=dict)
    modules: tuple = ()
    terms: object = None
    # The three rules of the counter the community wrote, as texts (one for each language); empty: the software's own
    staff_rules: tuple = ()
    # The quick start: what the owner's register may take in total before the basic pass, in cents of the community's
    # currency; 0 is off
    quick_start: int = 0


def source_of(text):
    """The text as the community or the software wrote it, for the hash: the dict of every language, or the English msgid."""
    if isinstance(text, (str, dict)):
        return text
    with translation.override("en"):
        return str(text)


def version_of(key, text):
    """The version of a statement: the hash of its key and of its text in every language. Any change, in any language, is
    another version and is asked again; a different order of the languages in the file is not."""
    body = json.dumps([key, source_of(text)], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()


def text_ok(value):
    """Text, or a text for each language."""
    if isinstance(value, dict):
        return bool(value) and all(isinstance(item, str) and item.strip() and len(item) <= MAX_TEXT for item in value.values())
    return isinstance(value, str) and bool(value.strip()) and len(value) <= MAX_TEXT


def shown(value, language=None):
    """A text in the page's language: the community's, or the software's."""
    return str(localized(value, language or translation.get_language() or settings.LANGUAGE_CODE))


def parse_question(entry, where, warn):
    if not isinstance(entry, dict) or not text_ok(entry.get("text")) or not text_ok(entry.get("explain")):
        warn(f"{where}: a question needs `text`, `options`, `answer` and `explain`, each text up to {MAX_TEXT} characters")
        return None
    options = entry.get("options")
    answer = entry.get("answer")
    if (not isinstance(options, list) or not 2 <= len(options) <= MAX_OPTIONS or not all(text_ok(item) for item in options)):
        warn(f"{where}: `options` is a list of 2 to {MAX_OPTIONS} texts")
        return None
    if type(answer) is not int or not 1 <= answer <= len(options):
        warn(f"{where}: `answer` is the number of the right option, from 1 to {len(options)}")
        return None
    return Question(entry["text"], tuple(options), answer - 1, entry["explain"])


def parse_statements(module_id, entries, where, warn):
    found = []
    for number, entry in enumerate(entries if isinstance(entries, list) else [], 1):
        ident = entry.get("id") if isinstance(entry, dict) else None
        if (not isinstance(ident, str) or not SLUG.fullmatch(ident) or not text_ok(entry.get("text"))
                or any(ident == other.id for other in found)):
            warn(f"{where}, declaration {number}: it needs a new `id` (letters, digits and dashes) and a `text`")
            continue
        key = f"{module_id}:{ident}"
        found.append(Statement(ident, key, entry["text"], version_of(key, entry["text"])))
        if len(found) == MAX_STATEMENTS:
            break
    return tuple(found)


def parse_module(entry, number, taken, warn):
    where = f"`training.modules` {number}"
    if not isinstance(entry, dict):
        warn(f"{where} is not a list of fields")
        return None
    ident = entry.get("id")
    if not isinstance(ident, str) or not SLUG.fullmatch(ident) or ident in RESERVED or ident in taken:
        warn(f"{where}: `id` is a word of letters, digits and dashes that no other module has (and not {', '.join(RESERVED)})")
        return None
    where = f"`training.modules` {ident}"
    if not text_ok(entry.get("title")):
        warn(f"{where}: `title` is missing")
        return None
    page = entry.get("page")
    if page is not None:
        try:
            community_link(page, settings.LANGUAGE_CODE)
        except ValueError as error:
            warn(f"{where}: `page`: {error}")
            page = None
    questions = tuple(question for count, item in enumerate(entry.get("questions") or [], 1)
                      if (question := parse_question(item, f"{where}, question {count}", warn)))[:MAX_QUESTIONS]
    statements = parse_statements(ident, entry.get("declarations"), where, warn)
    if not questions and not statements:
        warn(f"{where}: it needs questions or declarations")
        return None
    base = entry.get("base", True)
    return Module(ident, entry["title"], page, base if isinstance(base, bool) else True, questions, statements)


def terms_link(language):
    """The community's page of the conditions of the help (`merchant_terms`, also read as `help_terms`), or ''."""
    data = community()
    value = data.get("merchant_terms") or data.get("help_terms")
    value = value.get("path") if isinstance(value, dict) and "path" in value else value
    if not value:
        return ""
    try:
        return community_link(value, language)
    except ValueError:
        return ""


def parse(warn=None):
    """The block as a `Config`, or None when the training is off. What cannot be used is left out and told to `warn`."""
    say = warn or (lambda problem: None)
    data = community().get("training")
    if data is None or data is False:
        return None
    if not isinstance(data, dict):
        say("`training` is not a list of fields")
        return None
    numbers = {}
    for name in DEFAULTS:
        value = data.get(name, DEFAULTS[name])
        low, high = LIMITS[name]
        if type(value) is int and low <= value <= high:
            numbers[name] = value
        else:
            say(f"`training.{name}` must be a whole number from {low} to {high}: {value!r}; {DEFAULTS[name]} counts instead")
            numbers[name] = DEFAULTS[name]
    gate = data.get("gate", "unlock")
    if gate not in GATES:
        say(f"`training.gate` is {' or '.join(GATES)}: {gate!r}; `unlock` counts instead")
        gate = "unlock"
    quick = data.get("quick_start", 0)
    if quick is None or quick is False:
        quick = 0
    if (isinstance(quick, bool) or not isinstance(quick, (int, float)) or not 0 <= quick <= QUICK_START_MAX
            or 0 < quick < 0.01):
        say(f"`training.quick_start` is an amount from 0 to {QUICK_START_MAX} in the community's currency (0: off): "
            f"{data.get('quick_start')!r}; it stays off")
        quick = 0
    since = data.get("since")
    if isinstance(since, str):
        try:
            since = date.fromisoformat(since)
        except ValueError:
            since = False
    if since is not None and (isinstance(since, datetime) or not isinstance(since, date)):
        # A day that cannot be read is not "from now": the lock would fall on every register at once because of a slip
        # of the keyboard. Nothing is asked and nothing locks until it is written right
        say(f"`training.since` is a day, like 2026-12-01: {data.get('since')!r}; the training waits until it is fixed")
        since = date.max
    guides = {}
    for name, value in (data.get("guides") or {}).items() if isinstance(data.get("guides"), dict) else ():
        try:
            community_link(value, settings.LANGUAGE_CODE)
            guides[name] = value
        except ValueError as error:
            say(f"`training.guides.{name}`: {error}")
    taken, modules = set(), []
    entries = data.get("modules")
    for number, entry in enumerate(entries if isinstance(entries, list) else [], 1):
        module = parse_module(entry, number, taken, say)
        if module:
            modules.append(module)
            taken.add(module.id)
    if not modules:
        say("`training.modules` has no module that can be used: the training stays off")
        return None
    terms = None
    if terms_link(settings.LANGUAGE_CODE):
        key = f"{TERMS}:accept"
        terms = Module(TERMS, TERMS_TITLE, community().get("merchant_terms") or community().get("help_terms"), True, (),
                       (Statement("accept", key, TERMS_STATEMENT, version_of(key, TERMS_STATEMENT)),))
    rules = data.get("staff_rules")
    if rules is not None and not (isinstance(rules, list) and 1 <= len(rules) <= MAX_STATEMENTS and all(text_ok(rule) for rule in rules)):
        say(f"`training.staff_rules` is a list of 1 to {MAX_STATEMENTS} texts, each up to {MAX_TEXT} characters: the software's own rules count instead")
        rules = None
    flag = lambda name: data.get(name, True) is not False
    return Config(gate, numbers["review_months"], numbers["refresh_days"], since or None,
                  flag("public_sign"), guides, tuple(modules), terms, tuple(rules or ()), round(quick * 100))


def problems():
    found = []
    parse(found.append)
    return found


def all_modules(cfg):
    """The terms first, if the community has them, then the modules in the order they were written."""
    return ((cfg.terms,) if cfg.terms else ()) + cfg.modules


def find_module(cfg, ident):
    return next((module for module in all_modules(cfg) if module.id == ident), None)


def page_url(module, language=None):
    """The address of the community's page for a module, or ''."""
    language = language or translation.get_language() or settings.LANGUAGE_CODE
    if module.id == TERMS:
        return terms_link(language)
    if not module.page:
        return ""
    try:
        return community_link(module.page, language)
    except ValueError:
        return ""


def guide_url(cfg, name, language=None):
    value = cfg.guides.get(name)
    try:
        return community_link(value, language or translation.get_language() or settings.LANGUAGE_CODE) if value else ""
    except ValueError:
        return ""


# --- Months and days -------------------------------------------------------------------------------------------------

def add_months(day, months):
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    last = [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return date(year, month, min(day.day, last))


def today(now=None):
    return certified.local_day(now or timezone.now())


# --- The account's standing -------------------------------------------------------------------------------------------

def trainee_of(user):
    return Trainee.objects.filter(user=user).first() if getattr(user, "is_authenticated", False) else None


def get_trainee(user):
    try:
        with transaction.atomic():
            return Trainee.objects.get_or_create(user=user)[0]
    except IntegrityError:
        return Trainee.objects.get(user=user)


def main_shop(user):
    """The shop the path is about when the account has several: the oldest one with a register, else the oldest."""
    shops = list(user.shops.order_by("created"))
    return next((shop for shop in shops if shop.has_register), shops[0] if shops else None)


def quiz_passed(module, results):
    return not module.questions or bool(results.get(module.id) and results[module.id].passed)


def statement_state(statement, declared):
    """«done» for the current version, «old» when an earlier version was ticked and the text has changed, else «todo»."""
    versions = declared.get(statement.key, set())
    return "done" if statement.version in versions else "old" if versions else "todo"


def module_state(module, results, declared):
    """A module as the page shows it: the quiz, the statements and where it stands (todo, declare, update, done)."""
    states = [statement_state(statement, declared) for statement in module.statements]
    passed = quiz_passed(module, results)
    if not passed:
        state = "todo"
    elif "old" in states:
        state = "update"
    elif "todo" in states:
        state = "declare"
    else:
        state = "done"
    return {"module": module, "id": module.id, "quiz": passed, "state": state, "states": states,
            "done": state == "done", "statements_done": states.count("done"), "statements_total": len(states),
            "attempts": results[module.id].attempts if module.id in results else 0}


def results_of(user):
    return {result.module: result for result in ModuleResult.objects.filter(user=user)}


def declared_by(user, shop=None):
    """{key: versions ticked}: the module statements are the account's; the steps count for the shop they were ticked for."""
    found = {}
    for key, version, shop_id in Declaration.objects.filter(user=user).values_list("key", "version", "shop_id"):
        if key.startswith("step:") and shop is not None and shop_id != shop.pk:
            continue
        found.setdefault(key, set()).add(version)
    return found


def step_keys():
    """The steps a merchant can declare: the practice register is one only while the community has it (features.practice)."""
    return tuple(name for name in STEP_KEYS if name != "practice" or features.enabled(features.PRACTICE))


def step_key(name):
    return f"step:{name}"


def step_version(name):
    return version_of(step_key(name), STEP_TEXTS[name])


def badge_list(trainee, declared, user=None):
    """The badges, earned or not, in order. The practice step also counts as done when the community's own rule says
    this owner already practised at a register (config/hooks.py, `training_practice_done`)."""
    base = bool(trainee and trainee.base_earned)
    practised = bool(user) and hooks.yes("training_practice_done", user)
    found = [{"key": "base", "name": gettext("Basic pass"), "earned": base,
              "hint": gettext("Do the modules of the path: it takes a few minutes each.")}]
    for name in step_keys():
        found.append({"key": name, "name": str(STEP_NAMES[name]),
                      "earned": (step_version(name) in declared.get(step_key(name), set())
                                 or (name == "practice" and practised)),
                      "hint": str(STEP_HINTS[name]), "step": True})
    return found


@dataclass
class Standing:
    cfg: Config
    user: object
    shop: object
    trainee: object
    modules: list
    base: bool
    ready: bool
    steps: dict
    badges: list
    review: dict | None
    # The days the community's own rule gives this owner before the register waits for the pass: {"until", "days"}, or None
    grace: dict | None
    locked: bool
    formed: bool

    @property
    def modules_done(self):
        return sum(item["done"] for item in self.modules)

    @property
    def progress(self):
        """The bar, towards the next goal only: before the basic pass the modules it needs; after it the badges of the steps
        with a guide. None when everything is earned, so the card never shows an empty bar next to a pass."""
        if not self.base:
            needed = [item for item in self.modules if item["module"].base]
            return {"goal": "base", "value": sum(item["done"] for item in needed), "max": len(needed)}
        extra = [badge for badge in self.badges if badge["key"] != "base"]
        value = sum(badge["earned"] for badge in extra)
        return {"goal": "badges", "value": value, "max": len(extra)} if value < len(extra) else None


def review_state(cfg, trainee, now=None):
    """None before the basic pass; else when the next review is due, and whether it is near, due or late (still not locking)."""
    if not trainee or not trainee.base_earned:
        return None
    last = trainee.reviewed or trainee.base_earned
    due = add_months(certified.local_day(last), cfg.review_months)
    current = today(now)
    state = "late" if current > due + timedelta(days=cfg.refresh_days) else "due" if current >= due \
        else "soon" if current >= due - timedelta(days=REVIEW_WARNING) else "ok"
    return {"due": due, "state": state, "days": (due - current).days}


def in_force(cfg, now=None):
    """The community can say from which day the training counts (`since`): before it nothing is asked and nothing locks."""
    return not cfg.since or business_day.shift_day(now) >= cfg.since


def real_money(shop):
    return settings.NETWORK == "mainnet" and not is_demo(shop)


def standing(user, shop=None, now=None, cfg=None):
    """Everything the pages need to know about an account's path, in few queries. None when the training is off."""
    cfg = cfg or parse()
    if not cfg:
        return None
    shop = shop if shop is not None and shop.owner_id == user.pk else main_shop(user)
    trainee = trainee_of(user)
    results, declared = results_of(user), declared_by(user, shop)
    modules = [module_state(module, results, declared) for module in all_modules(cfg)]
    ready = all(item["done"] for item, module in zip(modules, all_modules(cfg)) if module.base)
    badges = badge_list(trainee, declared, user)
    base = bool(trainee and trainee.base_earned)
    review = review_state(cfg, trainee, now)
    steps = {name: badge for badge in badges for name in step_keys() if badge["key"] == name}
    # The community's own days, asked once and only where they change something: a real register that would
    # otherwise wait for the pass. Said in the panel, and the same answer decides the lock below
    waits = bool(shop) and not base and cfg.gate == "unlock" and real_money(shop) and in_force(cfg, now)
    grace = grace_of(user, now) if waits else None
    held = locked_for(cfg, user.pk, shop, now, trainee, user, grace if waits else False) if shop else False
    grace = grace if shop and shop.has_register else None
    return Standing(cfg, user, shop, trainee, modules, base, ready, steps, badges, review, grace, held,
                    base and (not review or review["state"] != "late"))


# --- Doing things ----------------------------------------------------------------------------------------------------

def grade(question, given):
    """True if `given` (what the form sent) is the right option."""
    try:
        return int(given) == question.answer
    except (TypeError, ValueError):
        return False


def answer_quiz(user, module, post, now=None):
    """Grades a submitted quiz. Returns the list of {question, given, right}; the module is passed when every answer is right.
    Tries only count: no limit, no penalty. A right answer stays right when the others are corrected."""
    now = now or timezone.now()
    marks = []
    for number, question in enumerate(module.questions):
        given = post.get(f"q{number}")
        marks.append({"number": number, "question": question, "given": given, "right": grade(question, given)})
    everything = all(mark["right"] for mark in marks)
    with transaction.atomic():
        result, _created = ModuleResult.objects.get_or_create(user=user, module=module.id)
        result.attempts = min(result.attempts + 1, 30000)
        if everything and not result.passed:
            result.passed = now
        result.save(update_fields=["attempts", "passed"])
    return marks, everything


def masked(name):
    """A name as the association's export shows it: the first letter and no more, so the proof says who without handing the
    name of a person to whoever opens the file. The row in the database keeps the whole name."""
    name = (name or "").strip()
    return f"{name[:1]}***" if name else ""


def record(user, shop, key, version, text, register=None, now=None):
    """Writes one declaration, once for the same person, statement, version and shop. Returns whether it is new. The row
    carries no address of the connection: who (the account's name, or the register's), the shop, the text shown and when."""
    who = register.name if register else user.get_username()
    values = {"who": who[:150], "shop_name": (shop.name if shop else "")[:120], "text": text,
              "language": (translation.get_language() or "")[:10], "made": now or timezone.now()}
    try:
        with transaction.atomic():
            if register:
                found = Declaration.objects.get_or_create(register=register, key=key, version=version,
                                                          defaults={**values, "shop": shop})
            elif shop:
                found = Declaration.objects.get_or_create(user=user, key=key, version=version, shop=shop,
                                                          defaults=values)
            else:
                # Without a shop: never one of the rows a deleted shop left behind, which have no shop either but
                # keep its name (and can be more than one)
                found = Declaration.objects.get_or_create(user=user, key=key, version=version, shop=None, shop_name="",
                                                          defaults={name: value for name, value in values.items()
                                                                    if name != "shop_name"})
    except IntegrityError:
        return False
    return found[1]


def declare(user, module, ids, shop, now=None):
    """Ticks the statements of a module whose ids were sent. Returns how many are new."""
    declared = declared_by(user)
    new = 0
    for statement in module.statements:
        if statement.id in ids and statement.version not in declared.get(statement.key, set()):
            new += bool(record(user, shop, statement.key, statement.version, shown(statement.text), now=now))
    return new


def declare_step(user, shop, name, now=None):
    """The merchant says they did one of the steps with a guide. For one shop of theirs."""
    if name not in step_keys() or shop is None or shop.owner_id != user.pk:
        return False
    return record(user, shop, step_key(name), step_version(name), str(STEP_TEXTS[name]), now=now)


def settle(user, cfg=None, now=None):
    """After a quiz or a declaration: the basic pass is earned when all the modules that count are done. Once."""
    cfg = cfg or parse()
    if not cfg:
        return False
    trainee = get_trainee(user)
    if trainee.base_earned:
        return False
    results, declared = results_of(user), declared_by(user)
    if not all(module_state(module, results, declared)["done"] for module in all_modules(cfg) if module.base):
        return False
    now = now or timezone.now()
    updated = Trainee.objects.filter(pk=trainee.pk, base_earned__isnull=True).update(base_earned=now, reviewed=now)
    if updated:
        # The map and /shops.json say which shops have a trained owner (shops/public_cache.py)
        public_cache.changed()
        # The basic pass may be the last thing the certificate of the owner's shops waited for
        certified.refresh(user.shops.all(), now)
    return bool(updated)


def review_picks(cfg):
    """One question for each module that has any, drawn now: {module id: index}."""
    return {module.id: secrets.randbelow(len(module.questions)) for module in all_modules(cfg) if module.questions}


def answer_review(user, cfg, post, now=None):
    """The short review: the questions the form carries (`pick-<module>` is the index, `a-<module>` the answer). Returns the
    marks and whether everything is right, in which case the review is passed. Without penalty and without limit."""
    marks = []
    for module in all_modules(cfg):
        if not module.questions:
            continue
        try:
            index = int(post.get(f"pick-{module.id}", ""))
        except ValueError:
            index = -1
        if not 0 <= index < len(module.questions):
            continue
        question = module.questions[index]
        given = post.get(f"a-{module.id}")
        marks.append({"module": module, "index": index, "question": question, "given": given, "right": grade(question, given)})
    everything = bool(marks) and len(marks) == sum(1 for module in all_modules(cfg) if module.questions) and all(
        mark["right"] for mark in marks)
    if everything:
        if Trainee.objects.filter(user=user, base_earned__isnull=False).update(reviewed=now or timezone.now()):
            public_cache.changed()
    return marks, everything


# --- The unlock of the real till ---------------------------------------------------------------------------------------

def grace_of(user, now=None):
    """The days the community's own rule gives this owner (config/hooks.py, `training_grace_until`): the last day on which
    the register opens without the pass, and how many days are left. None without a rule, without a day for this
    owner, or past it. The day ends with the business day, so the lock never falls in the middle of an evening."""
    # A database that does not answer inside the community's rule is passed on: `locked` never locks a till on it
    until = hooks.day("training_grace_until", user, passes=(DatabaseError,)) if user else None
    today = business_day.shift_day(now)
    return {"until": until, "days": (until - today).days} if until and today <= until else None


def locked_for(cfg, user_id, shop, now=None, trainee=False, user=None, grace=False):
    """Whether the real till of `shop` waits for the basic pass of its owner. See the head of the module. `grace`, when the
    caller already asked the community's rule for this owner, is its answer (grace_of), so that it is asked once."""
    if cfg.gate != "unlock" or not real_money(shop) or not user_id or not in_force(cfg, now):
        return False
    trainee = trainee_of_id(user_id) if trainee is False else trainee
    if trainee and trainee.base_earned:
        return False
    if grace is not False:
        return not grace
    if not hooks.defined("training_grace_until"):
        return True
    # Only a community with a rule of its own pays for this: the account is read when nobody passed it
    user = user or get_user_model().objects.filter(pk=user_id).first()
    return not grace_of(user, now)


def trainee_of_id(user_id):
    return Trainee.objects.filter(user_id=user_id).first()


def locked(shop, now=None):
    """True when the shop's real till may not take a new sale. One query, and none where the training is off or the money is
    not real. A database that cannot answer never locks a till."""
    if not real_money(shop) or not shop.owner_id:
        return False
    cfg = parse()
    if not cfg:
        return False
    try:
        return locked_for(cfg, shop.owner_id, shop, now)
    except DatabaseError:
        return False


def quick_start(shop, user, now=None):
    """The quick start of `user` at the register of `shop`, in cents: the owner's register of a shop that waits for the
    basic pass may take payments up to this in total, counted on the phone (static/register.js, quickLeft). 0 when the
    community has none, when the till is not locked, or for anyone but the owner: the staff registers wait for the pass."""
    cfg = parse()
    if not cfg or not cfg.quick_start or not getattr(user, "is_authenticated", False) or user.pk != shop.owner_id:
        return 0
    return cfg.quick_start if locked(shop, now) else 0


# --- The rest of the community's pages: the sign, the staff rules -----------------------------------

def formed_trainee(cfg, trainee, now=None):
    """The sign of a trained merchant: the pass is earned and the review is not late."""
    if not trainee or not trainee.base_earned:
        return False
    review = review_state(cfg, trainee, now)
    return review is None or review["state"] != "late"


def mark_trained(shops, now=None):
    """Sets `trained` on each shop of a list, with one query. False everywhere when the sign is off."""
    shops = list(shops)
    cfg = parse()
    owners = {shop.owner_id for shop in shops if shop.owner_id}
    trained = set()
    if cfg and cfg.public_sign and owners:
        trained = {trainee.user_id for trainee in Trainee.objects.filter(user_id__in=owners, base_earned__isnull=False)
                   if formed_trainee(cfg, trainee, now)}
    for shop in shops:
        shop.trained = shop.owner_id in trained
    return shops


def is_trained(shop, now=None):
    return mark_trained([shop], now)[0].trained


def has_pass(shop):
    """Whether the owner of `shop` has earned the basic pass, which the certificate needs (shops/certified.py)."""
    return bool(shop.owner_id) and Trainee.objects.filter(user_id=shop.owner_id, base_earned__isnull=False).exists()


def staff_rules_due(staff):
    """A register of someone who works with the owner that has never accepted the three rules: they come before the till.
    A register that has accepted them once, in any version, is never stopped again (it could be in the middle of a sale,
    and the server does not know its sales): a new text of the rules is asked of the registers paired after it. The
    owner who wants it read again pairs the register anew (a new register)."""
    cfg = parse()
    if staff.retired or not cfg:
        return False
    return not Declaration.objects.filter(register=staff, key=STAFF_KEY).exists()


STAFF_KEY = "staff:rules"


def staff_rules(cfg):
    """The three rules of the counter: the community's (`training.staff_rules`), or the software's own."""
    return cfg.staff_rules or STAFF_RULES


def staff_lines(cfg):
    """The rules in the page's language."""
    return [shown(rule) for rule in staff_rules(cfg)]


def staff_text(cfg):
    return "\n".join(f"{number}. {rule}" for number, rule in enumerate(staff_lines(cfg), 1))


def staff_version(cfg):
    """The hash of the rules in every language they are written in: the text of the community changes it."""
    return version_of(STAFF_KEY, [source_of(rule) for rule in staff_rules(cfg)])


def accept_staff_rules(staff, cfg=None, now=None):
    cfg = cfg or parse()
    return record(None, staff.shop, STAFF_KEY, staff_version(cfg), staff_text(cfg), register=staff, now=now)


# --- The owner's panel --------------------------------------------------------------------------------------------------

def panel(user, shops, now=None):
    """What the panel's training card shows, or None where the community has no training. `shops` are the account's, with
    `till_locked` already worked out by the panel."""
    cfg = parse()
    if not cfg:
        return None
    state = standing(user, None, now, cfg)
    pending = next((item for item in state.modules if not item["done"]), None)
    return {"state": state, "next": pending, "next_title": shown(pending["module"].title) if pending else "",
            "stale": [item for item in state.modules if item["state"] == "update" and item["module"].base],
            "locked": [shop for shop in shops if getattr(shop, "till_locked", False)],
            "quick": [shop for shop in shops if getattr(shop, "till_quick", 0)]}
