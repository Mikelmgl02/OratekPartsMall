from datetime import timedelta
from unittest.mock import patch
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase
from .models import (Account, Part, PartCode, User, SupplierItem, StockEntry, InventoryUpdate,
                     MatchingCase, MatchingDecision, MatchingQueue, SupplierCodeMapping, CatalogMerge)
from .services import ingest_inventory
from .matching_engine import MatchIndex, reconcile_items, reconcile_catalog, create_supplier_families, item_row, part_row
from .matching_worker import process_queue, analyze_ambiguous
from .matching_queue import enqueue_matching, matching_work


class SmartMatchingTests(APITestCase):
    description = 'TAMBOR KIA PICANTO 04-11'

    def setUp(self):
        self.admin = User.objects.create_superuser('matching-admin', 'matching-admin@example.invalid', 'Testing938!')
        self.suppliers = [Account.objects.create(name=f'SUPPLIER {i}') for i in range(2)]
        self.client.force_authenticate(self.admin)

    def ingest(self, code, *, supplier=0, stable=None, update=None, quantity=5, description=None, source='upload', brand=''):
        return ingest_inventory(supplier=self.suppliers[supplier], actor=self.admin,
            data={'update_id': update or code, 'supplier_invent_id': stable or code, 'codigo': code, 'brand': brand,
                  'description': self.description if description is None else description, 'source': source, 'quantity': quantity})

    def test_codes_from_different_suppliers_create_one_parent_and_preserve_ledgers(self):
        a = self.ingest('58411-07000-K', stable='STABLE-A')
        b = self.ingest('58411-07000-NP', supplier=1, stable='STABLE-B', source='apiag', quantity=10)
        stock = list(StockEntry.objects.order_by('pk').values())
        updates = list(InventoryUpdate.objects.order_by('pk').values())
        with matching_work():
            result = process_queue(force=True, use_ai=False)
        self.assertEqual(result['created_parents'], 1)
        self.assertEqual(result['matched_items'], 2)
        target = Part.objects.get(sku='58411-07000')
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual((a.part_id, b.part_id), (target.pk, target.pk))
        self.assertEqual((a.supplier_invent_id, b.supplier_invent_id), ('STABLE-A','STABLE-B'))
        # Supplier-local codes remain on the children, not in the universal library.
        self.assertFalse(target.codes.exists())
        self.assertEqual(list(StockEntry.objects.order_by('pk').values()),stock)
        self.assertEqual(list(InventoryUpdate.objects.order_by('pk').values()),updates)
        process_queue(force=True,use_ai=False)
        self.assertEqual(Part.objects.filter(active=True).count(),1)
        self.assertEqual(MatchingDecision.objects.count(),3)
        # A repeated upload changes only stock, never creates a second parent or child.
        self.ingest('58411-07000-K',stable='STABLE-A',update='again',quantity=10)
        self.assertEqual(SupplierItem.objects.count(),2)
        self.assertEqual(StockEntry.objects.last().quantity,5)
        self.assertEqual(StockEntry.objects.last().direction,'credit')

    def test_catalog_groups_all_four_children_and_preserves_media_and_supplier_stock(self):
        children = [Part.objects.create(sku='58411-08000-'+suffix,description=self.description) for suffix in ['K','KSM-NP','NP','RAZ']]
        items = [self.ingest(p.sku,stable=f'ITEM-{i}') for i,p in enumerate(children)]
        ledger = list(StockEntry.objects.values())
        with matching_work():
            self.assertEqual(reconcile_catalog(self.admin),4)
        parent=Part.objects.get(sku='58411-08000')
        self.assertEqual(parent.merged_parts.count(),4)
        self.assertEqual(parent.supplier_items.count(),4)
        self.assertEqual(list(StockEntry.objects.values()),ledger)
        self.assertEqual(CatalogMerge.objects.count(),1)
        self.assertEqual(reconcile_catalog(self.admin),0)

    def test_normalized_reference_preserves_leading_zeros_and_detects_collision(self):
        target=Part.objects.create(sku='00123-45678',description=self.description)
        a=self.ingest('0012345678')
        b=self.ingest('12345678')
        reconcile_items(MatchIndex())
        a.refresh_from_db();b.refresh_from_db()
        self.assertEqual(a.part_id,target.pk)
        self.assertIsNone(b.part_id)
        other=Part.objects.create(sku='0012345678',description=self.description)
        c=self.ingest('00123.45678')
        reconcile_items(MatchIndex());c.refresh_from_db()
        self.assertIsNone(c.part_id)
        self.assertEqual(MatchingCase.objects.get(item=c).status,'review')

    def test_numeric_position_dimension_and_year_variants_are_not_automerged(self):
        Part.objects.create(sku='58411-07000',description='DISCO KIA PICANTO TRAS 200 MM 04-11')
        inputs=[('58411-07000-L','DISCO KIA PICANTO IZQ 200 MM 04-11'),
                ('58411-07000-22','DISCO KIA PICANTO TRAS 220 MM 04-11'),
                ('58411-07000-K','DISCO KIA PICANTO TRAS 220 MM 04-11'),
                ('58411-07000-NP','DISCO KIA PICANTO TRAS 200 MM 15-20')]
        for code,desc in inputs:self.ingest(code,description=desc)
        reconcile_items(MatchIndex())
        self.assertFalse(SupplierItem.objects.filter(matching_status='matched').exists())
        self.assertEqual(create_supplier_families(),0)

    def test_same_description_without_code_evidence_only_proposes_match(self):
        target=Part.objects.create(sku='58411-07000',description=self.description)
        item=self.ingest('D-HYU-1R')
        reconcile_items(MatchIndex());item.refresh_from_db()
        self.assertIsNone(item.part_id)
        case=MatchingCase.objects.get(item=item)
        self.assertEqual(case.candidates[0]['id'],str(target.pk))
        response=self.client.post(f'/api/v1/management/matching/{case.pk}/',{
            'action':'approve','target_sku':target.sku,'fingerprint':case.fingerprint},format='json')
        self.assertEqual(response.status_code,200,response.data)
        item.refresh_from_db();self.assertEqual(item.part_id,target.pk)
        self.assertEqual(SupplierCodeMapping.objects.count(),1)
        next_item=self.ingest('D-HYU-1R',stable='SECOND-ID',update='second')
        reconcile_items(MatchIndex());next_item.refresh_from_db()
        self.assertEqual(next_item.part_id,target.pk)
        # Unbranded local identifiers do not become global aliases.
        other=self.ingest('D-HYU-1R',supplier=1)
        reconcile_items(MatchIndex());other.refresh_from_db()
        self.assertIsNone(other.part_id)

    def test_supplier_code_change_cannot_move_historical_stock_to_another_parent(self):
        old=Part.objects.create(sku='58411-07000',description=self.description)
        new=Part.objects.create(sku='58411-08000',description=self.description)
        item=self.ingest(old.sku,stable='STABLE')
        self.ingest(new.sku,stable='STABLE',update='rename')
        reconcile_items(MatchIndex());item.refresh_from_db()
        self.assertEqual(item.part_id,old.pk)
        self.assertEqual(item.matching_status,'review')
        self.assertEqual(StockEntry.objects.count(),1)

    def test_review_rejects_stale_supplier_data_and_remembers_dismissal(self):
        Part.objects.create(sku='58411-07000',description=self.description)
        item=self.ingest('D-HYU-1R')
        reconcile_items(MatchIndex());case=MatchingCase.objects.get(item=item)
        payload={'action':'approve','fingerprint':case.fingerprint}
        self.ingest(item.codigo,update='changed-description',description='TAMBOR KIA PICANTO 15-20')
        response=self.client.post(f'/api/v1/management/matching/{case.pk}/',payload,format='json')
        self.assertEqual(response.status_code,409)
        reconcile_items(MatchIndex());case.refresh_from_db()
        response=self.client.post(f'/api/v1/management/matching/{case.pk}/',{'action':'dismiss','fingerprint':case.fingerprint},format='json')
        self.assertEqual(response.status_code,200)
        reconcile_items(MatchIndex());case.refresh_from_db()
        self.assertEqual(case.status,'dismissed')
        self.assertIsNone(SupplierItem.objects.get(pk=item.pk).part_id)

    def test_later_catalog_addition_rematches_unchanged_pending_feed(self):
        item=self.ingest('58411-07000-K')
        reconcile_items(MatchIndex())
        Part.objects.create(sku='58411-07000',description=self.description)
        reconcile_items(MatchIndex());item.refresh_from_db()
        self.assertEqual(item.part.sku,'58411-07000')
        self.assertEqual(StockEntry.objects.count(),1)

    @override_settings(GEMINI_API_KEY='test-key')
    def test_ai_is_cached_bounded_and_cannot_apply_semantic_guess(self):
        target=Part.objects.create(sku='58411-07000',description=self.description)
        item=self.ingest('D-HYU-1R')
        reconcile_items(MatchIndex())
        result={'suggestions':[{'source_sku':item.codigo,'target_sku':target.sku,'confidence':1.0,'reason':'Descripción similar; requiere confirmar aplicación.'}]}
        with patch('mall.matching_worker.call_provider',return_value=result) as provider:
            self.assertEqual(analyze_ambiguous(),1)
            self.assertEqual(analyze_ambiguous(),0)
            provider.assert_called_once()
        item.refresh_from_db();self.assertIsNone(item.part_id)
        self.assertTrue(MatchingCase.objects.get(item=item).ai['needs_review'])

    @override_settings(GEMINI_API_KEY='test-key')
    def test_ai_failure_retains_stock_and_review(self):
        from .catalog_classification import ClassificationProviderError
        Part.objects.create(sku='58411-07000',description=self.description)
        item=self.ingest('D-HYU-1R')
        with patch('mall.matching_worker.call_provider',side_effect=ClassificationProviderError()):
            result=process_queue(force=True)
        self.assertEqual(result['ai_analyzed'],0)
        self.assertEqual(StockEntry.objects.count(),1)
        self.assertIn('error',MatchingCase.objects.get(item=item).ai)

    def test_queue_coalesces_transactions_and_worker_claim_prevents_duplicates(self):
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            enqueue_matching();enqueue_matching();enqueue_matching()
        self.assertEqual(len(callbacks),1)
        queue=MatchingQueue.objects.get(pk=1)
        self.assertEqual(queue.requested,1)
        queue.lease_until=timezone.now()+timedelta(minutes=5);queue.save()
        self.assertIsNone(process_queue(force=True,use_ai=False))

    def test_matching_api_is_superuser_only(self):
        self.client.force_authenticate(User.objects.create_user('ordinary','ordinary@example.invalid','Testing938!'))
        self.assertEqual(self.client.get('/api/v1/management/matching/').status_code,403)
        self.assertEqual(self.client.post('/api/v1/management/matching/',{},format='json').status_code,403)


    def test_reviewed_branded_codes_transfer_only_with_identical_specifications(self):
        target=Part.objects.create(sku='58411-07000',description=self.description)
        item=self.ingest('D-HYU-1R',brand='ACME')
        reconcile_items(MatchIndex());case=MatchingCase.objects.get(item=item)
        result=self.client.post(f'/api/v1/management/matching/{case.pk}/',{
            'action':'approve','fingerprint':case.fingerprint,'target_sku':target.sku},format='json')
        self.assertEqual(result.status_code,200)
        same=self.ingest('D-HYU-1R',supplier=1,brand='ACME')
        different=self.ingest('D-HYU-1R',supplier=1,brand='OTHER',stable='DIFFERENT',update='different')
        reconcile_items(MatchIndex());same.refresh_from_db();different.refresh_from_db()
        self.assertEqual(same.part_id,target.pk)
        self.assertIsNone(different.part_id)

    @override_settings(GEMINI_API_KEY='test-key')
    def test_noop_analysis_reports_scope_without_ai_or_web_research(self):
        part = Part.objects.create(sku='58411-07000', description=self.description)
        item = self.ingest(part.sku)
        item.part, item.matching_status = part, 'matched'
        item.save()
        with patch('mall.matching_worker.call_provider') as provider, patch('mall.oem_lookup.lookup_oem') as research:
            result = process_queue(force=True)
        provider.assert_not_called()
        research.assert_not_called()
        self.assertEqual(result['catalog_count'], 1)
        self.assertEqual(result['supplier_total'], 1)
        self.assertEqual(result['supplier_already_matched'], 1)
        self.assertEqual(result['supplier_examined'], 0)
        self.assertEqual(result['ai_status'], 'no_pending_cases')
        self.assertEqual(result['non_oem_skus'], 1)
        self.assertEqual(result['promoted_oems'], 0)
        response = self.client.get('/api/v1/management/matching/')
        self.assertEqual(response.data['state'], 'completed')
        self.assertFalse(response.data['queued'])
        self.assertEqual(response.data['stage'], 'completed')
        self.assertIsNotNone(response.data['started_at'])

    def test_analysis_overview_distinguishes_queued_running_and_retry(self):
        queue = MatchingQueue.objects.create(pk=1, requested=1)
        url = '/api/v1/management/matching/'
        self.assertEqual(self.client.get(url).data['state'], 'queued')
        import uuid
        queue.lease, queue.lease_until = uuid.uuid4(), timezone.now()+timedelta(minutes=5)
        queue.stage = 'suppliers'
        queue.save()
        self.assertEqual(self.client.get(url).data['state'], 'running')
        self.assertEqual(self.client.get(url).data['stage'], 'suppliers')
        queue.lease, queue.error = None, 'RETRY'
        queue.save()
        self.assertEqual(self.client.get(url).data['state'], 'retrying')

    @override_settings(GEMINI_API_KEY='test-key')
    def test_analysis_explains_provider_failure_without_claiming_no_work(self):
        from .catalog_classification import ClassificationProviderError
        Part.objects.create(sku='58411-07000', description=self.description)
        self.ingest('D-HYU-1R')
        with patch('mall.matching_worker.call_provider', side_effect=ClassificationProviderError()):
            result = process_queue(force=True)
        self.assertEqual(result['supplier_examined'], 1)
        self.assertEqual(result['ai_eligible'], 1)
        self.assertEqual(result['ai_status'], 'failed')
        self.assertEqual(result['ai_analyzed'], 0)
