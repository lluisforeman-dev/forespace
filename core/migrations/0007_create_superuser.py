from django.db import migrations
from django.contrib.auth.hashers import make_password


def create_superuser(apps, schema_editor):
    User = apps.get_model('auth', 'User')
    if not User.objects.filter(username='lluis').exists():
        User.objects.create(
            username='lluis',
            password=make_password('1111'),
            is_staff=True,
            is_superuser=True,
            is_active=True,
            email='',
            first_name='',
            last_name='',
        )


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0006_relation_current_view'),
    ]

    operations = [
        migrations.RunPython(create_superuser, migrations.RunPython.noop),
    ]
