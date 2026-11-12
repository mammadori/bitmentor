"""The pages of the merchant's training (shops/training.py): the path, a module with its quiz and declarations, the short
review, the steps declared after a guide, and the three rules of a staff register. They work without JavaScript: plain forms,
the answers graded on the server, which is the only place that knows the right ones."""
import uuid

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods, require_POST

from config import features, ratelimit

from . import staff as staff_registers
from . import training, views

# A person answering a quiz does not make hundreds of tries in ten minutes
TRIES = 80
WINDOW = 600
# The three rules at a staff register: the register's own limit, and one for the address, as for the one-time code
RULES_PER_IP = 60


def configured():
    cfg = training.parse()
    if not cfg:
        raise Http404
    return cfg


def chosen_shop(request):
    """The shop the path is about: the one asked for (`shop`), if it is the account's, else the main one."""
    asked = request.POST.get("shop") or request.GET.get("shop") or ""
    try:
        asked = uuid.UUID(asked)
    except ValueError:
        return training.main_shop(request.user)
    return request.user.shops.filter(pk=asked).first() or training.main_shop(request.user)


def busy(request):
    return ratelimit.over_limit(f"training:{request.user.pk}", TRIES, WINDOW)


def back(request, shop, name="training", *args):
    url = reverse(name, args=args)
    return redirect(f"{url}?shop={shop.pk}" if shop else url)


def quiz_items(module, marks=None):
    """The questions of a module as the form shows them: with the answer given and, when graded, whether it was right and the
    line that explains. A right answer stays chosen; a wrong one is cleared, and its explanation is read."""
    by_number = {mark["number"]: mark for mark in marks or ()}
    items = []
    for number, question in enumerate(module.questions):
        mark = by_number.get(number)
        chosen = int(mark["given"]) if mark and mark["right"] else None
        items.append({"number": number, "name": f"q{number}", "text": training.shown(question.text),
                      "options": [{"value": index, "text": training.shown(option), "checked": index == chosen}
                                  for index, option in enumerate(question.options)],
                      "state": None if not mark else "right" if mark["right"] else "wrong",
                      "explain": training.shown(question.explain) if mark and not mark["right"] else ""})
    return items


@views.not_indexed
@login_required(login_url="shops")
def home(request):
    """The path: the modules with their state, the badges and the bar, the steps to declare, the review."""
    cfg = configured()
    shop = chosen_shop(request)
    state = training.standing(request.user, shop, cfg=cfg)
    language = request.LANGUAGE_CODE
    modules = [{**item, "title": training.shown(item["module"].title), "page": training.page_url(item["module"], language),
                "questions": len(item["module"].questions)} for item in state.modules]
    next_item = next((item for item in modules if not item["done"]), None)
    steps = [{**badge, "guide": training.guide_url(cfg, badge["key"], language) if badge["key"] != "practice" else
              (reverse("practice", args=[shop.pk]) if shop and (shop.has_register or not shop.closed_on) else "")}
             for badge in state.badges if badge.get("step")]
    return render(request, "shops/training.html", {
        "state": state, "modules": modules, "next_item": next_item, "steps": steps, "shop": shop,
        "shops": list(request.user.shops.order_by("created")), "real_money": settings.NETWORK == "mainnet"})


@views.not_indexed
@login_required(login_url="shops")
@require_http_methods(["GET", "HEAD", "POST"])
def module(request, slug):
    """One module: the community's page to read, the quiz, then the statements to tick. The right answers never reach the
    browser before they are given."""
    cfg = configured()
    found = training.find_module(cfg, slug)
    if not found:
        raise Http404
    shop = chosen_shop(request)
    marks = None
    if request.method == "POST":
        if busy(request):
            messages.error(request, _("Too many tries in a short time: wait a few minutes."))
            return back(request, shop, "training_module", slug)
        action = request.POST.get("action")
        results = training.results_of(request.user)
        if action == "quiz" and found.questions:
            marks, passed = training.answer_quiz(request.user, found, request.POST)
            if passed:
                training.settle(request.user, cfg)
                messages.success(request, _("All the answers are right."))
                return back(request, shop, "training_module", slug)
            messages.info(request, _("Some answers are not right yet. Read the lines under them and try again: there is "
                                     "no penalty and no limit."))
        elif action == "declare" and training.quiz_passed(found, results):
            new = training.declare(request.user, found, set(request.POST.getlist("ticked")), shop)
            earned = training.settle(request.user, cfg)
            if earned:
                messages.success(request, _("You have the basic pass. Well done!"))
            elif new:
                messages.success(request, _("Saved. Thank you."))
            else:
                messages.info(request, _("Tick every statement you agree with, then press the button."))
            return back(request, shop, "training_module", slug)
    state = training.standing(request.user, shop, cfg=cfg)
    item = next(item for item in state.modules if item["id"] == slug)
    statements = [{"id": statement.id, "text": training.shown(statement.text), "state": state_name}
                  for statement, state_name in zip(found.statements, item["states"])]
    following = next((other for other in state.modules if not other["done"] and other["id"] != slug), None)
    return render(request, "shops/training_module.html", {
        "state": state, "module": found, "item": item, "title": training.shown(found.title), "shop": shop,
        "page": training.page_url(found, request.LANGUAGE_CODE), "items": quiz_items(found, marks), "marks": marks,
        "statements": statements, "following": following and {"id": following["id"], "title": training.shown(following["module"].title)}})


