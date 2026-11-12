"""The merchant's pages of «Something wrong?» (shops/helpdesk.py): the symptoms, what the site knows for each one, «Look
again» (the checks that ask the Lightning provider), and «Ask the association for help». Only the shop's owner; the pages
that change anything are POSTs with the CSRF token and a limit of requests; nothing here touches the register or where
the money goes. The staff and the sessions of /admin/ are visitors here (config/guard.py)."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from config import limits, ratelimit
from config.community import community

from . import helpdesk
from .views import not_indexed

# «Look again» asks the shop's Lightning provider: few per shop and hour, and some more per person
LOOKS_PER_SHOP = 6
LOOKS_PER_USER = 30
LOOK_WINDOW = 3600
# A request makes a diagnosis and writes to the moderators' chat: a stop for a script, beyond the shop's daily limit
ASKS_PER_USER = 30
ASK_WINDOW = 3600
# Messages to the moderators' chat: all the shops together, and each network. Beyond them the request is saved and only on
# the dashboard (and the merchant is told so): a chat nobody reads is worse than a list
MESSAGES_IN_ALL = 20
MESSAGES_PER_IP = 6
MESSAGE_WINDOW = 3600


def owned(request, shop):
    return get_object_or_404(request.user.shops, pk=shop)


def kind_or_404(key):
    found = helpdesk.kind(key)
    if found is None:
        raise Http404
    return found


def page(request, shop, found, look, form=None, status=200):
    """The page of a symptom: what the site knows (`look`), the steps, the form to ask."""
    form = form or helpdesk.AskForm(initial={"symptom": found.key}, shop=shop)
    return render(request, "shops/help_symptom.html", {
        "shop": shop, "kind": found, "look": look, "form": form, "open_request": helpdesk.open_request(shop),
        "can_look": shop.has_register and bool(found.deep), "community_email": community()["email"]}, status=status)


@not_indexed
@login_required(login_url="shops")
def home(request, shop):
    """The symptoms as big buttons, and the requests already made."""
    shop = owned(request, shop)
    return render(request, "shops/help_home.html", {
        "shop": shop, "kinds": list(helpdesk.KINDS.values()), "requests": helpdesk.history(shop)})


@not_indexed
@login_required(login_url="shops")
def symptom(request, shop, slug):
    """What the site knows about the shop for this symptom, from the database only: nothing slow while it loads."""
    shop = owned(request, shop)
    found = kind_or_404(slug)
    return page(request, shop, found, helpdesk.look(shop, found))


@not_indexed
@login_required(login_url="shops")
@require_POST
def again(request, shop, slug):
    """«Look again»: the same page with the checks that ask the Lightning provider, which have
    their own time limits. The answer is the page itself, not a redirect: a refresh asks again, within the limits."""
    shop = owned(request, shop)
    found = kind_or_404(slug)
    if not shop.has_register or not found.deep:
        return redirect("help_symptom", shop.pk, found.key)
    if (ratelimit.over_limit(f"help-look-shop:{shop.pk}", LOOKS_PER_SHOP, LOOK_WINDOW)
            or ratelimit.over_limit(f"help-look-user:{request.user.pk}", LOOKS_PER_USER, LOOK_WINDOW)):
        messages.error(request, _("You have looked many times in a short while: wait a few minutes. Meanwhile, you "
                                  "can ask the association for help."))
        return redirect("help_symptom", shop.pk, found.key)
    return page(request, shop, found, helpdesk.look(shop, found, deep=True))


@not_indexed
@login_required(login_url="shops")
@require_POST
def ask(request, shop):
    """The request: a new HelpRequest with the diagnosis, the message to the moderators, and the answer to the merchant,
    who is told whether the message went out."""
    shop = owned(request, shop)
    form = helpdesk.AskForm(request.POST, shop=shop)
    found = helpdesk.kind(request.POST.get("symptom", ""))
    if found is None:
        raise Http404
    if not form.is_valid():
        return page(request, shop, found, helpdesk.look(shop, found), form=form, status=200)
    if ratelimit.over_limit(f"help-ask-user:{request.user.pk}", ASKS_PER_USER, ASK_WINDOW):
        messages.error(request, _("Too many requests in a short time: try again in a while."))
        return redirect("help_symptom", shop.pk, found.key)
    data = form.cleaned_data
    try:
        row, state = helpdesk.create(shop, found.key, data["note"], data["channel"], request.user)
    except helpdesk.TooMany:
        messages.error(request, _("You have already asked {count} times today, and the association has your "
                                  "requests: we answer them one by one. If it is urgent, write to {email}.").format(
                                      count=limits.get("help_requests_per_shop_day"), email=community()["email"]))
        return redirect("help_symptom", shop.pk, found.key)
    except helpdesk.Full:
        messages.error(request, _("The association cannot take more requests here right now. Please write to {email}.").format(
            email=community()["email"]))
        return redirect("help_symptom", shop.pk, found.key)
    if state == helpdesk.ADDED:
        messages.success(request, _("You asked a moment ago: we added what you wrote to your request."))
        return redirect("help_home", shop.pk)
    if state == helpdesk.AGAIN:
        messages.success(request, _("You asked a moment ago, and we have your request."))
        return redirect("help_home", shop.pk)
    # Both counted, so that each limit sees every request
    spent = [ratelimit.over_limit("help-moderators", MESSAGES_IN_ALL, MESSAGE_WINDOW),
             ratelimit.over_limit(f"help-moderators-ip:{ratelimit.client_ip(request)}", MESSAGES_PER_IP, MESSAGE_WINDOW)]
    helpdesk.finish(row, notify=not any(spent))
    if row.delivery == helpdesk.SENT:
        messages.success(request, _("We have your request. The association has been told, and has what the site knows "
                                    "about your shop in front of it."))
    else:
        messages.success(request, _("We have your request and it is saved: the association sees it in its list. The "
                                    "message to them did not go out right now, so if it is urgent write to {email}.")
                         .format(email=community()["email"]))
    return redirect("help_home", shop.pk)
