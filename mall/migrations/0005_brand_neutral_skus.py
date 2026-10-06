from django.db import migrations, models
from django.db.models.functions import Lower


def populate_skus(apps, schema_editor):
    Part = apps.get_model('mall', 'Part')
    used = set()
    for part in Part.objects.using(schema_editor.connection.alias).order_by('id').iterator():
        sku = (part.name.strip().upper() or f'MP-{part.id.hex[:12]}')[:200]
        original = sku
        suffix = 0
        while sku in used:
            suffix += 1
            sku = f'{original[:155]}-{part.id}-{suffix}'
        used.add(sku)
        Part.objects.using(schema_editor.connection.alias).filter(pk=part.pk).update(sku=sku)


class Migration(migrations.Migration):
    dependencies = [('mall', '0004_spanish_role_names')]
    operations = [
        migrations.RenameField(model_name='part', old_name='brand', new_name='legacy_brand'),
        migrations.AlterField(model_name='part', name='legacy_brand', field=models.CharField(blank=True, default='', editable=False, max_length=120, verbose_name='marca histórica')),
        migrations.AddField(model_name='part', name='sku', field=models.CharField(max_length=200, null=True, verbose_name='SKU interno')),
        migrations.RunPython(populate_skus, migrations.RunPython.noop),
        migrations.AlterField(model_name='part', name='sku', field=models.CharField(max_length=200, unique=True, verbose_name='SKU interno')),
        migrations.AddConstraint(model_name='part', constraint=models.UniqueConstraint(Lower('sku'), name='unique_sku_case_insensitive')),
        migrations.AlterField(model_name='part', name='name', field=models.CharField(blank=True, default='', max_length=200, verbose_name='nombre')),
        migrations.AlterField(model_name='partcode', name='brand', field=models.CharField(blank=True, default='', max_length=120, verbose_name='marca opcional')),
    ]
