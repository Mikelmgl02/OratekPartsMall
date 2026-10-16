# Written by hand as the first part of step 3 of inventory refactor stage S10's contract (plan §1.2.5, §3 S10.2): the tables of the
# models 0087 took out of the state move to the archive schema, kept, never dropped:
#   SkuProposal, SkuProposalItem (Productos nuevos before S6), OEMReference, OEMReferenceSource, OEMCrossReference,
#   OEMCrossReferenceRetired, PartOEMLink (the OEM table before S4 and its links), SupplierCodeMapping (the learned codes before S6) and
#   Alternate (the alternos history).
# It runs only on a database whose rows the export of python -m mall.refactor.s10_archive --export holds: the manifest (S10_EXPORT, a
# directory or its export.json; else the main checkout's output/refactor/s10/export, mall.refactor.output.stage_dir) must list every
# non-empty table with its row count, and record the preconditions passing (the export checks them in its snapshot, S10.1 among them:
# every OEMCrossReference row has its S9 outcome). A database whose tables are all empty (a new or test database) needs none.
# First, the codes an open merge case of the merge pass (S9) waits on, filed under its lines' numbers, are copied into the case's
# evidence (catalog_rows; 4,162 OEMCrossReference rows with outcome espera_grupo on 2026-10-15), so deciding it still makes them
# References. 0087 recorded and dropped the foreign keys between these tables and the rest (any made since is recorded and dropped here
# the same way, archive.s10_constraints). ALTER TABLE ... SET SCHEMA moves each table with its indexes and sequences. Reversing it moves
# them back to public (0087's reverse makes the keys again).
import json
import os
from pathlib import Path

from django.db import migrations

from mall.refactor.migration_ops import with_lock_timeout
from mall.refactor.output import stage_dir

ARCHIVED = ('mall_skuproposalitem', 'mall_skuproposal', 'mall_oemcrossreferenceretired', 'mall_oemcrossreference', 'mall_partoemlink',
            'mall_oemreferencesource', 'mall_oemreference', 'mall_suppliercodemapping', 'mall_alternate')


def manifest_data():
    """The export manifest, or None when there is none."""
    location = Path(os.environ.get('S10_EXPORT') or stage_dir('s10') / 'export')
    path = location if location.suffix == '.json' else location / 'export.json'
    if not path.exists():
        return None
    return json.loads(path.read_text())


def manifest(section):
    """The export manifest's entries of a section (tables or columns), or None when there is no manifest."""
    data = manifest_data()
    return None if data is None else data.get(section) or {}


def require_export(cursor):
    """Refuse (RuntimeError, nothing changed) when a non-empty table is not in the export with its row count."""
    rows = {}
    for table in ARCHIVED:
        cursor.execute('SELECT to_regclass(%s) IS NOT NULL', [f'public.{table}'])
        if cursor.fetchone()[0]:
            cursor.execute(f'SELECT count(*) FROM {table}')
            rows[table] = cursor.fetchone()[0]
    wanted = {table: n for table, n in rows.items() if n}
    if not wanted:
        return
    exported = manifest('tables')
    if exported is None:
        raise RuntimeError('0088 archiva tablas con filas y no encuentra la exportación (export.json). Ejecuta primero '
                           'python -m mall.refactor.s10_archive --export /app/output/refactor/s10/export (o apunta S10_EXPORT a ella).')
    wrong = {table: (n, (exported.get(table) or {}).get('rows')) for table, n in wanted.items() if (exported.get(table) or {}).get('rows') != n}
    if wrong:
        raise RuntimeError('0088: la exportación no coincide con la base de datos (tabla: filas ahora, filas exportadas): '
                           + '; '.join(f'{table}: {now}, {then}' for table, (now, then) in sorted(wrong.items()))
                           + '. Exporta otra vez y vuelve a migrar.')
    # The export ran the preconditions in its snapshot (S10.1: every OEMCrossReference row has S9's outcome, I12...) and refuses
    # when one fails; a manifest that does not record them passing is no export of this database.
    checks = manifest_data().get('preconditions')
    if checks is None or not all(row.get('ok') for row in checks):
        failed = [f"{row.get('label')}: {row.get('value')}" for row in checks or [] if not row.get('ok')]
        raise RuntimeError('0088: la exportación no registra las precondiciones cumplidas'
                           + (f" ({'; '.join(failed)})" if failed else '') + '. Ejecuta python -m mall.refactor.s10_archive --export.')


