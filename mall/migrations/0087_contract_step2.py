# Written by hand as step 2 of inventory refactor stage S10's contract (plan §1.2.5, §3 S10.2-3): the state, and one database change
# (the foreign keys of the tables whose models leave, below). The fields and models step 1 stopped reading and writing leave the
# models, so the code of this step names them nowhere.
# - Part: legacy_name, legacy_category, legacy_subcategory and legacy_brand (columns name, category, subcategory, legacy_brand) go;
#   active and is_OEM are GeneratedFields of estado and tipo (the columns stay plain, written by 0086's trigger, until 0089 makes them
#   generated: Django never writes either, and reads them back after an insert); the CHECKs line_active_iff_publicada and
#   line_oem_iff_tipo leave the state (0089 drops them).
# - Reference: legacy_ref_type, legacy_reference_source and oem_reference go, with the CHECK reference_tipo_matches_ref_type.
# - PartType.legacy_category, SkuReviewVerdict.proposal (with its unique constraint and index) and SearchDocument.reach (with its GIN
#   index) go.
# - The models of the tables 0088 archives leave: SkuProposal, SkuProposalItem, OEMReference, OEMReferenceSource, OEMCrossReference,
#   OEMCrossReferenceRetired, PartOEMLink, SupplierCodeMapping and Alternate.
# Every column keeps its database default (0086) or is nullable, so the inserts of this code, which no longer name them, work until
# 0089 drops them.
# The one database change: the foreign keys between those nine tables and the rest (25 on the live database) are recorded in
# archive.s10_constraints and dropped (detach_archived), since no model enforces their ON DELETE any more: a row of a live table
# (a supplier item, an account, a line) deleted by this code must not wait on a row of a table no model knows. 0088 moves the tables
# to the archive schema. Reversing this migration makes the recorded keys again (NOT VALID: their rows were checked when they were
# made) and leaves the record.

from django.db import migrations, models

from mall.refactor.migration_ops import with_lock_timeout

ARCHIVED = ('mall_skuproposalitem', 'mall_skuproposal', 'mall_oemcrossreferenceretired', 'mall_oemcrossreference', 'mall_partoemlink',
            'mall_oemreferencesource', 'mall_oemreference', 'mall_suppliercodemapping', 'mall_alternate')


def tables(cursor, schema='public'):
    found = []
    for table in ARCHIVED:
        cursor.execute('SELECT to_regclass(%s) IS NOT NULL', [f'{schema}.{table}'])
        if cursor.fetchone()[0]:
            found.append(f'{schema}.{table}')
    return found


def detach_archived(apps, schema_editor):
    """Record and drop every foreign key from or to the tables whose models left (archive.s10_constraints keeps their definitions)."""
    with schema_editor.connection.cursor() as cursor:
        present = tables(cursor)
        if not present:
            return
        cursor.execute('CREATE SCHEMA IF NOT EXISTS archive')
        cursor.execute('CREATE TABLE IF NOT EXISTS archive.s10_constraints (table_name text NOT NULL, name text NOT NULL, '
                       'definition text NOT NULL, PRIMARY KEY (table_name, name))')
        cursor.execute("""SELECT c.conrelid::regclass::text, c.conname, pg_get_constraintdef(c.oid) FROM pg_constraint c
                          WHERE c.contype = 'f' AND (c.conrelid = ANY(%s::regclass[]) OR c.confrelid = ANY(%s::regclass[]))
                          ORDER BY 1, 2""", [present, present])
        for table, name, definition in cursor.fetchall():
            cursor.execute('INSERT INTO archive.s10_constraints (table_name, name, definition) VALUES (%s, %s, %s) '
                           'ON CONFLICT (table_name, name) DO UPDATE SET definition = EXCLUDED.definition', [table, name, definition])
            cursor.execute(f'ALTER TABLE {table} DROP CONSTRAINT "{name}"')


def reattach_archived(apps, schema_editor):
    """The recorded keys again, on the tables still in public (0088's reverse put them back there first)."""
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('archive.s10_constraints') IS NOT NULL")
        if not cursor.fetchone()[0]:
            return
        cursor.execute('SELECT table_name, name, definition FROM archive.s10_constraints ORDER BY table_name, name')
        for table, name, definition in cursor.fetchall():
            cursor.execute('SELECT to_regclass(%s) IS NOT NULL', [table])
            if not cursor.fetchone()[0]:
                continue
            cursor.execute('SELECT 1 FROM pg_constraint WHERE conrelid = %s::regclass AND conname = %s', [table, name])
            if cursor.fetchone() is None:
                cursor.execute(f'ALTER TABLE {table} ADD CONSTRAINT "{name}" {definition} NOT VALID')


