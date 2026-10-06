import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from unittest import skipUnless

from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APITestCase, APITransactionTestCase

from . import test_deals as deal_tests
from . import test_requests as request_tests
from .availability import check_lines
from .deal_views import DealActionSerializer, command_fingerprint
from .models import SupplierItem
from .pricing_models import PricingAuditEvent, SupplierPricingSettings
from .quote_draft_models import DealQuotationDraft, DealQuotationDraftLine
from .quote_drafts import CONFLICT_DETAIL, NOT_EDITABLE_DETAIL
from .request_models import DealCommand, DealEvent, DealQuotation, SupplierRequest

# Fingerprints of legacy deal payloads computed before draft_version existed; in-flight retries must keep matching them.
LEGACY_QUOTE = {'operation_id': '0b0f5c7e-1d2a-4c3b-9e8f-7a6b5c4d3e2f', 'expected_version': 2, 'action': 'quote', 'currency': 'PAB',
                'terms': ' retiro mañana ', 'lines': [{'order_line_id': '1a2b3c4d-5e6f-4a1b-8c2d-3e4f5a6b7c8d', 'quantity': 3, 'unit_price': '12.35'},
                                                     {'order_line_id': '9f8e7d6c-5b4a-4f3e-8d2c-1b0a9f8e7d6c', 'quantity': 0, 'unit_price': '0'}]}
LEGACY_ACCEPT = {'operation_id': '0b0f5c7e-1d2a-4c3b-9e8f-7a6b5c4d3e2f', 'expected_version': 3, 'action': 'accept',
                 'quotation_id': '5d4c3b2a-1f0e-4d9c-8b7a-6f5e4d3c2b1a'}
LEGACY_HASHES = {'quote': '848ae3f318db766b3ae44b251eea1d9b0c5cef34ec769c165be7e7f5c5fab9ea',
                 'accept': '061f084b34d830cb22d1cb1b051df83c08180ac40549bd405d79a671ccd5a1ea'}