S9_RULES = ('3d', '3e', 'paso0', '3f')  # mall.merge_pass.S9_RULES
LIVE = ('verificado', 'declarado', 'inferido', 'disputa')


def keep_case_rows(apps, cursor):
    """Before OEMCrossReference leaves: the codes an open merge case of the merge pass waits on (the rows filed under the library numbers
    of its lines: S9's outcome espera_grupo) go into its evidence (catalog_rows, as mall.merge_pass.keep_case_rows writes them), so
    deciding the case still makes them References (mall.merge_pass.convert_case_codes). Returns how many rows were kept."""
    MatchingCase = apps.get_model('mall', 'MatchingCase')
    cursor.execute("SELECT to_regclass('public.mall_oemcrossreference') IS NOT NULL")
    if not cursor.fetchone()[0]:
        return 0
    kept = 0
    for case in MatchingCase.objects.filter(kind='merge', status='review').order_by('pk'):
        evidence = dict(case.evidence or {})
        if evidence.get('regla') not in S9_RULES:
            continue
        lines = [str(pk) for pk in case.lines.values_list('pk', flat=True)]
        if not lines:
            continue
        cursor.execute("""SELECT x.id, x.ref_id, x.brand, x.code, x.number, x.citations FROM mall_oemcrossreference x
                          JOIN mall_partcode r ON r.id = x.ref_id
                          WHERE r.part_id = ANY(%s::uuid[]) AND r.tipo = 'oem' AND r.estado = ANY(%s) AND COALESCE(r.brand, '') <> ''
                          ORDER BY x.id""", [lines, list(LIVE)])
        rows = evidence.get('catalog_rows') or []
        index = {(row[1], row[2], row[4]) for row in rows}
        for pk, ref_id, brand, code, number, citations in cursor.fetchall():
            if (ref_id, brand, number) not in index:
                citations = json.loads(citations) if isinstance(citations, str) else (citations or [])
                rows.append([f'x{pk}', ref_id, brand, code, number, citations])
                index.add((ref_id, brand, number))
                kept += 1
        if rows:
            evidence['catalog_rows'] = rows
            MatchingCase.objects.filter(pk=case.pk).update(evidence=evidence)
    return kept


def archive(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        require_export(cursor)
        keep_case_rows(apps, cursor)
        cursor.execute('CREATE SCHEMA IF NOT EXISTS archive')
        cursor.execute('CREATE TABLE IF NOT EXISTS archive.s10_constraints (table_name text NOT NULL, name text NOT NULL, '
                       'definition text NOT NULL, PRIMARY KEY (table_name, name))')
        tables = []
        for table in ARCHIVED:
            cursor.execute('SELECT to_regclass(%s) IS NOT NULL', [f'public.{table}'])
            if cursor.fetchone()[0]:
                tables.append(f'public.{table}')
        if not tables:
            return
        cursor.execute("""SELECT c.conrelid::regclass::text, c.conname, pg_get_constraintdef(c.oid) FROM pg_constraint c
                          WHERE c.contype = 'f' AND (c.conrelid = ANY(%s::regclass[]) OR c.confrelid = ANY(%s::regclass[]))
                          ORDER BY 1, 2""", [tables, tables])
        keys = cursor.fetchall()
        for table, name, definition in keys:
            cursor.execute('INSERT INTO archive.s10_constraints (table_name, name, definition) VALUES (%s, %s, %s) '
                           'ON CONFLICT (table_name, name) DO UPDATE SET definition = EXCLUDED.definition', [table, name, definition])
            cursor.execute(f'ALTER TABLE {table} DROP CONSTRAINT "{name}"')
        for table in tables:
            cursor.execute(f'ALTER TABLE {table} SET SCHEMA archive')


def unarchive(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        for table in ARCHIVED:
            cursor.execute('SELECT to_regclass(%s) IS NOT NULL', [f'archive.{table}'])
            if cursor.fetchone()[0]:
                cursor.execute(f'ALTER TABLE archive.{table} SET SCHEMA public')


class Migration(migrations.Migration):

    dependencies = [
        ('mall', '0087_contract_step2'),
    ]

    operations = with_lock_timeout([migrations.RunPython(archive, unarchive)], timeout='10s')
