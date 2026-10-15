"""Inventory refactor S10's archive and drop (step 3 of the contract): the read-only preconditions and the export
(python -m mall.refactor.s10_archive), the guards of 0088 and 0089, which refuse a database whose rows the export does not hold, 0088's
copy of the codes an open merge case waits on, and what 0089 leaves in the database (the brand key, the generated mirrors, the case
CHECKs) and its reverse."""
import json
import os
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from django.apps import apps
from django.db import IntegrityError, connection, transaction
from django.test import TestCase

from .brand_models import Brand, BrandAlias
from .matching_models import MatchingCase
from .models import Part, Reference
from .refactor import s10_archive
from .test_factories import make_line, make_reference, migration_module

ARCHIVED = s10_archive.ARCHIVED


def sql(statement, params=None):
    with connection.cursor() as cursor:
        cursor.execute(statement, params or [])
        return cursor.fetchall() if cursor.description else None


def ddl(statement, params=None):
    """A schema change inside the test's transaction (undone when it rolls back), after the deferred key checks it would wait on."""
    sql('SET CONSTRAINTS ALL IMMEDIATE')
    return sql(statement, params)


def unarchive(table):
    """An archived table back in public, inside the test's transaction (it goes back when the test rolls back)."""
    ddl(f'ALTER TABLE archive.{table} SET SCHEMA public')


def write_manifest(folder, **sections):
    with open(os.path.join(folder, 'export.json'), 'w', encoding='utf-8') as handle:
        json.dump(sections, handle)


class ArchivedSchemaTests(TestCase):
    """What 0088 and 0089 leave in a migrated database."""

    def test_the_tables_are_in_the_archive_schema_and_their_keys_recorded(self):
        for table in ARCHIVED:
            with self.subTest(table=table):
                self.assertEqual(sql('SELECT to_regclass(%s) IS NOT NULL, to_regclass(%s) IS NOT NULL',
                                     [f'public.{table}', f'archive.{table}']), [(False, True)])
        self.assertEqual(sql("SELECT to_regclass('archive.s10_constraints') IS NOT NULL"), [(True,)])
        with connection.cursor() as cursor:
            self.assertEqual(s10_archive.present(cursor), [])

    def test_the_dropped_columns_are_gone(self):
        for table, _, columns in s10_archive.DROPPED:
            with connection.cursor() as cursor:
                present = [column for column in columns if s10_archive.column_exists(cursor, table, column)]
            self.assertEqual(present, [], table)


