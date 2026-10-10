import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.db import transaction
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from .inbox_models import ConversationRead, InboxCursor
from .request_models import DealEvent, DealMessage
from . import test_deals as deal_tests

RESEND = {'RESEND_API_KEY': 're_test', 'EMAIL_FROM_ADDRESS': 'avisos@example.invalid', 'EMAIL_FROM_NAME': 'MotionPartes',
          'SITE_URL': 'https://motionpartes.example.invalid/', 'VAPID_PRIVATE_KEY': ''}


@override_settings(**RESEND)
@patch('mall.push.dispatch', lambda job: job())
@patch('urllib.request.urlopen')
class EmailNotificationTests(APITestCase):
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

    def say(self, user, account, order, body):
        self.client.force_authenticate(user)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f'/api/v1/accounts/{account.pk}/deals/{order.pk}/messages/', {'message_id': str(uuid.uuid4()), 'body': body},
                                        format='json')
        self.assertEqual(response.status_code, 200)

    def sent(self, urlopen):
        emails = [json.loads(call.args[0].data) for call in urlopen.call_args_list]
        for call in urlopen.call_args_list:
            self.assertEqual((call.args[0].full_url, call.args[0].get_header('Authorization')), ('https://api.resend.com/emails', 'Bearer re_test'))
        urlopen.reset_mock()
        return emails

    def test_an_order_and_its_review_email_the_other_party_with_a_link_to_the_conversation(self, urlopen):
        order = self.order()
        [email] = self.sent(urlopen)
        self.assertEqual((email['to'], email['from'], email['subject']),
                         ([self.seller_a.email], 'MotionPartes <avisos@example.invalid>', f'CLIENTE ORIGINAL te envió una orden · {order.reference}'))
        self.assertIn(f'https://motionpartes.example.invalid/?conversacion={order.pk}', email['text'])
        self.assertIn(f'href="https://motionpartes.example.invalid/?conversacion={order.pk}"', email['html'])
        self.assertIn('Cuenta: PROVEEDOR A', email['text'])
        self.client.force_authenticate(self.seller_a)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(f'{self.url(self.supplier_a)}{order.pk}/review/', {}, format='json')
        [email] = self.sent(urlopen)
        self.assertEqual((email['to'], email['subject']), ([self.buyer.email], f'PROVEEDOR A empezó a revisar tu orden · {order.reference}'))

    def test_a_message_emails_people_away_from_the_app_once_per_conversation_until_the_cooldown(self, urlopen):
        order = self.order()
        self.sent(urlopen)
        self.say(self.buyer, self.client_account, order, '¿Pueden entregar el lunes? <b>urgente</b>')
        [email] = self.sent(urlopen)
        self.assertEqual((email['to'], email['subject']), ([self.seller_a.email], f'Nuevo mensaje de CLIENTE ORIGINAL · {order.reference}'))
        self.assertIn('¿Pueden entregar el lunes? &lt;b&gt;urgente&lt;/b&gt;', email['html'])  # the message is quoted, escaped
        self.say(self.buyer, self.client_account, order, 'otro mensaje')
        self.assertEqual(self.sent(urlopen), [])  # within the cooldown
        ConversationRead.objects.filter(user=self.seller_a, order=order).update(emailed_at=timezone.now() - timedelta(minutes=16))
        self.client.force_authenticate(self.seller_a)
        self.client.get('/api/v1/inbox/')  # using the app right now: the toast and the badges tell them
        self.say(self.buyer, self.client_account, order, 'y otro')
        self.assertEqual(self.sent(urlopen), [])
        InboxCursor.objects.filter(user=self.seller_a).update(active_at=timezone.now() - timedelta(minutes=5))
        self.say(self.buyer, self.client_account, order, 'el último')
        self.assertEqual([email['to'] for email in self.sent(urlopen)], [[self.seller_a.email]])

    def test_each_person_turns_either_kind_of_email_off(self, urlopen):
        order = self.order()
        self.sent(urlopen)
        self.client.force_authenticate(self.seller_a)
        self.assertEqual(self.client.get('/api/v1/inbox/email/').data,
                         {'enabled': True, 'address': self.seller_a.email, 'messages': True, 'updates': True})
        self.assertEqual(self.client.post('/api/v1/inbox/email/', {'messages': False}, format='json').data['messages'], False)
        InboxCursor.objects.filter(user=self.seller_a).update(active_at=None)
        self.say(self.buyer, self.client_account, order, 'hola')
        self.assertEqual(self.sent(urlopen), [])
        self.client.force_authenticate(self.buyer)
        self.client.post('/api/v1/inbox/email/', {'updates': False}, format='json')
        self.client.force_authenticate(self.seller_a)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(f'{self.url(self.supplier_a)}{order.pk}/review/', {}, format='json')
        self.assertEqual(self.sent(urlopen), [])

    def test_an_adjustment_sends_one_email_quoting_its_reason(self, urlopen):
        order = self.order()
        self.sent(urlopen)
        with self.captureOnCommitCallbacks(execute=True), transaction.atomic():  # as the request_adjustment action writes them
            DealMessage.objects.create(order=order, account=self.client_account, actor=self.buyer, message_id=uuid.uuid4(), body='MENOS UNIDADES')
            DealEvent.objects.create(order=order, account=self.client_account, actor=self.buyer, kind='request_adjustment', description='MENOS UNIDADES')
        [email] = self.sent(urlopen)
        self.assertEqual(email['subject'], f'CLIENTE ORIGINAL solicitó un ajuste · {order.reference}')
        self.assertIn('MENOS UNIDADES', email['text'])

    def test_nothing_is_sent_without_resend(self, urlopen):
        with override_settings(RESEND_API_KEY=''):
            self.order()
            self.client.force_authenticate(self.seller_a)
            self.assertEqual(self.client.get('/api/v1/inbox/email/').data['enabled'], False)
        urlopen.assert_not_called()
