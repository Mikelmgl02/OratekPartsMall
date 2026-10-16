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
        for flag in ('--check', '--dry-run'):  # --dry-run is the rehearsal's name for it
            out = StringIO()
            with self.subTest(flag=flag), redirect_stdout(out), patch('django.setup'):
                code = s10_archive.main([flag])
                self.assertEqual(code, 0, out.getvalue())
                self.assertIn('esquema archive', out.getvalue())
                self.assertIn('marcas de referencias que no son una marca (I12): 0', out.getvalue())

    def test_the_outcomes_file_is_s9s_whatever_the_rehearsal_folder(self):
        with patch.dict(os.environ, {'REFACTOR_OUTPUT': '/tmp/rehearsal/s10', 'S10_XREF_OUTCOMES': ''}):
            path = s10_archive.outcomes_path()
        self.assertEqual(path.parts[-4:], ('output', 'refactor', 's9', 'xref_outcomes.jsonl'))
        with patch.dict(os.environ, {'S10_XREF_OUTCOMES': '/tmp/outcomes.jsonl'}):
            self.assertEqual(str(s10_archive.outcomes_path()), '/tmp/outcomes.jsonl')


class ExportTests(TestCase):
    TRICKY = 'GWT "78" \\ A\nB'  # a quote, a backslash and a newline: text COPY would double the backslash and break the JSON

    def library_rows(self):
        """Two OEMCrossReference rows (the table back in public for the test), one with a code and citation full of escapes."""
        unarchive('mall_oemcrossreference')
        ref = make_reference(make_line('S10-EXP-1', description='BOMBA'), '16100-39315', brand='TOYOTA', tipo='oem')
        sql("INSERT INTO mall_oemcrossreference (brand, code, number, citations, created_at, updated_at, ref_id) "
            "VALUES ('GMB', 'GWT-78A', '1610039315', '[\"GMB 2024 p. 3\"]', now(), now(), %s)", [ref.pk])
        sql("INSERT INTO mall_oemcrossreference (brand, code, number, citations, created_at, updated_at, ref_id) "
            "VALUES ('GMB', %s, '1610039316', %s::jsonb, now(), now(), %s)", [self.TRICKY, json.dumps([self.TRICKY]), ref.pk])
        rows = [row[0] for row in sql('SELECT id FROM mall_oemcrossreference ORDER BY id')]
        return ref, {row: 'case' for row in rows}

    def test_the_export_writes_every_archived_table_and_dropped_value_with_its_manifest(self):
        ref, outcomes = self.library_rows()
        ddl("ALTER TABLE mall_part ADD COLUMN legacy_brand varchar(120) NOT NULL DEFAULT ''")  # a column 0089 drops, back for the test
        sql("UPDATE mall_part SET legacy_brand = %s WHERE id = %s", ['ANTIGUA \\ "1"', ref.part_id])
        with tempfile.TemporaryDirectory() as folder:
            manifest = s10_archive.export(folder, catalog_data=False, outcomes=outcomes)
            self.assertEqual(list(manifest['tables']), ['mall_oemcrossreference'])
            self.assertEqual(manifest['tables']['mall_oemcrossreference']['rows'], 2)
            self.assertEqual({table: (entry['columns'], entry['rows']) for table, entry in manifest['columns'].items()},
                             {'mall_part': (['legacy_brand'], 1)})
            self.assertTrue(manifest['preconditions'] and all(row['ok'] for row in manifest['preconditions']))
            saved = json.loads(open(os.path.join(folder, 'export.json'), encoding='utf-8').read())
            self.assertEqual((saved['tables'], saved['columns']), (manifest['tables'], manifest['columns']))
            path = os.path.join(folder, 'mall_oemcrossreference.jsonl')
            rows = [json.loads(line) for line in open(path, encoding='utf-8')]
            self.assertEqual(sorted((row['code'], row['ref_id'], row['citations']) for row in rows),
                             sorted([('GWT-78A', ref.pk, ['GMB 2024 p. 3']), (self.TRICKY, ref.pk, [self.TRICKY])]))
            import hashlib
            self.assertEqual(hashlib.sha256(open(path, 'rb').read()).hexdigest(), manifest['tables']['mall_oemcrossreference']['sha256'])
            values = [json.loads(line) for line in open(os.path.join(folder, 'mall_part.columns.jsonl'), encoding='utf-8')]
            self.assertEqual(values, [{'id': str(ref.part_id), 'legacy_brand': 'ANTIGUA \\ "1"'}])
            # The manifest the migrations read: 0088 and 0089 accept this database with it.
            with patch.dict(os.environ, {'S10_EXPORT': folder}), connection.cursor() as cursor:
                migration_module('0088_archive').require_export(cursor)
                migration_module('0089_drop').require_export(cursor)

    def test_a_failed_precondition_writes_nothing(self):
        _, outcomes = self.library_rows()
        outcomes.popitem()  # one library code without its S9 outcome (S10.1)
        with tempfile.TemporaryDirectory() as folder:
            target = os.path.join(folder, 'export')
            with self.assertRaisesMessage(s10_archive.ExportRefused, 'sin resultado de S9: 1'):
                s10_archive.export(target, catalog_data=False, outcomes=outcomes)
            self.assertFalse(os.path.exists(target))
            with patch.dict(os.environ, {'S10_EXPORT': target}), connection.cursor() as cursor, \
                    self.assertRaisesMessage(RuntimeError, 'no encuentra la exportación'):
                migration_module('0088_archive').require_export(cursor)

    def test_the_cli_exports_where_the_migrations_look(self):
        for argv in (['--export', '--no-catalog-data'], ['--apply', '--user', 'miguel', '--no-catalog-data']):
            out = StringIO()
            with self.subTest(argv=argv), tempfile.TemporaryDirectory() as folder, redirect_stdout(out), patch('django.setup'), \
                    patch.dict(os.environ, {'REFACTOR_OUTPUT': folder, 'S10_EXPORT': ''}):
                self.assertEqual(s10_archive.main(argv), 0)
                self.assertTrue(os.path.exists(os.path.join(folder, 'export', 'export.json')))
                self.assertEqual(migration_module('0088_archive').manifest('tables'), {})
                logged = [json.loads(line) for line in open(os.path.join(folder, 'manifest.jsonl'), encoding='utf-8')]
                self.assertEqual([(row['action'], row['user']) for row in logged], [('export', 'miguel' if '--apply' in argv else '')])


