from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class TeamConfig(AppConfig):
    """Team access: optional, off unless TEAM_ACCESS=on (docs/features/team-access.md). The tables always exist; the
    switch decides whether anything uses them (team/access.py)."""
    name = "team"
    verbose_name = _("Team access")
