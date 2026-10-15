"""Agrupación inteligente's matching from S6 (mall.reference_matching): what a load does at once, the worker's pass, the cases and their
decisions, the AI ranking (line and merge cases only, OD12) and the description hints of D1. The rules themselves are table-driven in
mall.test_reference_matching."""
from datetime import timedelta
from unittest.mock import patch
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase
from .brand_models import Brand
from . import references
from .lines import create_line
from .models import Account, Part, User, SupplierItem, StockEntry, InventoryUpdate, MatchingCase, MatchingDecision, MatchingQueue
from .services import ingest_inventory
from .matching_engine import MatchIndex, spec_conflict
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

    def decide(self, case, action, **data):
        return self.client.post(f'/api/v1/management/matching/{case.pk}/', {'action': action, 'fingerprint': case.fingerprint, **data},
                                format='json')

    def two_lines(self, code='58411-07000'):
        """A code on two lines under two brands: a brandless item with it waits in a line case."""
        first = create_line(code, description=self.description, main_brand='KIA', main_tipo='oem')
        second = create_line(f'{code}-DUP', description=self.description)
        references.add(second, code, 'MANDO', tipo='aftermarket')
        return first, second

    def test_suffixed_codes_no_line_has_become_new_products_and_no_parent_is_invented(self):
        a = self.ingest('58411-07000-K', stable='STABLE-A')
        b = self.ingest('58411-07000-NP', supplier=1, stable='STABLE-B', source='apiag', quantity=10)
        stock = list(StockEntry.objects.order_by('pk').values())
        updates = list(InventoryUpdate.objects.order_by('pk').values())
        with matching_work():
            result = process_queue(force=True, use_ai=False)
        self.assertFalse(Part.objects.filter(sku='58411-07000').exists())  # estado: no invented parent (S6)
        for item in (a, b):
            item.refresh_from_db()
            self.assertEqual((item.part.estado, item.part.origen, item.matching_status), ('preparacion', 'proveedor', 'pending'))
        self.assertNotEqual(a.part_id, b.part_id)
        self.assertEqual(list(StockEntry.objects.order_by('pk').values()), stock)
        self.assertEqual(list(InventoryUpdate.objects.order_by('pk').values()), updates)
        self.assertEqual((result['matched_items'], result['placed_items']), (0, 0))  # both were placed when they arrived
        # A repeated upload changes only stock, never a second line or item.
        self.ingest('58411-07000-K', stable='STABLE-A', update='again', quantity=10)
        self.assertEqual(SupplierItem.objects.count(), 2)
        self.assertEqual((StockEntry.objects.last().quantity, StockEntry.objects.last().direction), (5, 'credit'))

    def test_a_later_catalog_line_takes_the_item_by_rule_3_once_its_base_exists(self):
        target = create_line('58411-07000', description=self.description)
        item = self.ingest('58411-07000-GMB', description='TAMBOR KIA PICANTO TRAS 04-11')
        self.assertEqual((item.part_id, item.matching_status, item.reference.code), (target.pk, 'matched', '58411-07000-GMB'))
        self.assertEqual(StockEntry.objects.count(), 1)
        # D2, widened on 2026-10-15: an origin such as -K links on its own too when the part names agree.
        origin = self.ingest('58411-07000-K', description='TAMBOR KIA PICANTO TRAS 04-11')
        self.assertEqual((origin.part_id, origin.matching_status, origin.reference.code), (target.pk, 'matched', '58411-07000-K'))

    def test_leading_zeros_and_short_codes_keep_their_identity(self):
        target = create_line('00123-45678', description=self.description)
        a = self.ingest('0012345678')
        b = self.ingest('12345678')
        self.assertEqual(a.part_id, target.pk)
        self.assertNotEqual(b.part_id, target.pk)
        self.assertEqual(b.part.estado, 'preparacion')

    def test_position_size_and_year_variants_never_join_on_their_own(self):
        create_line('58411-07000', description='DISCO KIA PICANTO TRAS 200 MM 04-11')
        inputs = [('58411-07000-L', 'DISCO KIA PICANTO IZQ 200 MM 04-11'), ('58411-07000-22', 'DISCO KIA PICANTO TRAS 220 MM 04-11'),
                  ('58411-07000-K', 'DISCO KIA PICANTO TRAS 220 MM 04-11'), ('58411-07000-NP', 'DISCO KIA PICANTO TRAS 200 MM 15-20')]
        for code, text in inputs:
            self.ingest(code, description=text)
        self.assertFalse(SupplierItem.objects.filter(matching_status='matched').exists())
        # -K and -NP strip to the line (rule 3) but contradict it: line cases. -L and -22 strip to nothing: new products.
        outcome = {item.codigo: (item.part.estado if item.part_id else None, MatchingCase.objects.filter(item=item, kind='line').exists())
                   for item in SupplierItem.objects.select_related('part')}
        self.assertEqual(outcome, {'58411-07000-L': ('preparacion', False), '58411-07000-22': ('preparacion', False),
                                   '58411-07000-K': (None, True), '58411-07000-NP': (None, True)})

    def test_a_code_change_never_moves_historical_stock(self):
        old = create_line('58411-07000', description=self.description)
        new = create_line('58411-08000', description=self.description)
        item = self.ingest(old.sku, stable='STABLE')
        item = self.ingest(new.sku, stable='STABLE', update='rename')
        self.assertEqual((item.part_id, item.matching_status, item.codigo_pendiente), (old.pk, 'review', new.sku))
        self.assertEqual(StockEntry.objects.count(), 1)
        self.assertEqual(MatchingCase.objects.get(item=item).kind, 'code_change')

    def test_a_decision_on_stale_data_is_refused_and_a_dismissal_is_remembered(self):
        self.two_lines()
        item = self.ingest('58411-07000', update='stale-1')
        case = MatchingCase.objects.get(item=item, kind='line')
        stale = case.fingerprint
        third = create_line('58411-07000-TRI', description=self.description)  # a third line takes the code under a third brand
        references.add(third, '58411-07000', 'GMB', tipo='aftermarket')
        self.ingest('58411-07000', update='stale-2', quantity=7)
        response = self.client.post(f'/api/v1/management/matching/{case.pk}/', {'action': 'approve', 'fingerprint': stale,
                                                                                 'line': str(Part.objects.get(sku='58411-07000').pk)}, format='json')
        self.assertEqual(response.status_code, 409)
        variant = self.ingest('58411-07000', brand='KIA', stable='V-1', update='v-1', description='TAMBOR KIA PICANTO 15-20')  # years contradict
        case = MatchingCase.objects.get(item=variant, kind='variant')
        self.assertEqual(self.decide(case, 'dismiss').status_code, 200)
        self.ingest('58411-07000', brand='KIA', stable='V-1', description='TAMBOR KIA PICANTO 15-20', update='v-again', quantity=9)
        self.assertEqual(MatchingCase.objects.get(pk=case.pk).status, 'dismissed')  # the same evidence stays dismissed

    def code_with_contradiction(self):
        # The code is on two lines (a line case): the only kind, with merge cases, the AI ranks from S6 (OD12).
        first, second = self.two_lines()
        return first, self.ingest(first.sku, description='TAMBOR KIA PICANTO 15-20')

    @override_settings(GEMINI_API_KEY='test-key')
    def test_ai_is_cached_bounded_and_cannot_apply_semantic_guess(self):
        target, item = self.code_with_contradiction()
        self.assertEqual(MatchingCase.objects.get(item=item).status, 'review')
        result = {'suggestions': [{'source_sku': item.codigo, 'target_sku': target.sku, 'confidence': 1.0, 'reason': 'Descripción similar; requiere confirmar aplicación.'}]}
        with patch('mall.matching_worker.call_provider', return_value=result) as provider:
            self.assertEqual(analyze_ambiguous(), 1)
            self.assertEqual(analyze_ambiguous(), 0)
            provider.assert_called_once()
        self.assertEqual(provider.call_args.kwargs['feature'], 'matching')
        item.refresh_from_db()
        self.assertIsNone(item.part_id)
        self.assertTrue(MatchingCase.objects.get(item=item).ai['needs_review'])

    @override_settings(GEMINI_API_KEY='test-key')
    def test_ai_failure_retains_stock_and_review(self):
        from .catalog_classification import ClassificationProviderError
        _, item = self.code_with_contradiction()
        with patch('mall.matching_worker.call_provider', side_effect=ClassificationProviderError()):
            result = process_queue(force=True)
        self.assertEqual(result['ai_analyzed'], 0)
        self.assertEqual(StockEntry.objects.count(), 1)
        self.assertIn('error', MatchingCase.objects.get(item=item).ai)

    def test_queue_coalesces_transactions_and_worker_claim_prevents_duplicates(self):
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            enqueue_matching(); enqueue_matching(); enqueue_matching()
        self.assertEqual(len(callbacks), 1)
        queue = MatchingQueue.objects.get(pk=1)
        self.assertEqual(queue.requested, 1)
        queue.lease_until = timezone.now() + timedelta(minutes=5); queue.save()
        self.assertIsNone(process_queue(force=True, use_ai=False))

    def test_matching_api_is_superuser_only(self):
        self.client.force_authenticate(User.objects.create_user('ordinary', 'ordinary@example.invalid', 'Testing938!'))
        self.assertEqual(self.client.get('/api/v1/management/matching/').status_code, 403)
        self.assertEqual(self.client.post('/api/v1/management/matching/', {}, format='json').status_code, 403)

    def test_an_approved_line_takes_the_item_and_short_brandless_codes_stay_each_suppliers_own(self):
        first, second = self.two_lines('D-HYU-1R0001')
        item = self.ingest('D-HYU-1R0001')
        case = MatchingCase.objects.get(item=item, kind='line')
        self.assertEqual(self.decide(case, 'approve', line=str(first.pk)).status_code, 200)
        item.refresh_from_db()
        self.assertEqual((item.part_id, item.matching_status, item.reference.brand), (first.pk, 'matched', 'KIA'))
        self.assertEqual(MatchingDecision.objects.get(case=case).action, 'approved')
        short = self.ingest('ZZ-1', stable='SHORT')  # OD6: a short brandless code is its supplier's own (interno)
        self.assertEqual(references.main_of(short.part).tipo, 'interno')
        self.assertNotEqual(self.ingest('ZZ-1', supplier=1).part_id, short.part_id)
        self.assertEqual(self.ingest('ZZ-1', stable='SHORT-2', update='short-2').part_id, short.part_id)

    @override_settings(GEMINI_API_KEY='test-key')
    def test_noop_analysis_reports_scope_without_ai_or_web_research(self):
        part = create_line('58411-07000', description=self.description)
        item = self.ingest(part.sku)
        self.assertEqual(item.matching_status, 'matched')
        with patch('mall.matching_worker.call_provider') as provider, patch('mall.oem_lookup.lookup_oem') as research:
            result = process_queue(force=True)
        provider.assert_not_called()
        research.assert_not_called()
        self.assertEqual((result['catalog_count'], result['supplier_total'], result['supplier_already_matched'], result['supplier_examined']),
                         (1, 1, 1, 0))
        self.assertEqual(result['ai_status'], 'no_pending_cases')
        self.assertEqual(result['non_oem_skus'], 1)
        self.assertNotIn('promoted_oems', result)  # S6: no renames in the pass
        response = self.client.get('/api/v1/management/matching/')
        self.assertEqual((response.data['state'], response.data['queued'], response.data['stage']), ('completed', False, 'completed'))
        self.assertIsNotNone(response.data['started_at'])

    def test_analysis_overview_distinguishes_queued_running_and_retry(self):
        queue = MatchingQueue.objects.create(pk=1, requested=1)
        url = '/api/v1/management/matching/'
        self.assertEqual(self.client.get(url).data['state'], 'queued')
        import uuid
        queue.lease, queue.lease_until = uuid.uuid4(), timezone.now() + timedelta(minutes=5)
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
        self.code_with_contradiction()
        with patch('mall.matching_worker.call_provider', side_effect=ClassificationProviderError()):
            result = process_queue(force=True)
        self.assertEqual((result['supplier_examined'], result['ai_eligible'], result['ai_status'], result['ai_analyzed']), (1, 1, 'failed', 0))

    def test_word_only_items_go_to_new_products_and_keep_only_informative_hints(self):
        Brand.objects.get_or_create(nombre='KAIYI', defaults={'tipo': 'vehiculo'})  # a vehicle make (mall.sku_proposals.vehicle_makes)
        # D1: description words never link an item; they are hints (possible duplicates) for Revisión IA.
        create_line('025CHA-1504110', description='TAPA KAIYI FX11 2025 55W')
        create_line('RX1809-RAINX-KIT', description='LIMPIAPARABRISAS RAINX KAIYI 2025')
        farol = create_line('58411-07000', description='FAROL DELANTERO MOLDURA CROMADA')
        for i in range(25):  # FX11 is a model this supplier writes on many items: it says nothing about the product
            SupplierItem.objects.create(supplier=self.suppliers[0], supplier_invent_id=f'FX-{i}', codigo=f'FX11-{i}', brand='KAIYI',
                                        description=f'PIEZA {i} KAIYI FX11', matching_status='matched', part=farol)
        item = self.ingest('F16-4121020-01AA', brand='KAIYI', description='FAROL KAIYI FX11 2025 CROMADA 55W')
        lone = self.ingest('F16-4121020-02AA', brand='KAIYI', description='FAROL KAIYI FX11 2025', stable='LONE')
        self.assertEqual((item.part.estado, lone.part.estado), ('preparacion', 'preparacion'))
        index = MatchIndex()
        self.assertEqual([row['sku'] for row in index.hints(item)], ['58411-07000'])
        self.assertEqual(index.informative(item), {'FAROL', 'CROMADA'})
        self.assertEqual(index.hints(lone), [])  # one informative word is no hint
        with patch('mall.matching_worker.call_provider') as provider:
            result = process_queue(force=True)
        provider.assert_not_called()  # hints are never sent to the matching AI: Revisión IA sees them in Productos nuevos
        self.assertEqual(result['matched_items'], 0)
        self.assertFalse(MatchingCase.objects.filter(item__in=[item, lone]).exists())
        item.refresh_from_db()
        self.assertEqual((item.part.estado, item.matching_status), ('preparacion', 'pending'))  # placed at ingest (rule 4), no case

    def test_pads_sold_as_a_set_link_by_code_and_a_shared_code_goes_to_a_line_case(self):
        create_line('0446502220', description='PASTILLA FRENO DEL TOYOTA HILUX', main_brand='TOYOTA', main_tipo='oem')
        other = create_line('04465 02220-X', description='PASTILLA FRENO DEL TOYOTA HILUX')
        references.add(other, '04465 02220', 'AISIN', tipo='aftermarket')
        pads = create_line('04465-0K290', description='PASTILLA FRENO DEL TOYOTA HILUX')
        shared = self.ingest('04465-02220', description='PASTILLA FRENO DEL')
        self.assertEqual(MatchingCase.objects.get(item=shared).kind, 'line')  # its code is two lines': a person decides
        # «SET 4 PCS» on pads is how they are sold, not a kit: the code links it.
        item = self.ingest('044650K290', description='PASTILLA FRENO SET 4 PCS', stable='SET')
        self.assertEqual((item.part_id, item.matching_status), (pads.pk, 'matched'))