class BrandKeyTests(TestCase):
    """0089: Reference.brand is NULL for any brand and otherwise a Brand's nombre (a foreign key ON UPDATE CASCADE); Python keeps
    saying ''."""

    def setUp(self):
        self.line = make_line('S10-BRAND-1', description='FILTRO')
        self.toyota = Brand.objects.create(nombre='TOYOTA', tipo='vehiculo')
        BrandAlias.objects.create(alias='TOY', brand=self.toyota)

    def stored_brand(self, ref):
        return sql('SELECT brand FROM mall_partcode WHERE id = %s', [ref.pk])[0][0]

    def test_any_brand_is_null_in_the_database_and_blank_in_python(self):
        ref = make_reference(self.line, 'S10-ANY-1')
        ref.refresh_from_db()
        self.assertEqual((self.stored_brand(ref), ref.brand), (None, ''))
        self.assertEqual(list(Reference.objects.filter(brand='', code='S10-ANY-1').values_list('pk', flat=True)), [ref.pk])
        self.assertFalse(Reference.objects.filter(code='S10-ANY-1').exclude(brand='').exists())
        Reference.all_objects.filter(pk=ref.pk).update(brand='')  # '' is written as NULL too
        self.assertIsNone(self.stored_brand(ref))

    def test_a_blank_or_unknown_brand_written_in_sql_is_refused(self):
        ref = make_reference(self.line, 'S10-SQL-1', brand='TOYOTA')
        for value in ('', 'NO ES MARCA'):
            with self.subTest(value=value), self.assertRaises(IntegrityError), transaction.atomic():
                sql('UPDATE mall_partcode SET brand = %s WHERE id = %s', [value, ref.pk])
        self.assertEqual(self.stored_brand(ref), 'TOYOTA')

    def test_a_writer_outside_references_stores_the_nombre(self):
        alias = make_reference(self.line, 'S10-ALIAS-1', brand='toy')
        new = make_reference(self.line, 'S10-NEW-1', brand=' marca  nueva ')
        self.assertEqual((self.stored_brand(alias), self.stored_brand(new)), ('TOYOTA', 'MARCA NUEVA'))
        self.assertTrue(Brand.objects.filter(nombre='MARCA NUEVA').exists())

    def test_references_add_and_update_resolve_the_spelling(self):
        from . import references
        ref, added = references.add(self.line, 'S10-ADD-1', 'toy', tipo='oem')
        self.assertEqual((added, ref.brand, self.stored_brand(ref)), (True, 'TOYOTA', 'TOYOTA'))
        self.assertEqual(references.add(self.line, 'S10-ADD-1', 'TOYOTA')[0].pk, ref.pk)  # the same key, not a second Reference
        references.update(ref, brand='otra  marca')
        self.assertEqual(self.stored_brand(ref), 'OTRA MARCA')
        self.assertEqual(references.brand_name(' toy '), 'TOYOTA')
        self.assertEqual(references.brand_name(''), '')

    def test_bulk_create_stores_the_nombre(self):
        from . import references
        built = [references.build(self.line, 'S10-BULK-1', 'TOY'), references.build(self.line, 'S10-BULK-2', 'bulk  brand')]
        references.bulk_create(built)
        self.assertEqual([self.stored_brand(ref) for ref in built], ['TOYOTA', 'BULK BRAND'])

    def test_renaming_a_brand_in_sql_renames_its_codes(self):
        ref = make_reference(self.line, 'S10-CASCADE-1', brand='TOYOTA')
        sql("UPDATE mall_brand SET nombre = 'TOYOTA MOTOR' WHERE id = %s", [self.toyota.pk])
        self.assertEqual(self.stored_brand(ref), 'TOYOTA MOTOR')

    def test_a_brand_a_code_carries_cannot_be_deleted(self):
        make_reference(self.line, 'S10-PROTECT-1', brand='TOYOTA')
        with self.assertRaises(IntegrityError), transaction.atomic():
            sql('DELETE FROM mall_brandalias WHERE brand_id = %s', [self.toyota.pk])
            sql('DELETE FROM mall_brand WHERE id = %s', [self.toyota.pk])


class GeneratedMirrorTests(TestCase):
    """0089: Part.active and is_OEM are generated from estado and tipo; nothing can write them."""

    def test_the_mirrors_follow_estado_and_tipo_and_refuse_a_write(self):
        line = make_line('S10-GEN-1')
        Part.objects.filter(pk=line.pk).update(estado='retirada', tipo='OEM')
        self.assertEqual(sql('SELECT active, "is_OEM" FROM mall_part WHERE id = %s', [line.pk]), [(False, True)])
        for column in ('active', '"is_OEM"'):
            with self.subTest(column=column), self.assertRaises(Exception), transaction.atomic():
                sql(f'UPDATE mall_part SET {column} = true WHERE id = %s', [line.pk])
        self.assertEqual(sql('SELECT indexname FROM pg_indexes WHERE tablename = %s AND indexdef LIKE %s', ['mall_part', '%"is_OEM"%']),
                         [('mall_part_is_OEM_027dd94b',)])


class CaseCheckTests(TestCase):
    def test_a_legacy_kind_or_status_is_refused(self):
        for kind, status in (('supplier', 'review'), ('line', 'unmatched')):
            with self.subTest(kind=kind, status=status), self.assertRaises(IntegrityError), transaction.atomic():
                MatchingCase.objects.create(key=f'legacy:{kind}:{status}', kind=kind, status=status, fingerprint='x', method='', reason='')


