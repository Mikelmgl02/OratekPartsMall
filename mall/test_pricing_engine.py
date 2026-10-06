import datetime
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

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
from .pricing_engine import CLIENT_DISCOUNT_NAME, ENGINE_VERSION, evaluate_line, price_lines
from .pricing_models import (ClientPricingProfile, PriceList, PriceListEntry, PricingAuditEvent, PricingRule, SupplierItemPricing, SupplierPricingSettings,
                             pricing_settings)
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
                                            'price_list': {'id': str(general.pk), 'code': 'GENERAL', 'name': 'LISTA GENERAL', 'currency': 'USD'},
                                            'profile': {'exists': False, 'version': 0, 'discount_percent': '0.00', 'customer_code': '', 'internal_notes': ''}})
        self.assertEqual(draft['order_exceptions'], [{'code': 'no_profile', 'severity': 'info', 'context': '', 'acknowledged': False,
                                                      'message': 'Cliente sin perfil comercial; se usa tu lista predeterminada GENERAL.'}])
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
        self.assertEqual(draft['order_exceptions'][1:], [{'code': 'currency_parity', 'severity': 'info', 'context': '', 'acknowledged': False,
                                                          'message': 'Paridad USD/PAB 1:1 aplicada: tu lista está en USD y la cotización en PAB.'}])
        self.assertEqual(self.by_code(draft)[self.A]['exceptions'], [], 'A new fingerprint with the same price is not stale.')
        SupplierPricingSettings.objects.update_or_create(supplier=self.supplier_a, defaults={'usd_pab_parity': False, 'version': 2})
        mismatch = self.get_draft(order)
        self.assertEqual([item['code'] for item in mismatch['order_exceptions']], ['no_profile', 'currency_mismatch'])
        self.assertEqual(mismatch['order_exceptions'][1]['message'], 'Moneda distinta: tu lista está en USD y la cotización en PAB; sin precio sugerido.')
        line = self.by_code(mismatch)[self.A]
        self.assertEqual((line['unit_price'], line['suggestion']['unit_price'], self.codes(line)), ('10.00', None, ['stale_price']))
        self.assertEqual(line['exceptions'][0]['message'], 'Tu lista ya no sugiere un precio para este artículo (antes 10,00).')

    def lines(self, items, quantity=2):
        return [(SimpleNamespace(pk=index, supplier_item=item, supplier_item_id=item.pk, part_id=item.part_id, supplier_invent_id=item.supplier_invent_id,
                                 codigo=item.codigo, brand=item.brand, quantity=quantity), None) for index, item in enumerate(items)]

    def test_bulk_loader_uses_a_constant_number_of_queries(self):
        general = self.general()
        mayorista = PriceList.objects.create(supplier=self.supplier_a, code='MAYORISTA', name='MAYORISTA', created_by=self.seller_a)
        items = SupplierItem.objects.bulk_create([SupplierItem(supplier=self.supplier_a, supplier_invent_id=f'Q-{index:03}', codigo=f'Q-{index:03}', brand='KSM',
                                                               source='upload', matching_status='matched', part=self.part, reported_quantity=5) for index in range(50)])
        now = timezone.now()
        PriceListEntry.objects.bulk_create([PriceListEntry(price_list=general, item=item, unit_price=Decimal('1.00') + index, updated_by=self.seller_a, updated_at=now)
                                            for index, item in enumerate(items)]
                                           + [PriceListEntry(price_list=mayorista, item=item, unit_price=Decimal('0.50') + index, updated_by=self.seller_a, updated_at=now)
                                              for index, item in enumerate(items) if index % 2 == 0])
        SupplierItemPricing.objects.bulk_create([SupplierItemPricing(item=item, supplier=self.supplier_a, discount_group='FILTROS', updated_by=self.seller_a, updated_at=now)
                                                 for item in items])
        profile = ClientPricingProfile.objects.create(supplier=self.supplier_a, client=self.client_account, price_list=mayorista, discount_percent=Decimal('5.00'),
                                                      updated_by=self.seller_a)
        for name, values in [('FILTROS', {'scope': 'client', 'profile': profile, 'target': 'line', 'target_value': 'FILTROS', 'value': Decimal('10.00')}),
                             ('KSM', {'scope': 'all', 'target': 'brand', 'target_value': 'KSM', 'value': Decimal('15.00')}),
                             ('NETO', {'scope': 'client', 'profile': profile, 'target': 'item', 'item': items[3], 'kind': 'net_price', 'value': Decimal('3.00'), 'currency': 'USD'}),
                             ('VOLUMEN', {'scope': 'client', 'profile': profile, 'target': 'line', 'target_value': 'FILTROS', 'value': Decimal('15.00'), 'min_quantity': 10}),
                             ('OTRA LINEA', {'scope': 'all', 'target': 'line', 'target_value': 'FRENOS', 'value': Decimal('40.00')})]:
            PricingRule.objects.create(supplier=self.supplier_a, name=name, created_by=self.seller_a, updated_by=self.seller_a, **{'kind': 'discount', **values})
        settings_row = pricing_settings(self.supplier_a)
        counts = []
        for count in (2, 50):
            with CaptureQueriesContext(connection) as queries:
                results = price_lines(self.supplier_a, self.client_account.pk, 'USD', self.lines(items[:count]), settings_row=settings_row)
            counts.append(len(queries.captured_queries))
            self.assertEqual(len(results), count)
        # Profile, default list, entries of the client's list and its fallback, item pricing rows and candidate rules.
        self.assertEqual(counts, [5, 5])
        with CaptureQueriesContext(connection) as queries:
            price_lines(self.supplier_a, self.client_account.pk, 'USD', self.lines(items), settings_row=settings_row, profile=profile)
        self.assertEqual(len(queries.captured_queries), 4)
        first, second, net = results[0], results[1], results[3]
        self.assertEqual((first.unit_price, first.explanation['base']['price_list_code'], first.explanation['steps'][0]['name']), (Decimal('0.45'), 'MAYORISTA', 'FILTROS'))
        self.assertEqual((second.unit_price, second.explanation['base']['price_list_code'], second.explanation['base']['fallback'], second.codes),
                         (Decimal('1.80'), 'GENERAL', True, ['fallback_list']))
        self.assertEqual([item['name'] for item in first.explanation['discarded']], [CLIENT_DISCOUNT_NAME, 'KSM'])
        self.assertEqual(first.explanation['next_breaks'], [{'min_quantity': 10, 'unit_price': '0.43', 'rule_name': 'VOLUMEN'}])
        self.assertEqual((net.unit_price, net.explanation['steps'][0]['action']), (Decimal('3.00'), 'net_price'))
        # Without the pair, only general rules and the default list apply; another client never sees this pair's agreement.
        general_only = price_lines(self.supplier_a, None, 'USD', self.lines(items[:2]), settings_row=settings_row)
        self.assertEqual((general_only[0].unit_price, general_only[0].explanation['profile'], general_only[0].explanation['steps'][0]['name']),
                         (Decimal('0.85'), None, 'KSM'))
        other = self.account('OTRO TALLER', 'client_business', self.buyer)
        self.assertEqual(price_lines(self.supplier_a, other.pk, 'USD', self.lines(items[:1]), settings_row=settings_row)[0].unit_price, Decimal('0.85'))
        # An archived client list falls back to the default list with the fallback_list code.
        PriceList.objects.filter(pk=mayorista.pk).update(active=False)
        archived = price_lines(self.supplier_a, self.client_account.pk, 'USD', self.lines(items[:1]), settings_row=settings_row)[0]
        self.assertEqual((archived.unit_price, archived.explanation['price_list']['code'], archived.codes, archived.explanation['profile']['archived_list']),
                         (Decimal('0.90'), 'GENERAL', ['fallback_list'], 'MAYORISTA'))

    def test_rule_windows_are_evaluated_at_america_panama_dates(self):
        general = self.general()
        self.price(general, self.item_a, Decimal('20.00'))
        PricingRule.objects.create(supplier=self.supplier_a, name='HASTA EL 5', scope='all', target='all', kind='discount', value=Decimal('10.00'),
                                   valid_until=datetime.date(2026, 10, 5), created_by=self.seller_a, updated_by=self.seller_a)
        PricingRule.objects.create(supplier=self.supplier_a, name='DESDE EL 6', scope='all', target='all', kind='discount', value=Decimal('20.00'),
                                   valid_from=datetime.date(2026, 10, 6), created_by=self.seller_a, updated_by=self.seller_a)
        lines = self.lines([self.item_a])
        # 04:59 UTC is still 23:59 of the 5th in Panama (UTC-5); 05:00 UTC is midnight of the 6th.
        for moment, price, name in [(datetime.datetime(2026, 10, 6, 4, 59, tzinfo=datetime.timezone.utc), Decimal('18.00'), 'HASTA EL 5'),
                                    (datetime.datetime(2026, 10, 6, 5, 0, tzinfo=datetime.timezone.utc), Decimal('16.00'), 'DESDE EL 6')]:
            with self.subTest(moment=moment), mock.patch('django.utils.timezone.now', return_value=moment):
                result = price_lines(self.supplier_a, self.client_account.pk, 'USD', lines)[0]
                self.assertEqual((result.unit_price, result.explanation['steps'][0]['name']), (price, name))

    def test_the_client_profile_drives_list_discount_currency_and_terms_of_the_first_draft(self):
        general = self.general()
        mayorista = PriceList.objects.create(supplier=self.supplier_a, code='MAYORISTA', name='LISTA MAYORISTA', created_by=self.seller_a)
        self.price(general, self.item_a, Decimal('15.00'))
        self.price(general, self.item_a2, Decimal('4.00'))
        self.price(mayorista, self.item_a, Decimal('12.00'))
        profile = ClientPricingProfile.objects.create(supplier=self.supplier_a, client=self.client_account, price_list=mayorista, discount_percent=Decimal('5.00'),
                                                      preferred_currency='PAB', default_terms='PRECIOS NO INCLUYEN ITBMS', customer_code='C-00123',
                                                      internal_notes='PAGA A 30 DIAS', updated_by=self.seller_a)
        order = self.reviewed()
        draft = self.get_draft(order)
        self.assertEqual((draft['currency'], draft['terms'], draft['terms_origin']), ('PAB', 'PRECIOS NO INCLUYEN ITBMS', 'profile'))
        self.assertEqual((draft['pricing']['profile'], draft['pricing']['price_list']['code']),
                         ({'exists': True, 'version': 1, 'discount_percent': '5.00', 'customer_code': 'C-00123', 'internal_notes': 'PAGA A 30 DIAS'}, 'MAYORISTA'))
        first, second = self.by_code(draft)[self.A], self.by_code(draft)[self.A2]
        self.assertEqual((first['unit_price'], second['unit_price']), ('11.40', '3.80'))
        self.assertEqual([step['kind'] for step in first['suggestion']['explanation']['steps']], ['rule', 'parity'])
        self.assertEqual(first['suggestion']['explanation']['steps'][0]['name'], CLIENT_DISCOUNT_NAME)
        self.assertEqual(first['suggestion']['explanation']['profile'], {'id': str(profile.pk), 'version': 1, 'discount_percent': '5.00', 'archived_list': None})
        self.assertEqual([(item['code'], item['message']) for item in second['exceptions']], [('fallback_list', 'Precio de la lista GENERAL (respaldo).')])
        self.assertEqual([item['code'] for item in draft['order_exceptions']], ['currency_parity'])
        saved = self.save(order, 0).data
        self.assertEqual((saved['currency'], saved['terms'], saved['terms_origin']), ('PAB', 'PRECIOS NO INCLUYEN ITBMS', 'saved'))
        self.assertEqual({key: DealQuotationDraft.objects.get().pricing_context[key] for key in ('engine', 'profile_id', 'profile_version')},
                         {'engine': ENGINE_VERSION, 'profile_id': str(profile.pk), 'profile_version': 1})
        quote = self.publish(order, saved)
        self.assertEqual(quote.status_code, 200, quote.data)
        self.assertEqual(DealQuotationAudit.objects.values_list('profile_id', 'profile_version').get(), (profile.pk, 1))
        # The client receives the net prices and terms only: no list, discount, code or internal note.
        self.client.force_authenticate(self.buyer)
        body = self.client.get(f'/api/v1/accounts/{self.client_account.pk}/sent-requests/{order.pk}/').content.decode()
        self.assertIn('"unit_price":"11.40"', body)
        for marker in ['MAYORISTA', '12.00', '5.00', 'C-00123', 'PAGA A 30', 'DESCUENTO GENERAL', 'fallback']:
            self.assertNotIn(marker, body)
        self.assertEqual(self.action(order, 'request_adjustment', quotation_id=quote.data['quotation']['id'], reason='MENOS').status_code, 200)
        # An adjustment keeps the previous revision's currency and terms, whatever the profile says now.
        ClientPricingProfile.objects.filter(pk=profile.pk).update(preferred_currency='USD', default_terms='OTRAS', version=2)
        self.client.force_authenticate(self.seller_a)
        adjustment = self.get_draft(order)
        self.assertEqual((adjustment['currency'], adjustment['terms'], adjustment['terms_origin']), ('PAB', 'PRECIOS NO INCLUYEN ITBMS', 'previous'))
        # An archived client list prices from the default list, explained.
        PriceList.objects.filter(pk=mayorista.pk).update(active=False)
        line = self.by_code(self.get_draft(order))[self.A]
        self.assertIn(('fallback_list', 'La lista MAYORISTA del cliente está archivada; se usa tu lista GENERAL.'),
                      [(item['code'], item['message']) for item in line['exceptions']])

    def test_volume_rules_reprice_on_quantity_and_rule_or_profile_edits_raise_stale_price(self):
        general = self.general()
        self.price(general, self.item_a, Decimal('15.00'))
        self.price(general, self.item_a2, Decimal('4.00'))
        set_prices(self.supplier_a, self.seller_a, items=[{'supplier_item_id': self.item_a.pk, 'discount_group': 'FILTROS', 'expected_revision': None}])
        profile = ClientPricingProfile.objects.create(supplier=self.supplier_a, client=self.client_account, updated_by=self.seller_a)
        def rule(name, value, minimum=1):
            return PricingRule.objects.create(supplier=self.supplier_a, name=name, scope='client', profile=profile, target='line', target_value='FILTROS',
                                              kind='discount', value=Decimal(value), min_quantity=minimum, created_by=self.seller_a, updated_by=self.seller_a)
        rule('FILTROS TALLER CENTRAL', '10.00')
        volume = rule('FILTROS VOLUMEN 10+', '15.00', 10)
        order = self.reviewed()
        ids = self.ids(order)
        draft = self.get_draft(order)
        first = self.by_code(draft)[self.A]
        self.assertEqual((first['unit_price'], first['suggestion']['explanation']['next_breaks']),
                         ('13.50', [{'min_quantity': 10, 'unit_price': '12.75', 'rule_name': 'FILTROS VOLUMEN 10+'}]))
        self.assertEqual(draft['order_exceptions'], [], 'A client with a profile has no no_profile notice.')
        saved = self.save(order, 0).data
        moved = self.save(order, 1, lines=[{'order_line_id': ids[self.item_a.pk], 'quantity': 10}]).data
        line = self.by_code(moved)[self.A]
        self.assertEqual(moved['repriced'], [{'order_line_id': ids[self.item_a.pk], 'previous_unit_price': '13.50', 'unit_price': '12.75', 'reason': 'quantity'}])
        self.assertEqual((line['unit_price'], line['price_source'], line['suggestion']['explanation']['steps'][0]['name']), ('12.75', 'engine', 'FILTROS VOLUMEN 10+'))
        self.assertNotEqual(line['suggestion']['fingerprint'], self.by_code(saved)[self.A]['suggestion']['fingerprint'])
        more = self.save(order, 2, lines=[{'order_line_id': ids[self.item_a.pk], 'quantity': 12}]).data
        self.assertEqual((more['repriced'], self.by_code(more)[self.A]['suggestion']['fingerprint']), ([], line['suggestion']['fingerprint']))
        # A profile edit moves every fingerprint of the pair: a line whose price stays (its line rule still wins) needs nothing, a line the new
        # general discount now reaches shows stale_price; the stored prices never change by themselves.
        ClientPricingProfile.objects.filter(pk=profile.pk).update(discount_percent=Decimal('3.00'), version=2)
        edited = self.get_draft(order)
        unchanged, reached = self.by_code(edited)[self.A], self.by_code(edited)[self.A2]
        self.assertEqual((unchanged['unit_price'], unchanged['suggestion']['unit_price'], self.codes(unchanged)), ('12.75', '12.75', ['offered_gt_available', 'offered_gt_requested']))
        self.assertEqual((reached['unit_price'], reached['suggestion']['unit_price'], self.codes(reached), edited['pricing']['stale']), ('4.00', '3.88', ['stale_price'], 1))
        # Editing the winning rule never changes the stored price either: stale_price until the supplier recalculates.
        PricingRule.objects.filter(pk=volume.pk).update(value=Decimal('20.00'), revision=2)
        stale = self.get_draft(order)
        line = self.by_code(stale)[self.A]
        self.assertEqual((line['unit_price'], line['suggestion']['unit_price'], stale['pricing']['stale']), ('12.75', '12.00', 2))
        self.assertIn('stale_price', self.codes(line))
        self.assertEqual([row['unit_price'] for row in self.reprice(order, 3, 'engine').data['repriced']], ['12.00', '3.88'])
        # Two equally specific rules: the higher price wins and the line says so.
        rule('FILTROS ESPECIAL', '25.00', 10)
        conflict = self.by_code(self.get_draft(order))[self.A]
        self.assertEqual((conflict['suggestion']['unit_price'], conflict['suggestion']['explanation']['discarded'][0]),
                         ('12.00', {'rule_id': conflict['suggestion']['explanation']['discarded'][0]['rule_id'], 'name': 'FILTROS ESPECIAL', 'reason': 'precio_menor'}))
        self.assertEqual([(item['code'], item['message']) for item in conflict['exceptions'] if item['code'] == 'rule_conflict'],
                         [('rule_conflict', 'Dos reglas igual de específicas; se usó la de precio mayor.')])