class SpecConflictTests(SimpleTestCase):
    """The false positives the matching map found (§3.4) and the contradictions that must still block."""

    def test_a_part_for_either_side_contradicts_neither(self):
        self.assertFalse(spec_conflict('FAROL LH/RH KAIYI X3', 'FAROL LH KAIYI X3'))
        self.assertFalse(spec_conflict('FAROL RH-LH', 'FAROL DERECHO'))
        self.assertFalse(spec_conflict('FARO L/R', 'FARO RH'))
        self.assertFalse(spec_conflict('ESPEJO IZQ Y DER', 'ESPEJO DERECHO'))
        self.assertFalse(spec_conflict('AMORT DEL/TRAS', 'AMORT TRAS'))
        self.assertFalse(spec_conflict('FARO DEL-IZQ/DER', 'FARO DEL DER'))
        # Live data mostly writes the pair with a space or a comma; DEL is also the word «del».
        for text in ['AMORT HON CIVIC 96-01 DEL LH RH', 'AISLANTE ACUSTICO DEL PASO DE RUEDA TRASERA']:
            self.assertFalse(spec_conflict(text, text))
        self.assertFalse(spec_conflict('AMORT HON CIVIC 96-01 TRAS LH RH', 'AMORT HON CIVIC 96-01 TRAS LH'))
        self.assertFalse(spec_conflict('FAROL LH,RH', 'FAROL LH'))
        self.assertFalse(spec_conflict('FARO L/H R/H', 'FARO L/H'))
        self.assertTrue(spec_conflict('FAROL LH', 'FAROL RH'))
        self.assertTrue(spec_conflict('FARO L/H', 'FARO R/H'))
        self.assertTrue(spec_conflict('FAROL LH/RH DEL', 'FAROL TRAS'))
        self.assertTrue(spec_conflict('FARO DEL-IZQ', 'FARO TRAS'))

    def test_pads_and_shoes_sold_as_a_set_are_not_a_kit(self):
        self.assertFalse(spec_conflict('PASTILLA FRENO SET 4 PCS', 'PASTILLA FRENO DEL'))
        self.assertFalse(spec_conflict('JUEGO PASTILLAS DEL', 'PASTILLA FRENO DEL'))
        self.assertFalse(spec_conflict('ZAPATA FRENO JUEGO', 'ZAPATAS FRENO TRAS'))
        self.assertTrue(spec_conflict('BUJIA SET 4 PCS', 'BUJIA IRIDIUM'))
        self.assertTrue(spec_conflict('KIT PASTILLAS Y DISCOS', 'PASTILLA FRENO'))
        self.assertTrue(spec_conflict('KIT PASTILLA FRENO', 'PASTILLA FRENO'))
        self.assertTrue(spec_conflict('JUEGO EMPAQUES MOTOR', 'PASTILLA FRENO DEL'))   # only two pads excuse the set

    def test_type_nouns_read_plurals_and_erp_abbreviations(self):
        self.assertFalse(spec_conflict('AMORT DEL', 'AMORTIGUADOR DEL'))
        self.assertFalse(spec_conflict('AMORTIGUADORES DEL', 'AMORT DEL'))
        # A second type noun is a second type, wherever it stands.
        self.assertTrue(spec_conflict('ZAPATA FRENO DE TAMBOR', 'ZAPATA FRENO DE DISCO'))
        self.assertTrue(spec_conflict('PASTILLA FRENO DE DISCO', 'PASTILLA FRENO DEL'))
        self.assertTrue(spec_conflict('AMORT DEL', 'ROTULA DEL'))
        self.assertTrue(spec_conflict('PASTILLAS FRENO', 'DISCO FRENO'))
        self.assertTrue(spec_conflict('DISCO FRENO 200 MM', 'DISCO FRENO 220 MM'))
        self.assertTrue(spec_conflict('TAMBOR KIA PICANTO 04-11', 'TAMBOR KIA PICANTO 15-20'))
