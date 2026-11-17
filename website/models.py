from django.db import models


class BlossomVisit(models.Model):
    """A visitor of the Blossom server on a day, with no personal data: `who` is a keyed hash of the network and the
    User-Agent, with a key that changes every month (website/blossom_visits.py). Kept 90 days."""

    month = models.CharField(max_length=7)  # the month of the key: two months never compare
    who = models.CharField(max_length=64)
    day = models.DateField()
    kind = models.CharField(max_length=4)  # "blob" or "list"

    class Meta:
        constraints = [models.UniqueConstraint(fields=["month", "who", "day", "kind"], name="blossom_visit_once")]
        indexes = [models.Index(fields=["day"])]


class BlossomMilestone(models.Model):
    """A number of Blossom users the moderators were told about, once."""

    users = models.PositiveIntegerField(unique=True)
    reached = models.DateTimeField(auto_now_add=True)


class BlossomRetired(models.Model):
    """A file the community retired (website/blossom_retire.py): when it was first seen in the folder of the retired
    files, and the mirror found to have it. The row outlives the file, to say «gone» or where it is now."""

    sha256 = models.CharField(max_length=64, unique=True)
    path = models.CharField(max_length=500)  # inside the folder of the retired files
    size = models.BigIntegerField()
    seen = models.DateTimeField()
    mirror = models.CharField(max_length=300, blank=True)
    checked = models.DateTimeField(null=True, blank=True)
