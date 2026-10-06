"""Private pricing must never reach clients, competitors or Oratek staff.

Each slice that stores private pricing data seeds it in seed_private_markers(); the canary then calls every
client-reachable endpoint and fails if any marker leaks. The key-set pins fail on any new client-facing key.
"""
import json
import uuid

from django.contrib import admin
from rest_framework.test import APITestCase

from . import test_deals as deal_tests
from . import test_requests as request_tests
from .management import ManagedInventorySerializer
from .models import User
from .pricing_models import PricingAuditEvent, SupplierPricingSettings, record_pricing_event
from .request_models import DealCommand, SupplierRequest
from .serializers import SupplierItemSerializer

SUMMARY_KEYS = {'id', 'reference', 'status', 'version', 'created_at', 'updated_at', 'handshaked_at', 'reviewed_at', 'line_count',
                'unit_count', 'quotation_revision', 'quoted_unit_count', 'quotation_total', 'quotation_currency'}
SENT_SUMMARY_KEYS = SUMMARY_KEYS | {'supplier', 'submission_id'}
DEAL_KEYS = {'client', 'supplier', 'quotations', 'quotation', 'events'}
QUOTATION_KEYS = {'id', 'revision', 'currency', 'terms', 'total', 'created_at', 'supplier_confirmed_at', 'client_confirmed_at', 'lines'}
QUOTATION_LINE_KEYS = {'order_line_id', 'codigo', 'sku', 'description', 'quantity', 'unit_price', 'total'}
EVENT_KEYS = {'id', 'kind', 'description', 'account_name', 'actor_name', 'created_at', 'quotation_id'}
CLIENT_LINE_KEYS = {'id', 'part_id', 'sku', 'name', 'codigo', 'brand', 'description', 'quantity'}
SUPPLIER_LINE_KEYS = CLIENT_LINE_KEYS | {'supplier_item_id', 'supplier_invent_id', 'stock'}
MARKERS = ['777.77', '666.66', '555.55', 'SECRETO-REGLA', 'SECRETO-PERFIL', 'SECRETO-BORRADOR', 'SECRETO-IA', 'SECRETO-AUDITORIA']