class PreconditionTests(TestCase):
    def checks(self):
        with connection.cursor() as cursor:
            return {label: (value, ok) for label, value, ok, _ in s10_archive.preconditions(cursor, outcomes={})}

    def test_a_clean_database_passes(self):
        make_reference(make_line('S10-PRE-1', description='FILTRO'), 'S10-ALT-1', brand='MARCA REAL', tipo='aftermarket')
        checks = self.checks()
        self.assertTrue(all(ok for ok in (ok for _, ok in checks.values())), checks)
        self.assertEqual(checks['marcas de referencias que no son una marca (I12)'], (0, True))

    def test_a_brand_that_is_no_brand_stops_it(self):
        ref = make_reference(make_line('S10-PRE-2'), 'S10-ALT-2', brand='MARCA REAL', tipo='aftermarket')
        ddl('ALTER TABLE mall_partcode DROP CONSTRAINT reference_brand_fk')  # the state before 0089, in this test's transaction
        sql("UPDATE mall_partcode SET brand = 'MARCA FANTASMA' WHERE id = %s", [ref.pk])
        self.assertEqual(self.checks()['marcas de referencias que no son una marca (I12)'], (1, False))
        with connection.cursor() as cursor, self.assertRaisesMessage(RuntimeError, '1 referencias llevan una marca que no es una marca'):
            migration_module('0089_drop').require_brands(cursor)

    def test_every_library_code_needs_its_outcome(self):
        unarchive('mall_oemcrossreference')
        ref = make_reference(make_line('S10-PRE-3'), '90915-YZZD2', brand='TOYOTA', tipo='oem')
        sql("INSERT INTO mall_oemcrossreference (brand, code, number, citations, created_at, updated_at, ref_id) "
            "VALUES ('GMB', 'GFT-1', '9091510003', '[]', now(), now(), %s)", [ref.pk])
        row = sql('SELECT max(id) FROM mall_oemcrossreference')[0][0]
        label = 'códigos de la biblioteca (OEMCrossReference) sin resultado de S9'
        with connection.cursor() as cursor:
            self.assertEqual(dict((c[0], c[1:3]) for c in s10_archive.preconditions(cursor, outcomes={}))[label], (1, False))
            self.assertEqual(dict((c[0], c[1:3]) for c in s10_archive.preconditions(cursor, outcomes={row: 'case'}))[label], (0, True))

    def test_the_cli_prints_every_check(self):
        out = StringIO()
        with redirect_stdout(out), patch('django.setup'):
            code = s10_archive.main(['--check'])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn('esquema archive', out.getvalue())
        self.assertIn('marcas de referencias que no son una marca (I12): 0', out.getvalue())


class ExportTests(TestCase):
    def test_the_export_writes_every_archived_table_and_dropped_value_with_its_manifest(self):
        unarchive('mall_oemcrossreference')
        ref = make_reference(make_line('S10-EXP-1', description='BOMBA'), '16100-39315', brand='TOYOTA', tipo='oem')
        sql("INSERT INTO mall_oemcrossreference (brand, code, number, citations, created_at, updated_at, ref_id) "
            "VALUES ('GMB', 'GWT-78A', '1610039315', '[\"GMB 2024 p. 3\"]', now(), now(), %s)", [ref.pk])
        ddl("ALTER TABLE mall_part ADD COLUMN legacy_brand varchar(120) NOT NULL DEFAULT ''")  # a column 0089 drops, back for the test
        sql("UPDATE mall_part SET legacy_brand = 'ANTIGUA' WHERE id = %s", [ref.part_id])
        with tempfile.TemporaryDirectory() as folder:
            manifest = s10_archive.export(folder, catalog_data=False)
            self.assertEqual(list(manifest['tables']), ['mall_oemcrossreference'])
            self.assertEqual(manifest['tables']['mall_oemcrossreference']['rows'], 1)
            self.assertEqual({table: (entry['columns'], entry['rows']) for table, entry in manifest['columns'].items()},
                             {'mall_part': (['legacy_brand'], 1)})
            saved = json.loads(open(os.path.join(folder, 'export.json'), encoding='utf-8').read())
            self.assertEqual((saved['tables'], saved['columns']), (manifest['tables'], manifest['columns']))
            rows = [json.loads(line) for line in open(os.path.join(folder, 'mall_oemcrossreference.jsonl'), encoding='utf-8')]
            self.assertEqual([(row['code'], row['ref_id'], row['citations']) for row in rows], [('GWT-78A', ref.pk, ['GMB 2024 p. 3'])])
            values = [json.loads(line) for line in open(os.path.join(folder, 'mall_part.columns.jsonl'), encoding='utf-8')]
            self.assertEqual(values, [{'id': str(ref.part_id), 'legacy_brand': 'ANTIGUA'}])
            # The manifest the migrations read: 0088 and 0089 accept this database with it.
            with patch.dict(os.environ, {'S10_EXPORT': folder}), connection.cursor() as cursor:
                migration_module('0088_archive').require_export(cursor)
                migration_module('0089_drop').require_export(cursor)

    def test_the_cli_exports_where_the_migrations_look(self):
        out = StringIO()
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(out), patch('django.setup'), \
                patch.dict(os.environ, {'REFACTOR_OUTPUT': folder, 'S10_EXPORT': ''}):
            self.assertEqual(s10_archive.main(['--export', '--no-catalog-data']), 0)
            self.assertTrue(os.path.exists(os.path.join(folder, 'export', 'export.json')))
            self.assertEqual(migration_module('0088_archive').manifest('tables'), {})


