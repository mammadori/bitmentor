"""From «awaiting approval» (a flag set on new shops) to «approved on» (empty until someone approves): no shop goes on
the map without an approval. Every shop that was not waiting is approved as of its creation, by "before approvals"."""
from django.db import migrations, models


def approve_existing(apps, schema_editor):
    apps.get_model("shops", "Shop").objects.filter(awaiting_approval=False).update(
        approved_on=models.F("created"), approved_by="before approvals")


def back(apps, schema_editor):
    apps.get_model("shops", "Shop").objects.filter(approved_on=None).update(awaiting_approval=True)


class Migration(migrations.Migration):
    dependencies = [("shops", "0002_awaiting_approval")]

    operations = [
        migrations.AddField("shop", "approved_on", models.DateTimeField(
            blank=True, null=True, verbose_name="approved on",
            help_text="Empty: the shop waits for an approval, off maps and lists. The register works all the same.")),
        migrations.AddField("shop", "approved_by", models.CharField(blank=True, db_default="", max_length=150,
                                                                     verbose_name="approved by")),
        migrations.RunPython(approve_existing, back),
        # awaiting_approval stays one more release (shops/models.py): 0.1.2 put back on this database still reads it
        migrations.AlterField("shop", "awaiting_approval", models.BooleanField(db_default=False, default=False,
                                                                                editable=False)),
    ]
