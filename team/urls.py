"""Everything of team access lives under /team/, which Caddy leaves open on a site under construction when
TEAM_ACCESS=on (Caddyfile, `team-paths-on`). The names and the shapes of these addresses are what a community's own
under-construction page calls: docs/features/team-access.md. Caddy opens each of them by its exact path (Caddyfile,
`team-open`), and config/quiet.py keeps what follows /team/join/ and /team/login/ out of the logs."""
from django.urls import path

from . import views

urlpatterns = [
    path("login/", views.login, name="team_login"),
    path("login/<str:k1>/", views.poll, name="team_poll"),
    path("poll/", views.own_poll, name="team_own_poll"),
    path("auth/", views.auth, name="team_auth"),
    path("check/", views.check, name="team_check"),
    path("join.js", views.script, name="team_script"),
    path("join/<str:token>/", views.join, name="team_join"),
]