class ArchiveGuardTests(TestCase):
    """0088 and 0089 refuse when a table or column with rows is not in the export, or the export counts differ."""

    def test_0088_needs_the_export_of_every_table_with_rows(self):
        module = migration_module('0088_archive')
        with connection.cursor() as cursor:
            module.require_export(cursor)  # nothing left in public: nothing to lose
            unarchive('mall_oemcrossreference')
            module.require_export(cursor)  # an empty table needs no export
            ref = make_reference(make_line('S10-GUARD-1'), '16100-39316', brand='TOYOTA', tipo='oem')
            sql("INSERT INTO mall_oemcrossreference (brand, code, number, citations, created_at, updated_at, ref_id) "
                "VALUES ('GMB', 'GWT-79', '1610039316', '[]', now(), now(), %s)", [ref.pk])
            with patch.dict(os.environ, {'S10_EXPORT': '/nonexistent/s10'}), self.assertRaisesMessage(RuntimeError, 'no encuentra la exportación'):
                module.require_export(cursor)
            with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'S10_EXPORT': folder}):
                write_manifest(folder, tables={'mall_oemcrossreference': {'rows': 99}})
                with self.assertRaisesMessage(RuntimeError, 'mall_oemcrossreference: 1, 99'):
                    module.require_export(cursor)
                write_manifest(folder, tables={'mall_oemcrossreference': {'rows': 1}})
                module.require_export(cursor)

    def test_0089_needs_the_export_of_every_value_it_drops(self):
        module = migration_module('0089_drop')
        with connection.cursor() as cursor:
            module.require_export(cursor)  # the columns are gone already
            with patch.object(module, 'DROPPED', (('mall_part', ('description',)),)):
                make_line('S10-GUARD-2', description='CON VALOR')
                count = sql("SELECT count(*) FROM mall_part WHERE description <> ''")[0][0]
                with patch.dict(os.environ, {'S10_EXPORT': '/nonexistent/s10'}), self.assertRaisesMessage(RuntimeError, 'no encuentra la exportación'):
                    module.require_export(cursor)
                with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'S10_EXPORT': os.path.join(folder, 'export.json')}):
                    write_manifest(folder, columns={'mall_part': {'rows': count + 1}})
                    with self.assertRaisesMessage(RuntimeError, 'no coincide'):
                        module.require_export(cursor)
                    write_manifest(folder, columns={'mall_part': {'rows': count}})
                    module.require_export(cursor)