PROFILE = {'id': uuid.UUID('00000000-0000-4000-8000-0000000000a1'), 'version': 3, 'discount_percent': Decimal('5.00'), 'archived_list': None}
ROTONDA = {'id': uuid.UUID('00000000-0000-4000-8000-0000000000a2'), 'version': 1, 'discount_percent': Decimal('0'), 'archived_list': None}
MAYORISTA = {'id': uuid.UUID('00000000-0000-4000-8000-000000000002'), 'code': 'MAYORISTA', 'name': 'MAYORISTA', 'currency': 'USD'}
NIVEL_A = {'id': uuid.UUID('00000000-0000-4000-8000-000000000003'), 'code': 'NIVEL_A', 'name': 'NIVEL A', 'currency': 'USD'}
FILTRO = {'id': uuid.UUID('00000000-0000-4000-8000-0000000000b1'), 'brand': 'KSM', 'discount_group': 'FILTROS'}
PASTILLA = {'id': uuid.UUID('00000000-0000-4000-8000-0000000000b2'), 'brand': 'BOSCH', 'discount_group': ''}


def rule(name, scope, target, value, *, kind='discount', min_quantity=1, target_value='', currency='', valid_from=None, valid_until=None, item_id=None,
         profile=PROFILE, revision=1, created='2026-09-01T00:00:00', active=True):
    return {'id': uuid.uuid5(uuid.NAMESPACE_URL, name), 'revision': revision, 'name': name, 'scope': scope, 'profile_id': profile['id'] if scope == 'client' else None,
            'target': target, 'target_value': target_value, 'item_id': item_id, 'kind': kind, 'value': Decimal(value), 'currency': currency,
            'min_quantity': min_quantity, 'valid_from': valid_from, 'valid_until': valid_until, 'active': active, 'created_at': created}


