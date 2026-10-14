# Written by hand as the expand migration of inventory refactor stage S3 (plan §1.2.1, §3 S3): a variant (SupplierItem) hangs from a
# Reference of its line. Every new column is nullable or has a database default, so the code before S3 keeps inserting and updating
# items (they get no Reference until python -m mall.refactor.s3_variants or the S3 code attaches one):
# - reference (NULL, PROTECT; no index of its own: the (reference, part) index below serves it and 0078's composite foreign key);
# - codigo_pendiente and brand_pendiente (NULL): a supplier's code change that waits for review (rule 5b, OD7);
# - material, origen, garantia, calidad, condicion ('') and unidades_por_empaque (NULL), the variant's own data (rule 10, S3.2), and
#   field_sources ({});
# - the indexes (reference, part) and (supplier, codigo): there is no codigo index today.
# Reference gains UNIQUE (id, part), which the composite foreign key of 0078 points at. Today's rows satisfy it (id is unique).
# The tighten migration 0078 re-attaches what moved since the backfill and adds the composite foreign key and the two CHECKs.

import django.db.models.deletion
from django.db import migrations, models

from mall.refactor.migration_ops import with_lock_timeout


class Migration(migrations.Migration):

    dependencies = [
        ('mall', '0076_line_main_reference_checks'),
    ]

    operations = with_lock_timeout([
        migrations.AddField(
            model_name='supplieritem',
            name='reference',
            field=models.ForeignKey(blank=True, db_index=False, help_text='El código de la línea del que cuelga este artículo.', null=True,
                                    on_delete=django.db.models.deletion.PROTECT, related_name='variants', to='mall.reference',
                                    verbose_name='referencia'),
        ),
        migrations.AddField(
            model_name='supplieritem',
            name='codigo_pendiente',
            field=models.CharField(blank=True, help_text='El código nuevo que envió el proveedor y que espera revisión.', max_length=120,
                                   null=True, verbose_name='código pendiente'),
        ),
        migrations.AddField(
            model_name='supplieritem',
            name='brand_pendiente',
            field=models.CharField(blank=True, help_text='La marca nueva que envió el proveedor y que espera revisión.', max_length=120,
                                   null=True, verbose_name='marca pendiente'),
        ),
        migrations.AddField(
            model_name='supplieritem',
            name='material',
            field=models.CharField(blank=True, db_default='', default='', max_length=120, verbose_name='material'),
        ),
        migrations.AddField(
            model_name='supplieritem',
            name='origen',
            field=models.CharField(blank=True, db_default='', default='', max_length=120, verbose_name='país de origen'),
        ),
        migrations.AddField(
            model_name='supplieritem',
            name='garantia',
            field=models.CharField(blank=True, db_default='', default='', max_length=120, verbose_name='garantía'),
        ),
        migrations.AddField(
            model_name='supplieritem',
            name='calidad',
            field=models.CharField(blank=True, choices=[('original', 'Original'), ('oes', 'OES (equipo original)'), ('alternativo', 'Alternativo')],
                                   db_default='', default='', max_length=12, verbose_name='calidad'),
        ),
        migrations.AddField(
            model_name='supplieritem',
            name='condicion',
            field=models.CharField(blank=True, choices=[('nuevo', 'Nuevo'), ('remanufacturado', 'Remanufacturado')], db_default='', default='',
                                   max_length=16, verbose_name='condición'),
        ),
        migrations.AddField(
            model_name='supplieritem',
            name='unidades_por_empaque',
            field=models.PositiveSmallIntegerField(blank=True, null=True, verbose_name='unidades por empaque'),
        ),
        migrations.AddField(
            model_name='supplieritem',
            name='field_sources',
            field=models.JSONField(blank=True, db_default={}, default=dict, verbose_name='origen de cada campo'),
        ),
        migrations.AddIndex(
            model_name='supplieritem',
            index=models.Index(fields=['reference', 'part'], name='variant_reference_line_idx'),
        ),
        migrations.AddIndex(
            model_name='supplieritem',
            index=models.Index(fields=['supplier', 'codigo'], name='variant_supplier_codigo_idx'),
        ),
        migrations.AddConstraint(
            model_name='reference',
            constraint=models.UniqueConstraint(fields=('id', 'part'), name='reference_id_line'),
        ),
    ])
