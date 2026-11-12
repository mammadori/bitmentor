"""The days of the community's calendar, as a shop lives them: today, the business day that runs past midnight. A day is a calendar day, never 24 hours: the day the clocks change is 23 or
25 hours long."""
from datetime import timedelta

from django.utils import timezone

# A shift that runs past midnight is still the day before until this hour of the morning
SHIFT_NIGHT_HOUR = 5


def today(now=None):
    """Today in the community's time zone."""
    return timezone.localtime(now or timezone.now(), timezone.get_default_timezone()).date()


def shift_day(now=None):
    """The business day: today, but until SHIFT_NIGHT_HOUR in the morning still the day before, so a shift that passes
    midnight still belongs to the day it started."""
    now = now or timezone.now()
    local = timezone.localtime(now)
    return today(now) - timedelta(days=1) if local.hour < SHIFT_NIGHT_HOUR else today(now)

