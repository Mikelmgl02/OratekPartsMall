from django.db import migrations

def seed_roles(apps, schema_editor):
    Role = apps.get_model('mall', 'Role')
    for code, name, capability in [
        ('supplier_retail', 'Retail supplier', 'supplier'),
        ('supplier_wholesale', 'Wholesale supplier', 'supplier'),
        ('client_business', 'Business client', 'client'),
        ('client_walkin', 'Walk-in client', 'client'),
    ]:
        Role.objects.get_or_create(code=code, defaults={'name': name, 'capability': capability})

class Migration(migrations.Migration):
    dependencies = [('mall', '0001_initial')]
    operations = [migrations.RunPython(seed_roles, migrations.RunPython.noop)]
