import datetime
import uuid
from decimal import Decimal
from types import SimpleNamespace

from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APITestCase

from . import test_deals as deal_tests
from . import test_quote_drafts as draft_tests
from . import test_requests as request_tests
from .deal_views import EXCEPTIONS_DETAIL
from .models import SupplierItem
from .pricing_engine import ENGINE_VERSION, evaluate_line, price_lines
from .pricing_models import PriceList, PriceListEntry, PricingAuditEvent, SupplierItemPricing, SupplierPricingSettings, pricing_settings
from .pricing_services import set_prices
from .quote_draft_models import DealQuotationAudit, DealQuotationDraft, DealQuotationDraftLine, DealQuotationLineAudit
from .request_models import DealQuotation, SupplierRequest

TODAY = datetime.date(2026, 10, 5)
GENERAL = {'id': uuid.UUID('00000000-0000-4000-8000-000000000001'), 'code': 'GENERAL', 'name': 'LISTA GENERAL', 'currency': 'USD'}


def entry(price, revision=1, pk=7):
    return {'id': pk, 'revision': revision, 'unit_price': price, 'updated_at': timezone.make_aware(datetime.datetime(2026, 10, 1, 12))}


def evaluate(**overrides):
    return evaluate_line(**{'identity_ok': True, 'quantity': 4, 'currency': 'USD', 'parity': True, 'settings_version': 1, 'on_date': TODAY,
                            'price_list': GENERAL, 'entry': entry(Decimal('15.00')), 'floor_price': None, **overrides})


class PricingEngineCoreTests(SimpleTestCase):
    def test_base_price_explanation_and_decimal_only_inputs(self):
        result = evaluate()
        self.assertEqual((result.unit_price, result.status, result.parity_applied, result.configured), (Decimal('15.00'), 'priced', False, True))
        self.assertIsInstance(result.unit_price, Decimal)
        self.assertEqual(result.explanation, {
            'engine': ENGINE_VERSION, 'evaluated_on': '2026-10-05', 'quantity_basis': 4, 'status': 'priced', 'unit_price': '15.00', 'currency': 'USD',
            'profile': None, 'price_list': {'id': str(GENERAL['id']), 'code': 'GENERAL', 'name': 'LISTA GENERAL', 'currency': 'USD'},
            'base': {'price_list_id': str(GENERAL['id']), 'price_list_code': 'GENERAL', 'fallback': False, 'entry_revision': 1, 'unit_price': '15.00',
                     'currency': 'USD', 'updated_at': '2026-10-01T12:00:00-05:00'},
            'steps': [], 'discarded': [], 'next_breaks': [], 'floor_price': None, 'codes': [], 'rounding': 'ROUND_HALF_UP_0.01', 'fingerprint': result.fingerprint})
        # No float ever reaches the engine: it refuses instead of coercing.
        for overrides in [{'entry': entry(15.0)}, {'floor_price': 12.5}, {'entry': entry(True)}]:
            with self.subTest(overrides=overrides), self.assertRaises(TypeError):
                evaluate(**overrides)
        # Rounding happens once, half up (Python's default context would give 10.00).
        self.assertEqual(evaluate(entry=entry(Decimal('10.005'))).unit_price, Decimal('10.01'))
        self.assertEqual(evaluate(entry=entry(Decimal('10000000000.00'))).status, 'out_of_range')

    def test_identity_missing_list_missing_price_floor_and_currency(self):
        self.assertEqual((evaluate(identity_ok=False).unit_price, evaluate(identity_ok=False).codes), (None, ['identity_changed']))
        none = evaluate(price_list=None, entry=None)
        self.assertEqual((none.status, none.configured, none.codes, none.explanation['price_list']), ('no_price_list', False, [], None))
        missing = evaluate(entry=None, floor_price=Decimal('3.00'))
        self.assertEqual((missing.status, missing.codes, missing.floor_price, missing.explanation['floor_price']), ('missing', ['no_list_price'], Decimal('3.00'), '3.00'))
        self.assertEqual(evaluate(floor_price=Decimal('15.01')).codes, ['below_floor'])
        self.assertEqual(evaluate(floor_price=Decimal('15.00')).codes, [])
        parity = evaluate(currency='PAB')
        self.assertEqual((parity.unit_price, parity.parity_applied, parity.codes, parity.explanation['steps']),
                         (Decimal('15.00'), True, ['currency_parity'], [{'kind': 'parity', 'from': 'USD', 'to': 'PAB', 'rate': '1'}]))
        mismatch = evaluate(currency='PAB', parity=False)
        self.assertEqual((mismatch.unit_price, mismatch.status, mismatch.codes), (None, 'currency_mismatch', ['currency_mismatch']))

    def test_fingerprint_ignores_quantity_and_follows_what_shapes_the_price(self):
        base = evaluate().fingerprint
        self.assertEqual(evaluate(quantity=40).fingerprint, base)
        self.assertEqual(evaluate(floor_price=Decimal('1.00')).fingerprint, base)
        for overrides in [{'entry': entry(Decimal('15.00'), revision=2)}, {'entry': entry(Decimal('15.00'), pk=8)}, {'currency': 'PAB'},
                          {'settings_version': 2}, {'entry': entry(Decimal('15.10'))}, {'entry': None}]:
            with self.subTest(overrides=overrides):
                self.assertNotEqual(evaluate(**overrides).fingerprint, base)


