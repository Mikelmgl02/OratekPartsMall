import uuid
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase

from .models import DealThread, Role, SupplierRequest
from .test_deal_attachments import ChatStorage
from . import test_deals as deal_tests

PNG = b'\x89PNG\r\n\x1a\n' + bytes(range(32))


@patch('mall.push.dispatch', lambda job: job())
class ThreadTests(ChatStorage, APITestCase):
    """Preguntar al proveedor: a client and a supplier talk before any order, in the conversation their orders will join."""
    setUp_deals = deal_tests.DealWorkflowTests.setUp
    account = deal_tests.DealWorkflowTests.account
    item = deal_tests.DealWorkflowTests.item
    url = deal_tests.DealWorkflowTests.url
    payload = deal_tests.DealWorkflowTests.payload
    submit = deal_tests.DealWorkflowTests.submit

    def setUp(self):
        deal_tests.DealWorkflowTests.setUp(self)
        self.use_memory_storage()

    def start(self, supplier, user=None, account=None):
        self.client.force_authenticate(user or self.buyer)
        return self.client.post(f'/api/v1/accounts/{(account or self.client_account).pk}/threads/', {'supplier_id': str(supplier.pk)}, format='json')

    def messages(self, account, thread_id, **extra):
        return f'/api/v1/accounts/{account.pk}/threads/{thread_id}/messages/'

    def write(self, user, account, thread_id, body, **extra):
        self.client.force_authenticate(user)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(self.messages(account, thread_id), {'message_id': str(uuid.uuid4()), 'body': body, **extra}, format='json')

    def test_a_client_asks_a_supplier_before_ordering_and_the_order_joins_that_conversation(self):
        started = self.start(self.supplier_a)
        self.assertEqual(started.status_code, 200)
        thread_id = started.data['thread_id']
        self.assertEqual((started.data['counterpart'], self.start(self.supplier_a).data['thread_id']),
                         ({'id': str(self.supplier_a.pk), 'name': 'PROVEEDOR A'}, thread_id))  # one per client and supplier
        self.client.force_authenticate(self.seller_a)
        self.client.get('/api/v1/inbox/')
        asked = self.write(self.buyer, self.client_account, thread_id, '¿Tienen el tambor trasero del Accent?')
        self.assertEqual((asked.status_code, asked.data['order_id']), (200, None))  # before any order: general
        self.client.force_authenticate(self.seller_a)
        [row] = self.client.get('/api/v1/inbox/conversations/').data['results']
        self.assertEqual((row['id'], row['order_id'], row['reference'], row['orders'], row['unread']), (thread_id, None, '', [], 1))
        answered = self.write(self.seller_a, self.supplier_a, thread_id, 'Sí, tenemos dos.')
        self.assertEqual(answered.status_code, 200)
        self.client.force_authenticate(self.buyer)
        chat = self.client.get(self.messages(self.client_account, thread_id)).data
        self.assertEqual(([message['body'] for message in chat['results']], chat['thread_id']), (['¿Tienen el tambor trasero del Accent?', 'Sí, tenemos dos.'], thread_id))

        order = SupplierRequest.objects.get(pk=self.submit(self.payload([(self.item_a, 2)])).data['requests'][0]['id'])
        self.assertEqual(str(order.thread_id), thread_id)  # the order joins the conversation
        on_order = self.client.get(f'/api/v1/accounts/{self.client_account.pk}/deals/{order.pk}/messages/').data['results']
        self.assertEqual(len(on_order), 2)  # the questions before it, in the same line
        tagged = self.write(self.buyer, self.client_account, thread_id, 'Ya les envié la orden.', order_id=str(order.pk))
        self.assertEqual(tagged.data['order_reference'], order.reference)
        solo = self.client.get(self.messages(self.client_account, thread_id), {'order': str(order.pk)}).data['results']
        self.assertEqual([message['body'] for message in solo], ['Ya les envié la orden.'])

    def test_only_a_client_account_asks_an_active_supplier_never_itself_and_strangers_read_nothing(self):
        self.assertEqual(self.start(self.supplier_b).status_code, 200)
        self.client_account.roles.add(Role.objects.get(code='supplier_retail'))
        refused = self.start(self.client_account)
        self.assertEqual(refused.status_code, 400)
        self.assertIn('tu propia cuenta', str(refused.data))
        self.supplier_b.active = False
        self.supplier_b.save()
        self.assertEqual(self.start(self.supplier_b).status_code, 400)
        self.assertEqual(self.start(self.supplier_a, user=self.seller_a, account=self.supplier_a).status_code, 403)  # a supplier account does not ask
        thread_id = self.start(self.supplier_a).data['thread_id']
        self.client.force_authenticate(self.seller_b)
        self.assertEqual(self.client.get(self.messages(self.supplier_b, thread_id)).status_code, 404)
        self.supplier_b.active = True
        self.supplier_b.save()
        self.client.force_authenticate(self.buyer)
        other = DealThread.between(self.client_account.pk, self.supplier_b.pk)
        order = SupplierRequest.objects.get(pk=self.submit(self.payload([(self.item_b, 1)])).data['requests'][0]['id'])
        self.assertEqual(order.thread_id, other.pk)
        self.assertEqual(self.write(self.buyer, self.client_account, thread_id, 'x', order_id=str(order.pk)).status_code, 404)  # another conversation's order

    def test_files_go_and_open_through_the_conversation(self):
        thread_id = self.start(self.supplier_a).data['thread_id']
        self.client.force_authenticate(self.buyer)
        with self.captureOnCommitCallbacks(execute=True):
            sent = self.client.post(self.messages(self.client_account, thread_id), {'message_id': str(uuid.uuid4()), 'body': '',
                                                                                    'files': [SimpleUploadedFile('tambor.png', PNG, 'image/png')]}, format='multipart')
        [photo] = sent.data['attachments']
        self.client.force_authenticate(self.seller_a)
        opened = self.client.get(f'/api/v1/accounts/{self.supplier_a.pk}/threads/{thread_id}/attachments/{photo["id"]}/')
        self.assertEqual((opened.status_code, b''.join(opened.streaming_content)), (200, PNG))
        self.client.force_authenticate(self.seller_b)
        self.assertEqual(self.client.get(f'/api/v1/accounts/{self.supplier_b.pk}/threads/{thread_id}/attachments/{photo["id"]}/').status_code, 404)
