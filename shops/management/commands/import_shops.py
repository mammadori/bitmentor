"""Imports shops not yet claimed from a JSON file: running it again updates, it does not duplicate."""
import json
import math
from datetime import date

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import URLValidator
from django.db import transaction

from shops.details import normalize_vat, vat_doubt
from shops.models import Shop

# The business name and the VAT number go in like the other fields, for shops that no owner has taken yet. What customers
# see of them is the owner's choice (shops/details.py): a shop loaded here starts with the website and the address shown
FIELDS = ("name", "description", "address", "city", "website", "email", "phone", "legal_name", "vat_number", "discount",
          "listed", "lightning", "latitude", "longitude", "closed_on")


def plain_website(value):
    """The file comes from outside and the website goes into a link: no `javascript:`."""
    try:
        URLValidator(schemes=["http", "https"])(value)
    except ValidationError:
        return False
    return True


def finite(text):
    """`json` accepts NaN and Infinity, and 1e999 becomes infinite: in the map JSON they would be text no browser reads,
    and the map would stay empty for everyone."""
    value = float(text)
    if not math.isfinite(value):
        raise CommandError(f"Not a valid number in the file: {text}")
    return value


def closing_day(row):
    """A historic shop has an ISO date: a wrong date stops the import instead of vanishing silently."""
    try:
        return date.fromisoformat(row["closed_on"])
    except (TypeError, ValueError):
        raise CommandError(f"closed_on is not a YYYY-MM-DD date for {row['external_id']}: {row['closed_on']!r}") from None


def checked(row, values, new):
    """Every row goes through the model's own checks: a 150% discount, a latitude of 200 or an email that is not one
    stop the import, with the row and the field. Values come back converted ("5" becomes 5). A new shop needs everything
    the model asks for, the name first; an existing one only the fields of the row, so a row with external_id and
    closed_on still makes it historic."""
    shop = Shop(external_id=str(row["external_id"]), **values)
    exclude = [] if new else [field.name for field in Shop._meta.fields if field.name not in values]
    try:
        shop.full_clean(exclude=exclude, validate_unique=False, validate_constraints=False)
        # "NaN" written as text becomes nan, which is neither below nor above the limits: in the map JSON it would break it
        for field in ("latitude", "longitude"):
            if getattr(shop, field) is not None and not math.isfinite(getattr(shop, field)):
                raise ValidationError({field: "Not a finite number."})
    except ValidationError as error:
        problems = "; ".join(f"{field}: {' '.join(messages)}" for field, messages in error.message_dict.items())
        raise CommandError(f"Shop {row['external_id']} not imported: {problems}") from None
    return {field: getattr(shop, field) for field in values}


class Command(BaseCommand):
    help = "Imports shops from a JSON list with external_id; closed_on (YYYY-MM-DD) makes them historic"

    def add_arguments(self, parser):
        parser.add_argument("path")

    def handle(self, *args, path, **options):
        with open(path) as file:
            rows = json.load(file, parse_float=finite, parse_constant=finite)
        # All or nothing: an error halfway through the file does not leave half an import
        with transaction.atomic():
            for row in rows:
                values = {field: row[field] for field in FIELDS if row.get(field) is not None}
                if "website" in values and not plain_website(values["website"]):
                    del values["website"]
                if "closed_on" in values:
                    values["closed_on"] = closing_day(row)
                if "vat_number" in values:
                    # As the shop form keeps it; a number that looks wrong goes in as written, with a generic word here:
                    # never the number, nor the letters it starts with
                    values["vat_number"] = normalize_vat(values["vat_number"])
                    if vat_doubt(values["vat_number"]):
                        self.stdout.write(f"Shop {row['external_id']}: the VAT number may have a mistake: look at it in the admin.")
                values = checked(row, values, new=not Shop.objects.filter(external_id=str(row["external_id"])).exists())
                shop, created = Shop.objects.get_or_create(external_id=str(row["external_id"]), defaults=values)
                # A shop moved from another site belongs to its owner even while it waits for them
                if not created and shop.owner_id is None and not shop.moved_from:
                    Shop.objects.filter(pk=shop.pk).update(**values)
                elif not created and "closed_on" in values:
                    # The association decides "historic" even for a claimed shop: the rest belongs to the owner
                    Shop.objects.filter(pk=shop.pk).update(closed_on=values["closed_on"])
        self.stdout.write(f"{Shop.objects.filter(owner=None).count()} unclaimed shops")
