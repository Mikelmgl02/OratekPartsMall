import re
from django.db import migrations, models


def mark_verified_main_oems(apps, schema_editor):
    Part = apps.get_model('mall', 'Part')
    Code = apps.get_model('mall', 'PartCode')
    alias = schema_editor.connection.alias
    normalize = lambda value: re.sub(r'[^A-Z0-9]', '', value.upper())
    ids = set()
    for row in Code.objects.using(alias).filter(ref_type='oem').values(
            'part_id', 'part__sku', 'code', 'brand', 'reference_source').iterator():
        if (row['brand'].strip() and row['reference_source'].strip()
                and normalize(row['code']) == normalize(row['part__sku'])):
            ids.add(row['part_id'])
    ids = list(ids)
    for offset in range(0, len(ids), 500):
        Part.objects.using(alias).filter(pk__in=ids[offset:offset + 500]).update(is_OEM=True)


class Migration(migrations.Migration):
    dependencies = [('mall', '0025_oem_reference_identity')]
    operations = [
        migrations.AddField(model_name='part', name='is_OEM', field=models.BooleanField(
            default=False, db_index=True, verbose_name='El SKU principal es OEM')),
        migrations.RunPython(mark_verified_main_oems, migrations.RunPython.noop),
        migrations.AddField(model_name='matchingqueue', name='stage', field=models.CharField(
            max_length=30, blank=True, default='')),
        migrations.AddField(model_name='matchingqueue', name='started_at', field=models.DateTimeField(null=True)),
    ]
