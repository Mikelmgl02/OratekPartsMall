import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless

from django.db import connection
from django.test import override_settings
from rest_framework.test import APITestCase, APITransactionTestCase

from . import test_deals as deal_tests
from . import test_quote_drafts as draft_tests
from . import test_requests as request_tests
from .deal_views import ACCEPT_SHORTFALL_DETAIL, EXCEPTIONS_DETAIL, RETURN_SHORTFALL_DETAIL
from .models import SupplierItem
from .pricing_models import PricingAuditEvent, SupplierPricingSettings
from .quotation_exceptions import identity_context
from .quote_draft_models import DealQuotationAudit, DealQuotationLineAudit
from .request_models import DealCommand, DealEvent, DealQuotation, SupplierRequest


class QuoteExceptionTests(APITestCase):
    setUp = request_tests.SupplierRequestWorkflowTests.setUp
    account = request_tests.SupplierRequestWorkflowTests.account
    item = request_tests.SupplierRequestWorkflowTests.item
    url = request_tests.SupplierRequestWorkflowTests.url
    payload = request_tests.SupplierRequestWorkflowTests.payload
    submit = request_tests.SupplierRequestWorkflowTests.submit
    stock_snapshot = request_tests.SupplierRequestWorkflowTests.stock_snapshot
    order = deal_tests.DealWorkflowTests.order
    review = deal_tests.DealWorkflowTests.review
    action = deal_tests.DealWorkflowTests.action
    draft_url = draft_tests.QuoteDraftTests.draft_url
    reviewed = draft_tests.QuoteDraftTests.reviewed
    save = draft_tests.QuoteDraftTests.save
    get_draft = draft_tests.QuoteDraftTests.get_draft
    by_code = draft_tests.QuoteDraftTests.by_code

    def line_ids(self, order):
        return {line.supplier_item_id: str(line.pk) for line in order.lines.all()}

    def codes(self, line):
        return {item['code']: item for item in line['exceptions']}

    def stock(self, item, reported):
        SupplierItem.objects.filter(pk=item.pk).update(reported_quantity=reported)

    def publish(self, order, quantities, prices, **extra):
        ids = self.line_ids(order)
        self.client.force_authenticate(self.seller_a)
        return self.action(order, 'quote', lines=[{'order_line_id': ids[item.pk], 'quantity': quantity, 'unit_price': prices[item]}
                                                  for item, quantity in quantities.items()], **extra)

    def drafted(self, order, quantities, prices, version=0):
        """Saves every line of the draft and returns the saved draft."""
        ids = self.line_ids(order)
        response = self.save(order, version, lines=[{'order_line_id': ids[item.pk], 'quantity': quantity, 'unit_price': prices[item]}
                                                    for item, quantity in quantities.items()])
        self.assertEqual(response.status_code, 200)
        return response.data

    def quoted(self, quantities=None, prices=None, order=None):
        """A first revision published through a reviewed draft with every confirm-level alert acknowledged."""
        order = order or self.reviewed()
        quantities = quantities or {self.item_a: 3, self.item_a2: 2}
        prices = prices or {self.item_a: '10.00', self.item_a2: '4.00'}
        draft = self.drafted(order, quantities, prices)
        acks = [{'order_line_id': line['order_line_id'], 'acknowledge': [{'code': item['code'], 'context': item['context']}
                                                                         for item in line['exceptions'] if item['severity'] == 'confirm']}
                for line in draft['lines'] if any(item['severity'] == 'confirm' for item in line['exceptions'])]
        if acks:
            draft = self.save(order, draft['draft_version'], lines=acks).data
        response = self.publish(order, quantities, prices, draft_version=draft['draft_version'])
        self.assertEqual(response.status_code, 200, response.data)
        return order, response.data['quotation']

    def accept(self, order, quotation, **extra):
        self.client.force_authenticate(self.buyer)
        return self.action(order, 'accept', quotation_id=quotation['id'], **extra)

    def supplier_list_alert(self):
        self.client.force_authenticate(self.seller_a)
        return self.client.get(self.url(self.supplier_a)).data['results'][0]['availability_alert']

    def test_draft_exceptions_and_acknowledgements_are_tied_to_the_value_they_confirm(self):
        order = self.reviewed()
        ids = self.line_ids(order)
        draft = self.save(order, 0, lines=[{'order_line_id': ids[self.item_a.pk], 'quantity': 9, 'unit_price': '5.00'}]).data
        first, second = self.by_code(draft)['58411-1R000-G'], self.by_code(draft)['58411-1R000-KSM']
        self.assertEqual(first['exceptions'], [
            {'code': 'offered_gt_available', 'severity': 'confirm', 'message': 'Ofreces 9 y tienes 8 disponibles.', 'context': '9>8', 'acknowledged': False},
            {'code': 'offered_gt_requested', 'severity': 'confirm', 'message': 'Ofreces más unidades de las solicitadas (9 de 3).', 'context': '9>3', 'acknowledged': False}])
        self.assertEqual([item['code'] for item in second['exceptions']], ['price_missing'])
        self.assertEqual({key: draft['summary'][key] for key in ['blocking', 'to_confirm', 'info']}, {'blocking': 1, 'to_confirm': 2, 'info': 0})
        for change in [{'acknowledge': [{'code': 'desconocido', 'context': ''}]}, {'acknowledge': [{'code': 'quantity_missing', 'context': ''}]},
                       {'acknowledge': [{'code': 'zero_price', 'context': ''}], 'revoke': ['zero_price']}, {'revoke': ['price_missing']},
                       {'acknowledge': [{'code': 'zero_price', 'context': 'X' * 101}]}]:
            with self.subTest(change=change):
                self.assertEqual(self.save(order, 1, lines=[{'order_line_id': ids[self.item_a.pk], **change}]).status_code, 400)
        acknowledged = self.save(order, 1, lines=[{'order_line_id': ids[self.item_a.pk], 'acknowledge': [{'code': 'offered_gt_available', 'context': '9>8'}]}]).data
        line = self.by_code(acknowledged)['58411-1R000-G']
        self.assertEqual([item['acknowledged'] for item in line['exceptions']], [True, False])
        self.assertEqual(acknowledged['summary']['to_confirm'], 1)
        stored = order.quote_draft.lines.get(order_line_id=ids[self.item_a.pk]).acknowledgements
        self.assertEqual([(ack['code'], ack['context'], ack['user_id']) for ack in stored], [('offered_gt_available', '9>8', self.seller_a.pk)])
        again = self.save(order, 2, lines=[{'order_line_id': ids[self.item_a.pk], 'acknowledge': [{'code': 'offered_gt_available', 'context': '9>8'}]}]).data
        self.assertEqual(order.quote_draft.lines.get(order_line_id=ids[self.item_a.pk]).acknowledgements, stored)
        # A new quantity changes the context: the earlier confirmation no longer counts.
        edited = self.save(order, again['draft_version'], lines=[{'order_line_id': ids[self.item_a.pk], 'quantity': 10}]).data
        self.assertEqual(self.codes(self.by_code(edited)['58411-1R000-G'])['offered_gt_available'] | {'message': ''},
                         {'code': 'offered_gt_available', 'severity': 'confirm', 'message': '', 'context': '10>8', 'acknowledged': False})
        # So does a further stock drop.
        reacked = self.save(order, edited['draft_version'], lines=[{'order_line_id': ids[self.item_a.pk], 'acknowledge': [{'code': 'offered_gt_available', 'context': '10>8'}]}]).data
        self.assertTrue(self.codes(self.by_code(reacked)['58411-1R000-G'])['offered_gt_available']['acknowledged'])
        self.stock(self.item_a, 9)
        self.assertEqual(self.codes(self.by_code(self.get_draft(order))['58411-1R000-G'])['offered_gt_available']['context'], '10>7')
        self.assertFalse(self.codes(self.by_code(self.get_draft(order))['58411-1R000-G'])['offered_gt_available']['acknowledged'])
        revoked = self.save(order, reacked['draft_version'], lines=[{'order_line_id': ids[self.item_a.pk], 'revoke': ['offered_gt_available']}]).data
        self.assertEqual(order.quote_draft.lines.get(order_line_id=ids[self.item_a.pk]).acknowledgements, [])
        self.assertEqual(revoked['draft_version'], reacked['draft_version'] + 1)

    def test_quantity_price_and_order_level_codes(self):
        order = self.reviewed()
        ids = self.line_ids(order)
        draft = self.save(order, 0, lines=[{'order_line_id': ids[self.item_a.pk], 'quantity': 3, 'unit_price': '0'},
                                           {'order_line_id': ids[self.item_a2.pk], 'quantity': 0}]).data
        lines = self.by_code(draft)
        self.assertEqual([(item['code'], item['severity'], item['context']) for item in lines['58411-1R000-G']['exceptions']], [('zero_price', 'confirm', '3@0.00')])
        self.assertEqual([(item['code'], item['severity'], item['message']) for item in lines['58411-1R000-KSM']['exceptions']],
                         [('zero_offered', 'info', 'No ofreces este artículo.')])
        self.assertEqual(draft['order_exceptions'], [])
        empty = self.save(order, 1, lines=[{'order_line_id': ids[self.item_a.pk], 'quantity': 0}, {'order_line_id': ids[self.item_a2.pk], 'quantity': None}]).data
        self.assertEqual([item['code'] for item in self.by_code(empty)['58411-1R000-KSM']['exceptions']], ['quantity_missing'])
        self.assertEqual(empty['order_exceptions'], [])
        zero = self.save(order, 2, lines=[{'order_line_id': ids[self.item_a2.pk], 'quantity': 0}]).data
        self.assertEqual(zero['order_exceptions'], [{'code': 'all_zero', 'severity': 'block', 'message': 'Ofrece al menos una unidad.', 'context': '', 'acknowledged': False}])
        self.assertEqual((zero['summary']['blocking'], zero['summary']['info']), (1, 2))
        SupplierPricingSettings.objects.create(supplier=self.supplier_a, prefill_quantity='available')
        self.client.force_authenticate(self.buyer)
        reduced = self.reviewed([(self.item_a, 9), (self.item_a2, 2)])
        line = self.by_code(self.get_draft(reduced))['58411-1R000-G']
        self.assertEqual([(item['code'], item['message']) for item in line['exceptions']],
                         [('price_missing', 'Falta el precio.'), ('reduced_to_stock', 'Ajustado a tus existencias (8 de 9).')])

    def test_unacknowledged_alerts_block_publishing_and_acknowledged_ones_are_traced(self):
        order = self.reviewed()
        ids = self.line_ids(order)
        quantities, prices = {self.item_a: 9, self.item_a2: 2}, {self.item_a: '5.00', self.item_a2: '3.00'}
        draft = self.drafted(order, quantities, prices)
        refused = self.publish(order, quantities, prices, draft_version=1)
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.data, {'detail': EXCEPTIONS_DETAIL, 'exceptions': [
            {'order_line_id': ids[self.item_a.pk], 'code': 'offered_gt_available', 'severity': 'confirm', 'message': 'Ofreces 9 y tienes 8 disponibles.', 'context': '9>8', 'acknowledged': False},
            {'order_line_id': ids[self.item_a.pk], 'code': 'offered_gt_requested', 'severity': 'confirm', 'message': 'Ofreces más unidades de las solicitadas (9 de 3).', 'context': '9>3', 'acknowledged': False}]})
        self.assertFalse(DealQuotation.objects.exists())
        self.assertFalse(DealCommand.objects.exists())
        partial = self.save(order, draft['draft_version'], lines=[{'order_line_id': ids[self.item_a.pk], 'acknowledge': [{'code': 'offered_gt_available', 'context': '9>8'}]}]).data
        self.assertEqual([item['code'] for item in self.publish(order, quantities, prices, draft_version=partial['draft_version']).data['exceptions']], ['offered_gt_requested'])
        ready = self.save(order, partial['draft_version'], lines=[{'order_line_id': ids[self.item_a.pk], 'acknowledge': [{'code': 'offered_gt_requested', 'context': '9>3'}]}]).data
        self.assertEqual(ready['summary']['to_confirm'], 0)
        published = self.publish(order, quantities, prices, draft_version=ready['draft_version'])
        self.assertEqual((published.status_code, published.data['status']), (200, 'quoted'))
        quotation = DealQuotation.objects.get()
        audit = DealQuotationAudit.objects.get(quotation=quotation)
        self.assertEqual((audit.draft_version, audit.publisher_id, audit.publisher_permission, audit.order_exceptions, audit.accept_check),
                         (ready['draft_version'], self.seller_a.pk, 'staff', [], None))
        self.assertEqual(audit.settings_snapshot, {'version': 1, 'over_stock_policy': 'confirm', 'over_request_policy': 'confirm',
                                                   'accept_shortfall_policy': 'block', 'publish_min_permission': 'staff', 'prefill_quantity': 'requested',
                                                   'usd_pab_parity': True})
        rows = {row.line.order_line_id: row for row in DealQuotationLineAudit.objects.select_related('line')}
        self.assertEqual(set(rows), set(quotation.lines.values_list('order_line_id', flat=True)))
        first = rows[uuid.UUID(ids[self.item_a.pk])]
        self.assertEqual((first.line.quantity, first.available_at_quote, first.identity_ok_at_quote, first.available_at_accept, first.price_source,
                          first.quantity_source, first.suggested_price, first.engine_fingerprint), (9, 8, True, None, 'manual', 'manual', None, ''))
        self.assertEqual([(item['code'], item['severity'], item['context'], item['acknowledged_by']) for item in first.exceptions],
                         [('offered_gt_available', 'confirm', '9>8', self.seller_a.pk), ('offered_gt_requested', 'confirm', '9>3', self.seller_a.pk)])
        self.assertTrue(all(item['acknowledged_at'] for item in first.exceptions))
        second = rows[uuid.UUID(ids[self.item_a2.pk])]
        self.assertEqual((second.available_at_quote, second.price_source, second.quantity_source, second.exceptions), (10, 'manual', 'requested', []))
        event = PricingAuditEvent.objects.get(kind='quotation_published')
        self.assertEqual((event.supplier_id, event.actor_id, event.client_id, event.order_id, event.object_id, event.payload),
                         (self.supplier_a.pk, self.seller_a.pk, self.client_account.pk, order.pk, str(quotation.pk),
                          {'revision': 1, 'draft_version': ready['draft_version'], 'confirmed': 2, 'unacknowledged': 0,
                           'price_sources': {'manual': 2}}))

    @override_settings(QUOTES_REQUIRE_DRAFT=False)
    def test_block_policies_refuse_even_acknowledged_alerts_and_the_legacy_path_enforces_only_them(self):
        """The draftless legacy path exists only with QUOTES_REQUIRE_DRAFT=0."""
        order = self.reviewed()
        quantities, prices = {self.item_a: 9, self.item_a2: 2}, {self.item_a: '5.00', self.item_a2: '3.00'}
        legacy = self.publish(order, quantities, prices)
        self.assertEqual(legacy.status_code, 200)
        audit = DealQuotationAudit.objects.get()
        line = DealQuotationLineAudit.objects.get(line__order_line__supplier_item=self.item_a)
        self.assertEqual((audit.draft_version, line.price_source, line.quantity_source), (None, 'manual', 'manual'))
        self.assertEqual([(item['code'], item['acknowledged_by'], item['acknowledged_at']) for item in line.exceptions],
                         [('offered_gt_available', None, None), ('offered_gt_requested', None, None)])
        self.assertEqual(PricingAuditEvent.objects.get().payload['unacknowledged'], 2)
        SupplierPricingSettings.objects.create(supplier=self.supplier_a, over_stock_policy='block', over_request_policy='block')
        self.client.force_authenticate(self.buyer)
        blocked = self.reviewed()
        refused = self.publish(blocked, quantities, prices)
        self.assertEqual((refused.status_code, refused.data['detail']), (409, EXCEPTIONS_DETAIL))
        self.assertEqual([(item['code'], item['severity']) for item in refused.data['exceptions']], [('offered_gt_available', 'block'), ('offered_gt_requested', 'block')])
        ids = self.line_ids(blocked)
        draft = self.drafted(blocked, quantities, prices)
        self.assertEqual(draft['summary']['blocking'], 2)
        acked = self.save(blocked, draft['draft_version'], lines=[{'order_line_id': ids[self.item_a.pk], 'acknowledge': [
            {'code': 'offered_gt_available', 'context': '9>8'}, {'code': 'offered_gt_requested', 'context': '9>3'}]}]).data
        self.assertEqual([item['acknowledged'] for item in self.by_code(acked)['58411-1R000-G']['exceptions']], [False, False])
        response = self.publish(blocked, quantities, prices, draft_version=acked['draft_version'])
        self.assertEqual((response.status_code, len(response.data['exceptions'])), (409, 2))
        self.assertEqual(DealQuotation.objects.filter(order=blocked).count(), 0)

    def test_adjustment_revision_derives_price_and_quantity_sources_on_the_server(self):
        order, quotation = self.quoted()
        self.client.force_authenticate(self.buyer)
        self.assertEqual(self.action(order, 'request_adjustment', quotation_id=quotation['id'], reason='SIN EL SEGUNDO').status_code, 200)
        ids = self.line_ids(order)
        self.client.force_authenticate(self.seller_a)
        draft = self.save(order, 0, lines=[{'order_line_id': ids[self.item_a2.pk], 'quantity': 0, 'unit_price': None}]).data
        self.assertEqual(self.publish(order, {self.item_a: 3, self.item_a2: 0}, {self.item_a: '10.00', self.item_a2: '0.00'},
                                      draft_version=draft['draft_version']).status_code, 200)
        latest = DealQuotation.objects.get(revision=2)
        sources = {row.line.order_line.supplier_item_id: (row.price_source, row.quantity_source) for row in DealQuotationLineAudit.objects.filter(line__quotation=latest).select_related('line__order_line')}
        self.assertEqual(sources, {self.item_a.pk: ('previous', 'previous'), self.item_a2.pk: ('none', 'manual')})

    def test_accept_after_stock_drops_is_blocked_with_a_private_trace_and_a_digit_free_message(self):
        order, quotation = self.quoted()
        self.stock(self.item_a, 4)  # 4 reported - 2 reserved = 2 available, below the 3 units quoted.
        before, version = self.stock_snapshot(), SupplierRequest.objects.get(pk=order.pk).version
        key = uuid.uuid4()
        response = self.accept(order, quotation, operation_id=key)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data, {'detail': ACCEPT_SHORTFALL_DETAIL})
        self.assertIsNone(re.search(r'\d', response.content.decode()))
        order.refresh_from_db()
        self.assertEqual((order.status, order.version), ('quoted', version))
        self.assertFalse(DealCommand.objects.filter(account=self.client_account).exists())
        self.assertFalse(DealEvent.objects.filter(kind='accept').exists())
        self.assertIsNone(DealQuotation.objects.get().client_confirmed_at)
        self.assertEqual(self.stock_snapshot(), before)
        event = PricingAuditEvent.objects.get(kind='accept_blocked_shortfall')
        self.assertEqual((event.supplier_id, event.actor_id, event.client_id, event.order_id), (self.supplier_a.pk, self.buyer.pk, self.client_account.pk, order.pk))
        self.assertEqual([(line['quantity'], line['available_at_quote'], line['available_at_accept']) for line in event.payload['lines']], [(3, 8, 2)])
        audit = DealQuotationAudit.objects.get()
        self.assertEqual(audit.accept_check['result'], 'blocked')
        self.assertEqual({line['quantity']: line['shortfall'] for line in audit.accept_check['lines']}, {3: True, 2: False})
        self.assertEqual(sorted(DealQuotationLineAudit.objects.values_list('available_at_accept', flat=True)), [2, 10])
        self.assertEqual(self.supplier_list_alert(), 'blocked')
        self.client.force_authenticate(self.buyer)
        sent = self.client.get(f'/api/v1/accounts/{self.client_account.pk}/sent-requests/').data['results'][0]
        self.assertNotIn('availability_alert', sent)
        # A retry of the same decision is checked again: one audit event per attempt, still no command stored.
        self.assertEqual(self.accept(order, quotation, operation_id=key).status_code, 409)
        self.assertEqual(PricingAuditEvent.objects.filter(kind='accept_blocked_shortfall').count(), 2)
        self.stock(self.item_a, 5)
        accepted = self.accept(order, quotation, operation_id=key)
        self.assertEqual((accepted.status_code, accepted.data['status']), (200, 'handshaked'))
        self.assertNotIn('stock', accepted.data['lines'][0])
        audit.refresh_from_db()
        self.assertEqual(audit.accept_check['result'], 'ok')
        self.assertEqual(sorted(DealQuotationLineAudit.objects.values_list('available_at_accept', flat=True)), [3, 10])
        self.assertIsNone(self.supplier_list_alert())

    def test_a_line_confirmed_above_stock_blocks_only_below_the_units_relied_on(self):
        self.stock(self.item_a, 5)  # 3 available.
        self.client.force_authenticate(self.buyer)
        order = self.reviewed([(self.item_a, 5), (self.item_a2, 2)])
        order, quotation = self.quoted({self.item_a: 5, self.item_a2: 2}, order=order)
        self.assertEqual(DealQuotationLineAudit.objects.get(line__order_line__supplier_item=self.item_a).exceptions[0]['context'], '5>3')
        self.stock(self.item_a, 4)  # 2 available: below the 3 units relied on.
        self.assertEqual(self.accept(order, quotation).status_code, 409)
        self.stock(self.item_a, 5)  # Back to 3: the confirmed shortfall of 2 units does not count.
        self.assertEqual(self.accept(order, quotation).status_code, 200)

    def test_quotations_without_a_trace_are_exempt_and_the_allow_policy_records_the_shortfall(self):
        order, quotation = self.quoted()
        DealQuotationLineAudit.objects.all().delete()
        DealQuotationAudit.objects.all().delete()
        self.client.force_authenticate(self.seller_a)
        trace = f'{self.url(self.supplier_a)}{order.pk}/quotations/{quotation["id"]}/trace/'
        self.assertEqual(self.client.get(trace).data, {'available': False})
        self.stock(self.item_a, 2)
        self.assertEqual(self.accept(order, quotation).status_code, 200)
        self.assertFalse(PricingAuditEvent.objects.filter(kind__startswith='accept').exists())
        SupplierPricingSettings.objects.create(supplier=self.supplier_a, accept_shortfall_policy='allow')
        self.stock(self.item_a, 10)
        self.client.force_authenticate(self.buyer)
        order, quotation = self.quoted(order=self.reviewed())
        self.stock(self.item_a, 3)
        before = self.stock_snapshot()
        accepted = self.accept(order, quotation)
        self.assertEqual((accepted.status_code, accepted.data['status']), (200, 'handshaked'))
        self.assertEqual(self.stock_snapshot(), before)
        audit = DealQuotationAudit.objects.get(quotation_id=quotation['id'])
        self.assertEqual(audit.accept_check['result'], 'accepted_with_shortfall')
        self.assertEqual(PricingAuditEvent.objects.filter(kind='accept_with_shortfall', order=order).count(), 1)
        self.client.force_authenticate(self.seller_a)
        listed = {row['id']: row['availability_alert'] for row in self.client.get(self.url(self.supplier_a)).data['results']}
        self.assertEqual(listed[str(order.pk)], 'accepted_with_shortfall')

    def test_return_quote_after_a_stock_drop_is_rejected(self):
        order, quotation = self.quoted()
        self.client.force_authenticate(self.buyer)
        self.assertEqual(self.action(order, 'request_adjustment', quotation_id=quotation['id'], reason='REVISAR').status_code, 200)
        self.stock(self.item_a2, 1)
        self.client.force_authenticate(self.seller_a)
        refused = self.action(order, 'return_quote', quotation_id=quotation['id'])
        self.assertEqual((refused.status_code, refused.data['detail']), (409, RETURN_SHORTFALL_DETAIL))
        order.refresh_from_db()
        self.assertEqual(order.status, 'adjustment')
        self.stock(self.item_a2, 2)
        self.assertEqual(self.action(order, 'return_quote', quotation_id=quotation['id']).status_code, 200)

    def test_identity_changes_need_a_confirmation_tied_to_the_live_item(self):
        order = self.reviewed()
        ids = self.line_ids(order)
        SupplierItem.objects.filter(pk=self.item_a.pk).update(codigo='OTRO-CODIGO')
        draft = self.drafted(order, {self.item_a: 3, self.item_a2: 2}, {self.item_a: '10.00', self.item_a2: '4.00'})
        found = self.codes(self.by_code(draft)['58411-1R000-G'])['identity_changed']
        self.assertEqual((found['severity'], found['context']), ('confirm', identity_context(SupplierItem.objects.get(pk=self.item_a.pk))))
        acked = self.save(order, draft['draft_version'], lines=[{'order_line_id': ids[self.item_a.pk], 'acknowledge': [{'code': 'identity_changed', 'context': found['context']}]}]).data
        self.assertTrue(self.codes(self.by_code(acked)['58411-1R000-G'])['identity_changed']['acknowledged'])
        SupplierItem.objects.filter(pk=self.item_a.pk).update(brand='OTRA')
        changed = self.codes(self.by_code(self.get_draft(order))['58411-1R000-G'])['identity_changed']
        self.assertNotEqual(changed['context'], found['context'])
        self.assertFalse(changed['acknowledged'])
        SupplierItem.objects.filter(pk=self.item_a.pk).update(brand='KSM')
        self.assertEqual(self.publish(order, {self.item_a: 3, self.item_a2: 2}, {self.item_a: '10.00', self.item_a2: '4.00'},
                                      draft_version=acked['draft_version']).status_code, 200)
        line = DealQuotationLineAudit.objects.get(line__order_line__supplier_item=self.item_a)
        self.assertEqual((line.identity_ok_at_quote, line.available_at_quote), (False, None))
        # Already changed and confirmed at quote time: it cannot "disappear" afterwards.
        self.assertEqual(self.accept(order, {'id': str(DealQuotation.objects.get(order=order).pk)}).status_code, 200)
        self.client.force_authenticate(self.buyer)
        other, quotation = self.quoted(order=self.reviewed([(self.item_a2, 2)]), quantities={self.item_a2: 2}, prices={self.item_a2: '4.00'})
        SupplierItem.objects.filter(pk=self.item_a2.pk).update(matching_status='review')
        self.assertEqual(self.accept(other, quotation).status_code, 409)

    def test_trace_is_supplier_only(self):
        order, quotation = self.quoted({self.item_a: 9, self.item_a2: 2})
        path = lambda account, quote_id=quotation['id']: f'/api/v1/accounts/{account.pk}/requests/{order.pk}/quotations/{quote_id}/trace/'
        self.client.force_authenticate(self.seller_a)
        trace = self.client.get(path(self.supplier_a))
        self.assertEqual(trace.status_code, 200)
        self.assertEqual(set(trace.data), {'available', 'quotation_id', 'revision', 'draft_version', 'publisher', 'publisher_permission', 'published_at',
                                           'settings_snapshot', 'order_exceptions', 'accept_check', 'lines'})
        self.assertEqual((trace.data['available'], trace.data['revision'], trace.data['publisher'], trace.data['accept_check']), (True, 1, {'name': 'request-seller-a'}, None))
        line = next(value for value in trace.data['lines'] if value['codigo'] == '58411-1R000-G')
        self.assertEqual({key: line[key] for key in ['quantity', 'unit_price', 'available_at_quote', 'identity_ok_at_quote', 'price_source', 'quantity_source']},
                         {'quantity': 9, 'unit_price': '10.00', 'available_at_quote': 8, 'identity_ok_at_quote': True, 'price_source': 'manual', 'quantity_source': 'manual'})
        self.assertEqual([(item['code'], item['acknowledged_by']) for item in line['exceptions']],
                         [('offered_gt_available', {'name': 'request-seller-a'}), ('offered_gt_requested', {'name': 'request-seller-a'})])
        self.assertEqual(self.client.get(path(self.supplier_a, uuid.uuid4())).status_code, 404)
        for label, user, account, expected in [('cliente', self.buyer, self.client_account, 403), ('otro proveedor', self.seller_b, self.supplier_b, 404),
                                               ('no miembro', self.seller_b, self.supplier_a, 404), ('superusuario', self.root, self.supplier_a, 404)]:
            with self.subTest(label):
                self.client.force_authenticate(user)
                self.assertEqual(self.client.get(path(account)).status_code, expected)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(path(self.supplier_a)).status_code, 401)
        self.stock(self.item_a, 3)
        self.accept(order, quotation)
        self.client.force_authenticate(self.seller_a)
        check = self.client.get(path(self.supplier_a)).data['accept_check']
        self.assertEqual((check['result'], {line['quantity']: line['shortfall'] for line in check['lines']}), ('blocked', {9: True, 2: False}))


@skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL row locks.')
class ConcurrentAvailabilityTests(APITransactionTestCase):
    setUp = deal_tests.ConcurrentDealTests.setUp
    call = deal_tests.ConcurrentDealTests.call

    def test_accept_racing_a_stock_ingest_neither_deadlocks_nor_writes_stock(self):
        SupplierItem.objects.filter(pk=self.item.pk).update(source='upload')
        for attempt in range(3):
            self.assertEqual(self.call(self.seller, f'/api/v1/accounts/{self.supplier.pk}/inventory/ingest/', {
                'update_id': f'restock-{attempt}', 'supplier_invent_id': '1', 'codigo': 'CONCURRENT-DEAL', 'brand': '', 'source': 'upload', 'quantity': 10})[0], 200)
            _, result = self.call(self.buyer, f'/api/v1/accounts/{self.client_account.pk}/requests/',
                                  {'submission_id': str(uuid.uuid4()), 'lines': [{'supplier_item_id': str(self.item.pk), 'quantity': 4}]})
            order = SupplierRequest.objects.get(pk=result['requests'][0]['id'])
            self.call(self.seller, f'/api/v1/accounts/{self.supplier.pk}/requests/{order.pk}/review/', {})
            order.refresh_from_db()
            lines = [{'order_line_id': str(order.lines.get().pk), 'quantity': 4, 'unit_price': '1.25'}]
            _, draft = self.call(self.seller, f'/api/v1/accounts/{self.supplier.pk}/requests/{order.pk}/draft/',
                                 {'save_id': str(uuid.uuid4()), 'expected_draft_version': 0, 'lines': lines})
            code, quoted = self.call(self.seller, f'/api/v1/accounts/{self.supplier.pk}/deals/{order.pk}/actions/', {
                'operation_id': str(uuid.uuid4()), 'expected_version': order.version, 'action': 'quote', 'draft_version': draft['draft_version'], 'lines': lines})
            self.assertEqual(code, 200)
            def accept():
                return self.call(self.buyer, f'/api/v1/accounts/{self.client_account.pk}/deals/{order.pk}/actions/', {
                    'operation_id': str(uuid.uuid4()), 'expected_version': quoted['version'], 'action': 'accept', 'quotation_id': quoted['quotation']['id']})
            def ingest():
                return self.call(self.seller, f'/api/v1/accounts/{self.supplier.pk}/inventory/ingest/', {
                    'update_id': f'drop-{attempt}', 'supplier_invent_id': '1', 'codigo': 'CONCURRENT-DEAL', 'brand': '', 'source': 'upload', 'quantity': 1})
            with ThreadPoolExecutor(max_workers=2) as pool:
                accepted, ingested = pool.submit(accept), pool.submit(ingest)
                results = accepted.result(), ingested.result()
            with self.subTest(attempt=attempt):
                self.assertEqual(results[1][0], 200)
                self.assertIn(results[0][0], [200, 409])
                order.refresh_from_db()
                self.assertEqual(order.status, 'handshaked' if results[0][0] == 200 else 'quoted')
                self.assertEqual(SupplierItem.objects.get(pk=self.item.pk).reported_quantity, 1)
                self.assertEqual(DealQuotationAudit.objects.get(quotation_id=quoted['quotation']['id']).accept_check['result'],
                                 'ok' if results[0][0] == 200 else 'blocked')
