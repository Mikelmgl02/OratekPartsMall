import io
import json
from datetime import datetime

from django.core.files.uploadedfile import SimpleUploadedFile
from openpyxl import Workbook
from rest_framework.test import APITestCase

from .models import Account, CatalogImportJob, Membership, Part, PartCode, Role, StockEntry, User
from .services import ingest_inventory


class CatalogDateRecoveryTests(APITestCase):
    url = '/api/v1/management/catalog/import/'
    columns = ['SKU', 'NOMBRE', 'DESCRIPCION', 'CODIGO_ALTERNO', 'MARCA_ALTERNO', 'ACTIVO']

    def setUp(self):
        self.root = User.objects.create_superuser('recovery-root', 'recovery-root@example.invalid', 'RecoveryRoot938!')
        self.client.force_authenticate(self.root)

    def excel(self, rows, formats=None):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = 'Inventario'
        sheet.append(self.columns)
        for row in rows:
            sheet.append(row)
        for address, number_format in (formats or {}).items():
            sheet[address].number_format = number_format
        stream = io.BytesIO()
        workbook.save(stream)
        return stream.getvalue()

    def upload(self, raw, *, corrections=None, skip_rows=None, **options):
        data = {'file': SimpleUploadedFile('inventario.xlsx', raw), **options}
        if corrections is not None:
            data['corrections'] = json.dumps(corrections)
        if skip_rows is not None:
            data['skip_rows'] = json.dumps(skip_rows)
        return self.client.post(self.url, data, format='multipart')

    def job_url(self, result):
        return f"{self.url}jobs/{result['job_id']}/"

    def test_description_recovers_the_complete_code_and_date_name_before_saving(self):
        original = datetime(3042, 10, 1)
        raw = self.excel([[original, original, 'BALINERA 10-3042']], {'A2': 'mmm-yy', 'B2': 'mmm-yy'})
        response = self.upload(raw)
        self.assertEqual(response.status_code, 200)
        result = response.data
        self.assertTrue(result['valid'])
        self.assertEqual(result['preview'][0]['sku'], '10-3042')
        self.assertEqual(result['preview'][0]['name'], '10-3042')
        self.assertEqual(result['date_recovery_count'], 1)
        self.assertEqual(result['numeric_warning_count'], 0)
        audit = result['recovery_rows'][0]
        self.assertEqual((audit['row'], audit['column']), (2, 'SKU'))
        self.assertTrue(audit['automatic'])
        self.assertTrue(audit['resolved'])
        self.assertFalse(audit['skipped'])
        self.assertEqual(audit['value'], '10-3042')
        self.assertTrue(audit['reason'])
        self.assertEqual(Part.objects.count(), 0)
        saved = self.client.post(self.job_url(result), {'batch_index': 0})
        self.assertTrue(saved.data['imported'])
        self.assertEqual(Part.objects.get(sku='10-3042').name, '10-3042')

    def test_nearby_intact_numeric_hyphen_family_recovers_four_digit_suffix(self):
        original = datetime(3595, 2, 1)
        raw = self.excel([
            ['2-0741', '', 'EMP CUELLO'],
            [original, original, 'EMP CUELLO HON CIVIC'],
            ['2-0758', '', 'EMP CUELLO'],
        ], {'A3': 'mmm-yy', 'B3': 'mmm-yy'})
        result = self.upload(raw).data
        self.assertTrue(result['valid'])
        self.assertEqual([item['sku'] for item in result['preview']], ['2-0741', '2-3595', '2-0758'])
        audit = result['recovery_rows'][0]
        self.assertEqual(audit['row'], 3)
        self.assertTrue(audit['automatic'])
        self.assertEqual(audit['value'], '2-3595')
        self.assertEqual(Part.objects.count(), 0)

    def test_two_digit_year_does_not_guess_a_century_without_evidence(self):
        raw = self.excel([[datetime(2021, 10, 1), '', 'BALINERA ALT']], {'A2': 'mmm-yy'})
        result = self.upload(raw).data
        self.assertFalse(result['valid'])
        self.assertEqual(result['error_count'], 1)
        self.assertEqual(result['status'], 'review')
        self.assertEqual(result['pending_error_count'], 1)
        audit = result['recovery_rows'][0]
        self.assertFalse(audit['automatic'])
        self.assertFalse(audit['resolved'])
        self.assertTrue({'10-21', '10-2021'}.issubset(set(audit['candidates'])))
        self.assertEqual(Part.objects.count(), 0)

    def test_incidental_vehicle_year_range_is_not_evidence_of_a_complete_part_code(self):
        raw = self.excel([[datetime(2021, 10, 1), '', 'FILTRO TOYOTA CAMRY 10-21']], {'A2': 'mmm-yy'})
        result = self.upload(raw).data
        self.assertFalse(result['valid'])
        self.assertEqual(result['status'], 'review')
        self.assertEqual(result['pending_error_count'], 1)
        self.assertFalse(result['recovery_rows'][0]['automatic'])
        self.assertFalse(result['recovery_rows'][0]['resolved'])
        self.assertEqual(Part.objects.count(), 0)

    def test_ambiguous_day_month_is_corrected_in_preview_with_uppercase_and_zero_padding(self):
        original = datetime(2026, 6, 12)
        raw = self.excel([[original, original, 'ABRAZADERA INOXIDABLE 1/2']], {'A2': 'd-mmm', 'B2': 'd-mmm'})
        before = self.upload(raw).data
        self.assertFalse(before['valid'])
        self.assertTrue({'6-12', '12-6'}.issubset(set(before['recovery_rows'][0]['candidates'])))
        response = self.upload(raw, corrections=[{'row': 2, 'column': 'SKU', 'value': ' 00ab-001 '}])
        self.assertEqual(response.status_code, 200)
        result = response.data
        self.assertTrue(result['valid'])
        self.assertEqual(result['preview'][0]['sku'], '00AB-001')
        self.assertEqual(result['preview'][0]['name'], '00AB-001')
        audit = result['recovery_rows'][0]
        self.assertFalse(audit['automatic'])
        self.assertTrue(audit['resolved'])
        self.assertEqual(audit['value'], '00AB-001')
        self.assertEqual(Part.objects.count(), 0)
        self.client.post(self.job_url(result), {'batch_index': 0})
        self.assertTrue(Part.objects.filter(sku='00AB-001').exists())

    def test_date_converted_alterno_can_be_corrected_without_changing_the_sku(self):
        raw = self.excel([['CANONICAL', '', 'ALTERNATIVO', datetime(2026, 6, 12), 'OEM']], {'D2': 'd-mmm'})
        response = self.upload(raw, corrections=[{'row': 2, 'column': 'CODIGO_ALTERNO', 'value': '000alt-001'}])
        self.assertEqual(response.status_code, 200)
        result = response.data
        self.assertTrue(result['valid'])
        self.assertEqual(result['preview'][0]['sku'], 'CANONICAL')
        self.assertEqual(result['preview'][0]['codes'], [{'brand': 'OEM', 'code': '000ALT-001'}])
        self.assertEqual(result['recovery_rows'][0]['column'], 'CODIGO_ALTERNO')
        self.client.post(self.job_url(result), {'batch_index': 0})
        self.assertEqual(PartCode.objects.get().code, '000ALT-001')

    def test_explicit_skip_only_excludes_that_row_and_persists_the_audit(self):
        raw = self.excel([
            ['KEEP-001'],
            [datetime(2026, 6, 12), '', 'ABRAZADERA'],
            ['000KEEP-002'],
        ], {'A3': 'd-mmm'})
        result = self.upload(raw, skip_rows=[3]).data
        self.assertTrue(result['valid'])
        self.assertEqual(result['source_rows'], 3)
        self.assertEqual(result['skipped_rows'], [3])
        self.assertEqual(result['skipped_row_count'], 1)
        self.assertEqual(result['summary']['rows'], 2)
        self.assertEqual(result['summary']['skipped_rows'], 1)
        self.assertEqual([item['sku'] for item in result['preview']], ['KEEP-001', '000KEEP-002'])
        self.assertTrue(result['recovery_rows'][0]['skipped'])
        restored = self.client.get(self.job_url(result)).data
        for key in ['source_rows', 'skipped_rows', 'skipped_row_count', 'recovery_rows']:
            self.assertEqual(restored[key], result[key])
        saved = self.client.post(self.job_url(result), {'batch_index': 0})
        self.assertTrue(saved.data['imported'])
        self.assertEqual(saved.data['skipped_rows'], [3])
        self.assertEqual(Part.objects.count(), 2)
        self.assertTrue(Part.objects.filter(sku='000KEEP-002').exists())

    def test_ai_regrouping_preserves_original_recovery_and_skip_metadata(self):
        from .catalog_import import load_job_groups, replace_job_groups

        raw = self.excel([
            [datetime(3042, 10, 1), '', 'BALINERA 10-3042'],
            [datetime(2026, 6, 12), '', 'ABRAZADERA'],
            [146690],
        ], {'A2': 'mmm-yy', 'A3': 'd-mmm'})
        result = self.upload(raw, skip_rows=[3]).data
        self.assertTrue(result['valid'])
        self.assertEqual(result['numeric_warning_count'], 1)
        job = CatalogImportJob.objects.get(pk=result['job_id'])
        groups = load_job_groups(job)
        groups['10-3042']['fields']['category'] = 'RODAMIENTOS'
        replace_job_groups(job, groups)
        restored = self.client.get(self.job_url(result)).data
        for key in ['source_rows', 'skipped_rows', 'skipped_row_count', 'recovery_rows', 'numeric_warning_count', 'date_recovery_count']:
            self.assertEqual(restored[key], result[key], key)
        self.assertEqual(restored['summary']['skipped_rows'], 1)
        self.assertEqual(restored['warning_count'], result['warning_count'])
        self.assertEqual(restored['preview'][0]['category'], 'RODAMIENTOS')

    def test_corrected_conflicting_alterno_still_requires_a_valid_catalog_plan(self):
        existing = Part.objects.create(sku='OTHER')
        PartCode.objects.create(part=existing, code='TAKEN')
        raw = self.excel([['NEW', '', '', datetime(2026, 6, 12)]], {'D2': 'd-mmm'})
        result = self.upload(raw, corrections=[{'row': 2, 'column': 'CODIGO_ALTERNO', 'value': 'taken'}]).data
        self.assertFalse(result['valid'])
        self.assertEqual(result['errors'][0]['row'], 2)
        self.assertEqual(result['status'], 'review')
        self.assertEqual(result['pending_error_count'], 1)
        self.assertEqual(Part.objects.count(), 1)
        self.assertEqual(PartCode.objects.count(), 1)

    def test_unsupported_date_format_can_be_reviewed_but_is_never_automatically_recovered(self):
        raw = self.excel([[datetime(2026, 6, 12)]], {'A2': 'yyyy/mm/dd'})
        before = self.upload(raw).data
        self.assertFalse(before['valid'])
        self.assertFalse(before['recovery_rows'][0]['automatic'])
        response = self.upload(raw, corrections=[{'row': 2, 'column': 'SKU', 'value': 'KNOWN-001'}])
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['valid'])
        self.assertEqual(response.data['preview'][0]['sku'], 'KNOWN-001')

    def test_corrections_and_skips_cannot_bypass_normal_or_formula_row_validation(self):
        raw = self.excel([['INTACT'], ['=A2'], [datetime(2026, 6, 12)]], {'A4': 'd-mmm'})
        for corrections, skip_rows in [
            ([{'row': 2, 'column': 'SKU', 'value': 'CHANGED'}], None),
            ([{'row': 3, 'column': 'SKU', 'value': 'FORMULA-BYPASS'}], None),
            ([{'row': 4, 'column': 'NOMBRE', 'value': 'INVALID-FIELD'}], None),
            ([{'row': 5, 'column': 'SKU', 'value': 'OUTSIDE-SHEET'}], None),
            (None, [2]),
            (None, [3]),
            (None, [5]),
        ]:
            with self.subTest(corrections=corrections, skip_rows=skip_rows):
                response = self.upload(raw, corrections=corrections, skip_rows=skip_rows)
                self.assertEqual(response.status_code, 400)
        self.assertEqual(CatalogImportJob.objects.count(), 0)
        self.assertEqual(Part.objects.count(), 0)

    def test_duplicate_empty_and_overlength_corrections_are_rejected(self):
        raw = self.excel([[datetime(2026, 6, 12)]], {'A2': 'd-mmm'})
        for corrections in [
            [{'row': 2, 'column': 'SKU', 'value': 'ONE'}, {'row': 2, 'column': 'SKU', 'value': 'TWO'}],
            [{'row': 2, 'column': 'SKU', 'value': ''}],
            [{'row': 2, 'column': 'SKU', 'value': '   '}],
            [{'row': 2, 'column': 'SKU', 'value': 'X' * 201}],
            [{'row': 2, 'column': 'SKU', 'value': 123}],
        ]:
            with self.subTest(corrections=corrections):
                response = self.upload(raw, corrections=corrections)
                self.assertEqual(response.status_code, 400)
        self.assertEqual(CatalogImportJob.objects.count(), 0)

    def test_skipping_a_date_row_does_not_hide_errors_on_other_rows(self):
        raw = self.excel([[datetime(2026, 6, 12)], ['VALID', '', '', '=A2']], {'A2': 'd-mmm'})
        result = self.upload(raw, skip_rows=[2]).data
        self.assertFalse(result['valid'])
        self.assertEqual(result['error_count'], 2)
        self.assertEqual({item['row'] for item in result['errors']}, {2, 3})
        self.assertEqual(result['status'], 'review')
        self.assertEqual(result['pending_error_count'], 2)
        self.assertEqual(Part.objects.count(), 0)

    def test_reviewed_recovery_preserves_existing_catalog_ids_supplier_ids_and_stock_ledger(self):
        existing = Part.objects.create(sku='6-12', name='BEFORE')
        old_code = PartCode.objects.create(part=existing, code='OLD-CODE')
        supplier = Account.objects.create(name='Proveedor Recovery')
        supplier.roles.add(Role.objects.get(code='supplier_retail'))
        member = User.objects.create_user('recovery-supplier', 'recovery-supplier@example.invalid', 'RecoverySupplier938!')
        Membership.objects.create(user=member, account=supplier)
        stock = ingest_inventory(supplier=supplier, actor=member, data={
            'update_id': 'before-recovery', 'supplier_invent_id': 'stable-recovery-id',
            'codigo': 'OLD-CODE', 'brand': 'OEM', 'source': 'upload', 'quantity': 7,
        })
        stock_id = stock.pk
        ledger_before = list(StockEntry.objects.values())
        raw = self.excel([[datetime(2026, 6, 12), '', 'ABRAZADERA', 'NEW-ALT']], {'A2': 'd-mmm'})
        result = self.upload(raw, corrections=[{'row': 2, 'column': 'SKU', 'value': '6-12'}]).data
        self.assertTrue(result['valid'])
        self.assertEqual(Part.objects.count(), 1)
        self.client.post(self.job_url(result), {'batch_index': 0})
        existing.refresh_from_db()
        stock.refresh_from_db()
        self.assertEqual(Part.objects.get(sku='6-12').pk, existing.pk)
        self.assertEqual(stock.pk, stock_id)
        self.assertEqual(stock.supplier_invent_id, 'stable-recovery-id')
        self.assertEqual(stock.part_id, existing.pk)
        self.assertEqual(stock.reported_quantity, 7)
        self.assertTrue(PartCode.objects.filter(pk=old_code.pk).exists())
        self.assertEqual(list(StockEntry.objects.values()), ledger_before)
