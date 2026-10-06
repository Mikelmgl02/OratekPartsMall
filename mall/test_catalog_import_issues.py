from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APITestCase

from .models import CatalogImportIssue, CatalogImportJob, Part, PartCode, User


class CatalogImportIssueApiTests(APITestCase):
    url = '/api/v1/management/catalog/import/issues/'

    def setUp(self):
        self.root = User.objects.create_superuser('issue-root', 'issue-root@example.invalid', 'IssueRoot938!')
        self.other = User.objects.create_superuser('issue-other', 'issue-other@example.invalid', 'IssueOther938!')
        self.staff = User.objects.create_user('issue-staff', 'issue-staff@example.invalid', 'IssueStaff938!', is_staff=True)
        self.client.force_authenticate(self.root)
        self.job = CatalogImportJob.objects.create(owner=self.root, filename='IMPORTADO.xlsx', status='completed', expires_at=timezone.now() - timedelta(days=30))
        self.issue = self.make_issue(self.job, 276, sku='', description='ABRAZADERA INOXIDABLE 1/2')

    def make_issue(self, job, row, **values):
        return CatalogImportIssue.objects.create(
            job=job, row=row, values={**dict.fromkeys(['sku', 'name', 'description', 'code', 'brand', 'active', 'category', 'subcategory'], ''), **values},
            original={'sku': {'kind': 'date', 'value': '2026-06-12', 'number_format': 'd-mmm'}},
            errors=[{'row': row, 'column': 'SKU', 'message': 'Revisa el código convertido en fecha.'}],
            recovery=[{'suggested_code': '6-12'}],
        )

    def detail(self, issue=None):
        return f'{self.url}{(issue or self.issue).pk}/'

    def test_queue_requires_current_superuser_and_owner(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.client.force_authenticate(self.staff)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.detail(), {'values': {'sku': '6-12'}}, format='json').status_code, 403)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(self.url).data['count'], 0)
        self.assertEqual(self.client.get(self.detail()).status_code, 404)
        self.assertEqual(self.client.post(self.detail(), {'values': {'sku': '6-12'}}, format='json').status_code, 404)

    def test_list_pagination_filters_and_count_are_owner_scoped(self):
        for row in range(2, 62):
            self.make_issue(self.job, row, sku=f'PENDIENTE-{row}')
        resolved = self.make_issue(self.job, 70, sku='RESUELTA')
        resolved.status = 'resolved'
        resolved.save()
        other_job = CatalogImportJob.objects.create(owner=self.other, filename='OTHER.xlsx', expires_at=timezone.now())
        self.make_issue(other_job, 2, sku='PRIVATE')
        response = self.client.get(self.url)
        self.assertEqual(response.data['count'], 61)
        self.assertEqual(response.data['pending_count'], 61)
        self.assertEqual(len(response.data['results']), 50)
        self.assertIsNotNone(response.data['next'])
        self.assertEqual(self.client.get(self.url, {'status': 'resolved'}).data['count'], 1)
        self.assertEqual(self.client.get(self.url, {'search': 'INOXIDABLE'}).data['count'], 1)
        self.assertEqual(self.client.get(self.url, {'job': str(other_job.pk)}).data['count'], 0)
        self.assertEqual(self.client.get(self.url, {'job': 'invalid'}).status_code, 400)
        self.assertEqual(self.client.get(self.url, {'status': 'invalid'}).status_code, 400)
        detail = self.client.get(self.detail()).data
        self.assertEqual(detail['job_id'], str(self.job.pk))
        self.assertEqual(detail['filename'], 'IMPORTADO.xlsx')
        self.assertEqual(detail['original'], self.issue.original)
        self.assertEqual(detail['recovery'], self.issue.recovery)

    def test_completed_expired_job_repairs_and_repeated_save_is_idempotent(self):
        response = self.client.post(self.detail(), {'values': {'sku': ' 6-12 ', 'name': 'abrazadera'}}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['valid'])
        part = Part.objects.get(sku='6-12')
        self.assertEqual(part.name, 'ABRAZADERA')
        self.assertEqual(response.data['summary']['created_skus'], 1)
        self.assertEqual(response.data['part_id'], str(part.pk))
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.status, 'resolved')
        self.assertIsNotNone(self.issue.resolved_at)
        again = self.client.post(self.detail(), {'values': {'sku': 'DIFFERENT'}}, format='json')
        self.assertTrue(again.data['valid'])
        self.assertEqual(again.data['summary'], response.data['summary'])
        self.assertEqual(Part.objects.count(), 1)
        self.assertFalse(Part.objects.filter(sku='DIFFERENT').exists())
        self.assertEqual(self.client.get(self.url).data['pending_count'], 0)

    def test_job_must_finish_valid_batches_before_error_correction(self):
        self.job.status = 'ready'
        self.job.expires_at = timezone.now() + timedelta(days=7)
        self.job.save()
        self.assertEqual(self.client.post(self.detail(), {'values': {'sku': '6-12'}}, format='json').status_code, 409)
        self.assertEqual(Part.objects.count(), 0)
        self.job.status = 'review'
        self.job.save()
        self.assertTrue(self.client.post(self.detail(), {'values': {'sku': '6-12'}}, format='json').data['valid'])

    def test_expired_unfinished_job_keeps_its_rejected_rows_repairable(self):
        self.job.status = 'importing'
        self.job.save()
        self.assertTrue(self.client.get(self.detail()).data['can_repair'])
        repaired = self.client.post(self.detail(), {'values': {'sku': '6-12'}}, format='json')
        self.assertTrue(repaired.data['valid'])
        self.assertEqual(Part.objects.get().sku, '6-12')

    def test_invalid_edits_are_saved_without_catalog_changes(self):
        response = self.client.post(self.detail(), {'values': {'sku': 'new', 'active': 'MAYBE', 'brand': 'oem'}}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['valid'])
        self.assertEqual({error['column'] for error in response.data['errors']}, {'ACTIVO', 'CODIGO_ALTERNO'})
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.values['sku'], 'NEW')
        self.assertEqual(self.issue.values['brand'], 'OEM')
        self.assertEqual(self.issue.status, 'pending')
        self.assertEqual(Part.objects.count(), 0)
        for values in [{'sku': 612}, {'unknown': 'FIELD'}, ['invalid']]:
            self.assertEqual(self.client.post(self.detail(), {'values': values}, format='json').status_code, 400)
        long_value = self.client.post(self.detail(), {'values': {'sku': 'A' * 201, 'brand': '', 'active': ''}}, format='json')
        self.assertFalse(long_value.data['valid'])
        self.assertEqual(long_value.data['errors'][0]['column'], 'SKU')

    def test_original_formula_must_be_replaced_and_minus_code_is_accepted(self):
        self.issue.original = {'code': {'kind': 'formula', 'value': '=A2', 'number_format': 'General'}}
        self.issue.values.update({'sku': 'SKU-1', 'code': '=A2'})
        self.issue.save()
        rejected = self.client.post(self.detail(), {'values': {'name': 'NUEVO'}}, format='json')
        self.assertFalse(rejected.data['valid'])
        self.assertEqual(rejected.data['errors'][0]['column'], 'CODIGO_ALTERNO')
        corrected = self.client.post(self.detail(), {'values': {'code': '-ABC'}}, format='json')
        self.assertTrue(corrected.data['valid'])
        self.assertEqual(PartCode.objects.get().code, '-ABC')

    def test_blank_fields_preserve_existing_metadata_and_correct_copied_date_name(self):
        existing = Part.objects.create(sku='6-12', name='NOMBRE EXISTENTE', active=False)
        self.issue.values['description'] = ''
        self.issue.save()
        result = self.client.post(self.detail(), {'values': {'sku': '6-12'}}, format='json')
        self.assertTrue(result.data['valid'])
        existing.refresh_from_db()
        self.assertEqual(existing.name, 'NOMBRE EXISTENTE')
        self.assertFalse(existing.active)
        copied = self.make_issue(self.job, 282, description='ABRAZADERA')
        copied.original['name'] = dict(copied.original['sku'])
        copied.save()
        self.assertTrue(self.client.post(self.detail(copied), {'values': {'sku': '9-16'}}, format='json').data['valid'])
        self.assertEqual(Part.objects.get(sku='9-16').name, '9-16')