class Migration(migrations.Migration):

    dependencies = [
        ('mall', '0086_contract_step1'),
    ]

    operations = with_lock_timeout([
        migrations.RunPython(detach_archived, reattach_archived),
        migrations.SeparateDatabaseAndState(state_operations=[
            migrations.RemoveField(
                model_name='alternate',
                name='part',
            ),
            migrations.RemoveField(
                model_name='alternate',
                name='replacement',
            ),
            migrations.RemoveField(
                model_name='oemcrossreference',
                name='legacy_reference',
            ),
            migrations.RemoveField(
                model_name='oemcrossreference',
                name='reference',
            ),
            migrations.RemoveField(
                model_name='oemcrossreferenceretired',
                name='legacy_reference',
            ),
            migrations.RemoveField(
                model_name='oemcrossreferenceretired',
                name='reference',
            ),
            migrations.RemoveField(
                model_name='oemcrossreferenceretired',
                name='retired_by',
            ),
            migrations.RemoveField(
                model_name='oemreference',
                name='created_by',
            ),
            migrations.RemoveField(
                model_name='oemreference',
                name='superseded_by',
            ),
            migrations.RemoveField(
                model_name='oemreference',
                name='updated_by',
            ),
            migrations.RemoveField(
                model_name='oemreferencesource',
                name='reference',
            ),
            migrations.RemoveField(
                model_name='partoemlink',
                name='legacy_reference',
            ),
            migrations.RemoveField(
                model_name='reference',
                name='oem_reference',
            ),
            migrations.RemoveField(
                model_name='oemreferencesource',
                name='created_by',
            ),
            migrations.RemoveField(
                model_name='partoemlink',
                name='part',
            ),
            migrations.RemoveField(
                model_name='partoemlink',
                name='reference',
            ),
            migrations.RemoveField(
                model_name='skuproposal',
                name='decided_by',
            ),
            migrations.RemoveField(
                model_name='skuproposal',
                name='part',
            ),
            migrations.RemoveField(
                model_name='skuproposal',
                name='part_type',
            ),
            migrations.RemoveField(
                model_name='skureviewverdict',
                name='proposal',
            ),
            migrations.RemoveField(
                model_name='skuproposalitem',
                name='proposal',
            ),
            migrations.RemoveField(
                model_name='skuproposalitem',
                name='item',
            ),
            migrations.RemoveField(
                model_name='suppliercodemapping',
                name='actor',
            ),
            migrations.RemoveField(
                model_name='suppliercodemapping',
                name='part',
            ),
            migrations.RemoveField(
                model_name='suppliercodemapping',
                name='supplier',
            ),
            migrations.RemoveConstraint(
                model_name='part',
                name='line_active_iff_publicada',
            ),
            migrations.RemoveConstraint(
                model_name='part',
                name='line_oem_iff_tipo',
            ),
            migrations.RemoveConstraint(
                model_name='reference',
                name='reference_tipo_matches_ref_type',
            ),
            migrations.RemoveConstraint(
                model_name='skureviewverdict',
                name='sku_review_one_verdict_per_run',
            ),
            migrations.RemoveIndex(
                model_name='searchdocument',
                name='search_document_reach_trgm',
            ),
            migrations.RemoveIndex(
                model_name='skureviewverdict',
                name='sku_review_latest',
            ),
            migrations.RemoveField(
                model_name='part',
                name='legacy_brand',
            ),
            migrations.RemoveField(
                model_name='part',
                name='legacy_category',
            ),
            migrations.RemoveField(
                model_name='part',
                name='legacy_name',
            ),
            migrations.RemoveField(
                model_name='part',
                name='legacy_subcategory',
            ),
            migrations.RemoveField(
                model_name='parttype',
                name='legacy_category',
            ),
            migrations.RemoveField(
                model_name='reference',
                name='legacy_ref_type',
            ),
            migrations.RemoveField(
                model_name='reference',
                name='legacy_reference_source',
            ),
            migrations.RemoveField(
                model_name='searchdocument',
                name='reach',
            ),
            migrations.AlterField(
                model_name='part',
                name='active',
                field=models.GeneratedField(db_persist=True, expression=models.Q(('estado', 'publicada')), output_field=models.BooleanField(), verbose_name='activo'),
            ),
            migrations.AlterField(
                model_name='part',
                name='is_OEM',
                field=models.GeneratedField(db_index=True, db_persist=True, expression=models.Q(('tipo', 'OEM')), output_field=models.BooleanField(), verbose_name='El SKU principal es OEM'),
            ),
            migrations.DeleteModel(
                name='Alternate',
            ),
            migrations.DeleteModel(
                name='OEMCrossReference',
            ),
            migrations.DeleteModel(
                name='OEMCrossReferenceRetired',
            ),
            migrations.DeleteModel(
                name='OEMReference',
            ),
            migrations.DeleteModel(
                name='OEMReferenceSource',
            ),
            migrations.DeleteModel(
                name='PartOEMLink',
            ),
            migrations.DeleteModel(
                name='SkuProposal',
            ),
            migrations.DeleteModel(
                name='SkuProposalItem',
            ),
            migrations.DeleteModel(
                name='SupplierCodeMapping',
            ),
        ]),
    ])
