import hashlib
import hmac
import io
import json
import urllib.error
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from .inbox_models import ConversationRead, InboxCursor, WhatsAppMessage
from . import test_deals as deal_tests

CLOUD = {'WHATSAPP_ACCESS_TOKEN': 'EAAG-prueba', 'WHATSAPP_PHONE_NUMBER_ID': '1055', 'WHATSAPP_API_VERSION': 'v23.0', 'WHATSAPP_APP_SECRET': 'secreto',
         'WHATSAPP_VERIFY_TOKEN': 'verifica', 'RESEND_API_KEY': '', 'VAPID_PRIVATE_KEY': '', 'CHANNEL_LAYERS': {},
         'SITE_URL': 'https://motionpartes.example.invalid'}


class Reply:
    def __init__(self, data):
        self.data = json.dumps(data).encode()

    def read(self, *args):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@override_settings(**CLOUD)
@patch('mall.push.dispatch', lambda job: job())
@patch('urllib.request.urlopen')
class WhatsAppTests(APITestCase):
    setUp = deal_tests.DealWorkflowTests.setUp
    account = deal_tests.DealWorkflowTests.account
    item = deal_tests.DealWorkflowTests.item
    url = deal_tests.DealWorkflowTests.url
    payload = deal_tests.DealWorkflowTests.payload
    submit = deal_tests.DealWorkflowTests.submit

    def order(self):
        self.client.force_authenticate(self.buyer)
        with self.captureOnCommitCallbacks(execute=True):
            return deal_tests.DealWorkflowTests.order(self)

    def opt_in(self, user, number):
        self.client.force_authenticate(user)
        response = self.client.post('/api/v1/inbox/whatsapp/', {'number': number, 'opt_in': True}, format='json')
        self.assertEqual(response.status_code, 200)
        InboxCursor.objects.filter(user=user).update(active_at=None)
        return response.data

    def say(self, user, account, order, body):
        self.client.force_authenticate(user)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(f'/api/v1/accounts/{account.pk}/deals/{order.pk}/messages/', {'message_id': str(uuid.uuid4()), 'body': body}, format='json')

    def sent(self, urlopen):
        calls = [(call.args[0].full_url, json.loads(call.args[0].data)) for call in urlopen.call_args_list]
        urlopen.reset_mock()
        for url, _ in calls:
            self.assertEqual(url, 'https://graph.facebook.com/v23.0/1055/messages')
        return [payload for _, payload in calls]

    def webhook(self, value, secret='secreto'):
        body = json.dumps({'entry': [{'changes': [{'value': value}]}]}).encode()
        signature = 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        self.client.force_authenticate(None)
        return self.client.generic('POST', '/api/v1/whatsapp/webhook/', body, content_type='application/json', HTTP_X_HUB_SIGNATURE_256=signature)

    def test_people_opt_in_with_their_number_and_choose_their_notices(self, urlopen):
        self.client.force_authenticate(self.buyer)
        self.assertEqual(self.client.get('/api/v1/inbox/whatsapp/').data,
                         {'enabled': True, 'number': '', 'opted_in': False, 'messages': True, 'updates': True})
        self.assertEqual(self.client.post('/api/v1/inbox/whatsapp/', {'number': '6123-4567'}, format='json').status_code, 400)  # no consent
        self.assertEqual(self.client.post('/api/v1/inbox/whatsapp/', {'number': '12', 'opt_in': True}, format='json').status_code, 400)
        data = self.client.post('/api/v1/inbox/whatsapp/', {'number': '6123 4567', 'opt_in': True}, format='json').data
        self.assertEqual((data['number'], data['opted_in']), ('+50761234567', True))  # a local number gets Panamá's code
        self.assertEqual(self.client.post('/api/v1/inbox/whatsapp/', {'updates': False}, format='json').data['updates'], False)
        self.assertEqual(self.client.post('/api/v1/inbox/whatsapp/', {'opt_in': False}, format='json').data['opted_in'], False)
        self.assertEqual(self.client.post('/api/v1/inbox/whatsapp/', {'number': '+1 (305) 555-0100', 'opt_in': True}, format='json').data['number'], '+13055550100')
        with override_settings(WHATSAPP_ACCESS_TOKEN=''):
            self.assertEqual(self.client.get('/api/v1/inbox/whatsapp/').data['enabled'], False)
            self.assertEqual(self.client.post('/api/v1/inbox/whatsapp/', {'opt_in': True}, format='json').status_code, 409)
        urlopen.assert_not_called()

    def test_a_deal_event_reaches_the_other_party_that_opted_in_with_a_link_to_the_deal(self, urlopen):
        urlopen.side_effect = lambda request, timeout=None: Reply({'messages': [{'id': 'wamid.UNO'}]})
        order = self.order()
        self.opt_in(self.buyer, '+507 6111 1111')
        self.opt_in(self.seller_a, '+507 6222 2222')
        self.sent(urlopen)
        self.client.force_authenticate(self.seller_a)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(f'{self.url(self.supplier_a)}{order.pk}/review/', {}, format='json')
        [payload] = self.sent(urlopen)
        self.assertEqual(payload['to'], '50761111111')
        self.assertEqual(payload['template']['name'], 'motionpartes_novedad_orden')
        body, button = payload['template']['components']
        self.assertEqual([item['text'] for item in body['parameters']], [order.reference, 'PROVEEDOR A empezó a revisar tu orden'])
        self.assertEqual((button['sub_type'], button['parameters'][0]['text']), ('url', str(order.pk)))
        row = WhatsAppMessage.objects.get(user=self.buyer)
        self.assertEqual((row.status, row.wamid, row.kind, row.order_id), ('accepted', 'wamid.UNO', 'review_started', order.pk))

    def test_a_message_reaches_people_away_from_the_app_once_per_conversation_until_the_cooldown(self, urlopen):
        urlopen.side_effect = lambda request, timeout=None: Reply({'messages': [{'id': 'wamid.MSG'}]})
        order = self.order()
        self.opt_in(self.seller_a, '+50762222222')
        self.say(self.buyer, self.client_account, order, '¿Pueden entregar\nel lunes?')
        [payload] = self.sent(urlopen)
        self.assertEqual(payload['template']['name'], 'motionpartes_mensaje_nuevo')
        self.assertEqual([item['text'] for item in payload['template']['components'][0]['parameters']],
                         ['CLIENTE ORIGINAL', order.reference, '¿Pueden entregar el lunes?'])  # no newlines in a template parameter
        self.say(self.buyer, self.client_account, order, 'otro')
        self.assertEqual(self.sent(urlopen), [])  # within the cooldown
        ConversationRead.objects.filter(user=self.seller_a, order=order).update(whatsapp_at=timezone.now() - timedelta(minutes=31))
        InboxCursor.objects.filter(user=self.seller_a).update(active_at=timezone.now())  # using the app right now
        self.say(self.buyer, self.client_account, order, 'y otro')
        self.assertEqual(self.sent(urlopen), [])

    def test_a_refused_notice_is_logged_with_meta_s_reason(self, urlopen):
        urlopen.side_effect = urllib.error.HTTPError('https://graph.facebook.com', 400, 'Bad Request', {},
                                                     io.BytesIO(b'{"error":{"message":"Template name does not exist"}}'))
        order = self.order()
        self.opt_in(self.seller_a, '+50762222222')
        self.say(self.buyer, self.client_account, order, 'hola')
        row = WhatsAppMessage.objects.get()
        self.assertEqual(row.status, 'failed')
        self.assertIn('Template name does not exist', row.error)

    def test_the_webhook_is_verified_signed_and_keeps_the_delivery_status(self, urlopen):
        self.assertEqual(self.client.get('/api/v1/whatsapp/webhook/', {'hub.mode': 'subscribe', 'hub.verify_token': 'verifica', 'hub.challenge': '42'}).content, b'42')
        self.assertEqual(self.client.get('/api/v1/whatsapp/webhook/', {'hub.mode': 'subscribe', 'hub.verify_token': 'otro', 'hub.challenge': '42'}).status_code, 403)
        order = self.order()
        row = WhatsAppMessage.objects.create(user=self.buyer, order=order, number='+50761111111', template='t', kind='quote', wamid='wamid.X', status='accepted')
        self.assertEqual(self.webhook({'statuses': [{'id': 'wamid.X', 'status': 'delivered'}]}, secret='falso').status_code, 403)
        self.assertEqual(self.webhook({'statuses': [{'id': 'wamid.X', 'status': 'delivered'}]}).status_code, 200)
        self.webhook({'statuses': [{'id': 'wamid.X', 'status': 'sent'}]})  # late: never moves it back
        row.refresh_from_db()
        self.assertEqual(row.status, 'delivered')
        self.webhook({'statuses': [{'id': 'wamid.X', 'status': 'failed', 'errors': [{'code': 131026, 'title': 'Message undeliverable'}]}]})
        row.refresh_from_db()
        self.assertEqual((row.status, row.error), ('failed', '131026 Message undeliverable'))

    def test_replies_get_one_answer_and_baja_opts_out(self, urlopen):
        urlopen.side_effect = lambda request, timeout=None: Reply({'messages': [{'id': 'wamid.R'}]})
        self.opt_in(self.buyer, '+50761111111')
        self.webhook({'messages': [{'from': '50761111111', 'type': 'text', 'text': {'body': 'ok gracias'}}]})
        [answer] = self.sent(urlopen)
        self.assertEqual((answer['to'], answer['type']), ('50761111111', 'text'))
        self.assertIn('https://motionpartes.example.invalid/', answer['text']['body'])
        self.webhook({'messages': [{'from': '50761111111', 'type': 'text', 'text': {'body': '?'}}]})
        self.assertEqual(self.sent(urlopen), [])  # one answer every 12 hours at most
        self.webhook({'messages': [{'from': '50769999999', 'type': 'text', 'text': {'body': 'hola'}}]})
        self.assertEqual(self.sent(urlopen), [])  # not someone of the platform
        self.webhook({'messages': [{'from': '50761111111', 'type': 'text', 'text': {'body': 'Baja.'}}]})
        [goodbye] = self.sent(urlopen)
        self.assertIn('ya no recibirás avisos', goodbye['text']['body'])
        self.assertIsNone(InboxCursor.objects.get(user=self.buyer).whatsapp_opt_in_at)
