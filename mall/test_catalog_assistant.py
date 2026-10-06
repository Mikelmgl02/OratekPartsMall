import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from .catalog_assistant_models import CatalogAssistantJob
from .catalog_classification import ClassificationProviderError
from .models import Account, CatalogMerge, Part, PartCode, Role, StockEntry, SupplierItem, User
from .services import ingest_inventory


@override_settings(GEMINI_API_KEY='assistant-test-key')
class CatalogAssistantTests(APITestCase):
    url = '/api/v1/management/catalog/assistant/'

    def setUp(self):
        self.root = User.objects.create_superuser('assistant-root', 'assistant-root@example.invalid', 'Assistant938!')
        self.staff = User.objects.create_user('assistant-staff', 'assistant-staff@example.invalid', 'Assistant938!', is_staff=True)
        self.client.force_authenticate(self.root)
        self.base = Part.objects.create(sku='58411-1R000', description='TAMBOR DE FRENO HYUNDAI ACCENT')
        self.variant = Part.objects.create(sku='58411-1R000-G', description=self.base.description)

    def start(self, **extra):
        response = self.client.post(self.url, {'id': str(uuid.uuid4()), 'scope': 'unclassified', **extra}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def action(self, job, **data):
        return self.client.post(f'{self.url}{job["id"]}/', data, format='json')

    def provider(self, source, candidates, allowed, instructions='', **kwargs):
        return {'suggestions': [{
            'source_sku': row['sku'], 'target_sku': self.base.sku if row['sku']==self.variant.sku else row['sku'],
            'category': 'frenos', 'subcategory': 'tambores de freno', 'confidence': 0.85,
            'reason': 'Revisar el código base completo y la descripción del tambor.',
            'source_reference': self.base.sku if row['sku']==self.variant.sku else '',
            'target_reference': self.base.sku if row['sku']==self.variant.sku else '',
        } for row in source]}

    def analyzed(self, **extra):
        job = self.start(**extra)
        with patch('mall.catalog_assistant.call_provider', side_effect=self.provider):
            response = self.action(job, mode='classify', batch_index=0)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def decisions(self, job):
        return [{'id': item['id'], 'target_sku': item['target_sku'], 'category': item['category'], 'subcategory': item['subcategory']}
                for item in job['results']]

    def test_superuser_owner_scope_and_configuration(self):
        job = self.start()
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.client.force_authenticate(self.staff)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.action(job, mode='classify', batch_index=0).status_code, 403)
        other = User.objects.create_superuser('assistant-other', 'assistant-other@example.invalid', 'Assistant938!')
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(self.url).data['jobs'], [])
        self.assertEqual(self.action(job, mode='classify', batch_index=0).status_code, 404)
        self.client.force_authenticate(self.root)
        with override_settings(GEMINI_API_KEY='', GEM_API_KEY=''):
            self.assertFalse(self.client.get(self.url).data['configured'])
            self.assertEqual(self.action(job, mode='classify', batch_index=0).status_code, 503)

    def test_filtered_scope_idempotent_create_and_candidates_outside_filter(self):
        self.base.category, self.base.subcategory = 'FRENOS', 'TAMBORES'
        self.base.save()
        self.assertEqual(self.client.get(self.url).data['eligible_count'], 1)
        self.assertEqual(self.client.get(self.url, {'scope':'all'}).data['eligible_count'], 2)
        job = self.start(search=self.variant.sku.lower(), instructions='usar frenos')
        self.assertEqual(job['search'], self.variant.sku)
        self.assertEqual(job['instructions'], 'USAR FRENOS')
        repeated = self.client.post(self.url, {'id':job['id'], 'scope':'unclassified', 'search':self.variant.sku, 'instructions':'USAR FRENOS'}, format='json')
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(CatalogAssistantJob.objects.count(), 1)
        with patch('mall.catalog_assistant.call_provider', side_effect=self.provider) as provider:
            result = self.action(job, mode='classify', batch_index=0)
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual((provider.call_args.kwargs['instructions'], provider.call_args.kwargs['feature']), ('USAR FRENOS', 'catalog_assistant'))
        self.assertEqual(result.data['results'][0]['target_sku'], self.base.sku)
        self.assertEqual(result.data['results'][0]['candidates'][0]['sku'], self.base.sku)
        self.assertEqual(self.client.post(self.url, {'id':job['id'],'scope':'all'}, format='json').status_code, 409)

    def test_resumable_batches_and_retries_do_not_write_catalog(self):
        Part.objects.bulk_create([Part(sku=f'AI-{index:06}', description='FILTRO DE ACEITE') for index in range(53)])
        job = self.start()
        with patch('mall.catalog_assistant.call_provider', side_effect=self.provider) as provider:
            result = self.action(job, mode='classify', batch_index=0)
            self.assertEqual((result.data['completed_batches'],result.data['count']), (1,50))
            retry = self.action(job, mode='classify', batch_index=0)
            self.assertEqual(retry.data['count'],50)
            self.assertEqual(provider.call_count,1)
            self.assertEqual(self.action(job, mode='classify', batch_index=2).status_code,400)
            last = self.action(job, mode='classify', batch_index=1)
            self.assertEqual((last.data['status'], last.data['count']), ('completed',55))
            page = self.client.get(f'{self.url}{job["id"]}/', {'offset':50})
            self.assertEqual(len(page.data['results']),5)
        self.assertEqual(Part.objects.filter(category='').count(),55)
        self.assertFalse(CatalogMerge.objects.exists())

    def test_reviewed_merge_preserves_stock_and_is_idempotent(self):
        supplier = Account.objects.create(name='ASSISTANT SUPPLIER')
        supplier.roles.add(Role.objects.get(code='supplier_retail'))
        item = ingest_inventory(supplier=supplier, actor=self.root, data={'update_id':'assistant-stock', 'supplier_invent_id':'STABLE-123',
            'codigo':self.variant.sku, 'brand':'GENERIC', 'source':'upload', 'quantity':12})
        before = SupplierItem.objects.values().get(pk=item.pk)
        ledger = list(StockEntry.objects.values())
        job = self.analyzed()
        decisions = self.decisions(job)
        for value in decisions:
            value['category'] = 'frenos'
        reviewed = self.action(job, mode='apply', decisions=decisions)
        self.assertEqual(reviewed.status_code,200,reviewed.data)
        self.assertEqual(reviewed.data['applied_count'],2)
        self.base.refresh_from_db();self.variant.refresh_from_db()
        self.assertEqual(self.base.category,'FRENOS')
        self.assertEqual(self.variant.merged_into_id,self.base.pk)
        self.assertTrue(PartCode.objects.filter(part=self.base,code=self.variant.sku).exists())
        self.assertEqual(SupplierItem.objects.values().get(pk=item.pk),{**before,'part_id':self.base.pk})
        self.assertEqual(list(StockEntry.objects.values()),ledger)
        self.assertEqual(self.action(job,mode='apply',decisions=decisions).status_code,200)
        self.assertEqual(CatalogMerge.objects.count(),1)
        self.assertEqual(list(StockEntry.objects.values()),ledger)

    def test_bad_provider_output_and_failure_release_claim_for_retry(self):
        job = self.start()
        with patch('mall.catalog_assistant.call_provider',return_value={'suggestions':[]}):
            self.assertEqual(self.action(job,mode='classify',batch_index=0).status_code,502)
        saved = CatalogAssistantJob.objects.get(pk=job['id'])
        self.assertIsNone(saved.claim_id)
        self.assertEqual(saved.suggestions.count(),0)
        with patch('mall.catalog_assistant.call_provider',side_effect=ClassificationProviderError('PROVIDER LIMIT')):
            self.assertEqual(self.action(job,mode='classify',batch_index=0).status_code,502)
        with patch('mall.catalog_assistant.call_provider',side_effect=self.provider):
            self.assertEqual(self.action(job,mode='classify',batch_index=0).status_code,200)

    def test_unverified_grouping_is_kept_separate_without_blocking_classifications(self):
        job = self.start()
        def uncertain_provider(source, candidates, allowed, instructions='', **kwargs):
            output = self.provider(source, candidates, allowed, instructions)
            variant = next(item for item in output['suggestions'] if item['source_sku'] == self.variant.sku)
            variant['source_reference'] = 'NO VERIFIABLE REFERENCE'
            return output
        with patch('mall.catalog_assistant.call_provider', side_effect=uncertain_provider):
            result = self.action(job, mode='classify', batch_index=0)
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual((result.data['completed_batches'], result.data['pending_count']), (1, 2))
        variant = next(item for item in result.data['results'] if item['source_sku'] == self.variant.sku)
        self.assertEqual(variant['target_sku'], self.variant.sku)
        self.assertEqual(variant['category'], 'FRENOS')
        self.assertEqual(variant['source_reference'], '')
        self.assertEqual(variant['target_reference'], '')
        self.assertLessEqual(variant['confidence'], 0.5)
        self.assertIn('separado', variant['reason'])
        self.assertIn('evidencia verificable', variant['reason'])
        self.assertEqual(result.data['applied_count'], 0)
        self.assertFalse(CatalogMerge.objects.exists())
        self.assertEqual(Part.objects.filter(category='').count(), 2)

    def test_live_claim_blocks_second_request_and_expired_lease_can_resume(self):
        job = self.start()
        saved = CatalogAssistantJob.objects.get(pk=job['id'])
        saved.claim_id,saved.claimed_at=uuid.uuid4(),timezone.now()
        saved.save()
        self.assertEqual(self.action(job,mode='classify',batch_index=0).status_code,409)
        saved.claimed_at=timezone.now()-timedelta(minutes=3)
        saved.save()
        with patch('mall.catalog_assistant.call_provider',side_effect=self.provider):
            self.assertEqual(self.action(job,mode='classify',batch_index=0).status_code,200)

    def test_stale_descriptions_categories_or_aliases_cannot_overwrite_changes(self):
        job = self.analyzed()
        self.variant.description='CHANGED APPLICATION'
        self.variant.save()
        self.assertEqual(self.action(job,mode='apply',decisions=self.decisions(job)).status_code,409)
        self.assertFalse(CatalogMerge.objects.exists())
        self.assertEqual(CatalogAssistantJob.objects.get(pk=job['id']).suggestions.filter(status='pending').count(),2)
        self.variant.description=self.base.description
        self.variant.save()
        self.base.category='MANUAL EDIT'
        self.base.save()
        self.assertEqual(self.action(job,mode='apply',decisions=self.decisions(job)).status_code,409)
        self.base.refresh_from_db()
        self.assertEqual(self.base.category,'MANUAL EDIT')

    def test_dismiss_uncertain_results_or_preserve_separate_without_erasing_metadata(self):
        job = self.analyzed()
        source = next(item for item in job['results'] if item['source_sku']==self.variant.sku)
        result=self.action(job,mode='dismiss',ids=[source['id']])
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.data['dismissed_count'],1)
        base = next(item for item in job['results'] if item['source_sku']==self.base.sku)
        self.base.category='MANUAL GROUP';self.base.save()
        result=self.action(job,mode='apply',decisions=[{'id':base['id'],'target_sku':self.base.sku,'category':'','subcategory':''}])
        self.assertEqual(result.status_code,200,result.data)
        self.base.refresh_from_db()
        self.assertEqual(self.base.category,'MANUAL GROUP')
        self.assertFalse(CatalogMerge.objects.exists())

    def test_invalid_destinations_cycles_and_duplicate_decisions_are_atomic(self):
        job = self.analyzed()
        values = self.decisions(job)
        tampered = [{**values[0],'target_sku':'UNKNOWN'}]
        self.assertEqual(self.action(job,mode='apply',decisions=tampered).status_code,400)
        self.assertEqual(self.action(job,mode='apply',decisions=[values[0],values[0]]).status_code,400)
        cyclic = [{**value,'target_sku':self.variant.sku if value['target_sku']==self.base.sku and next(item for item in job['results'] if item['id']==value['id'])['source_sku']==self.base.sku else self.base.sku} for value in values]
        self.assertEqual(self.action(job,mode='apply',decisions=cyclic).status_code,400)
        self.assertFalse(CatalogMerge.objects.exists())
        self.assertEqual(Part.objects.filter(category='').count(),2)