class KeepCaseRowsTests(TestCase):
    """0088: before OEMCrossReference leaves, an open merge case of the merge pass keeps the codes filed under its lines' numbers."""

    def test_an_open_s9_case_keeps_the_rows_of_its_lines_numbers_once(self):
        unarchive('mall_oemcrossreference')
        keep = migration_module('0088_archive').keep_case_rows
        first, second = make_line('S10-KEEP-1'), make_line('S10-KEEP-2')
        number = make_reference(first, '04465-02220', brand='TOYOTA', tipo='oem')
        other = make_reference(second, '04465-02230', brand='TOYOTA', tipo='oem')
        for ref, code in ((number, 'D2220'), (other, 'D2230')):
            sql("INSERT INTO mall_oemcrossreference (brand, code, number, citations, created_at, updated_at, ref_id) "
                "VALUES ('BREMBO', %s, '0446502220', '[\"BREMBO p. 9\"]', now(), now(), %s)", [code, ref.pk])
        case = MatchingCase.objects.create(key='merge:s10-keep', kind='merge', status='review', fingerprint='x', method='', reason='',
                                           evidence={'regla': '3d'})
        case.lines.add(first)
        decided = MatchingCase.objects.create(key='merge:s10-decided', kind='merge', status='applied', fingerprint='x', method='',
                                              reason='', evidence={'regla': '3d'})
        decided.lines.add(second)
        with connection.cursor() as cursor:
            self.assertEqual(keep(apps, cursor), 1)
            self.assertEqual(keep(apps, cursor), 0)  # idempotent
        case.refresh_from_db()
        decided.refresh_from_db()
        rows = case.evidence['catalog_rows']
        self.assertEqual([(row[1], row[2], row[3], row[4], row[5]) for row in rows], [(number.pk, 'BREMBO', 'D2220', '0446502220', ['BREMBO p. 9'])])
        self.assertTrue(rows[0][0].startswith('x'))
        self.assertNotIn('catalog_rows', decided.evidence)


class DropReverseTests(TestCase):
    """0089's SQL goes back (the columns, 0086's triggers and CHECKs) and forward again on a migrated database."""

    def test_the_reverse_brings_back_the_columns_and_triggers_and_the_drop_applies_again(self):
        from .technical_data import part_type_for
        module = migration_module('0089_drop')
        pads, discs = part_type_for('FRENOS', 'PASTILLAS'), part_type_for('FRENOS', 'DISCOS')
        line = make_line('S10-REV-1', part_type=pads)
        ref = make_reference(line, 'S10-REV-A')
        sql('SET CONSTRAINTS ALL IMMEDIATE')
        with connection.cursor() as cursor:
            for statement in module.REVERSE_DDL:
                cursor.execute(statement)
        self.assertEqual(sql('SELECT brand, ref_type FROM mall_partcode WHERE id = %s', [ref.pk]), [('', 'unknown')])
        # The subgrupo's text comes back from the subgrupo, and 0086's triggers keep it.
        self.assertEqual(sql('SELECT category, subcategory FROM mall_part WHERE id = %s', [line.pk]), [('FRENOS', 'PASTILLAS')])
        self.assertEqual(sql('SELECT category FROM mall_parttype WHERE id = %s', [pads.pk]), [('FRENOS',)])
        sql('UPDATE mall_part SET part_type_id = %s WHERE id = %s', [discs.pk, line.pk])
        self.assertEqual(sql('SELECT category, subcategory FROM mall_part WHERE id = %s', [line.pk]), [('FRENOS', 'DISCOS')])
        sql("UPDATE mall_part SET estado = 'retirada' WHERE id = %s", [line.pk])  # 0086's trigger keeps the plain column in step
        self.assertEqual(sql('SELECT active, name FROM mall_part WHERE id = %s', [line.pk]), [(False, '')])
        with connection.cursor() as cursor:
            for statement in module.DDL:
                cursor.execute(statement)
        self.assertEqual(sql('SELECT brand, active FROM mall_partcode r JOIN mall_part p ON p.id = r.part_id WHERE r.id = %s', [ref.pk]),
                         [(None, False)])
        self.assertEqual(sql("SELECT count(*) FROM pg_trigger WHERE tgname LIKE '%%s10%%' AND NOT tgisinternal"), [(0,)])