class PricingPrivacyTests(APITestCase):
    setUp = request_tests.SupplierRequestWorkflowTests.setUp
    account = request_tests.SupplierRequestWorkflowTests.account
    item = request_tests.SupplierRequestWorkflowTests.item
    url = request_tests.SupplierRequestWorkflowTests.url
    payload = request_tests.SupplierRequestWorkflowTests.payload
    submit = request_tests.SupplierRequestWorkflowTests.submit
    order = deal_tests.DealWorkflowTests.order
    review = deal_tests.DealWorkflowTests.review
    action = deal_tests.DealWorkflowTests.action
    messages = deal_tests.DealWorkflowTests.messages

    def quote(self, order, price='13.00'):
        self.client.force_authenticate(self.seller_a)
        response = self.action(order, 'quote', lines=[{'order_line_id': str(line.pk), 'quantity': line.quantity, 'unit_price': price}
                                                      for line in order.lines.all()], terms='ENTREGA EN 48 HORAS')
        self.assertEqual(response.status_code, 200)
        return response

    def seed_private_markers(self, order):
        """Later slices add real rows here: prices and floors (S3), net rules and profiles (S5), drafts (S1), assistant output (S7)."""
        SupplierPricingSettings.objects.create(supplier=self.supplier_a, default_currency='PAB', config_min_permission='manager',
                                               accept_shortfall_policy='allow', assistant_enabled=True, updated_by=self.seller_a)
        record_pricing_event(self.supplier_a, self.seller_a, 'prices_edited', client=self.client_account, order=order, object_id='SECRETO-AUDITORIA',
                             payload={'list_price': '777.77', 'floor_price': '666.66', 'net_price': '555.55', 'rule': 'SECRETO-REGLA',
                                      'profile_note': 'SECRETO-PERFIL', 'draft_note': 'SECRETO-BORRADOR', 'assistant': 'SECRETO-IA'})
        stored = json.dumps(list(PricingAuditEvent.objects.values('object_id', 'payload')))
        self.assertTrue(all(marker in stored for marker in MARKERS), 'The canary must seed every marker it looks for.')

    def calls(self, user, account, order, *, writes=()):
        self.client.force_authenticate(user)
        account_url, part = f'/api/v1/accounts/{account.pk}', self.part.pk
        responses = {path: self.client.get(path) for path in [
            '/api/v1/accounts/', '/api/v1/catalog/?include_facets=1', f'/api/v1/catalog/?search={self.part.sku}&include_facets=1',
            f'/api/v1/catalog/{part}/technical/', f'/api/v1/catalog/{part}/suppliers/', '/api/v1/wishlist/', '/api/v1/wishlist/state/',
            f'{account_url}/catalog/{part}/request-state/', f'{account_url}/sent-requests/', f'{account_url}/sent-requests/{order.pk}/',
            f'{account_url}/requests/', f'{account_url}/requests/{order.pk}/', self.messages(order, account),
            f'/api/v1/accounts/{self.supplier_a.pk}/pricing/settings/']}
        responses[f'PUT /api/v1/wishlist/{part}/'] = self.client.put(f'/api/v1/wishlist/{part}/')
        responses['POST messages'] = self.client.post(self.messages(order, account), {'message_id': str(uuid.uuid4()), 'body': 'HOLA'}, format='json')
        for name, call in writes:
            responses[name] = call()
        return responses

    def assert_no_markers(self, label, responses):
        for path, response in responses.items():
            self.assertLess(response.status_code, 500, f'{label} {path}')
            body = response.content.decode()
            for marker in MARKERS:
                self.assertNotIn(marker, body, f'{label} {path} filtró {marker}')

    def test_private_pricing_markers_never_reach_clients_or_competitors(self):
        order = self.order()
        self.seed_private_markers(order)
        self.review(order)
        quote = self.quote(order).data['quotation']
        responses = self.calls(self.buyer, self.client_account, order, writes=[
            ('POST request_adjustment', lambda: self.action(order, 'request_adjustment', quotation_id=quote['id'], reason='MENOS UNIDADES'))])
        self.assertIn('"unit_price":"13.00"', responses[f'/api/v1/accounts/{self.client_account.pk}/sent-requests/{order.pk}/'].content.decode())
        self.client.force_authenticate(self.seller_a)
        self.assertEqual(self.action(order, 'return_quote', quotation_id=quote['id']).status_code, 200)
        self.client.force_authenticate(self.buyer)
        responses['POST accept'] = self.action(order, 'accept', quotation_id=quote['id'])
        responses['POST requests'] = self.submit(self.payload([(self.item_a, 1), (self.item_b, 1)]))
        self.assertEqual((responses['POST accept'].status_code, responses['POST requests'].status_code), (200, 200))
        self.assert_no_markers('cliente A', responses)
        buyer_b = User.objects.create_user('privacy-buyer-b', 'privacy-buyer-b@example.invalid', 'Request938!')
        client_b = self.account('CLIENTE B', 'client_walkin', buyer_b)
        self.assert_no_markers('cliente B', self.calls(buyer_b, client_b, order, writes=[
            ('POST accept', lambda: self.action(order, 'accept', account=client_b, quotation_id=quote['id'])),
            ('POST requests', lambda: self.submit(self.payload([(self.item_a, 2)]), account=client_b))]))
        self.assert_no_markers('proveedor B', self.calls(self.seller_b, self.supplier_b, order, writes=[
            ('POST quote', lambda: self.action(order, 'quote', account=self.supplier_b, lines=[]))]))
        self.assertEqual(DealCommand.objects.filter(account=self.client_account).count(), 2)
        for command in DealCommand.objects.all():
            for marker in MARKERS:
                self.assertNotIn(marker, json.dumps(command.result))

    def test_client_facing_payload_key_sets_are_pinned(self):
        receipt = self.submit(self.payload([(self.item_a, 3), (self.item_a2, 2)]))
        self.assertEqual(set(receipt.data), {'submission_id', 'created_at', 'requests'})
        self.assertEqual(set(receipt.data['requests'][0]), {'id', 'reference', 'supplier', 'line_count', 'unit_count', 'added_unit_count', 'appended'})
        order = SupplierRequest.objects.get(pk=receipt.data['requests'][0]['id'])
        self.review(order)
        quoted = self.quote(order)
        self.assertEqual(set(quoted.data), SUMMARY_KEYS | DEAL_KEYS | {'notes', 'lines'})
        self.assertEqual({key for line in quoted.data['lines'] for key in line}, SUPPLIER_LINE_KEYS)
        self.client.force_authenticate(self.seller_a)
        listed = self.client.get(self.url(self.supplier_a)).data['results'][0]
        self.assertEqual(set(listed), SUMMARY_KEYS | {'client', 'draft_state', 'availability_alert'})
        self.assertEqual((listed['draft_state'], listed['availability_alert']), (None, None))
        self.assertEqual(set(self.client.get(f'{self.url(self.supplier_a)}{order.pk}/').data), set(quoted.data))
        self.client.force_authenticate(self.buyer)
        base = f'/api/v1/accounts/{self.client_account.pk}'
        sent = self.client.get(f'{base}/sent-requests/').data['results'][0]
        self.assertEqual(set(sent), SENT_SUMMARY_KEYS)
        detail = self.client.get(f'{base}/sent-requests/{order.pk}/').data
        accepted = self.action(order, 'accept', quotation_id=detail['quotation']['id']).data
        for payload in [detail, accepted, DealCommand.objects.get(account=self.client_account).result]:
            self.assertEqual(set(payload), SENT_SUMMARY_KEYS | DEAL_KEYS | {'notes', 'lines'})
            self.assertEqual({key for line in payload['lines'] for key in line}, CLIENT_LINE_KEYS)
            self.assertEqual(set(payload['quotation']), QUOTATION_KEYS)
            self.assertEqual({key for quotation in payload['quotations'] for key in quotation}, QUOTATION_KEYS)
            self.assertEqual({key for line in payload['quotation']['lines'] for key in line}, QUOTATION_LINE_KEYS)
            self.assertEqual({key for event in payload['events'] for key in event}, EVENT_KEYS)
            self.assertEqual((set(payload['client']), set(payload['supplier'])), ({'id', 'name'}, {'id', 'name'}))
        self.assertEqual(set(DealCommand.objects.get(account=self.supplier_a).result), SUMMARY_KEYS | DEAL_KEYS | {'notes', 'lines'})
        state = self.client.get(f'{base}/catalog/{self.part.pk}/request-state/').data
        self.assertEqual((set(state), set(state['items'][0])), ({'part_id', 'totals', 'items'}, {
            'supplier_item_id', 'supplier_id', 'codigo', 'brand', 'sent_quantity', 'pending_quantity', 'reviewed_quantity',
            'quoted_quantity', 'adjustment_quantity', 'handshaked_quantity'}))
        message = self.client.post(self.messages(order), {'message_id': str(uuid.uuid4()), 'body': 'GRACIAS'}, format='json').data
        self.assertEqual(set(message), {'id', 'message_id', 'body', 'account_id', 'account_name', 'actor_name', 'created_at'})
        self.assertEqual(set(self.client.get('/api/v1/accounts/').data['results'][0]), {'id', 'name', 'roles', 'capabilities', 'permission'})
        part = self.client.get('/api/v1/catalog/').data['results'][0]
        self.assertEqual(set(part), {'id', 'sku', 'is_OEM', 'name', 'description', 'category', 'subcategory', 'part_type', 'codes', 'availability', 'images'})
        self.assertEqual(set(part['availability']), {'status', 'supplier_count', 'updated_at'})
        offers = self.client.get(f'/api/v1/catalog/{self.part.pk}/suppliers/').data
        self.assertEqual({key for offer in offers for key in offer}, {'supplier_id', 'supplier_name', 'in_stock', 'items'})
        self.assertEqual({key for offer in offers for item in offer['items'] for key in item}, {'id', 'codigo', 'brand', 'description'})

    def test_pricing_models_stay_out_of_admin_and_management_serializers(self):
        for model in [SupplierPricingSettings, PricingAuditEvent]:
            self.assertNotIn(model, admin.site._registry)
        self.assertEqual(SupplierItemSerializer.Meta.fields, ['id', 'supplier_invent_id', 'part', 'codigo', 'brand', 'description', 'references',
                                                              'matching_status', 'source', 'reported_quantity', 'reserved_quantity',
                                                              'available_quantity', 'updated_at'])
        self.assertEqual(ManagedInventorySerializer.Meta.fields, SupplierItemSerializer.Meta.fields + ['supplier', 'supplier_name', 'part_name', 'part_sku'])
