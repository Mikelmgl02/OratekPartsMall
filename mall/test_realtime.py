import uuid
from unittest.mock import patch

from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from django.test import override_settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APITransactionTestCase

from config.asgi import application
from .models import Role
from . import test_deals as deal_tests

MEMORY = {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}}


@override_settings(CHANNEL_LAYERS=MEMORY, RESEND_API_KEY='', VAPID_PRIVATE_KEY='')
@patch('mall.push.dispatch', lambda job: job())
class InboxSocketTests(APITransactionTestCase):
    # Committed transactions (the socket reads the database from another thread). They start from an emptied database, so the roles
    # the migrations seed are created again, as in test_deals.ConcurrentDealTests.
    def setUp(self):
        for code, capability in [('client_business', 'client'), ('supplier_retail', 'supplier'), ('supplier_wholesale', 'supplier')]:
            Role.objects.get_or_create(code=code, defaults={'name': code.upper(), 'capability': capability})
        deal_tests.DealWorkflowTests.setUp(self)

    account = deal_tests.DealWorkflowTests.account
    item = deal_tests.DealWorkflowTests.item
    url = deal_tests.DealWorkflowTests.url
    payload = deal_tests.DealWorkflowTests.payload
    submit = deal_tests.DealWorkflowTests.submit

    def order(self):
        self.client.force_authenticate(self.buyer)
        return deal_tests.DealWorkflowTests.order(self)

    def say(self, user, account, order, body):
        self.client.force_authenticate(user)
        response = self.client.post(f'/api/v1/accounts/{account.pk}/deals/{order.pk}/messages/', {'message_id': str(uuid.uuid4()), 'body': body},
                                    format='json')
        self.assertEqual(response.status_code, 200)

    async def socket(self, user=None, origin=b'http://localhost'):
        headers = [(b'origin', origin)]
        if user:
            token = await sync_to_async(lambda: Token.objects.get_or_create(user=user)[0].key)()
            headers.append((b'cookie', f'partsmall_session={token}'.encode()))
        communicator = WebsocketCommunicator(application, '/ws/inbox/', headers=headers)
        connected, _ = await communicator.connect()
        return communicator, connected

    async def test_only_a_signed_in_page_of_the_site_connects(self):
        _, connected = await self.socket()
        self.assertFalse(connected)
        _, connected = await self.socket(self.buyer, origin=b'https://elsewhere.example')
        self.assertFalse(connected)
        communicator, connected = await self.socket(self.buyer)
        self.assertTrue(connected)
        await communicator.disconnect()

    async def test_a_message_nudges_both_parties_and_nobody_else(self):
        order = await sync_to_async(self.order)()
        buyer, _ = await self.socket(self.buyer)
        seller, _ = await self.socket(self.seller_a)
        stranger, _ = await self.socket(self.seller_b)
        await sync_to_async(self.say)(self.buyer, self.client_account, order, 'hola')
        nudge = {'kind': 'message', 'order_id': str(order.pk)}
        self.assertEqual(await seller.receive_json_from(timeout=2), nudge)
        self.assertEqual(await buyer.receive_json_from(timeout=2), nudge)  # the author's other tabs too
        self.assertTrue(await stranger.receive_nothing(timeout=0.3))
        for communicator in (buyer, seller, stranger):
            await communicator.disconnect()

    async def test_a_read_mark_nudges_only_the_reader(self):
        order = await sync_to_async(self.order)()
        await sync_to_async(self.say)(self.buyer, self.client_account, order, 'hola')
        seller, _ = await self.socket(self.seller_a)
        buyer, _ = await self.socket(self.buyer)

        def read():
            self.client.force_authenticate(self.seller_a)
            self.client.get('/api/v1/inbox/')
            return self.client.post(f'/api/v1/inbox/conversations/{order.pk}/read/', {'last_message_id': 10 ** 9}, format='json').status_code
        self.assertEqual(await sync_to_async(read)(), 200)
        self.assertEqual(await seller.receive_json_from(timeout=2), {'kind': 'read', 'order_id': str(order.pk)})
        self.assertTrue(await buyer.receive_nothing(timeout=0.3))
        await seller.disconnect()
        await buyer.disconnect()
