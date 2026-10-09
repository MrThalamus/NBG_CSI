from django.db import migrations


def seed_mutex(apps, schema_editor):
    ProcessingMutex = apps.get_model("production", "ProcessingMutex")
    ProcessingMutex.objects.get_or_create(name="production")


class Migration(migrations.Migration):
    dependencies = [
        ("production", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_mutex, migrations.RunPython.noop),
    ]
