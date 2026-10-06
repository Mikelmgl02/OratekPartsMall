from django.db import migrations


ROLE_NAMES = [
    ('supplier_retail', 'Retail supplier', 'Proveedor minorista'),
    ('supplier_wholesale', 'Wholesale supplier', 'Proveedor mayorista'),
    ('client_business', 'Business client', 'Cliente empresarial'),
    ('client_walkin', 'Walk-in client', 'Cliente particular'),
]


def translate_roles(apps, schema_editor):
    Role = apps.get_model('mall', 'Role')
    for code, english, spanish in ROLE_NAMES:
        Role.objects.using(schema_editor.connection.alias).filter(code=code, name=english).update(name=spanish)


def restore_roles(apps, schema_editor):
    Role = apps.get_model('mall', 'Role')
    for code, english, spanish in ROLE_NAMES:
        Role.objects.using(schema_editor.connection.alias).filter(code=code, name=spanish).update(name=english)


class Migration(migrations.Migration):
    dependencies = [('mall', '0003_spanish_labels')]
    operations = [migrations.RunPython(translate_roles, restore_roles)]
