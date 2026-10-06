"""Revising a staged import must retain ownership and rejected source rows."""
import io
import json
from datetime import datetime, timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from openpyxl import Workbook
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from .catalog_import import load_job_groups, replace_job_groups
from .models import CatalogImportIssue, CatalogImportJob, Part, PartCode, User


class CatalogImportRevisionTests(APITestCase):
    url = '/api/v1/management/catalog/import/'

    def setUp(self):
        self.root = User.objects.create_superuser('revision-root', 'revision-root@example.invalid', 'RevisionRoot938!')
        self.client.force_authenticate(self.root)

    def workbook(self, rows):
        book = Workbook()
        sheet = book.active
        sheet.title = 'Inventario'
        sheet.append(['SKU', 'NOMBRE', 'DESCRIPCION', 'CODIGO_ALTERNO', 'MARCA_ALTERNO', 'ACTIVO'])
        for row in rows:
            sheet.append(row)
        stream = io.BytesIO()
        book.save(stream)
        return stream.getvalue()

    def upload(self, raw, **options):
        return self.client.post(self.url, {
            'file': SimpleUploadedFile('revision.xlsx', raw), **options,
        }, format='multipart')

    def date_row(self):
        date = datetime(2026, 6, 12)
        return [date, date, 'ABRAZADERA INOXIDABLE 1/2']

    def correction(self, row):
        return json.dumps([{'row': row, 'column': 'SKU', 'value': '6-12'}])

    def test_repreviewing_the_same_file_replaces_its_pending_plan_without_duplicate_jobs_or_parts(self):
        raw = self.workbook([['REVISION-GOOD'], self.date_row()])
        initial = self.upload(raw)
        self.assertEqual(initial.status_code, 200)
        job_id = initial.data['job_id']
        self.assertEqual(initial.data['pending_error_count'], 1)
        self.assertEqual(CatalogImportIssue.objects.filter(job_id=job_id).count(), 1)
        revised = self.upload(raw, job_id=job_id, corrections=self.correction(3))
        self.assertEqual(revised.status_code, 200, revised.data)
        self.assertEqual(revised.data['job_id'], job_id)
        self.assertTrue(revised.data['valid'])
        self.assertEqual(revised.data['summary']['rows'], 2)
        self.assertEqual(revised.data['pending_error_count'], 0)
        self.assertEqual(revised.data['rejected_row_count'], 0)
        self.assertEqual(CatalogImportJob.objects.count(), 1)
        self.assertEqual(CatalogImportIssue.objects.count(), 0)
        job = CatalogImportJob.objects.get(pk=job_id)
        self.assertEqual(set(load_job_groups(job)), {'REVISION-GOOD', '6-12'})
        self.assertEqual(job.total_skus, 2)
        self.assertEqual(job.total_batches, 1)
        self.assertEqual(Part.objects.count(), 0)
        self.assertNotIn('_file_hash', revised.data)

    def test_other_owner_changed_file_and_started_import_cannot_replace_the_original_issues(self):
        raw = self.workbook([[f'REVISION-{index:04}'] for index in range(501)] + [self.date_row()])
        initial = self.upload(raw)
        self.assertEqual(initial.status_code, 200)
        job_id = initial.data['job_id']
        issue = CatalogImportIssue.objects.get(job_id=job_id)
        original = issue.original
        other = User.objects.create_superuser('revision-other', 'revision-other@example.invalid', 'RevisionOther938!')
        self.client.force_authenticate(other)
        denied = self.upload(raw, job_id=job_id, corrections=self.correction(503))
        self.assertEqual(denied.status_code, 404)
        self.client.force_authenticate(self.root)
        changed = self.upload(self.workbook([['DIFFERENT-FILE'], self.date_row()]), job_id=job_id, corrections=self.correction(3))
        self.assertEqual(changed.status_code, 409)
        first = self.client.post(f'{self.url}jobs/{job_id}/', {'batch_index': 0}, format='json')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data['status'], 'importing')
        self.assertEqual(Part.objects.count(), 500)
        started = self.upload(raw, job_id=job_id, corrections=self.correction(503))
        self.assertEqual(started.status_code, 409)
        issue.refresh_from_db()
        self.assertEqual(issue.original, original)
        self.assertEqual(issue.status, 'pending')
        self.assertEqual(CatalogImportIssue.objects.filter(job_id=job_id).count(), 1)
        job = CatalogImportJob.objects.get(pk=job_id)
        self.assertEqual(job.completed_batches, 1)
        self.assertEqual(job.total_skus, 501)
        self.assertEqual(Part.objects.count(), 500)
        self.assertFalse(Part.objects.filter(sku='6-12').exists())

    def test_old_pending_review_and_completed_rows_survive_cleanup_and_remain_repairable(self):
        review = self.upload(self.workbook([self.date_row()])).data
        completed = self.upload(self.workbook([['OLD-COMPLETED-GOOD'], self.date_row()])).data
        committed = self.client.post(f"{self.url}jobs/{completed['job_id']}/", {'batch_index': 0}, format='json')
        self.assertTrue(committed.data['imported'])
        clean = self.upload(self.workbook([['EXPIRED-CLEAN-DRAFT']])).data
        old_ids = [review['job_id'], completed['job_id'], clean['job_id']]
        CatalogImportJob.objects.filter(pk__in=old_ids).update(expires_at=timezone.now() - timedelta(days=31))
        fresh = self.upload(self.workbook([['FRESH-DRAFT']]))
        self.assertEqual(fresh.status_code, 200)
        self.assertTrue(CatalogImportJob.objects.filter(pk=review['job_id']).exists())
        self.assertTrue(CatalogImportJob.objects.filter(pk=completed['job_id']).exists())
        self.assertFalse(CatalogImportJob.objects.filter(pk=clean['job_id']).exists())
        pending = self.client.get(f'{self.url}issues/')
        self.assertEqual(pending.status_code, 200)
        self.assertEqual(pending.data['pending_count'], 2)
        for job_id, sku in [(review['job_id'], 'OLD-REVIEW-REPAIRED'), (completed['job_id'], 'OLD-COMPLETED-REPAIRED')]:
            issue = CatalogImportIssue.objects.get(job_id=job_id)
            fixed = self.client.post(f'{self.url}issues/{issue.pk}/', {'values': {'sku': sku}}, format='json')
            self.assertEqual(fixed.status_code, 200, fixed.data)
            self.assertTrue(fixed.data['valid'])
            self.assertTrue(Part.objects.filter(sku=sku).exists())
        self.assertEqual(self.client.get(f'{self.url}issues/').data['pending_count'], 0)
        self.assertEqual(Part.objects.count(), 3)

    def test_ai_plan_replacement_cannot_hide_a_new_conflict_behind_rejected_row_metadata(self):
        initial = self.upload(self.workbook([['AI-REVISION-GOOD'], self.date_row()])).data
        job = CatalogImportJob.objects.get(pk=initial['job_id'])
        self.assertEqual(job.preview['error_count'], 1)
        self.assertEqual(job.issues.count(), 1)
        source_data = list(job.batches.values_list('data', flat=True))
        source_summary = job.summary.copy()
        existing = Part.objects.create(sku='ALIAS-OWNER')
        PartCode.objects.create(part=existing, code='OWNED-ALIAS')
        proposed = load_job_groups(job)
        proposed['AI-REVISION-GOOD']['fields']['category'] = 'FRENOS'
        proposed['AI-REVISION-GOOD']['codes'][('', 'OWNED-ALIAS')] = 2
        with self.assertRaises(ValidationError):
            replace_job_groups(job, proposed)
        job.refresh_from_db()
        self.assertEqual(job.summary, source_summary)
        self.assertEqual(list(job.batches.values_list('data', flat=True)), source_data)
        self.assertEqual(job.issues.count(), 1)
        self.assertEqual(job.preview['pending_error_count'], 1)
        self.assertEqual(Part.objects.count(), 1)
        self.assertFalse(Part.objects.filter(sku='AI-REVISION-GOOD').exists())