class PricedDraftTests(APITestCase):
    setUp = request_tests.SupplierRequestWorkflowTests.setUp
    account = request_tests.SupplierRequestWorkflowTests.account
    item = request_tests.SupplierRequestWorkflowTests.item
    url = request_tests.SupplierRequestWorkflowTests.url
    payload = request_tests.SupplierRequestWorkflowTests.payload
    submit = request_tests.SupplierRequestWorkflowTests.submit
    order = deal_tests.DealWorkflowTests.order
    review = deal_tests.DealWorkflowTests.review
    action = deal_tests.DealWorkflowTests.action
    draft_url = draft_tests.QuoteDraftTests.draft_url
    reviewed = draft_tests.QuoteDraftTests.reviewed
    save = draft_tests.QuoteDraftTests.save
    get_draft = draft_tests.QuoteDraftTests.get_draft
    by_code = draft_tests.QuoteDraftTests.by_code

    A, A2 = '58411-1R000-G', '58411-1R000-KSM'

    def price(self, price_list, item, value, *, floor=None):
        current = PriceListEntry.objects.filter(price_list=price_list, item=item).first()
        pricing = SupplierItemPricing.objects.filter(item=item).first()
        set_prices(self.supplier_a, self.seller_a, changes=[{'list_id': price_list.pk, 'supplier_item_id': item.pk, 'unit_price': value,
                                                              'expected_revision': current.revision if current else None}],
                   items=[{'supplier_item_id': item.pk, 'floor_price': floor, 'expected_revision': pricing.revision if pricing else None}] if floor is not None else ())

    def general(self, currency='USD'):
        return PriceList.objects.create(supplier=self.supplier_a, code='GENERAL', name='LISTA GENERAL', currency=currency, is_default=True, created_by=self.seller_a)

    def reprice(self, order, version, scope, ids=(), save_id=None):
        return self.client.post(self.draft_url(order, suffix='reprice/'), {'save_id': str(save_id or uuid.uuid4()), 'expected_draft_version': version, 'scope': scope,
                                                                           **({'order_line_ids': [str(value) for value in ids]} if ids else {})}, format='json')

    def ids(self, order):
        return {line.supplier_item_id: str(line.pk) for line in order.lines.all()}

    def codes(self, line):
        return [item['code'] for item in line['exceptions']]

    def publish(self, order, draft, **extra):
        self.client.force_authenticate(self.seller_a)
        return self.action(order, 'quote', currency=draft['currency'], terms=draft['terms'], draft_version=draft['draft_version'],
                           lines=[{'order_line_id': line['order_line_id'], 'quantity': line['quantity'], 'unit_price': line['unit_price'] or '0.00'}
                                  for line in draft['lines']], **extra)

    def test_virtual_draft_arrives_priced_with_an_explanation_and_reads_write_nothing(self):
        general = self.general()
        self.price(general, self.item_a, Decimal('10.00'), floor=Decimal('9.00'))
        order = self.reviewed()
        before = (PricingAuditEvent.objects.count(), SupplierPricingSettings.objects.count())
        with CaptureQueriesContext(connection) as queries:
            draft = self.get_draft(order)
        self.assertEqual([query['sql'] for query in queries.captured_queries if not query['sql'].lstrip().upper().startswith('SELECT')], [])
        self.assertEqual((PricingAuditEvent.objects.count(), SupplierPricingSettings.objects.count(), DealQuotationDraft.objects.count()), (*before, 0))
        self.assertEqual(draft['pricing'], {'engine': ENGINE_VERSION, 'configured': True, 'stale': 0, 'fillable': 0,
                                            'price_list': {'id': str(general.pk), 'code': 'GENERAL', 'name': 'LISTA GENERAL', 'currency': 'USD'}})
        first, second = self.by_code(draft)[self.A], self.by_code(draft)[self.A2]
        self.assertEqual((first['unit_price'], first['price_source'], first['suggestion']['unit_price']), ('10.00', 'engine', '10.00'))
        explanation = first['suggestion']['explanation']
        self.assertEqual((explanation['base']['price_list_code'], explanation['base']['entry_revision'], explanation['floor_price'], explanation['quantity_basis']),
                         ('GENERAL', 1, '9.00', 3))
        self.assertEqual(first['exceptions'], [])
        self.assertEqual((second['unit_price'], second['price_source'], second['suggestion']['unit_price']), (None, 'none', None))
        self.assertEqual(self.codes(second), ['price_missing', 'no_list_price'])
        self.assertEqual(draft['summary']['total'], '30.00')
        # The first save materializes the same engine prices with their fingerprints.
        saved = self.save(order, 0, terms='ENTREGA HOY').data
        stored = DealQuotationDraftLine.objects.get(order_line_id=self.ids(order)[self.item_a.pk])
        self.assertEqual((stored.unit_price, stored.price_source, stored.engine_unit_price, stored.engine_fingerprint, stored.explanation['status']),
                         (Decimal('10.00'), 'engine', Decimal('10.00'), first['suggestion']['fingerprint'], 'priced'))
        self.assertEqual(self.by_code(saved)[self.A]['unit_price'], '10.00')
        self.assertEqual(DealQuotationDraft.objects.get().pricing_context['engine'], ENGINE_VERSION)

    def test_suppliers_without_lists_see_no_suggestion_but_floors_still_apply(self):
        SupplierItemPricing.objects.create(item=self.item_a, supplier=self.supplier_a, floor_price=Decimal('5.00'), updated_by=self.seller_a, updated_at=timezone.now())
        order = self.reviewed()
        draft = self.save(order, 0, lines=[{'order_line_id': self.ids(order)[self.item_a.pk], 'unit_price': '4.00'}]).data
        self.assertEqual((draft['pricing']['configured'], draft['pricing']['price_list']), (False, None))
        line = self.by_code(draft)[self.A]
        self.assertEqual((line['suggestion'], line['price_source']), (None, 'manual'))
        self.assertEqual(line['exceptions'], [{'code': 'below_floor', 'severity': 'confirm', 'message': 'Precio por debajo de tu mínimo (5,00).',
                                               'context': '4.00<5.00', 'acknowledged': False}])

    def test_typed_prices_quantity_changes_and_publish_derive_sources_on_the_server(self):
        general = self.general()
        self.price(general, self.item_a, Decimal('10.00'), floor=Decimal('9.50'))
        self.price(general, self.item_a2, Decimal('4.00'))
        order = self.reviewed()
        ids = self.ids(order)
        manual = self.save(order, 0, lines=[{'order_line_id': ids[self.item_a.pk], 'unit_price': '9.00'}]).data
        line = self.by_code(manual)[self.A]
        self.assertEqual((line['price_source'], self.codes(line)), ('manual', ['below_floor', 'manual_price']))
        self.assertEqual(next(item for item in line['exceptions'] if item['code'] == 'manual_price')['message'], 'Precio manual · sugerido 10,00.')
        self.assertEqual(next(item for item in line['exceptions'] if item['code'] == 'below_floor')['context'], '9.00<9.50')
        back = self.save(order, 1, lines=[{'order_line_id': ids[self.item_a.pk], 'unit_price': '10'}]).data
        self.assertEqual((self.by_code(back)[self.A]['price_source'], self.codes(self.by_code(back)[self.A])), ('engine', []))
        stored = DealQuotationDraftLine.objects.get(order_line_id=ids[self.item_a.pk])
        self.assertEqual((stored.engine_unit_price, stored.explanation['unit_price']), (Decimal('10.00'), '10.00'))
        requantified = self.save(order, 2, lines=[{'order_line_id': ids[self.item_a2.pk], 'quantity': 1}])
        self.assertEqual((requantified.data['repriced'], self.by_code(requantified.data)[self.A2]['price_source']), ([], 'engine'))
        draft = self.save(order, 3, lines=[{'order_line_id': ids[self.item_a.pk], 'unit_price': '12.00'}]).data
        published = self.publish(order, draft)
        self.assertEqual(published.status_code, 200, published.data)
        audit = DealQuotationAudit.objects.get()
        self.assertEqual(audit.engine_version, ENGINE_VERSION)
        rows = {row.line.order_line_id: row for row in DealQuotationLineAudit.objects.select_related('line')}
        first, second = rows[uuid.UUID(ids[self.item_a.pk])], rows[uuid.UUID(ids[self.item_a2.pk])]
        self.assertEqual((first.price_source, first.suggested_price, first.explanation['base']['unit_price']), ('manual', Decimal('10.00'), '10.00'))
        self.assertEqual((second.price_source, second.suggested_price, second.engine_fingerprint, second.quantity_source), ('engine', Decimal('4.00'),
                                                                                                                         self.by_code(draft)[self.A2]['suggestion']['fingerprint'], 'manual'))
        self.assertEqual(PricingAuditEvent.objects.get(kind='quotation_published').payload['price_sources'], {'manual': 1, 'engine': 1})
        # The client sees the published prices only: no suggestion, explanation or source.
        self.client.force_authenticate(self.buyer)
        body = self.client.get(f'/api/v1/accounts/{self.client_account.pk}/sent-requests/{order.pk}/').content.decode()
        for marker in ['10.00', 'GENERAL', 'engine', 'suggest', 'explanation', 'fingerprint']:
            self.assertNotIn(marker, body)

    def test_adjustment_keeps_the_previous_price_and_offers_todays_list_price(self):
        general = self.general()
        self.price(general, self.item_a, Decimal('10.00'))
        self.price(general, self.item_a2, Decimal('4.00'))
        order = self.reviewed()
        draft = self.save(order, 0, lines=[{'order_line_id': self.ids(order)[self.item_a.pk], 'unit_price': '12.50'}]).data
        quote = self.publish(order, draft).data['quotation']
        self.client.force_authenticate(self.buyer)
        self.assertEqual(self.action(order, 'request_adjustment', quotation_id=quote['id'], reason='MENOS').status_code, 200)
        self.price(general, self.item_a2, Decimal('4.40'))
        self.client.force_authenticate(self.seller_a)
        adjustment = self.get_draft(order)
        first, second = self.by_code(adjustment)[self.A], self.by_code(adjustment)[self.A2]
        self.assertEqual((first['unit_price'], first['price_source'], second['unit_price'], second['price_source']), ('12.50', 'previous', '4.00', 'previous'))
        self.assertEqual([(item['code'], item['message']) for item in first['exceptions']],
                         [('differs_from_list', 'La versión anterior usó 12,50; tu lista sugiere hoy 10,00.')])
        self.assertEqual(self.codes(second), ['differs_from_list'])
        # "Usar precio actual" replaces only the chosen line with today's suggestion.
        replaced = self.reprice(order, 0, 'lines', [second['order_line_id']])
        self.assertEqual(replaced.status_code, 200, replaced.data)
        self.assertEqual(replaced.data['repriced'], [{'order_line_id': second['order_line_id'], 'previous_unit_price': '4.00', 'unit_price': '4.40', 'reason': 'lines'}])
        self.assertEqual([(line['unit_price'], line['price_source']) for line in replaced.data['lines']], [('12.50', 'previous'), ('4.40', 'engine')])

    def test_a_list_edit_after_saving_raises_stale_price_and_never_changes_the_stored_price(self):
        general = self.general()
        self.price(general, self.item_a, Decimal('13.50'))
        self.price(general, self.item_a2, Decimal('4.00'))
        order = self.reviewed()
        ids = self.ids(order)
        draft = self.save(order, 0, terms='X').data
        old_fingerprint = self.by_code(draft)[self.A]['suggestion']['fingerprint']
        self.price(general, self.item_a, Decimal('13.10'))
        stale = self.get_draft(order)
        line = self.by_code(stale)[self.A]
        self.assertEqual((line['unit_price'], line['price_source'], line['suggestion']['unit_price']), ('13.50', 'engine', '13.10'))
        self.assertEqual(line['exceptions'], [{'code': 'stale_price', 'severity': 'confirm', 'message': 'Tu precio sugerido cambió de 13,50 a 13,10 desde que se calculó.',
                                               'context': f'{old_fingerprint[:12]}>{line["suggestion"]["fingerprint"][:12]}', 'acknowledged': False}])
        self.assertEqual((stale['pricing']['stale'], stale['summary']['to_confirm']), (1, 1))
        self.assertEqual(DealQuotationDraftLine.objects.get(order_line_id=ids[self.item_a.pk]).unit_price, Decimal('13.50'))
        refused = self.publish(order, stale)
        self.assertEqual((refused.status_code, refused.data['detail'], [item['code'] for item in refused.data['exceptions']]), (409, EXCEPTIONS_DETAIL, ['stale_price']))
        # A quantity edit does not adopt the list change either: in v1 the quantity never moves the price, so the stale price keeps its value.
        requantified = self.save(order, 1, lines=[{'order_line_id': ids[self.item_a.pk], 'quantity': 2}]).data
        line = self.by_code(requantified)[self.A]
        self.assertEqual((requantified['repriced'], line['unit_price'], line['price_source'], self.codes(line)), ([], '13.50', 'engine', ['stale_price']))
        self.assertEqual(DealQuotationDraftLine.objects.get(order_line_id=ids[self.item_a.pk]).engine_fingerprint, old_fingerprint)
        # A settings change moves every fingerprint but not the price: nothing new to confirm.
        settings_row = pricing_settings(self.supplier_a, lock=True)
        settings_row.version, settings_row.over_stock_policy = 2, 'block'
        settings_row.save()
        moved = self.get_draft(order)
        self.assertEqual((self.codes(self.by_code(moved)[self.A]), self.codes(self.by_code(moved)[self.A2])), (['stale_price'], []))
        # Confirming keeps the old price; "Recalcular precios sugeridos" adopts the new one and clears the alert.
        context = self.by_code(moved)[self.A]['exceptions'][0]['context']
        acknowledged = self.save(order, 2, lines=[{'order_line_id': ids[self.item_a.pk], 'acknowledge': [{'code': 'stale_price', 'context': context}]}]).data
        self.assertTrue(self.by_code(acknowledged)[self.A]['exceptions'][0]['acknowledged'])
        recalculated = self.reprice(order, 3, 'engine').data
        self.assertEqual(recalculated['repriced'], [{'order_line_id': ids[self.item_a.pk], 'previous_unit_price': '13.50', 'unit_price': '13.10', 'reason': 'engine'}])
        self.assertEqual((self.by_code(recalculated)[self.A]['exceptions'], recalculated['pricing']['stale']), ([], 0))
        self.assertEqual(self.publish(order, recalculated).status_code, 200)
        self.assertEqual(DealQuotationLineAudit.objects.get(line__order_line_id=ids[self.item_a.pk]).price_source, 'engine')

    def test_reprice_scopes_versions_and_retries(self):
        general = self.general()
        order = self.reviewed()
        ids = self.ids(order)
        draft = self.save(order, 0, lines=[{'order_line_id': ids[self.item_a.pk], 'unit_price': '9.00'}]).data
        self.assertEqual(draft['pricing']['fillable'], 0)
        self.price(general, self.item_a, Decimal('10.00'))
        self.price(general, self.item_a2, Decimal('4.00'))
        self.assertEqual(self.get_draft(order)['pricing']['fillable'], 1)
        key = uuid.uuid4()
        filled = self.reprice(order, 1, 'blank', save_id=key)
        self.assertEqual(filled.status_code, 200)
        self.assertEqual([(line['unit_price'], line['price_source']) for line in filled.data['lines']], [('9.00', 'manual'), ('4.00', 'engine')])
        self.assertEqual((filled.data['draft_version'], filled.data['pricing']['fillable'], [row['reason'] for row in filled.data['repriced']]), (2, 0, ['blank']))
        self.assertEqual(self.reprice(order, 1, 'blank', save_id=key).data['draft_version'], 2, 'A retry of the same reprice returns the draft.')
        self.assertEqual(self.reprice(order, 1, 'engine', save_id=key).status_code, 409)
        self.assertEqual(self.reprice(order, 1, 'blank').status_code, 409)
        self.assertEqual(self.reprice(order, 2, 'engine').data['repriced'], [], 'engine only refreshes engine prices, never manual ones')
        overwritten = self.reprice(order, 3, 'lines', [ids[self.item_a.pk]]).data
        self.assertEqual([(line['unit_price'], line['price_source']) for line in overwritten['lines']], [('10.00', 'engine'), ('4.00', 'engine')])
        # Recalculating after the list lost a price leaves the line blank, so publishing asks for it.
        self.price(general, self.item_a2, None)
        emptied = self.reprice(order, 4, 'engine').data
        self.assertEqual([(line['unit_price'], line['price_source']) for line in emptied['lines']], [('10.00', 'engine'), (None, 'none')])
        self.assertEqual(self.codes(self.by_code(emptied)[self.A2]), ['price_missing', 'no_list_price'])
        for body in [{'scope': 'lines'}, {'scope': 'todo'}, {'scope': 'lines', 'order_line_ids': [str(uuid.uuid4())]}]:
            with self.subTest(body=body):
                self.assertEqual(self.client.post(self.draft_url(order, suffix='reprice/'), {'save_id': str(uuid.uuid4()), 'expected_draft_version': 5, **body},
                                                  format='json').status_code, 400)
        self.client.force_authenticate(self.buyer)
        self.assertEqual(self.client.post(self.draft_url(order, self.client_account, 'reprice/'), {}, format='json').status_code, 403)
        self.client.force_authenticate(self.seller_b)
        self.assertEqual(self.client.post(self.draft_url(order, suffix='reprice/'), {}, format='json').status_code, 404)
        self.client.force_authenticate(self.buyer)
        virtual = self.reviewed([(self.item_a, 1)])
        materialized = self.reprice(virtual, 0, 'blank').data
        self.assertEqual((materialized['persisted'], materialized['draft_version'], materialized['lines'][0]['price_source']), (True, 1, 'engine'))

    def test_currency_parity_and_mismatch_are_explained_without_converting(self):
        general = self.general()
        self.price(general, self.item_a, Decimal('10.00'))
        self.price(general, self.item_a2, Decimal('4.00'))
        order = self.reviewed()
        draft = self.save(order, 0, currency='PAB').data
        self.assertEqual([(line['unit_price'], line['price_source']) for line in draft['lines']], [('10.00', 'engine'), ('4.00', 'engine')])
        self.assertEqual(draft['order_exceptions'], [{'code': 'currency_parity', 'severity': 'info', 'context': '', 'acknowledged': False,
                                                      'message': 'Paridad USD/PAB 1:1 aplicada: tu lista está en USD y la cotización en PAB.'}])
        self.assertEqual(self.by_code(draft)[self.A]['exceptions'], [], 'A new fingerprint with the same price is not stale.')
        SupplierPricingSettings.objects.update_or_create(supplier=self.supplier_a, defaults={'usd_pab_parity': False, 'version': 2})
        mismatch = self.get_draft(order)
        self.assertEqual([item['code'] for item in mismatch['order_exceptions']], ['currency_mismatch'])
        self.assertEqual(mismatch['order_exceptions'][0]['message'], 'Moneda distinta: tu lista está en USD y la cotización en PAB; sin precio sugerido.')
        line = self.by_code(mismatch)[self.A]
        self.assertEqual((line['unit_price'], line['suggestion']['unit_price'], self.codes(line)), ('10.00', None, ['stale_price']))
        self.assertEqual(line['exceptions'][0]['message'], 'Tu lista ya no sugiere un precio para este artículo (antes 10,00).')

    def test_bulk_loader_uses_a_constant_number_of_queries(self):
        general = self.general()
        items = SupplierItem.objects.bulk_create([SupplierItem(supplier=self.supplier_a, supplier_invent_id=f'Q-{index:03}', codigo=f'Q-{index:03}', brand='KSM',
                                                               source='upload', matching_status='matched', part=self.part, reported_quantity=5) for index in range(50)])
        PriceListEntry.objects.bulk_create([PriceListEntry(price_list=general, item=item, unit_price=Decimal('1.00') + index, updated_by=self.seller_a,
                                                           updated_at=timezone.now()) for index, item in enumerate(items)])
        other = PriceList.objects.create(supplier=self.supplier_a, code='NIVEL_A', name='NIVEL A', created_by=self.seller_a)
        PriceListEntry.objects.create(price_list=other, item=items[0], unit_price=Decimal('0.50'), updated_by=self.seller_a, updated_at=timezone.now())
        settings_row = pricing_settings(self.supplier_a)
        def lines(count):
            return [(SimpleNamespace(pk=index, supplier_item=item, supplier_item_id=item.pk, part_id=item.part_id, supplier_invent_id=item.supplier_invent_id,
                                     codigo=item.codigo, brand=item.brand, quantity=2), None) for index, item in enumerate(items[:count])]
        counts = []
        for count in (2, 50):
            with CaptureQueriesContext(connection) as queries:
                results = price_lines(self.supplier_a, self.client_account.pk, 'USD', lines(count), settings_row=settings_row)
            counts.append(len(queries.captured_queries))
            self.assertEqual(len(results), count)
        self.assertEqual(counts, [3, 3])
        # Only the default list prices in v1: profile lists and the fallback between lists arrive with client profiles.
        self.assertEqual((results[0].unit_price, results[49].unit_price, results[0].explanation['base']['fallback']), (Decimal('1.00'), Decimal('50.00'), False))
        PriceListEntry.objects.filter(price_list=general, item=items[1]).delete()
        self.assertEqual(price_lines(self.supplier_a, self.client_account.pk, 'USD', lines(2), settings_row=settings_row)[1].status, 'missing')
