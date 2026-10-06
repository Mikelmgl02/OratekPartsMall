import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from unittest import skipUnless

from django.db import close_old_connections, connection
from rest_framework.test import APITestCase, APITransactionTestCase, APIClient

from .models import Account, Membership, Part, Role, SupplierItem, User
from .request_models import (SupplierRequest, SupplierRequestLine, ClientRequestSubmission,
                             RequestContribution, DealQuotation, DealEvent, DealMessage)
from . import test_requests as request_tests


class DealWorkflowTests(APITestCase):
    setUp = request_tests.SupplierRequestWorkflowTests.setUp
    account = request_tests.SupplierRequestWorkflowTests.account
    item = request_tests.SupplierRequestWorkflowTests.item
    url = request_tests.SupplierRequestWorkflowTests.url
    payload = request_tests.SupplierRequestWorkflowTests.payload
    submit = request_tests.SupplierRequestWorkflowTests.submit
    stock_snapshot = request_tests.SupplierRequestWorkflowTests.stock_snapshot

    def order(self):
        response = self.submit(self.payload([(self.item_a, 3), (self.item_a2, 2)]))
        self.assertEqual(response.status_code, 200)
        return SupplierRequest.objects.get(pk=response.data['requests'][0]['id'])

    def review(self, order):
        self.client.force_authenticate(self.seller_a)
        response = self.client.post(f'{self.url(self.supplier_a)}{order.pk}/review/', {}, format='json')
        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        return response

    def action(self, order, action, *, account=None, expected_version=None, operation_id=None, **extra):
        order.refresh_from_db()
        if account is None:
            account = self.supplier_a if action in ['quote', 'return_quote'] else self.client_account
        body = {'operation_id': str(operation_id or uuid.uuid4()), 'expected_version': expected_version or order.version,
                'action': action, **extra}
        return self.client.post(f'/api/v1/accounts/{account.pk}/deals/{order.pk}/actions/', body, format='json')

    def quote(self, order, **kwargs):
        return self.action(order, 'quote', lines=[{'order_line_id': str(line.pk), 'quantity': line.quantity,
                                                'unit_price': '12.35'} for line in order.lines.all()],
                           terms=' retiro mañana ', **kwargs)

    def messages(self, order, account=None):
        return f'/api/v1/accounts/{(account or self.client_account).pk}/deals/{order.pk}/messages/'

    def test_submissions_append_only_to_same_client_pending_order_and_freeze_receipts(self):
        first_payload = self.payload([(self.item_a, 3)])
        first = self.submit(first_payload)
        second = self.submit(self.payload([(self.item_a, 2), (self.item_a2, 4)]))
        order = SupplierRequest.objects.get()
        self.assertEqual(first.data['requests'][0]['id'], second.data['requests'][0]['id'])
        self.assertTrue(second.data['requests'][0]['appended'])
        self.assertEqual(second.data['requests'][0]['unit_count'], 9)
        self.assertEqual(second.data['requests'][0]['added_unit_count'], 6)
        self.assertEqual(dict(order.lines.values_list('supplier_item_id', 'quantity')), {self.item_a.pk: 5, self.item_a2.pk: 4})
        self.assertEqual(self.submit(first_payload).data, first.data)
        self.assertEqual(order.version, 2)
        self.assertEqual(RequestContribution.objects.count(), 2)
        other = self.account('OTRO CLIENTE', 'client_business', self.buyer)
        self.assertEqual(self.submit(self.payload([(self.item_a, 1)]), account=other).status_code, 200)
        self.assertEqual(SupplierRequest.objects.count(), 2)

    def test_review_locks_lines_and_next_send_creates_new_supplier_order(self):
        order = self.order()
        before = list(order.lines.values())
        reviewed = self.review(order)
        self.assertEqual(reviewed.data['status'], 'reviewed')
        self.assertEqual(order.version, 2)
        self.assertEqual(self.review(order).data, reviewed.data)
        self.client.force_authenticate(self.buyer)
        sent = self.submit(self.payload([(self.item_a, 5)]))
        self.assertNotEqual(sent.data['requests'][0]['id'], str(order.pk))
        self.assertFalse(sent.data['requests'][0]['appended'])
        self.assertEqual(list(order.lines.values()), before)

    def test_supplier_confirmed_quote_and_client_accept_close_exact_revision(self):
        order = self.order()
        before = self.stock_snapshot()
        self.review(order)
        quoted = self.quote(order)
        self.assertEqual(quoted.status_code, 200)
        quote = quoted.data['quotation']
        self.assertEqual((quote['revision'], quote['total'], quote['currency']), (1, '61.75', 'USD'))
        self.assertIsNotNone(quote['supplier_confirmed_at'])
        self.assertIsNone(quote['client_confirmed_at'])
        self.assertEqual(quote['terms'], 'RETIRO MAÑANA')
        self.assertEqual(self.quote(order).status_code, 409)
        self.client.force_authenticate(self.buyer)
        accepted = self.action(order, 'accept', quotation_id=quote['id'])
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.data['status'], 'handshaked')
        self.assertIsNotNone(accepted.data['handshaked_at'])
        self.assertIsNotNone(accepted.data['quotation']['client_confirmed_at'])
        self.assertNotIn('stock', accepted.data['lines'][0])
        self.assertEqual(self.stock_snapshot(), before)
        self.assertEqual(self.action(order, 'request_adjustment', quotation_id=quote['id'], reason='OTRO').status_code, 409)
        self.client.force_authenticate(self.seller_a)
        self.assertEqual(self.quote(order).status_code, 409)
        self.client.force_authenticate(self.buyer)
        history = self.client.get(f'/api/v1/accounts/{self.client_account.pk}/sent-requests/', {'status': 'handshaked'})
        self.assertEqual(history.data['count'], 1)

    def test_adjustment_preserves_prices_new_quote_revises_and_stale_accept_is_rejected(self):
        order = self.order(); self.review(order)
        quote1 = self.quote(order).data['quotation']
        self.client.force_authenticate(self.buyer)
        adjusted = self.action(order, 'request_adjustment', quotation_id=quote1['id'], reason=' bajar cantidad ')
        self.assertEqual(adjusted.status_code, 200)
        self.assertEqual(adjusted.data['status'], 'adjustment')
        self.assertEqual(DealMessage.objects.get().body, 'BAJAR CANTIDAD')
        self.assertEqual(self.action(order, 'accept', quotation_id=quote1['id']).status_code, 409)
        self.client.force_authenticate(self.seller_a)
        lines = [{'order_line_id': str(line.pk), 'quantity': 1, 'unit_price': '10.00'} for line in order.lines.all()]
        quote2 = self.action(order, 'quote', lines=lines).data['quotation']
        self.assertEqual((quote2['revision'], quote2['total']), (2, '20.00'))
        self.assertEqual(DealQuotation.objects.get(pk=quote1['id']).total, Decimal('61.75'))
        self.client.force_authenticate(self.buyer)
        self.assertEqual(self.action(order, 'accept', quotation_id=quote1['id']).status_code, 409)
        self.assertEqual(self.action(order, 'accept', quotation_id=quote2['id']).status_code, 200)

    def test_return_same_quote_and_retry_decisions_are_idempotent(self):
        order = self.order(); self.review(order)
        key = uuid.uuid4(); version = order.version
        first = self.quote(order, operation_id=key, expected_version=version)
        again = self.quote(order, operation_id=key, expected_version=version)
        self.assertEqual(again.data, first.data)
        self.assertEqual(DealQuotation.objects.count(), 1)
        quote_id = first.data['quotation']['id']
        self.client.force_authenticate(self.buyer)
        self.action(order, 'request_adjustment', quotation_id=quote_id, reason='DESCUENTO')
        self.client.force_authenticate(self.seller_a)
        returned = self.action(order, 'return_quote', quotation_id=quote_id)
        self.assertEqual(returned.data['status'], 'quoted')
        self.assertEqual(returned.data['quotation']['id'], quote_id)
        self.assertEqual(DealQuotation.objects.count(), 1)
        self.client.force_authenticate(self.buyer)
        order.refresh_from_db(); version = order.version; key = uuid.uuid4()
        first = self.action(order, 'accept', quotation_id=quote_id, operation_id=key, expected_version=version)
        again = self.action(order, 'accept', quotation_id=quote_id, operation_id=key, expected_version=version)
        self.assertEqual(first.data, again.data)
        self.assertEqual(DealEvent.objects.filter(kind='accept').count(), 1)
        changed = self.action(order, 'accept', quotation_id=uuid.uuid4(), operation_id=key, expected_version=version)
        self.assertEqual(changed.status_code, 409)

    def test_stale_version_wrong_party_and_outsiders_cannot_decide_or_read_chat(self):
        order = self.order(); self.review(order)
        self.assertEqual(self.quote(order, expected_version=1).status_code, 409)
        self.client.force_authenticate(self.buyer)
        self.assertEqual(self.quote(order, account=self.client_account).status_code, 403)
        self.client.force_authenticate(self.seller_b)
        self.assertEqual(self.quote(order, account=self.supplier_b).status_code, 404)
        self.assertEqual(self.client.get(self.messages(order, self.supplier_b)).status_code, 404)
        self.client.force_authenticate(self.root)
        self.assertEqual(self.client.get(self.messages(order)).status_code, 404)

    def test_quote_requires_review_and_all_owned_lines_and_valid_prices(self):
        order = self.order(); self.client.force_authenticate(self.seller_a)
        self.assertEqual(self.quote(order).status_code, 409)
        self.review(order)
        line = order.lines.first()
        for lines in [[], [{'order_line_id': str(line.pk), 'quantity': 1, 'unit_price': '-1'}],
                      [{'order_line_id': str(line.pk), 'quantity': 1, 'unit_price': '1.001'}],
                      [{'order_line_id': str(uuid.uuid4()), 'quantity': 1, 'unit_price': '2'}],
                      [{'order_line_id': str(line.pk), 'quantity': True, 'unit_price': '2'}],
                      [{'order_line_id': str(value.pk), 'quantity': 0, 'unit_price': '0'} for value in order.lines.all()]]:
            self.assertEqual(self.action(order, 'quote', lines=lines).status_code, 400)
        self.assertFalse(DealQuotation.objects.exists())
        lines = [{'order_line_id': str(value.pk), 'quantity': 0 if value.pk == line.pk else 1, 'unit_price': '3.50'} for value in order.lines.all()]
        self.assertEqual(self.action(order, 'quote', lines=lines).data['quotation']['total'], '3.50')

    def test_messages_are_private_durable_retry_safe_and_cursor_paginated(self):
        order = self.order(); body = {'message_id': str(uuid.uuid4()), 'body': ' necesito retiro '}
        url = self.messages(order)
        first = self.client.post(url, body, format='json')
        again = self.client.post(url, body, format='json')
        self.assertEqual(first.status_code, 200); self.assertEqual(first.data, again.data)
        self.assertEqual(first.data['body'], 'NECESITO RETIRO')
        self.assertEqual(DealMessage.objects.count(), 1)
        self.assertEqual(self.client.post(url, {**body, 'body': 'OTRO'}, format='json').status_code, 409)
        self.client.force_authenticate(self.seller_a)
        response = self.client.get(self.messages(order, self.supplier_a))
        self.assertEqual(response.data['results'][0]['id'], first.data['id'])
        reply = self.client.post(self.messages(order, self.supplier_a), {'message_id': str(uuid.uuid4()), 'body': 'SI'}, format='json')
        self.client.force_authenticate(self.buyer)
        received = self.client.get(url, {'after': first.data['id']})
        self.assertEqual([value['id'] for value in received.data['results']], [reply.data['id']])
        self.assertEqual(self.client.get(url, {'after': received.data['cursor']}).data['results'], [])
        self.assertEqual(self.client.get(url, {'after': -1}).status_code, 400)
        self.assertEqual(self.client.post(url, {'message_id': str(uuid.uuid4()), 'body': ' '}, format='json').status_code, 400)

    def test_accumulated_quantity_overflow_rolls_back_every_supplier_contribution(self):
        first = self.submit(self.payload([(self.item_a, 9999)]))
        failed = self.submit(self.payload([(self.item_a, 1), (self.item_b, 1)]))
        self.assertEqual(failed.status_code, 400)
        self.assertEqual(SupplierRequest.objects.count(), 1)
        self.assertEqual(ClientRequestSubmission.objects.count(), 1)
        self.assertEqual(SupplierRequestLine.objects.get().quantity, 9999)
        self.assertFalse(RequestContribution.objects.filter(appended=True).exists())

    def test_chat_can_load_earlier_messages_and_keeps_new_cursor_separate(self):
        order = self.order()
        DealMessage.objects.bulk_create([DealMessage(order=order, account=self.client_account, actor=self.buyer,
                                                    message_id=uuid.uuid4(), body=str(index)) for index in range(205)])
        url = self.messages(order)
        latest = self.client.get(url).data
        self.assertEqual(len(latest['results']), 100)
        self.assertTrue(latest['has_earlier'])
        earlier = self.client.get(url, {'before': latest['results'][0]['id']}).data
        self.assertEqual(len(earlier['results']), 100)
        self.assertTrue(earlier['has_earlier'])
        earliest = self.client.get(url, {'before': earlier['results'][0]['id']}).data
        self.assertEqual(len(earliest['results']), 5)
        self.assertFalse(earliest['has_earlier'])
        self.assertEqual(self.client.get(url, {'after': latest['cursor']}).data['results'], [])
        self.assertEqual(self.client.get(url, {'before': 3, 'after': 1}).status_code, 400)


@skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL row locks.')
class ConcurrentDealTests(APITransactionTestCase):
    def setUp(self):
        self.buyer = User.objects.create_user('deal-concurrent-buyer', 'deal-concurrent-buyer@example.invalid', 'Request938!')
        self.seller = User.objects.create_user('deal-concurrent-seller', 'deal-concurrent-seller@example.invalid', 'Request938!')
        self.client_account = Account.objects.create(name='CLIENTE')
        self.supplier = Account.objects.create(name='PROVEEDOR')
        client_role, _ = Role.objects.get_or_create(code='client_business', defaults={'name': 'CLIENTE', 'capability': 'client'})
        supplier_role, _ = Role.objects.get_or_create(code='supplier_retail', defaults={'name': 'PROVEEDOR', 'capability': 'supplier'})
        self.client_account.roles.add(client_role); self.supplier.roles.add(supplier_role)
        Membership.objects.create(user=self.buyer, account=self.client_account)
        Membership.objects.create(user=self.seller, account=self.supplier)
        part = Part.objects.create(sku='CONCURRENT-DEAL')
        self.item = SupplierItem.objects.create(supplier=self.supplier, supplier_invent_id='1', codigo=part.sku,
                                               part=part, matching_status='matched', reported_quantity=10)

    def call(self, user, path, payload):
        close_old_connections()
        try:
            client = APIClient(); client.force_authenticate(User.objects.get(pk=user.pk))
            response = client.post(path, payload, format='json')
            return response.status_code, response.data
        finally:
            close_old_connections()

    def test_parallel_distinct_sends_create_one_order_and_aggregate_without_lost_units(self):
        path = f'/api/v1/accounts/{self.client_account.pk}/requests/'
        def send(_):
            return self.call(self.buyer, path, {'submission_id': str(uuid.uuid4()), 'lines': [{'supplier_item_id': str(self.item.pk), 'quantity': 2}]})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(send, range(2)))
        self.assertEqual([value[0] for value in results], [200, 200])
        self.assertEqual(SupplierRequest.objects.count(), 1)
        self.assertEqual(SupplierRequestLine.objects.get().quantity, 4)
        self.assertEqual(RequestContribution.objects.count(), 2)

    def test_parallel_quote_decisions_cannot_create_two_current_revisions(self):
        _, result = self.call(self.buyer, f'/api/v1/accounts/{self.client_account.pk}/requests/',
                             {'submission_id': str(uuid.uuid4()), 'lines': [{'supplier_item_id': str(self.item.pk), 'quantity': 2}]})
        order = SupplierRequest.objects.get(pk=result['requests'][0]['id'])
        self.call(self.seller, f'/api/v1/accounts/{self.supplier.pk}/requests/{order.pk}/review/', {})
        order.refresh_from_db()
        path = f'/api/v1/accounts/{self.supplier.pk}/deals/{order.pk}/actions/'
        def quote(_):
            return self.call(self.seller, path, {'operation_id': str(uuid.uuid4()), 'expected_version': order.version,
                'action': 'quote', 'lines': [{'order_line_id': str(order.lines.get().pk), 'quantity': 2, 'unit_price': '1.25'}]})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(quote, range(2)))
        self.assertEqual(sorted(value[0] for value in results), [200, 409])
        self.assertEqual(DealQuotation.objects.count(), 1)


    def test_review_racing_with_cart_addition_never_modifies_locked_lines(self):
        send_path = f'/api/v1/accounts/{self.client_account.pk}/requests/'
        def send():
            return self.call(self.buyer, send_path, {'submission_id': str(uuid.uuid4()),
                             'lines': [{'supplier_item_id': str(self.item.pk), 'quantity': 2}]})
        _, result = send()
        order = SupplierRequest.objects.get(pk=result['requests'][0]['id'])
        review_path = f'/api/v1/accounts/{self.supplier.pk}/requests/{order.pk}/review/'
        with ThreadPoolExecutor(max_workers=2) as pool:
            sent = pool.submit(send)
            reviewed = pool.submit(self.call, self.seller, review_path, {})
            results = [sent.result(), reviewed.result()]
        self.assertEqual([value[0] for value in results], [200, 200])
        order.refresh_from_db()
        self.assertEqual(order.status, 'reviewed')
        quantities = list(SupplierRequestLine.objects.values_list('quantity', flat=True))
        self.assertEqual(sum(quantities), 4)
        self.assertIn(order.lines.get().quantity, [2, 4])
        self.assertLessEqual(SupplierRequest.objects.filter(status='pending').count(), 1)