class QuoteDraftTests(APITestCase):
    setUp = request_tests.SupplierRequestWorkflowTests.setUp
    account = request_tests.SupplierRequestWorkflowTests.account
    item = request_tests.SupplierRequestWorkflowTests.item
    url = request_tests.SupplierRequestWorkflowTests.url
    payload = request_tests.SupplierRequestWorkflowTests.payload
    submit = request_tests.SupplierRequestWorkflowTests.submit
    order = deal_tests.DealWorkflowTests.order
    review = deal_tests.DealWorkflowTests.review
    action = deal_tests.DealWorkflowTests.action
    quote = deal_tests.DealWorkflowTests.quote
    drafted = deal_tests.DealWorkflowTests.drafted

    def draft_url(self, order, account=None, suffix=''):
        return f'/api/v1/accounts/{(account or self.supplier_a).pk}/requests/{order.pk}/draft/{suffix}'

    def reviewed(self, lines=None):
        order = self.order() if lines is None else SupplierRequest.objects.get(pk=self.submit(self.payload(lines)).data['requests'][0]['id'])
        self.review(order)
        return order

    def lines(self, order):
        return {line.supplier_item_id: line for line in order.lines.all()}

    def save(self, order, version=0, *, save_id=None, account=None, **changes):
        return self.client.post(self.draft_url(order, account), {'save_id': str(save_id or uuid.uuid4()), 'expected_draft_version': version, **changes},
                                format='json')

    def discard(self, order, version=0, account=None):
        return self.client.post(self.draft_url(order, account, 'discard/'), {'save_id': str(uuid.uuid4()), 'expected_draft_version': version}, format='json')

    def get_draft(self, order, account=None):
        response = self.client.get(self.draft_url(order, account))
        self.assertEqual(response.status_code, 200)
        return response.data['draft']

    def by_code(self, draft):
        return {line['codigo']: line for line in draft['lines']}

    def test_get_returns_a_virtual_draft_and_writes_nothing(self):
        order = self.order()
        self.client.force_authenticate(self.seller_a)
        self.assertEqual(self.client.get(self.draft_url(order)).data, {'editable': False, 'draft': None})
        self.review(order)
        before = SupplierRequest.objects.values('version', 'updated_at').get(pk=order.pk)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.draft_url(order))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([query['sql'] for query in queries.captured_queries if not query['sql'].lstrip().upper().startswith('SELECT')], [])
        self.assertTrue(response.data['editable'])
        draft = response.data['draft']
        self.assertEqual({key: draft[key] for key in ['persisted', 'draft_version', 'status', 'base_quotation_id', 'currency', 'terms', 'terms_origin',
                                                       'updated_at', 'updated_by', 'permissions', 'order_exceptions', 'summary']},
                         {'persisted': False, 'draft_version': 0, 'status': 'editing', 'base_quotation_id': None, 'currency': 'USD', 'terms': '',
                          'terms_origin': 'none', 'updated_at': None, 'updated_by': None, 'permissions': {'can_publish': True, 'publish_requires': 'staff'},
                          'order_exceptions': [], 'summary': {'blocking': 2, 'to_confirm': 0, 'info': 0, 'total': '0.00', 'line_count': 2}})
        line = self.by_code(draft)['58411-1R000-G']
        self.assertEqual({key: value for key, value in line.items() if key not in ['order_line_id', 'stock']},
                         {'codigo': '58411-1R000-G', 'brand': 'KSM', 'description': 'TAMBOR A-001', 'requested': 3, 'quantity': 3, 'quantity_source': 'requested',
                          'unit_price': None, 'price_source': 'none', 'suggestion': None, 'note': '',
                          'exceptions': [{'code': 'price_missing', 'severity': 'block', 'message': 'Falta el precio.', 'context': '', 'acknowledged': False}]})
        self.assertEqual({key: line['stock'][key] for key in ['reported_quantity', 'reserved_quantity', 'available_quantity', 'shortfall']},
                         {'reported_quantity': 10, 'reserved_quantity': 2, 'available_quantity': 8, 'shortfall': 0})
        self.assertFalse(DealQuotationDraft.objects.exists())
        self.assertFalse(SupplierPricingSettings.objects.exists())
        self.assertFalse(PricingAuditEvent.objects.exists())
        self.assertEqual(SupplierRequest.objects.values('version', 'updated_at').get(pk=order.pk), before)

    def test_first_save_creates_the_draft_and_later_saves_are_versioned_and_retry_safe(self):
        order = self.reviewed()
        lines = self.lines(order)
        first_line, second_line = str(lines[self.item_a.pk].pk), str(lines[self.item_a2.pk].pk)
        key = uuid.uuid4()
        body = {'currency': 'PAB', 'terms': ' entrega mañana ', 'lines': [{'order_line_id': first_line, 'unit_price': '12.5', 'note': ' solo caja '}]}
        first = self.save(order, 0, save_id=key, **body)
        self.assertEqual(first.status_code, 200)
        self.assertEqual({key: first.data[key] for key in ['persisted', 'draft_version', 'currency', 'terms', 'terms_origin', 'updated_by']},
                         {'persisted': True, 'draft_version': 1, 'currency': 'PAB', 'terms': 'ENTREGA MAÑANA', 'terms_origin': 'saved',
                          'updated_by': {'name': 'request-seller-a'}})
        saved = self.by_code(first.data)
        self.assertEqual([saved['58411-1R000-G'][key] for key in ['quantity', 'quantity_source', 'unit_price', 'price_source', 'note']],
                         [3, 'requested', '12.50', 'manual', 'SOLO CAJA'])
        self.assertEqual([saved['58411-1R000-KSM'][key] for key in ['quantity', 'unit_price', 'price_source']], [2, None, 'none'])
        self.assertEqual(first.data['summary']['total'], '37.50')
        self.assertEqual((DealQuotationDraft.objects.count(), DealQuotationDraftLine.objects.count()), (1, 2))
        self.assertEqual(self.save(order, 0, save_id=key, **body).data, first.data)
        reused = self.save(order, 0, save_id=key, currency='USD')
        self.assertEqual((reused.status_code, reused.data['detail']), (409, 'Este identificador ya se usó para otro cambio del borrador.'))
        stale = self.save(order, 0, lines=[{'order_line_id': first_line, 'quantity': 1}])
        self.assertEqual((stale.status_code, stale.data['detail']), (409, CONFLICT_DETAIL))
        self.assertEqual(stale.data['draft'], first.data)
        second = self.save(order, 1, lines=[{'order_line_id': first_line, 'quantity': 0, 'unit_price': None}])
        line = self.by_code(second.data)['58411-1R000-G']
        self.assertEqual((second.data['draft_version'], line['quantity'], line['quantity_source'], line['unit_price'], line['price_source'], line['note']),
                         (2, 0, 'manual', None, 'none', 'SOLO CAJA'))
        unchanged = self.save(order, 2, lines=[{'order_line_id': second_line, 'quantity': 2}])
        self.assertEqual((unchanged.data['draft_version'], self.by_code(unchanged.data)['58411-1R000-KSM']['quantity_source']), (3, 'requested'))
        self.assertEqual(self.get_draft(order), unchanged.data)

    def test_saves_never_touch_the_order_version_events_or_shared_payloads(self):
        order = self.reviewed()
        before = (SupplierRequest.objects.values('version', 'updated_at').get(pk=order.pk), DealEvent.objects.count(), DealCommand.objects.count())
        detail = f'{self.url(self.supplier_a)}{order.pk}/'
        supplier_view = self.client.get(detail).data
        line = str(order.lines.first().pk)
        self.assertEqual(self.save(order, 0, lines=[{'order_line_id': line, 'unit_price': '9.99', 'note': 'PRIVADA'}]).status_code, 200)
        self.assertEqual(self.save(order, 1, terms='OTRAS').status_code, 200)
        # Only the supplier's own list shows the private draft badge (S6); the detail and every client payload stay unchanged.
        self.assertEqual(self.client.get(self.url(self.supplier_a)).data['results'][0]['draft_state'], 'editing')
        self.assertEqual(self.client.get(detail).data, supplier_view)
        self.assertEqual(self.discard(order, 2).status_code, 200)
        self.assertEqual((SupplierRequest.objects.values('version', 'updated_at').get(pk=order.pk), DealEvent.objects.count(), DealCommand.objects.count()), before)

    def test_writes_are_rejected_outside_review_and_adjustment(self):
        order = self.order()
        self.client.force_authenticate(self.seller_a)
        for status in ['pending', 'quoted', 'handshaked']:
            with self.subTest(status):
                if status == 'quoted':
                    self.review(order)
                    quote = self.quote(order).data['quotation']
                elif status == 'handshaked':
                    self.client.force_authenticate(self.buyer)
                    self.assertEqual(self.action(order, 'accept', quotation_id=quote['id']).status_code, 200)
                    self.client.force_authenticate(self.seller_a)
                self.assertEqual(self.client.get(self.draft_url(order)).data, {'editable': False, 'draft': None})
                for response in [self.save(order, 0, lines=[{'order_line_id': str(order.lines.first().pk), 'unit_price': '1.00'}]), self.discard(order)]:
                    self.assertEqual((response.status_code, response.data['detail']), (409, NOT_EDITABLE_DETAIL))
        self.assertFalse(DealQuotationDraft.objects.exists())

    def test_only_members_of_the_supplier_account_reach_the_draft(self):
        order = self.reviewed()
        cases = [('cliente sin rol de proveedor', self.buyer, self.client_account, 403), ('otro proveedor', self.seller_b, self.supplier_b, 404),
                 ('no miembro', self.seller_b, self.supplier_a, 404), ('superusuario', self.root, self.supplier_a, 404)]
        for label, user, account, expected in cases:
            with self.subTest(label):
                self.client.force_authenticate(user)
                self.assertEqual(self.client.get(self.draft_url(order, account)).status_code, expected)
                self.assertEqual(self.save(order, 0, account=account).status_code, expected)
                self.assertEqual(self.discard(order, 0, account=account).status_code, expected)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.draft_url(order)).status_code, 401)
        self.assertEqual(self.save(order).status_code, 401)
        self.assertFalse(DealQuotationDraft.objects.exists())

    def test_validation_is_lenient_with_blanks_and_rejects_invalid_values(self):
        order = self.reviewed()
        line = str(order.lines.first().pk)
        invalid = [{'lines': [{'order_line_id': line, **change}]} for change in [
            {'quantity': True}, {'quantity': -1}, {'quantity': 10000}, {'quantity': 1.5}, {'unit_price': '1.001'}, {'unit_price': '-1'},
            {'unit_price': True}, {'unit_price': '12345678901.00'}, {'note': 'X' * 501}]]
        invalid += [{'lines': [{'order_line_id': str(uuid.uuid4()), 'quantity': 1}]}, {'lines': [{'order_line_id': line}, {'order_line_id': line}]},
                    {'currency': 'EUR'}, {'terms': 'X' * 5001}]
        for body in invalid:
            with self.subTest(body=str(body)[:80]):
                self.assertEqual(self.save(order, 0, **body).status_code, 400)
        self.assertEqual(self.client.post(self.draft_url(order), {'expected_draft_version': 0}, format='json').status_code, 400)
        self.assertEqual(self.save(order, -1).status_code, 400)
        self.assertFalse(DealQuotationDraft.objects.exists())
        blank = self.save(order, 0, lines=[{'order_line_id': line, 'quantity': None, 'unit_price': None}])
        self.assertEqual(blank.status_code, 200)
        self.assertEqual([value for value in blank.data['lines'] if value['order_line_id'] == line][0]['quantity'], None)

    def test_adjustment_draft_starts_from_the_previous_revision_and_keeps_its_base(self):
        order = self.reviewed()
        lines = [{'order_line_id': str(line.pk), 'quantity': 0 if line.supplier_item_id == self.item_a2.pk else 3,
                  'unit_price': '0' if line.supplier_item_id == self.item_a2.pk else '12.35'} for line in order.lines.all()]
        quote = self.quote(order, lines, currency='PAB', terms='retiro').data['quotation']
        self.client.force_authenticate(self.buyer)
        self.assertEqual(self.action(order, 'request_adjustment', quotation_id=quote['id'], reason='MENOS').status_code, 200)
        self.client.force_authenticate(self.seller_a)
        draft = self.get_draft(order)
        self.assertEqual([draft[key] for key in ['persisted', 'base_quotation_id', 'currency', 'terms', 'terms_origin']],
                         [False, quote['id'], 'PAB', 'RETIRO', 'previous'])
        self.assertEqual([[line[key] for key in ['quantity', 'quantity_source', 'unit_price', 'price_source']] for line in self.by_code(draft).values()],
                         [[3, 'previous', '12.35', 'previous'], [0, 'previous', '0.00', 'previous']])
        self.assertEqual(draft['summary']['total'], '37.05')
        saved = self.save(order, 0).data
        self.assertEqual([saved[key] for key in ['persisted', 'draft_version', 'base_quotation_id', 'terms_origin']], [True, 1, quote['id'], 'saved'])
        self.assertEqual(saved['lines'], draft['lines'])
        self.assertEqual(str(DealQuotationDraft.objects.get().base_quotation_id), quote['id'])

    def test_prefill_can_be_limited_to_available_stock_and_ignores_changed_items(self):
        SupplierPricingSettings.objects.create(supplier=self.supplier_a, prefill_quantity='available')
        order = self.reviewed([(self.item_a, 9), (self.item_a2, 2)])
        lines = self.by_code(self.get_draft(order))
        self.assertEqual([lines['58411-1R000-G'][key] for key in ['quantity', 'quantity_source']], [8, 'available'])
        self.assertEqual([lines['58411-1R000-KSM'][key] for key in ['quantity', 'quantity_source']], [2, 'requested'])
        SupplierItem.objects.filter(pk=self.item_a.pk).update(codigo='OTRO-CODIGO')
        changed = self.by_code(self.get_draft(order))['58411-1R000-G']
        self.assertEqual([changed[key] for key in ['stock', 'quantity', 'quantity_source']], [None, 9, 'requested'])

    def test_discard_deletes_the_draft_with_a_private_audit_and_is_a_no_op_without_one(self):
        order = self.reviewed()
        noop = self.discard(order, 0)
        self.assertEqual((noop.status_code, noop.data['persisted']), (200, False))
        self.assertFalse(PricingAuditEvent.objects.exists())
        self.save(order, 0, lines=[{'order_line_id': str(order.lines.first().pk), 'unit_price': '5.00'}])
        stale = self.discard(order, 2)
        self.assertEqual((stale.status_code, stale.data['detail'], stale.data['draft']['draft_version']), (409, CONFLICT_DETAIL, 1))
        done = self.discard(order, 1)
        self.assertEqual((done.status_code, done.data['persisted'], done.data['draft_version']), (200, False, 0))
        self.assertEqual({line['unit_price'] for line in done.data['lines']}, {None})
        self.assertFalse(DealQuotationDraft.objects.exists())
        event = PricingAuditEvent.objects.get()
        self.assertEqual((event.kind, event.supplier_id, event.client_id, event.order_id, event.actor_id, event.payload['draft_version']),
                         ('draft_discarded', self.supplier_a.pk, self.client_account.pk, order.pk, self.seller_a.pk, 1))

    def test_publish_is_bound_to_the_saved_draft_and_consumes_it(self):
        order = self.reviewed()
        lines = self.lines(order)
        first, second = str(lines[self.item_a.pk].pk), str(lines[self.item_a2.pk].pk)
        self.save(order, 0, currency='PAB', terms=' retiro ', lines=[{'order_line_id': first, 'unit_price': '10.00'}, {'order_line_id': second, 'quantity': 0}])

        def publish(price='10.00', currency='PAB', terms='retiro', **extra):
            return self.action(order, 'quote', currency=currency, terms=terms, lines=[{'order_line_id': first, 'quantity': 3, 'unit_price': price},
                                                                                       {'order_line_id': second, 'quantity': 0, 'unit_price': '0.00'}], **extra)
        cases = [('sin draft_version', {}, 'Hay un borrador guardado; revísalo antes de publicar.'),
                 ('versión vieja', {'draft_version': 2}, 'El borrador cambió. Revísalo antes de publicar.'),
                 ('otro precio', {'draft_version': 1, 'price': '10.01'}, 'La cotización no coincide con el borrador guardado.'),
                 ('otra moneda', {'draft_version': 1, 'currency': 'USD'}, 'La cotización no coincide con el borrador guardado.'),
                 ('otras condiciones', {'draft_version': 1, 'terms': 'otra'}, 'La cotización no coincide con el borrador guardado.')]
        for label, extra, detail in cases:
            with self.subTest(label):
                response = publish(**extra)
                self.assertEqual((response.status_code, response.data['detail']), (409, detail))
        self.assertFalse(DealQuotation.objects.exists())
        self.assertFalse(DealCommand.objects.exists())
        key = uuid.uuid4()
        published = publish(draft_version=1, operation_id=key)
        self.assertEqual((published.status_code, published.data['quotation']['total'], published.data['status']), (200, '30.00', 'quoted'))
        self.assertFalse(DealQuotationDraft.objects.exists())
        self.assertFalse(DealQuotationDraftLine.objects.exists())
        self.assertEqual(publish(draft_version=1, operation_id=key, expected_version=2).data, published.data)
        late = self.save(order, 1, lines=[{'order_line_id': first, 'unit_price': '11.00'}])
        self.assertEqual((late.status_code, late.data['detail']), (409, NOT_EDITABLE_DETAIL))
        self.assertFalse(DealQuotationDraft.objects.exists())
        self.assertEqual(self.client.get(self.draft_url(order)).data, {'editable': False, 'draft': None})

    def test_draft_version_without_a_saved_draft_is_rejected_and_virtual_drafts_publish_after_saving(self):
        order = self.reviewed()
        response = self.quote(order, draft_version=1)
        self.assertEqual((response.status_code, response.data['detail']), (409, 'El borrador cambió. Revísalo antes de publicar.'))
        saved = self.save(order, 0, terms='retiro mañana', lines=[{'order_line_id': str(line.pk), 'unit_price': '12.35'} for line in order.lines.all()])
        self.assertEqual(saved.data['draft_version'], 1)
        self.assertEqual(self.quote(order, draft_version=1).status_code, 200)

    def test_legacy_publish_keeps_its_command_fingerprint(self):
        for name, payload in [('quote', LEGACY_QUOTE), ('accept', LEGACY_ACCEPT)]:
            with self.subTest(name):
                serializer = DealActionSerializer(data=payload)
                serializer.is_valid(raise_exception=True)
                self.assertNotIn('draft_version', serializer.validated_data)
                self.assertEqual(command_fingerprint(serializer.validated_data), LEGACY_HASHES[name])
        order = self.reviewed()
        key = uuid.uuid4()
        body = {'operation_id': str(key), 'expected_version': order.version, 'action': 'quote', 'terms': ' retiro ',
                'lines': [{'order_line_id': str(line.pk), 'quantity': line.quantity, 'unit_price': '12.35'} for line in order.lines.all()]}
        # Only through the legacy path, re-opened with QUOTES_REQUIRE_DRAFT=0; with the default a draftless quote is refused.
        with self.settings(QUOTES_REQUIRE_DRAFT=False):
            self.assertEqual(self.client.post(f'/api/v1/accounts/{self.supplier_a.pk}/deals/{order.pk}/actions/', body, format='json').status_code, 200)
        serializer = DealActionSerializer(data=body)
        serializer.is_valid(raise_exception=True)
        self.assertEqual(DealCommand.objects.get(operation_id=key).payload_hash, command_fingerprint(serializer.validated_data))

    def test_return_quote_and_accept_delete_drafts(self):
        order = self.reviewed()
        quote = self.quote(order).data['quotation']
        self.client.force_authenticate(self.buyer)
        self.action(order, 'request_adjustment', quotation_id=quote['id'], reason='DESCUENTO')
        self.client.force_authenticate(self.seller_a)
        self.assertEqual(self.save(order, 0, lines=[{'order_line_id': str(order.lines.first().pk), 'unit_price': '1.00'}]).status_code, 200)
        self.assertEqual(self.action(order, 'return_quote', quotation_id=quote['id']).status_code, 200)
        self.assertFalse(DealQuotationDraft.objects.exists())
        DealQuotationDraft.objects.create(order=order, supplier=self.supplier_a, base_quotation_id=quote['id'], created_by=self.seller_a, updated_by=self.seller_a)
        self.client.force_authenticate(self.buyer)
        self.assertEqual(self.action(order, 'accept', quotation_id=quote['id']).status_code, 200)
        self.assertFalse(DealQuotationDraft.objects.exists())

    def test_drafts_prepared_on_an_older_revision_are_ignored_and_replaced(self):
        order = self.reviewed()
        quote = self.quote(order).data['quotation']
        self.client.force_authenticate(self.buyer)
        self.action(order, 'request_adjustment', quotation_id=quote['id'], reason='DESCUENTO')
        self.client.force_authenticate(self.seller_a)
        DealQuotationDraft.objects.create(order=order, supplier=self.supplier_a, draft_version=7, terms='VIEJO', created_by=self.seller_a, updated_by=self.seller_a)
        draft = self.get_draft(order)
        self.assertEqual([draft[key] for key in ['persisted', 'draft_version', 'terms', 'base_quotation_id']], [False, 0, 'RETIRO MAÑANA', quote['id']])
        self.assertEqual(self.save(order, 7).status_code, 409)
        saved = self.save(order, 0)
        self.assertEqual([saved.data[key] for key in ['persisted', 'draft_version', 'terms']], [True, 1, 'RETIRO MAÑANA'])
        self.assertEqual(str(DealQuotationDraft.objects.get().base_quotation_id), quote['id'])

    def test_check_lines_uses_the_request_identity_guard_and_reads_constant_queries(self):
        order = self.order()
        lines = list(order.lines.select_related('supplier_item'))
        checked = {entry['order_line_id']: entry for entry in check_lines((line, 1) for line in lines)}
        first = next(line for line in lines if line.supplier_item_id == self.item_a.pk)
        self.assertEqual({key: checked[first.pk][key] for key in ['identity_ok', 'available', 'reported', 'reserved']},
                         {'identity_ok': True, 'available': 8, 'reported': 10, 'reserved': 2})
        SupplierItem.objects.filter(pk=self.item_a.pk).update(codigo='OTRO-CODIGO')
        with transaction.atomic():
            locked = {entry['order_line_id']: entry for entry in check_lines([(line, 1) for line in order.lines.select_related('supplier_item')], lock=True)}
        self.assertEqual((locked[first.pk]['identity_ok'], locked[first.pk]['available']), (False, None))
        self.assertTrue(all(entry['identity_ok'] for pk, entry in locked.items() if pk != first.pk))
        self.review(order)
        extra = [SupplierItem.objects.create(supplier=self.supplier_a, supplier_invent_id=f'A-10{index}', codigo='58411-1R000-G', brand=f'MARCA {index}',
                                             part=self.part, matching_status='matched', reported_quantity=5) for index in range(4)]
        self.client.force_authenticate(self.buyer)
        large = self.reviewed([(item, 1) for item in extra])
        for state in ['virtual', 'saved']:
            counts = []
            for value in [order, large]:
                if state == 'saved':
                    self.save(value, 0)
                with CaptureQueriesContext(connection) as queries:
                    self.get_draft(value)
                counts.append(len(queries))
            with self.subTest(state):
                self.assertEqual(counts[0], counts[1])


@skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL row locks.')
class ConcurrentQuoteDraftTests(APITransactionTestCase):
    setUp = deal_tests.ConcurrentDealTests.setUp
    call = deal_tests.ConcurrentDealTests.call

    def reviewed_order(self):
        _, result = self.call(self.buyer, f'/api/v1/accounts/{self.client_account.pk}/requests/',
                              {'submission_id': str(uuid.uuid4()), 'lines': [{'supplier_item_id': str(self.item.pk), 'quantity': 2}]})
        order = SupplierRequest.objects.get(pk=result['requests'][0]['id'])
        self.call(self.seller, f'/api/v1/accounts/{self.supplier.pk}/requests/{order.pk}/review/', {})
        order.refresh_from_db()
        return order, order.lines.get(), f'/api/v1/accounts/{self.supplier.pk}/requests/{order.pk}/draft/'

    def test_parallel_first_saves_create_one_draft_and_return_one_conflict(self):
        order, line, path = self.reviewed_order()
        def save(price):
            return self.call(self.seller, path, {'save_id': str(uuid.uuid4()), 'expected_draft_version': 0,
                                                 'lines': [{'order_line_id': str(line.pk), 'unit_price': price}]})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, ['1.00', '2.00']))
        self.assertEqual(sorted(code for code, _ in results), [200, 409])
        winner, conflict = (next(data for code, data in results if code == status) for status in (200, 409))
        self.assertEqual((conflict['detail'], conflict['draft']['draft_version']), (CONFLICT_DETAIL, 1))
        self.assertEqual(DealQuotationDraft.objects.get().draft_version, 1)
        self.assertEqual(DealQuotationDraftLine.objects.get().unit_price, Decimal(winner['lines'][0]['unit_price']))

    def test_save_racing_publish_leaves_one_quotation_and_no_orphan_draft(self):
        order, line, path = self.reviewed_order()
        self.assertEqual(self.call(self.seller, path, {'save_id': str(uuid.uuid4()), 'expected_draft_version': 0,
                                                       'lines': [{'order_line_id': str(line.pk), 'unit_price': '1.25'}]})[0], 200)
        def publish(version):
            return self.call(self.seller, f'/api/v1/accounts/{self.supplier.pk}/deals/{order.pk}/actions/', {
                'operation_id': str(uuid.uuid4()), 'expected_version': order.version, 'action': 'quote', 'draft_version': version,
                'lines': [{'order_line_id': str(line.pk), 'quantity': 2, 'unit_price': '1.25'}]})
        def save():
            return self.call(self.seller, path, {'save_id': str(uuid.uuid4()), 'expected_draft_version': 1,
                                                 'lines': [{'order_line_id': str(line.pk), 'note': 'REVISADO'}]})
        with ThreadPoolExecutor(max_workers=2) as pool:
            published, saved = pool.submit(publish, 1), pool.submit(save)
            results = {'publish': published.result(), 'save': saved.result()}
        self.assertEqual(sorted(code for code, _ in results.values()), [200, 409])
        if results['publish'][0] == 409:
            self.assertEqual(results['publish'][1]['detail'], 'El borrador cambió. Revísalo antes de publicar.')
            self.assertEqual(publish(2)[0], 200)
        else:
            self.assertEqual(results['save'][1]['detail'], NOT_EDITABLE_DETAIL)
        self.assertEqual(DealQuotation.objects.count(), 1)
        self.assertFalse(DealQuotationDraft.objects.exists())
        self.assertFalse(DealQuotationDraftLine.objects.exists())
        order.refresh_from_db()
        self.assertEqual(order.status, 'quoted')