R1 = rule('PROMO KSM OCTUBRE', 'all', 'brand', '15.00', target_value='KSM', valid_from=datetime.date(2026, 10, 1), valid_until=datetime.date(2026, 10, 31))
R2 = rule('FILTROS TALLER CENTRAL', 'client', 'line', '10.00', target_value='FILTROS', revision=2)
R3 = rule('FILTROS VOLUMEN 10+', 'client', 'line', '15.00', target_value='FILTROS', min_quantity=10)
R4 = rule('BOSCH 12.5', 'all', 'brand', '12.50', target_value='BOSCH')
R5 = rule('NETO BP-778', 'client', 'item', '16.80', kind='net_price', currency='USD', min_quantity=10, item_id=PASTILLA['id'], profile=ROTONDA)


def example1(**overrides):
    """§3.4: TALLER CENTRAL, list MAYORISTA (USD) 15.00, general discount 5 %, 4 units quoted in PAB with parity."""
    return evaluate(**{'price_list': MAYORISTA, 'entry': entry(Decimal('15.00'), revision=7), 'currency': 'PAB', 'item': FILTRO, 'profile': PROFILE,
                       'rules': [R1, R2, R3], **overrides})


def example2(**overrides):
    """§3.5: TIENDA LA ROTONDA, list NIVEL_A without the item, GENERAL 19.99 as fallback, floor 17.00, 2 units in USD."""
    return evaluate(**{'price_list': NIVEL_A, 'entry': {**entry(Decimal('19.99')), 'list': GENERAL, 'fallback': True}, 'quantity': 2,
                       'floor_price': Decimal('17.00'), 'item': PASTILLA, 'profile': ROTONDA, 'rules': [R4, R5], **overrides})


