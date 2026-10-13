import json
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from django.test import override_settings
from rest_framework.test import APITestCase

from .inbox_models import PushSubscription
from . import test_deals as deal_tests

VAPID = {'VAPID_PUBLIC_KEY': 'BPublic', 'VAPID_PRIVATE_KEY': 'private', 'VAPID_SUBJECT': 'mailto:equipo@example.invalid'}


def subscription(name):
    return {'endpoint': f'https://push.example.invalid/{name}', 'keys': {'p256dh': f'key-{name}', 'auth': f'auth-{name}'}}


@override_settings(**VAPID)
@patch('mall.push.dispatch', lambda job: job())
class PushTests(APITestCase):
    setUp = deal_tests.DealWorkflowTests.setUp
    account = deal_tests.DealWorkflowTests.account
    item = deal_tests.DealWorkflowTests.item
    url = deal_tests.DealWorkflowTests.url
    payload = deal_tests.DealWorkflowTests.payload
    submit = deal_tests.DealWorkflowTests.submit

    def subscribe(self, user, name):
        self.client.force_authenticate(user)
        return self.client.post('/api/v1/inbox/push/', subscription(name), format='json')

    def order(self):
        self.client.force_authenticate(self.buyer)
        return deal_tests.DealWorkflowTests.order(self)

    def say(self, user, account, order, body):
        self.client.force_authenticate(user)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f'/api/v1/accounts/{account.pk}/deals/{order.pk}/messages/', {'message_id': str(uuid.uuid4()), 'body': body},
                                        format='json')
        self.assertEqual(response.status_code, 200)

    def pushed(self, sent):
        return [(call.args[0]['endpoint'].rsplit('/', 1)[1], json.loads(call.args[1])) for call in sent.call_args_list]

    def test_the_key_and_subscriptions_follow_the_configuration_and_the_browser(self):
        self.client.force_authenticate(self.buyer)
        self.assertEqual(self.client.get('/api/v1/inbox/push/').data, {'enabled': True, 'public_key': 'BPublic'})
        self.assertEqual(self.subscribe(self.buyer, 'navegador').status_code, 200)
        self.assertEqual(PushSubscription.objects.get().user, self.buyer)
        self.subscribe(self.seller_a, 'navegador')  # someone else signs in on that browser and turns notifications on
        self.assertEqual(list(PushSubscription.objects.values_list('user', 'auth')), [(self.seller_a.pk, 'auth-navegador')])
        self.client.force_authenticate(self.buyer)
        self.client.post('/api/v1/inbox/push/remove/', {'endpoint': subscription('navegador')['endpoint']}, format='json')
        self.assertEqual(PushSubscription.objects.count(), 1)  # not theirs to remove
        self.client.force_authenticate(self.seller_a)
        self.client.post('/api/v1/inbox/push/remove/', {'endpoint': subscription('navegador')['endpoint']}, format='json')
        self.assertEqual(PushSubscription.objects.count(), 0)
        for bad in [{**subscription('x'), 'endpoint': 'http://push.example.invalid/x'}, {'endpoint': 'https://push.example.invalid/x', 'keys': {'auth': 'a'}}]:
            self.assertEqual(self.client.post('/api/v1/inbox/push/', bad, format='json').status_code, 400)
        with override_settings(VAPID_PRIVATE_KEY=''):
            self.assertEqual(self.client.get('/api/v1/inbox/push/').data, {'enabled': False, 'public_key': ''})
            self.assertEqual(self.subscribe(self.buyer, 'apagado').status_code, 409)

    @patch('pywebpush.webpush')
    def test_a_message_pushes_to_the_other_party_only(self, sent):
        order = self.order()
        for user, name in [(self.buyer, 'cliente'), (self.seller_a, 'proveedor'), (self.seller_b, 'ajeno')]:
            self.subscribe(user, name)
        self.say(self.buyer, self.client_account, order, '¿Pueden entregar el lunes?')
        [(endpoint, data)] = self.pushed(sent)
        self.assertEqual(endpoint, 'proveedor')
        self.assertEqual(data, {'title': f'CLIENTE ORIGINAL · {order.reference}', 'body': '¿Pueden entregar el lunes?', 'tag': f'deal-{order.pk}',
                                'url': f'/?conversacion={order.pk}', 'order_id': str(order.pk)})
        self.assertEqual(sent.call_args.kwargs['vapid_claims'], {'sub': 'mailto:equipo@example.invalid'})
        sent.reset_mock()
        self.say(self.seller_a, self.supplier_a, order, 'Sí')
        self.assertEqual([endpoint for endpoint, _ in self.pushed(sent)], ['cliente'])

    @patch('pywebpush.webpush')
    def test_a_deal_event_pushes_its_notification_and_a_gone_browser_is_dropped(self, sent):
        from pywebpush import WebPushException
        order = self.order()
        self.subscribe(self.buyer, 'cliente')
        self.subscribe(self.buyer, 'cliente-viejo')
        sent.side_effect = lambda info, *args, **kwargs: (_ for _ in ()).throw(WebPushException('gone', response=SimpleNamespace(status_code=410))) \
            if info['endpoint'].endswith('viejo') else None
        self.client.force_authenticate(self.seller_a)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.post(f'{self.url(self.supplier_a)}{order.pk}/review/', {}, format='json').status_code, 200)
        titles = {endpoint: data['title'] for endpoint, data in self.pushed(sent)}
        self.assertEqual(titles, {'cliente': 'PROVEEDOR A empezó a revisar tu orden', 'cliente-viejo': 'PROVEEDOR A empezó a revisar tu orden'})
        self.assertEqual(list(PushSubscription.objects.values_list('endpoint', flat=True)), [subscription('cliente')['endpoint']])

    @patch('pywebpush.webpush')
    def test_the_orders_progress_pushes_to_the_party_that_did_not_act(self, sent):
        from django.db import transaction
        from .models import DealEvent
        order = self.order()
        self.subscribe(self.buyer, 'cliente')
        self.subscribe(self.seller_a, 'proveedor')
        for kind, account, actor, endpoint, title in [
                ('process', self.supplier_a, self.seller_a, 'cliente', 'PROVEEDOR A está procesando tu orden'),
                ('ship', self.supplier_a, self.seller_a, 'cliente', 'PROVEEDOR A envió tu orden: va en camino'),
                ('deliver', self.supplier_a, self.seller_a, 'cliente', 'Confirma que recibiste tu orden: PROVEEDOR A la marcó como entregada'),
                ('receive', self.client_account, self.buyer, 'proveedor', 'CLIENTE ORIGINAL confirmó que recibió la orden')]:
            with self.captureOnCommitCallbacks(execute=True), transaction.atomic():
                DealEvent.objects.create(order=order, account=account, actor=actor, kind=kind, description=kind.upper())
            self.assertEqual([(name, data['title'], data['body']) for name, data in self.pushed(sent)], [(endpoint, title, f'Orden {order.reference}')])
            sent.reset_mock()

    @patch('pywebpush.webpush')
    def test_nothing_is_pushed_without_keys(self, sent):
        order = self.order()
        self.subscribe(self.seller_a, 'proveedor')
        with override_settings(VAPID_PUBLIC_KEY=''):
            self.say(self.buyer, self.client_account, order, 'hola')
        sent.assert_not_called()
