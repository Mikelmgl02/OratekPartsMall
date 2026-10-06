from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def forwards(apps, schema_editor):
    Code = apps.get_model('mall', 'PartCode')
    Code.objects.using(schema_editor.connection.alias).filter(ref_type='alias').update(ref_type='unknown')
    Code.objects.using(schema_editor.connection.alias).filter(ref_type='manufacturer').update(ref_type='company')


def backwards(apps, schema_editor):
    Code = apps.get_model('mall', 'PartCode')
    Code.objects.using(schema_editor.connection.alias).filter(ref_type='unknown').update(ref_type='alias')
    Code.objects.using(schema_editor.connection.alias).filter(ref_type='company').update(ref_type='manufacturer')


class Migration(migrations.Migration):
    dependencies = [('mall', '0024_fast_category_suggestions'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name='OEMLookupCache', fields=[
            ('fingerprint', models.CharField(max_length=64, primary_key=True, serialize=False)),
            ('result', models.JSONField(default=dict)), ('created_at', models.DateTimeField(auto_now_add=True)),
        ]),
        migrations.RenameField(model_name='partcode', old_name='kind', new_name='ref_type'),
        migrations.RunPython(forwards, backwards),
        migrations.AlterField(model_name='partcode', name='ref_type', field=models.CharField(choices=[('unknown', 'SIN CLASIFICAR'), ('oem', 'OEM'), ('company', 'EMPRESA / FABRICANTE')], db_index=True, default='unknown', max_length=20)),
        migrations.CreateModel(name='CatalogIdentityChange', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('previous_sku', models.CharField(max_length=200)), ('sku', models.CharField(max_length=200)),
            ('reference', models.JSONField(default=dict)), ('created_at', models.DateTimeField(auto_now_add=True)),
            ('actor', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ('part', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='identity_changes', to='mall.part')),
        ]),
    ]
