"""«Your shop on OpenStreetMap» for the merchant (shops/osm.py, shops/osm_request.py): what the site knows of the shop's
entry there, the steps to do it alone with the editor already on the right place, and one tap that asks the association to
do it. Only the shop's owner; the one page that changes anything is a POST with the CSRF token and a limit of requests.
Nothing here writes to OpenStreetMap, and no server is asked while the page loads: it shows what the last monthly reading
found. The staff and the sessions of /admin/ are visitors here (config/guard.py)."""
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_POST

from config import ratelimit
from config.community import community

from . import osm, osm_request
from .views import not_indexed

# Requests a person can send in an hour, on top of the shop's own daily limit; and the messages to the moderators' chat,
# all the shops together and each network: beyond them the request is saved and only in /admin/ (the merchant is told so)
ASKS_PER_USER = 10
ASK_WINDOW = 3600
MESSAGES_IN_ALL = 20
MESSAGES_PER_IP = 6
MESSAGE_WINDOW = 3600


class AskForm(forms.Form):
    """The tick «The association does it for me»: nothing is sent without it."""
    association = forms.BooleanField(required=True, label=gettext_lazy("The association does it for me"),
                                     error_messages={"required": gettext_lazy("Tick the box to send the request.")})


def owned(request, shop):
    return get_object_or_404(request.user.shops, pk=shop)


@not_indexed
@login_required(login_url="shops")
def home(request, shop, form=None, status=200):
    """The page: what the site knows, the steps, the tick. 404 for a community that does not use OpenStreetMap."""
    shop = owned(request, shop)
    state = osm.page(shop)
    if state is None:
        raise Http404
    return render(request, "shops/osm.html", {
        "shop": shop, "osm": state, "form": form or AskForm(), "asked": osm_request.panel(shop),
        "community_email": community()["email"]}, status=status)


@not_indexed
@login_required(login_url="shops")
@require_POST
def ask(request, shop):
    """The request: a new OsmRequest and the message to the moderators, and the answer to the merchant, who is told whether
    the message went out."""
    shop = owned(request, shop)
    state = osm.page(shop)
    if state is None:
        raise Http404
    form = AskForm(request.POST)
    if not form.is_valid():
        return home(request, shop.pk, form=form)
    if not state["can_ask"]:
        messages.error(request, _("There is nothing to send for this shop right now."))
        return redirect("osm_home", shop.pk)
    if ratelimit.over_limit(f"osm-ask-user:{request.user.pk}", ASKS_PER_USER, ASK_WINDOW):
        messages.error(request, _("Too many requests in a short time: try again in a while."))
        return redirect("osm_home", shop.pk)
    try:
        row, new = osm_request.create(shop, request.user)
    except osm_request.TooMany:
        messages.error(request, _("You have already asked {count} times today, and the association has your requests: "
                                  "we do them one by one. If it is urgent, write to {email}.").format(
                                      count=osm_request.ASK_PER_DAY, email=community()["email"]))
        return redirect("osm_home", shop.pk)
    if not new:
        messages.success(request, _("You asked a moment ago, and we have your request."))
        return redirect("osm_home", shop.pk)
    spent = [ratelimit.over_limit("osm-moderators", MESSAGES_IN_ALL, MESSAGE_WINDOW),
             ratelimit.over_limit(f"osm-moderators-ip:{ratelimit.client_ip(request)}", MESSAGES_PER_IP, MESSAGE_WINDOW)]
    osm_request.finish(row, notify=not any(spent))
    messages.success(request, _("We have your request. A person of the association does it by hand, and you see here when it "
                                "is done."))
    return redirect("osm_home", shop.pk)