def winner(result):
    return next((step['name'] for step in result.explanation['steps'] if step['kind'] == 'rule'), None)


class PricingRulesCoreTests(SimpleTestCase):
    def test_worked_example_1_client_agreement_volume_break_and_parity(self):
        result = example1()
        self.assertEqual((result.unit_price, result.status, result.parity_applied, result.codes), (Decimal('13.50'), 'priced', True, ['currency_parity']))
        explanation = result.explanation
        self.assertEqual(explanation['steps'], [
            {'kind': 'rule', 'rule_id': str(R2['id']), 'rule_revision': 2, 'name': 'FILTROS TALLER CENTRAL', 'scope': 'client', 'target': 'line',
             'target_value': 'FILTROS', 'min_quantity': 1, 'action': 'discount', 'value': '10.00', 'before': '15.00', 'after': '13.50'},
            {'kind': 'parity', 'from': 'USD', 'to': 'PAB', 'rate': '1'}])
        self.assertEqual(explanation['discarded'], [{'rule_id': 'synthetic:client_discount', 'name': 'DESCUENTO GENERAL DEL CLIENTE', 'reason': 'menos_especifica'},
                                                    {'rule_id': str(R1['id']), 'name': 'PROMO KSM OCTUBRE', 'reason': 'menos_especifica'}])
        self.assertEqual(explanation['next_breaks'], [{'min_quantity': 10, 'unit_price': '12.75', 'rule_name': 'FILTROS VOLUMEN 10+'}])
        self.assertEqual((explanation['profile'], explanation['base']['price_list_code'], explanation['base']['entry_revision'], explanation['quantity_basis']),
                         ({'id': str(PROFILE['id']), 'version': 3, 'discount_percent': '5.00', 'archived_list': None}, 'MAYORISTA', 7, 4))
        # At 10 units the volume rule wins (same scope and target, higher minimum); one unit below the threshold keeps R2.
        at_ten, at_nine = example1(quantity=10), example1(quantity=9)
        self.assertEqual((at_ten.unit_price, winner(at_ten), at_ten.explanation['next_breaks']), (Decimal('12.75'), 'FILTROS VOLUMEN 10+', []))
        self.assertEqual((at_nine.unit_price, winner(at_nine)), (Decimal('13.50'), 'FILTROS TALLER CENTRAL'))
        self.assertNotEqual(at_ten.fingerprint, result.fingerprint)
        self.assertEqual(at_nine.fingerprint, result.fingerprint)
        # No stacking: neither the client's 5 % nor the October promotion is applied on top.
        self.assertEqual(len([step for step in explanation['steps'] if step['kind'] == 'rule']), 1)

    def test_worked_example_2_fallback_list_rounding_and_net_price_break(self):
        result = example2()
        self.assertEqual((result.unit_price, result.codes, result.explanation['base']['price_list_code'], result.explanation['base']['fallback']),
                         (Decimal('17.49'), ['fallback_list'], 'GENERAL', True))
        self.assertEqual(result.explanation['price_list']['code'], 'NIVEL_A')
        self.assertEqual(result.unit_price * 2, Decimal('34.98'))
        self.assertEqual(result.explanation['next_breaks'], [{'min_quantity': 10, 'unit_price': '16.80', 'rule_name': 'NETO BP-778'}])
        ten = example2(quantity=10)
        self.assertEqual((ten.unit_price, winner(ten), ten.codes, ten.explanation['discarded']),
                         (Decimal('16.80'), 'NETO BP-778', ['fallback_list', 'below_floor'], [{'rule_id': str(R4['id']), 'name': 'BOSCH 12.5', 'reason': 'menos_especifica'}]))
        self.assertEqual((ten.explanation['steps'][0]['before'], ten.explanation['steps'][0]['after']), ('19.99', '16.80'))
        missing = example2(entry=None)
        self.assertEqual((missing.unit_price, missing.status, missing.codes, missing.explanation['next_breaks']),
                         (None, 'missing', ['no_list_price'], [{'min_quantity': 10, 'unit_price': '16.80', 'rule_name': 'NETO BP-778'}]))
        self.assertEqual([item['reason'] for item in missing.explanation['discarded']], ['sin_precio_base'])
        # One ROUND_HALF_UP at the end: 10.05 at 50 % is 5.025, which becomes 5.03 (half-even would give 5.02).
        for rules, profile in [([rule('MITAD', 'all', 'all', '50.00')], None), ((), {**PROFILE, 'discount_percent': Decimal('50.00')})]:
            with self.subTest(profile=bool(profile)):
                self.assertEqual(evaluate(entry=entry(Decimal('10.05')), rules=rules, profile=profile, item=FILTRO).unit_price, Decimal('5.03'))

    def test_precedence_client_first_then_target_then_minimum_quantity(self):
        cascade = [rule('TODO', 'all', 'all', '5.00'), rule('MARCA', 'all', 'brand', '6.00', target_value='KSM'), rule('LINEA', 'all', 'line', '7.00', target_value='FILTROS'),
                   rule('ARTICULO', 'all', 'item', '8.00', item_id=FILTRO['id'])]
        for count, name in enumerate(['TODO', 'MARCA', 'LINEA', 'ARTICULO'], start=1):
            with self.subTest(name=name):
                self.assertEqual(winner(evaluate(item=FILTRO, rules=list(reversed(cascade[:count])))), name)
        # Decision D2: a client agreement beats any general rule, even a more specific and cheaper one (the simulator shows this case).
        client_wide = evaluate(item=FILTRO, profile=PROFILE, rules=cascade)
        self.assertEqual((winner(client_wide), client_wide.unit_price), (CLIENT_DISCOUNT_NAME, Decimal('14.25')))
        self.assertEqual([item['reason'] for item in client_wide.explanation['discarded']], ['menos_especifica'] * 4)
        # Another pair's agreement or an inactive or out-of-window rule never applies.
        other = rule('OTRO CLIENTE', 'client', 'all', '50.00', profile=ROTONDA)
        ignored = [other, rule('ARCHIVADA', 'all', 'all', '40.00', active=False), rule('VENCIDA', 'all', 'all', '30.00', valid_until=datetime.date(2026, 10, 4)),
                   rule('FUTURA', 'all', 'all', '20.00', valid_from=datetime.date(2026, 10, 6)), rule('HOY', 'all', 'all', '10.00', valid_from=TODAY, valid_until=TODAY)]
        self.assertEqual(winner(evaluate(item=FILTRO, profile=PROFILE | {'discount_percent': Decimal('0')}, rules=ignored)), 'HOY')
        self.assertIsNone(winner(evaluate(item=FILTRO, profile=None, rules=[R2, other])))
        self.assertIsNone(winner(evaluate(item=PASTILLA, rules=[R2, R1, rule('ARTICULO', 'all', 'item', '8.00', item_id=FILTRO['id'])])))

    def test_ties_take_the_higher_price_then_the_oldest_rule(self):
        cheap = rule('ESPECIAL 12', 'client', 'line', '12.00', target_value='FILTROS', created='2026-01-01T00:00:00')
        conflict = evaluate(item=FILTRO, profile=PROFILE, rules=[cheap, R2])
        self.assertEqual((conflict.unit_price, winner(conflict), conflict.codes), (Decimal('13.50'), 'FILTROS TALLER CENTRAL', ['rule_conflict']))
        self.assertEqual(conflict.explanation['discarded'][0], {'rule_id': str(cheap['id']), 'name': 'ESPECIAL 12', 'reason': 'precio_menor'})
        twin = rule('GEMELA', 'client', 'line', '10.00', target_value='FILTROS', created='2026-01-01T00:00:00')
        same = evaluate(item=FILTRO, profile=PROFILE, rules=[R2, twin])
        self.assertEqual((winner(same), same.codes, same.explanation['discarded'][0]['reason']), ('GEMELA', [], 'empate'))
        # The conflict is reported even when the capped discarded list is already full of more specific rules that cannot apply.
        unusable = [rule(f'NETO PAB {index}', 'client', 'item', '9.00', kind='net_price', currency='PAB', item_id=FILTRO['id'], created=f'2026-01-0{index}T00:00:00')
                    for index in range(1, 7)]
        hidden = evaluate(item=FILTRO, profile=PROFILE, rules=[*unusable, cheap, R2], parity=False)
        self.assertEqual((hidden.unit_price, winner(hidden), hidden.codes, len(hidden.explanation['discarded'])),
                         (Decimal('13.50'), 'FILTROS TALLER CENTRAL', ['rule_conflict'], 5))

    def test_net_prices_need_no_base_and_follow_the_parity_setting(self):
        net = rule('NETO', 'client', 'item', '16.80', kind='net_price', currency='USD', item_id=FILTRO['id'])
        alone = evaluate(item=FILTRO, profile=PROFILE, rules=[net], entry=None)
        self.assertEqual((alone.unit_price, alone.status, alone.codes, alone.explanation['steps'][0]['before']), (Decimal('16.80'), 'priced', [], None))
        unlisted = evaluate(item=FILTRO, profile=PROFILE, rules=[net], entry=None, price_list=None)
        self.assertEqual((unlisted.unit_price, unlisted.configured, unlisted.explanation['price_list']), (Decimal('16.80'), True, None))
        self.assertEqual((evaluate(item=FILTRO, entry=None, price_list=None).configured, evaluate(item=FILTRO, entry=None, price_list=None).status), (False, 'no_price_list'))
        balboas = rule('NETO PAB', 'client', 'item', '16.80', kind='net_price', currency='PAB', item_id=FILTRO['id'])
        parity = evaluate(item=FILTRO, profile=PROFILE, rules=[balboas])
        self.assertEqual((parity.unit_price, parity.codes, parity.explanation['steps'][1]), (Decimal('16.80'), ['currency_parity'], {'kind': 'parity', 'from': 'PAB', 'to': 'USD', 'rate': '1'}))
        strict = evaluate(item=FILTRO, profile=PROFILE, rules=[balboas], parity=False)
        self.assertEqual((strict.unit_price, winner(strict), strict.explanation['discarded']),
                         (Decimal('14.25'), CLIENT_DISCOUNT_NAME, [{'rule_id': str(balboas['id']), 'name': 'NETO PAB', 'reason': 'moneda_distinta'}]))
        # A PAB quotation without parity: the USD list cannot be used, the PAB net price can.
        self.assertEqual((evaluate(item=FILTRO, profile=PROFILE, rules=[balboas], parity=False, currency='PAB').unit_price), Decimal('16.80'))
        blocked = evaluate(item=FILTRO, profile=PROFILE, rules=[R2], parity=False, currency='PAB')
        self.assertEqual((blocked.unit_price, blocked.status, blocked.codes, [item['reason'] for item in blocked.explanation['discarded']]),
                         (None, 'currency_mismatch', ['currency_mismatch'], ['moneda_distinta', 'moneda_distinta']))

    def test_fingerprint_is_stable_within_a_rule_and_follows_entry_rule_and_profile_edits(self):
        base = example1().fingerprint
        for quantity in (1, 4, 9):
            self.assertEqual(example1(quantity=quantity).fingerprint, base)
        self.assertEqual(example1(rules=[R1, R2, R3, rule('UNO POR CIENTO', 'all', 'all', '1.00')]).fingerprint, base, 'A rule that does not win changes nothing.')
        for label, overrides in [('entrada', {'entry': entry(Decimal('15.00'), revision=8)}), ('regla', {'rules': [R1, {**R2, 'revision': 3}, R3]}),
                                 ('perfil', {'profile': {**PROFILE, 'version': 4}}), ('valor', {'rules': [R1, {**R2, 'value': Decimal('11.00')}, R3]}),
                                 ('lista', {'price_list': {**MAYORISTA, 'id': uuid.uuid4()}})]:
            with self.subTest(label):
                self.assertNotEqual(example1(**overrides).fingerprint, base)

    def test_caps_archived_lists_and_decimal_only_inputs(self):
        losers = [rule(f'GENERAL {index}', 'all', 'all', f'{index}.00') for index in range(1, 8)]
        capped = evaluate(item=FILTRO, profile=PROFILE, rules=[R2, *losers, *[rule(f'VOLUMEN {count}', 'client', 'line', f'{count}.00', target_value='FILTROS',
                                                                                   min_quantity=count) for count in (20, 30, 40)]])
        self.assertEqual((len(capped.explanation['discarded']), [item['min_quantity'] for item in capped.explanation['next_breaks']]), (5, [20, 30]))
        # A higher threshold whose winner keeps the previous break's price is no new break (the client's 10+ still beats a general 20+).
        repeated = example1(rules=[R1, R2, R3, rule('KSM 20+', 'all', 'brand', '20.00', target_value='KSM', min_quantity=20)])
        self.assertEqual(repeated.explanation['next_breaks'], [{'min_quantity': 10, 'unit_price': '12.75', 'rule_name': 'FILTROS VOLUMEN 10+'}])
        stepped = example1(rules=[R1, R2, R3, rule('KSM 20+', 'all', 'brand', '20.00', target_value='KSM', min_quantity=20),
                                  rule('FILTROS 50+', 'client', 'line', '30.00', target_value='FILTROS', min_quantity=50)])
        self.assertEqual([(item['min_quantity'], item['unit_price']) for item in stepped.explanation['next_breaks']], [(10, '12.75'), (50, '10.50')])
        archived = evaluate(item=FILTRO, profile={**PROFILE, 'archived_list': 'NIVEL_A'})
        self.assertEqual((archived.codes, archived.explanation['profile']['archived_list']), (['fallback_list'], 'NIVEL_A'))
        for overrides in [{'rules': [{**R2, 'value': 10.0}]}, {'profile': {**PROFILE, 'discount_percent': 5.0}}]:
            with self.subTest(overrides=overrides), self.assertRaises(TypeError):
                evaluate(item=FILTRO, **overrides)
