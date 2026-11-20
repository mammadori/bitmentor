from django.apps import AppConfig
from django.core import checks
from django.utils.translation import gettext_lazy as _


def wrong_team_area(app_configs, **kwargs):
    """A value of the `team_area` block that is not true or false counts as false (the volunteers' area does not show the
    names of the staff registers): `manage.py check` and the log of `web` say so. A warning, like the others."""
    from .area import problems

    return [
        checks.Warning(problem, hint="The block is documented in examples/community/community.yml", id="team.W001")
        for problem in problems()
    ]


class TeamConfig(AppConfig):
    """Team access: optional, off unless TEAM_ACCESS=on (docs/features/team-access.md). The tables always exist; the
    switch decides whether anything uses them (team/access.py)."""

    name = "team"
    verbose_name = _("Team access")

    def ready(self):
        checks.register(wrong_team_area)
