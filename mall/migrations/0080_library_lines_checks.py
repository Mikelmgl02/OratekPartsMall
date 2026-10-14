# Written by hand as the tighten migration of inventory refactor stage S4 (plan §1.2.4, §3 S4), applied from the worktree after the
# backfill (python -m mall.refactor.s4_library --apply, before landing and once more after) and before its commit lands.
#
# Its RunPython first catches up, idempotently, what has one right answer: an aftermarket code, tombstone or SKU link written by the
# code before S4 (reference_id only) takes ref_id = the Reference whose uid is its reference_id, as the backfill re-points them, and
# two rows one key would then hold (the same number, brand and code; the same line, number and source) become one: the older row
# keeps, the citations add up (a tombstone keeps the older). It stops, with counts, only for what needs a person: a number of the OEM
# table without its Reference (run the backfill), or a row still without ref_id. Then ref_id becomes NOT NULL and the uniqueness of
# these rows moves to it (the old constraints on reference_id go). OEMReference and OEMReferenceSource are frozen from here (nothing
# writes them any more); their legacy columns leave in S10. On an empty database it changes nothing.
#
# Rolling S4 back (catalog frozen): reverse this migration first (python manage.py migrate mall 0079_library_lines, from the worktree),
# then python -m mall.refactor.s4_library --rollback (it refuses while ref_id is NOT NULL; it gives reference_id back to what the code
# wrote after landing and reports what reversing 0079 needs), then revert the code, then reverse 0079 from the worktree.

import django.db.models.deletion
from django.db import migrations, models

from mall.refactor.migration_ops import with_lock_timeout

TABLES = {'mall_oemcrossreference': ('ref_id', 'brand', 'number'), 'mall_oemcrossreferenceretired': ('ref_id', 'brand', 'number'),
          'mall_partoemlink': ('part_id', 'ref_id', 'source')}
MODELS = {'mall_oemcrossreference': 'OEMCrossReference', 'mall_oemcrossreferenceretired': 'OEMCrossReferenceRetired',
          'mall_partoemlink': 'PartOEMLink'}


def citations(*lists):
    out = []
    for text in (text for entries in lists for text in (entries or [])):
        text = str(text or '').strip()[:500]
        if text and text not in out:
            out.append(text)
    return out


def catch_up(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        for table in TABLES:
            cursor.execute(f'UPDATE {table} AS x SET ref_id = r.id FROM mall_partcode AS r WHERE r.uid = x.reference_id AND x.ref_id IS NULL')
        for table, key in TABLES.items():
            model = apps.get_model('mall', MODELS[table])
            columns = ', '.join(key)
            cursor.execute(f'SELECT {columns}, array_agg(id ORDER BY id) FROM {table} WHERE ref_id IS NOT NULL GROUP BY {columns} HAVING count(*) > 1')
            for *_, ids in cursor.fetchall():
                rows = list(model.objects.filter(pk__in=ids).order_by('pk'))
                keeper, rest = rows[0], rows[1:]
                if hasattr(keeper, 'citations'):
                    merged = citations(keeper.citations, *(row.citations for row in rest))
                    if merged != keeper.citations:
                        model.objects.filter(pk=keeper.pk).update(citations=merged)
                model.objects.filter(pk__in=[row.pk for row in rest]).delete()
        problems = []
        cursor.execute('SELECT count(*) FROM mall_oemreference o WHERE NOT EXISTS (SELECT 1 FROM mall_partcode r WHERE r.uid = o.id)')
        missing = cursor.fetchone()[0]
        if missing:
            problems.append(f'{missing} números de la tabla OEM sin su referencia')
        for table in TABLES:
            cursor.execute(f'SELECT count(*) FROM {table} WHERE ref_id IS NULL')
            count = cursor.fetchone()[0]
            if count:
                problems.append(f'{count} filas de {table} sin ref_id')
    if problems:
        raise RuntimeError('0080_library_lines_checks: ' + '; '.join(problems) + '. Ejecuta python -m mall.refactor.s4_library --apply '
                           '(catálogo congelado) y vuelve a migrar.')


def reference(related_name):
    return models.ForeignKey(db_column='ref_id', on_delete=django.db.models.deletion.PROTECT, related_name=related_name, to='mall.reference',
                             verbose_name='referencia OEM')


class Migration(migrations.Migration):

    dependencies = [
        ('mall', '0079_library_lines'),
    ]

    operations = with_lock_timeout([
        migrations.RunPython(catch_up, migrations.RunPython.noop),
        migrations.AlterField(model_name='oemcrossreference', name='reference', field=reference('cross_references')),
        migrations.AlterField(model_name='oemcrossreferenceretired', name='reference', field=reference('retired_cross_references')),
        migrations.AlterField(model_name='partoemlink', name='reference', field=reference('part_links')),
        migrations.RemoveConstraint(model_name='oemcrossreference', name='oem_cross_reference_unique'),
        migrations.AddConstraint(model_name='oemcrossreference', constraint=models.UniqueConstraint(
            fields=('reference', 'brand', 'number'), name='cross_reference_unique')),
        migrations.RemoveConstraint(model_name='oemcrossreferenceretired', name='oem_cross_reference_retired_unique'),
        migrations.AddConstraint(model_name='oemcrossreferenceretired', constraint=models.UniqueConstraint(
            fields=('reference', 'brand', 'number'), name='cross_reference_retired_unique')),
        migrations.RemoveConstraint(model_name='partoemlink', name='part_oem_link_unique'),
        migrations.AddConstraint(model_name='partoemlink', constraint=models.UniqueConstraint(
            fields=('part', 'reference', 'source'), name='part_link_unique')),
    ])
