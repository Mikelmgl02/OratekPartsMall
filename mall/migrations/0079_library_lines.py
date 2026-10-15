# Written by hand as the expand migration of inventory refactor stage S4 (plan §1.2.1, §3 S4): library numbers become References, and
# the numbers no product carries become lines in referencia, which may share a codigo across makes.
#
# - Part: UNIQUE(sku) and unique_sku_case_insensitive give way to line_codigo_unique_catalog (Lower(sku) unique among publicada and
#   retirada lines), which today's data satisfies (every codigo was unique); sku keeps a plain index, and Lower(sku) one for the
#   lookups of a codigo in every estado.
# - OEMCrossReference, OEMCrossReferenceRetired and PartOEMLink: in the database a nullable ref_id joins them to mall_partcode (NO
#   ACTION) and reference_id loses its NOT NULL; in the state reference_id becomes legacy_reference (db_column='reference_id') and the
#   new reference points to Reference (db_column='ref_id', PROTECT). Nothing is renamed in the database, so the code before S4 keeps
#   reading and writing reference_id. Their unique constraints stay on reference_id until 0080.
#
# python -m mall.refactor.s4_library fills ref_id (and the References, the referencia lines and their sources) before landing and once
# more after; the tighten migration 0080_library_lines_checks catches up, makes ref_id NOT NULL and moves the unique constraints to it.

import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q
from django.db.models.functions import Lower

from mall.refactor.migration_ops import with_lock_timeout

LEGACY = {  # model -> (related name of the legacy relation, related name of the new one on Reference, unique constraint)
    'oemcrossreference': ('legacy_cross_references', 'cross_references', ['legacy_reference', 'brand', 'number'], 'oem_cross_reference_unique'),
    'oemcrossreferenceretired': ('legacy_retired_cross_references', 'retired_cross_references', ['legacy_reference', 'brand', 'number'],
                                 'oem_cross_reference_retired_unique'),
    'partoemlink': ('legacy_part_links', 'part_links', ['part', 'legacy_reference', 'source'], 'part_oem_link_unique'),
}


def split(model):
    legacy_related, related, fields, constraint = LEGACY[model]
    return migrations.SeparateDatabaseAndState(
        database_operations=[
            # reference_id loses its NOT NULL (a number added after S4 has no OEMReference), its foreign key left as it is ...
            migrations.RunSQL(f'ALTER TABLE mall_{model} ALTER COLUMN reference_id DROP NOT NULL',
                              f'ALTER TABLE mall_{model} ALTER COLUMN reference_id SET NOT NULL'),
            # ... and ref_id joins the row to its Reference (the field's name here is the database's only: the state below names it).
            migrations.AddField(model_name=model, name='ref', field=models.ForeignKey(
                null=True, blank=True, db_column='ref_id', on_delete=django.db.models.deletion.PROTECT, related_name='+',
                to='mall.reference')),
        ],
        state_operations=[
            migrations.RemoveConstraint(model_name=model, name=constraint),
            migrations.RenameField(model_name=model, old_name='reference', new_name='legacy_reference'),
            migrations.AlterField(model_name=model, name='legacy_reference', field=models.ForeignKey(
                blank=True, null=True, db_column='reference_id', on_delete=django.db.models.deletion.CASCADE, related_name=legacy_related,
                to='mall.oemreference', verbose_name='referencia OEM (antes de S4)')),
            migrations.AddField(model_name=model, name='reference', field=models.ForeignKey(
                blank=True, null=True, db_column='ref_id', on_delete=django.db.models.deletion.PROTECT, related_name=related,
                to='mall.reference', verbose_name='referencia OEM')),
            migrations.AddConstraint(model_name=model, constraint=models.UniqueConstraint(fields=fields, name=constraint)),
        ],
    )


class Migration(migrations.Migration):

    dependencies = [
        ('mall', '0078_variant_reference_checks'),
    ]

    operations = with_lock_timeout([
        migrations.RemoveConstraint(model_name='part', name='unique_sku_case_insensitive'),
        migrations.AlterField(model_name='part', name='sku', field=models.CharField(db_index=True, max_length=200, verbose_name='SKU interno')),
        migrations.AddConstraint(model_name='part', constraint=models.UniqueConstraint(
            Lower('sku'), condition=Q(estado__in=['publicada', 'retirada']), name='line_codigo_unique_catalog')),
        migrations.AddIndex(model_name='part', index=models.Index(Lower('sku'), name='line_codigo_lower_idx')),
        split('oemcrossreference'),
        split('oemcrossreferenceretired'),
        split('partoemlink'),
    ])