@views.not_indexed
@login_required(login_url="shops")
@require_POST
def step(request):
    """«I did it»: one of the steps declared after a guide, for one shop."""
    configured()
    shop = chosen_shop(request)
    if busy(request):
        messages.error(request, _("Too many tries in a short time: wait a few minutes."))
    elif request.POST.get("step") in training.step_keys() and request.POST.get("agree") and training.declare_step(
            request.user, shop, request.POST["step"]):
        messages.success(request, _("Saved. Thank you."))
    else:
        messages.info(request, _("Tick the box to say you did it, then press the button."))
    return back(request, shop)


@views.not_indexed
@login_required(login_url="shops")
@require_http_methods(["GET", "HEAD", "POST"])
def review(request):
    """The short review: one question from each module, without penalty. Open to whoever has the basic pass."""
    cfg = configured()
    shop = chosen_shop(request)
    state = training.standing(request.user, shop, cfg=cfg)
    if not state.base:
        return back(request, shop)
    marks = None
    if request.method == "POST":
        if busy(request):
            messages.error(request, _("Too many tries in a short time: wait a few minutes."))
            return back(request, shop, "training_review")
        marks, passed = training.answer_review(request.user, cfg, request.POST)
        if passed:
            messages.success(request, _("Review done. See you next year!"))
            return back(request, shop)
        messages.info(request, _("Some answers are not right yet. Read the lines under them and try again."))
        picks = {mark["module"].id: mark["index"] for mark in marks}
    else:
        picks = training.review_picks(cfg)
    by_module = {mark["module"].id: mark for mark in marks or ()}
    items = []
    for module_ in training.all_modules(cfg):
        if module_.id not in picks or not module_.questions:
            continue
        question = module_.questions[picks[module_.id]]
        mark = by_module.get(module_.id)
        chosen = int(mark["given"]) if mark and mark["right"] else None
        items.append({"module": module_, "title": training.shown(module_.title), "index": picks[module_.id],
                      "text": training.shown(question.text), "name": f"a-{module_.id}",
                      "options": [{"value": index, "text": training.shown(option), "checked": index == chosen}
                                  for index, option in enumerate(question.options)],
                      "state": None if not mark else "right" if mark["right"] else "wrong",
                      "explain": training.shown(question.explain) if mark and not mark["right"] else ""})
    return render(request, "shops/training_review.html", {"state": state, "items": items, "shop": shop})


@views.not_indexed
@require_http_methods(["GET", "HEAD", "POST"])
def staff_rules(request, register):
    """The three rules of the counter, before the first sale of a staff register. «I understood» is kept like a
    declaration, for the register, with no address of the phone. The key is in the query for the page and in the body for the
    form, as the register's one-time code."""
    cfg = training.parse()
    if not cfg:
        raise Http404
    key = request.POST.get("k", "") if request.method == "POST" else staff_registers.page_key(request)
    staff = staff_registers.staff_register(register, key)
    if staff.retired:
        raise Http404
    if request.method == "POST":
        if ratelimit.over_limit(f"staff-rules:{ratelimit.client_ip(request)}", RULES_PER_IP, WINDOW):
            return staff_registers.private(render(request, "shops/staff_rules.html", {"staff": staff, "key": key, "busy": True,
                                                                                         "rules": []}, status=429))
        if request.POST.get("agree"):
            training.accept_staff_rules(staff, cfg)
            return staff_registers.private(redirect(staff_registers.page_url(staff)))
    return staff_registers.private(render(request, "shops/staff_rules.html", {
        "staff": staff, "shop": staff.shop, "key": key, "rules": training.staff_lines(cfg), "missing": request.method == "POST",
        "practice": f"{reverse('staff_practice', args=[staff.id])}?k={staff.key}" if features.enabled(features.PRACTICE) else "",
        "register": staff_registers.page_url(staff)}))
