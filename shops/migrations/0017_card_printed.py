from django.db import migrations, models
from django.db.models import F


def from_the_sign(apps, schema_editor):
    """The card for the till was in the step of the sign: a shop that ticked that one printed both."""
    Shop = apps.get_model("shops", "Shop")
    Shop.objects.filter(sign_printed__isnull=False).update(card_printed=F("sign_printed"))


class Migration(migrations.Migration):
    dependencies = [
        ("shops", "0016_delivery_label"),
    ]

    operations = [
        migrations.AddField(
            model_name="shop",
            name="card_printed",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(from_the_sign, migrations.RunPython.noop),
    ]
