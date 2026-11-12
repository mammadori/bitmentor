"""The owner's pages for a closure (shops/vacation.py): the form «I close from … until …», «closed from today until I say
so», «I am back today», cancelling one still to come, and the owner closing the «welcome back» card. Only the shop's
owner; every change is a POST with the CSRF token and a limit of requests; nothing here touches the register."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from config import ratelimit
from . import vacation
from .directory import waits_for_wallet
from .models import Closure
from .views import not_indexed

# A change is a few seconds of a person's time: this is only a stop for a script
CHANGES_PER_USER = 40
CHANGE_WINDOW = 3600


def owned(request, shop):
    """The owner's shop that can close for a while: on the site's map, not a historic one (closed for good) and not one
    still waiting for its wallet."""
    found = get_object_or_404(request.user.shops, pk=shop)
    if found.closed_on or waits_for_wallet(found):
        raise Http404
    return found


def back(shop):
    return redirect(f"{reverse('shops')}#break-{shop.pk}")


@not_indexed
@login_required(login_url="shops")
def shop_break(request, shop):
    """The form, and what it saves. The closure in course, or the next one, is the one that is edited."""
    shop = owned(request, shop)
    day = vacation.day_of()
    editing = vacation.current_for_edit(shop, day)
    form = vacation.BreakForm(request.POST or None, day=day, editing=editing)
    if request.method == "POST":
        if ratelimit.over_limit(f"break-user:{request.user.pk}", CHANGES_PER_USER, CHANGE_WINDOW):
            messages.error(request, _("Too many changes in a short time: try again in a while."))
        elif form.is_valid():
            saved = vacation.save(shop, form.cleaned_data, day)
            if saved is None:
                messages.success(request, _("The shop is open."))
            elif saved.reopens_on is not None and saved.reopens_on <= day:
                messages.success(request, _("Welcome back! The shop says it is open again."))
            elif saved.first_day <= day:
                messages.success(request, _("Saved: the shop says it is closed."))
            else:
                messages.success(request, _("Saved: the shop will say it is closed."))
            return back(shop)
    return render(request, "shops/break.html", {
        "shop": shop, "form": form, "editing": editing, "started": form.started, "today": day,
        "state": vacation.panel(shop, day), "note": vacation.note(shop, day), "limit": vacation.LONGEST})


def quick(request, shop, action):
    shop = owned(request, shop)
    day = vacation.day_of()
    if ratelimit.over_limit(f"break-user:{request.user.pk}", CHANGES_PER_USER, CHANGE_WINDOW):
        messages.error(request, _("Too many changes in a short time: try again in a while."))
        return back(shop)
    action(shop, day)
    return back(shop)


@not_indexed
@login_required(login_url="shops")
@require_POST
def break_now(request, shop):
    """«Closed from today, I open again when I say so»: one tap."""
    def act(found, day):
        reason = request.POST.get("reason", "")
        vacation.close_now(found, reason if reason in Closure.Reason.values else Closure.Reason.OTHER, day)
        messages.success(request, _("Saved: the shop says it is closed."))
    return quick(request, shop, act)


@not_indexed
@login_required(login_url="shops")
@require_POST
def break_over(request, shop):
    """«I am back»: the closure in course ends today, one still to come is cancelled."""
    def act(found, day):
        done = vacation.reopen_today(found, day)
        if done == "reopened":
            messages.success(request, _("Welcome back! The shop says it is open again."))
        elif done == "cancelled":
            messages.success(request, _("Cancelled: the shop is open."))
    return quick(request, shop, act)


@not_indexed
@login_required(login_url="shops")
@require_POST
def break_seen(request, shop):
    """The owner closes the «welcome back» card."""
    shop = owned(request, shop)
    try:
        pk = int(request.POST.get("closure", ""))
    except ValueError:
        return HttpResponseBadRequest()
    vacation.mark_seen(shop, pk)
    return back(shop)