class RollbackTests(TestCase):
    """--rollback, once 0089 is reversed: the values the export holds go back to their rows."""

    def test_the_exported_values_go_back(self):
        import hashlib
        import uuid
        line = make_line('S10-RB-1', description='BOMBA')
        other = make_line('S10-RB-2', description='DISCO')
        ddl("ALTER TABLE mall_part ADD COLUMN name varchar(200) NOT NULL DEFAULT '', "
            "ADD COLUMN legacy_brand varchar(120) NOT NULL DEFAULT ''")  # 0089 reversed: its columns back, for the test
        sql("UPDATE mall_part SET name = %s, legacy_brand = 'VIEJA' WHERE id = %s", ['Bomba "A" \\ 1', line.pk])
        sql("UPDATE mall_part SET name = 'DISCO' WHERE id = %s", [other.pk])
        with tempfile.TemporaryDirectory() as folder:
            manifest = s10_archive.export(folder, catalog_data=False, outcomes={})
            self.assertEqual(manifest['columns']['mall_part']['rows'], 2)
            # A row deleted since the export is skipped (here: one no line has).
            path = os.path.join(folder, 'mall_part.columns.jsonl')
            with open(path, 'a', encoding='utf-8') as handle:
                handle.write(json.dumps({'id': str(uuid.uuid4()), 'name': 'NADIE', 'legacy_brand': ''}) + '\n')
            manifest['columns']['mall_part']['sha256'] = hashlib.sha256(open(path, 'rb').read()).hexdigest()
            write_manifest(folder, **manifest)
            sql("UPDATE mall_part SET name = '', legacy_brand = ''")  # what 0089's reverse leaves
            self.assertEqual(s10_archive.rollback(folder, batch=2), {'mall_part': 2})
            self.assertEqual(sql('SELECT name, legacy_brand FROM mall_part WHERE id = %s', [line.pk]), [('Bomba "A" \\ 1', 'VIEJA')])
            self.assertEqual(sql('SELECT name, legacy_brand FROM mall_part WHERE id = %s', [other.pk]), [('DISCO', '')])
            # The CLI, on the same export (a test database is never the live one: no freeze needed), and its log.
            out = StringIO()
            with redirect_stdout(out), patch('django.setup'), patch.dict(os.environ, {'REFACTOR_OUTPUT': folder}):
                self.assertEqual(s10_archive.main(['--rollback', folder]), 0)
            self.assertIn('mall_part: 2 filas restauradas', out.getvalue())
            logged = [json.loads(row) for row in open(os.path.join(folder, 'manifest.jsonl'), encoding='utf-8')]
            self.assertEqual([row['action'] for row in logged], ['rollback'])

    def test_it_refuses_before_0089_is_reversed_or_on_a_changed_file(self):
        make_line('S10-RB-3', description='FILTRO')
        with tempfile.TemporaryDirectory() as folder:
            write_manifest(folder, columns={'mall_part': {'file': 'mall_part.columns.jsonl', 'key': 'id', 'columns': ['name'], 'rows': 0,
                                                          'sha256': 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'}})
            open(os.path.join(folder, 'mall_part.columns.jsonl'), 'w').close()
            with self.assertRaisesMessage(SystemExit, 'revierte 0089 primero'):
                s10_archive.rollback(folder)
            with open(os.path.join(folder, 'mall_part.columns.jsonl'), 'w') as handle:
                handle.write('{}\n')
            with self.assertRaisesMessage(SystemExit, 'no coincide con su sha256'):
                s10_archive.rollback(folder)


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
                passed = [{'label': 'precondición', 'value': 0, 'ok': True}]
                write_manifest(folder, tables={'mall_oemcrossreference': {'rows': 99}}, preconditions=passed)
                with self.assertRaisesMessage(RuntimeError, 'mall_oemcrossreference: 1, 99'):
                    module.require_export(cursor)
                write_manifest(folder, tables={'mall_oemcrossreference': {'rows': 1}})  # no preconditions recorded
                with self.assertRaisesMessage(RuntimeError, 'no registra las precondiciones'):
                    module.require_export(cursor)
                write_manifest(folder, tables={'mall_oemcrossreference': {'rows': 1}},
                               preconditions=[{'label': 'sin resultado de S9', 'value': 3, 'ok': False}])
                with self.assertRaisesMessage(RuntimeError, 'sin resultado de S9: 3'):
                    module.require_export(cursor)
                write_manifest(folder, tables={'mall_oemcrossreference': {'rows': 1}}, preconditions=passed)
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
        self.assertEqual(case.evidence['s10_rows'], [rows[0][0]])
        self.assertNotIn('catalog_rows', decided.evidence)

    def test_the_reverse_takes_off_only_the_rows_0088_added(self):
        unarchive('mall_oemcrossreference')
        module = migration_module('0088_archive')
        line = make_line('S10-KEEP-3')
        number = make_reference(line, '04465-02240', brand='TOYOTA', tipo='oem')
        sql("INSERT INTO mall_oemcrossreference (brand, code, number, citations, created_at, updated_at, ref_id) "
            "VALUES ('BREMBO', 'D2240', '0446502240', '[]', now(), now(), %s)", [number.pk])
        own = ['imp-1', number.pk, 'ATE', 'A2240', '0446502240', []]  # a row an import put there (mall.merge_pass.join_numbers)
        case = MatchingCase.objects.create(key='merge:s10-keep-3', kind='merge', status='review', fingerprint='x', method='', reason='',
                                           evidence={'regla': '3f', 'catalog_rows': [own]})
        case.lines.add(line)
        with connection.cursor() as cursor:
            self.assertEqual(module.keep_case_rows(apps, cursor), 1)
        self.assertEqual(module.drop_case_rows(apps), 1)
        case.refresh_from_db()
        self.assertEqual(case.evidence, {'regla': '3f', 'catalog_rows': [own]})


class CaseConversionTests(TestCase):
    """0086's convert_cases and its reverse (restore_cases) put a legacy case back as it was, its reason included."""

    def test_a_converted_case_comes_back_as_it_was(self):
        from types import SimpleNamespace
        from .models import SupplierItem
        from .test_factories import make_supplier
        step1 = migration_module('0086_contract_step1')
        for name in ('matching_case_valid_kind', 'matching_case_valid_status'):  # the legacy kinds, as before 0089
            ddl(f'ALTER TABLE mall_matchingcase DROP CONSTRAINT {name}')
        supplier = make_supplier('PROVEEDOR CASOS S10')
        item = SupplierItem.objects.create(supplier=supplier, supplier_invent_id='s10-case-1', codigo='S10-CASE', brand='', source='upload')
        MatchingCase.objects.filter(item=item).delete()
        holder = MatchingCase.objects.create(key=f'line:{item.pk}', kind='line', status='review', fingerprint='x', method='', reason='',
                                             item=item)
        legacy = MatchingCase.objects.create(key='supplier:s10-case-1', kind='supplier', status='unmatched', fingerprint='x', method='m',
                                             reason='Sin evidencia de código', item=item)
        editor = SimpleNamespace(connection=connection)
        step1.convert_cases(apps, editor)
        legacy.refresh_from_db()
        self.assertEqual((legacy.kind, legacy.status, legacy.key), ('line', 'dismissed', 'supplier:s10-case-1'))
        self.assertTrue(legacy.reason.startswith('Ya hay un caso de línea para este artículo.'))
        step1.restore_cases(apps, editor)
        legacy.refresh_from_db()
        holder.refresh_from_db()
        self.assertEqual((legacy.kind, legacy.status, legacy.key, legacy.reason), ('supplier', 'unmatched', 'supplier:s10-case-1',
                                                                                  'Sin evidencia de código'))
        self.assertNotIn('s10', legacy.evidence)
        self.assertEqual((holder.kind, holder.status), ('line', 'review'))


class DropReverseTests(TestCase):
    """0089's SQL goes back (the columns, 0086's triggers and CHECKs) and forward again on a migrated database."""

    def test_the_reverse_brings_back_the_columns_and_triggers_and_the_drop_applies_again(self):
        from .technical_data import part_type_for
        module = migration_module('0089_drop')
        pads, discs = part_type_for('FRENOS', 'PASTILLAS'), part_type_for('FRENOS', 'DISCOS')
        line = make_line('S10-REV-1', part_type=pads)
        ref = make_reference(line, 'S10-REV-A')
        # No SET CONSTRAINTS here: the reverse runs its own (this test's inserts leave deferred key checks pending, as a deploy's
        # writes do, and an index cannot be built on a table with pending trigger events).
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
