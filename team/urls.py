"""Everything of team access lives under /team/, which Caddy leaves open on a site under construction when
TEAM_ACCESS=on (Caddyfile, `team-paths-on`). The names and the shapes of these addresses are what a community's own
under-construction page calls: docs/features/team-access.md. Caddy opens each of them by its exact path (Caddyfile,
`team-open`), and config/quiet.py keeps what follows /team/join/ and /team/login/ out of the logs.

The volunteers' area (TEAM_AREA, team/area.py) is the other addresses: /team/ and what follows it below. Caddy opens
none of them from outside on a site under construction: they are reached with a pass, like the rest of the site."""

from django.urls import path

from . import area, views

urlpatterns = [
    path("login/", views.login, name="team_login"),
    path("login/<str:k1>/", views.poll, name="team_poll"),
    path("poll/", views.own_poll, name="team_own_poll"),
    path("auth/", views.auth, name="team_auth"),
    path("check/", views.check, name="team_check"),
    path("join.js", views.script, name="team_script"),
    path("join/<str:token>/", views.join, name="team_join"),
    # The volunteers' area
    path("", area.sign_in, name="team_area"),
    path("today/", area.today, name="team_today"),
    path("help/", area.help_requests, name="team_help"),
    path("help/<int:number>/solved/", area.solve, name="team_help_solved"),
    path("help/<int:number>/waiting/", area.reopen, name="team_help_waiting"),
    path("shops/", area.new_shops, name="team_shops"),
    path("shops/<uuid:shop>/approve/", area.approve, name="team_approve"),
    path("osm/", area.osm_requests, name="team_osm"),
    path("osm/<int:number>/done/", area.osm_done, name="team_osm_done"),
    path("changes/", area.account_changes, name="team_changes"),
    path("me/", area.me, name="team_me"),
    path("me/end/", area.end, name="team_end"),
]
