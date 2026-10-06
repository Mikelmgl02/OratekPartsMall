"""Import valid rows now without losing the source rows that need repair."""
import io
from datetime import datetime
from urllib.parse import urlsplit

from django.core.files.uploadedfile import SimpleUploadedFile
from openpyxl import Workbook
from rest_framework.test import APITestCase

from .models import (
    Account, CatalogImportIssue, CatalogImportJob, Membership, Part, PartCode,
    Role, StockEntry, User,
)
from .services import ingest_inventory


class CatalogPartialImportTests(APITestCase):
    import_url = '/api/v1/management/catalog/import/'
    issues_url = f'{import_url}issues/'
    columns = ['SKU', 'NOMBRE', 'DESCRIPCION', 'CODIGO_ALTERNO', 'MARCA_ALTERNO', 'ACTIVO']

    def setUp(self):
        self.root = User.objects.create_superuser('partial-root', 'partial-root@example.invalid', 'PartialRoot938!')
        self.client.force_authenticate(self.root)

    def workbook(self, rows, headers=None):
        book = Workbook()
        sheet = book.active
        sheet.title = 'Inventario'
        sheet.append(headers or self.columns)
        for row in rows:
            sheet.append(row)
        stream = io.BytesIO()
        book.save(stream)
        return stream.getvalue()

    def preview(self, rows, **options):
        raw = self.workbook(rows)
        response = self.client.post(self.import_url, {
            'file': SimpleUploadedFile('partial-inventory.xlsx', raw), **options,
        }, format='multipart')
        self.assertEqual(response.status_code, 200)
        return response.data

    def complete(self, job_id):
        url = f'{self.import_url}jobs/{job_id}/'
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        for index in range(response.data['progress']['completed_batches'], response.data['progress']['total_batches']):
            response = self.client.post(url, {'batch_index': index}, format='json')
            self.assertEqual(response.status_code, 200)
        return response.data

    def repair(self, issue, values):
        response = self.client.post(f'{self.issues_url}{issue.pk}/', {'values': values}, format='json')
        self.assertEqual(response.status_code, 200)
        return response.data

    def test_valid_rows_are_staged_and_imported_while_date_row_remains_repairable(self):
        date = datetime(2026, 6, 12)
        preview = self.preview([
            ['PARTIAL-VALID', 'válido', '', 'PARTIAL-VALID-ALT'],
            [date, date, 'ABRAZADERA INOXIDABLE 1/2'],
        ])
        self.assertTrue(preview['valid'])
        self.assertEqual(preview['summary']['rows'], 1)
        self.assertEqual(preview['summary']['rejected_rows'], 1)
        self.assertEqual(preview['source_rows'], 2)
        self.assertEqual(preview['rejected_row_count'], 1)
        self.assertEqual(preview['pending_error_count'], 1)
        self.assertEqual(preview['preview_count'], 1)
        self.assertEqual(preview['preview'][0]['sku'], 'PARTIAL-VALID')
        self.assertEqual(Part.objects.count(), 0)
        issue = CatalogImportIssue.objects.get(job_id=preview['job_id'])
        self.assertEqual(issue.row, 3)
        self.assertEqual(issue.status, 'pending')
        self.assertTrue(issue.original)
        self.assertTrue(issue.recovery)
        original = issue.original
        completed = self.complete(preview['job_id'])
        self.assertTrue(completed['imported'])
        self.assertEqual(completed['pending_error_count'], 1)
        self.assertEqual(Part.objects.count(), 1)
        self.assertEqual(Part.objects.get().sku, 'PARTIAL-VALID')
        issue.refresh_from_db()
        self.assertEqual(issue.original, original)
        fixed = self.repair(issue, {'sku': '6-12', 'name': 'abrazadera inoxidable 1/2'})
        self.assertTrue(fixed['valid'])
        self.assertEqual(Part.objects.get(sku='6-12').name, 'ABRAZADERA INOXIDABLE 1/2')
        self.assertEqual(self.client.get(self.issues_url).data['pending_count'], 0)
        issue.refresh_from_db()
        self.assertEqual(issue.original, original)
        self.assertEqual(issue.status, 'resolved')

    def test_all_rejected_rows_still_create_a_durable_review_job(self):
        preview = self.preview([[datetime(2026, 6, 12), '', 'ABRAZADERA'], ['BAD-ACTIVE', '', '', '', '', 'MAYBE']])
        self.assertFalse(preview['valid'])
        self.assertEqual(preview['status'], 'review')
        self.assertEqual(preview['summary']['rows'], 0)
        self.assertEqual(preview['rejected_row_count'], 2)
        self.assertEqual(preview['progress']['total_batches'], 0)
        job = CatalogImportJob.objects.get(pk=preview['job_id'])
        self.assertEqual(job.issues.count(), 2)
        self.assertEqual(job.batches.count(), 0)
        reread = self.client.get(f'{self.import_url}jobs/{job.pk}/')
        self.assertEqual(reread.data['pending_error_count'], 2)
        issue = job.issues.get(row=2)
        self.assertTrue(self.repair(issue, {'sku': '6-12', 'name': 'abrazadera'})['valid'])
        self.assertEqual(Part.objects.count(), 1)
        self.assertEqual(self.client.get(self.issues_url).data['pending_count'], 1)

    def test_every_rejected_row_is_saved_even_when_preview_truncates_errors(self):
        preview = self.preview([['GOOD']] + [[f'REJECT-{index:03}', '', '', '', '', 'MAYBE'] for index in range(131)])
        self.assertTrue(preview['valid'])
        self.assertEqual(preview['summary']['rows'], 1)
        self.assertEqual(preview['rejected_row_count'], 131)
        self.assertEqual(CatalogImportIssue.objects.filter(job_id=preview['job_id']).count(), 131)
        url = self.issues_url
        rows = []
        while url:
            page = self.client.get(url)
            self.assertEqual(page.status_code, 200)
            self.assertEqual(page.data['count'], 131)
            self.assertEqual(page.data['pending_count'], 131)
            rows.extend(item['row'] for item in page.data['results'])
            next_url = page.data['next']
            parsed = urlsplit(next_url) if next_url else None
            url = f'{parsed.path}?{parsed.query}' if parsed else None
        self.assertEqual(sorted(rows), list(range(3, 134)))
        self.complete(preview['job_id'])
        self.assertEqual(Part.objects.count(), 1)

    def test_inconsistent_repeated_sku_defers_every_contributing_row(self):
        preview = self.preview([
            ['CONFLICT', 'FIRST', '', 'ALTERNATE-A'],
            ['CONFLICT', 'SECOND', '', 'ALTERNATE-B'],
            ['UNRELATED', 'VALID'],
        ])
        self.assertTrue(preview['valid'])
        self.assertEqual(preview['summary']['rows'], 1)
        self.assertEqual(preview['rejected_row_count'], 2)
        self.assertEqual(set(CatalogImportIssue.objects.filter(job_id=preview['job_id']).values_list('row', flat=True)), {2, 3})
        self.complete(preview['job_id'])
        self.assertEqual(list(Part.objects.values_list('sku', flat=True)), ['UNRELATED'])
        self.assertEqual(PartCode.objects.count(), 0)

    def test_invalid_row_with_readable_sku_defers_its_other_group_rows(self):
        preview = self.preview([
            ['SAME-SKU', 'FIRST', '', 'ALTERNATE-A'],
            ['SAME-SKU', '', '', '', '', 'MAYBE'],
            ['UNRELATED'],
        ])
        self.assertTrue(preview['valid'])
        self.assertEqual(preview['rejected_row_count'], 2)
        self.assertEqual(preview['summary']['rows'], 1)
        self.complete(preview['job_id'])
        self.assertFalse(Part.objects.filter(sku='SAME-SKU').exists())
        self.assertTrue(Part.objects.filter(sku='UNRELATED').exists())

    def test_shared_alias_conflict_defers_both_groups_and_preserves_unrelated_rows(self):
        preview = self.preview([
            ['FIRST', '', '', 'SHARED'],
            ['SECOND', '', '', 'SHARED'],
            ['UNRELATED', '', '', 'UNIQUE'],
        ])
        self.assertTrue(preview['valid'])
        self.assertEqual(preview['summary']['rows'], 1)
        self.assertEqual(preview['rejected_row_count'], 2)
        self.complete(preview['job_id'])
        self.assertEqual(list(Part.objects.values_list('sku', flat=True)), ['UNRELATED'])
        self.assertEqual(list(PartCode.objects.values_list('code', flat=True)), ['UNIQUE'])

    def test_repair_revalidates_current_catalog_and_can_be_retried_after_conflict(self):
        preview = self.preview([[datetime(2026, 6, 12), '', 'ABRAZADERA']])
        issue = CatalogImportIssue.objects.get(job_id=preview['job_id'])
        existing = Part.objects.create(sku='EXISTING')
        PartCode.objects.create(part=existing, code='6-12')
        invalid = self.repair(issue, {'sku': '6-12'})
        self.assertFalse(invalid['valid'])
        self.assertTrue(invalid['errors'])
        self.assertEqual(Part.objects.count(), 1)
        issue.refresh_from_db()
        self.assertEqual(issue.status, 'pending')
        self.assertEqual(issue.values['sku'], '6-12')
        valid = self.repair(issue, {'sku': 'CORRECTED-6-12'})
        self.assertTrue(valid['valid'])
        self.assertEqual(Part.objects.count(), 2)
        self.assertTrue(Part.objects.filter(sku='CORRECTED-6-12').exists())

    def test_repair_is_idempotent_and_does_not_change_stock_or_internal_ids(self):
        existing = Part.objects.create(sku='STOCKED', name='OLD NAME')
        old_code = PartCode.objects.create(part=existing, code='STOCKED-ALT')
        other = Part.objects.create(sku='OTHER')
        PartCode.objects.create(part=other, code='OWNED-BY-OTHER')
        supplier_user = User.objects.create_user('partial-supplier', 'partial-supplier@example.invalid', 'Supplier938!')
        supplier = Account.objects.create(name='Supplier Partial')
        supplier.roles.add(Role.objects.get(code='supplier_retail'))
        Membership.objects.create(user=supplier_user, account=supplier)
        stock = ingest_inventory(supplier=supplier, actor=supplier_user, data={
            'update_id': 'partial-before', 'supplier_invent_id': 'stable-supplier-id',
            'codigo': old_code.code, 'brand': 'OEM', 'source': 'upload', 'quantity': 7,
        })
        ledger_before = list(StockEntry.objects.values())
        stock_id, part_id = stock.pk, existing.pk
        preview = self.preview([['GOOD'], ['DEFER', 'UPDATED NAME', '', 'OWNED-BY-OTHER']])
        self.assertTrue(preview['valid'])
        self.complete(preview['job_id'])
        issue = CatalogImportIssue.objects.get(job_id=preview['job_id'])
        fixed = self.repair(issue, {'sku': existing.sku, 'code': 'SAFE-NEW-CODE'})
        self.assertTrue(fixed['valid'])
        self.assertEqual(str(fixed['part_id']), str(part_id))
        self.assertEqual(fixed['summary']['updated_skus'], 1)
        self.assertEqual(Part.objects.count(), 3)
        retry = self.repair(issue, {'sku': 'SHOULD-NOT-APPEAR', 'code': 'SHOULD-NOT-APPEAR'})
        self.assertTrue(retry['valid'])
        self.assertEqual(retry['part_id'], fixed['part_id'])
        self.assertEqual(retry['summary'], fixed['summary'])
        self.assertFalse(Part.objects.filter(sku='SHOULD-NOT-APPEAR').exists())
        existing.refresh_from_db()
        stock.refresh_from_db()
        self.assertEqual(existing.pk, part_id)
        self.assertEqual(existing.name, 'UPDATED NAME')
        self.assertEqual(stock.pk, stock_id)
        self.assertEqual(stock.part_id, part_id)
        self.assertEqual(stock.supplier_invent_id, 'stable-supplier-id')
        self.assertEqual(stock.reported_quantity, 7)
        self.assertEqual(list(StockEntry.objects.values()), ledger_before)
        self.assertTrue(PartCode.objects.filter(pk=old_code.pk).exists())

    def test_invalid_workbook_headers_remain_file_errors_without_review_jobs(self):
        for raw in [b'not a workbook', self.workbook([['BAD']], headers=['UNKNOWN']), self.workbook([], headers=['SKU'])]:
            response = self.client.post(self.import_url, {
                'file': SimpleUploadedFile('invalid.xlsx', raw),
            }, format='multipart')
            self.assertEqual(response.status_code, 400)
        self.assertEqual(CatalogImportJob.objects.count(), 0)
        self.assertEqual(CatalogImportIssue.objects.count(), 0)

