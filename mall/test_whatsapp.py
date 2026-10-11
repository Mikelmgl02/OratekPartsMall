import hashlib
import hmac
import io
import json
import urllib.error
import time
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from .inbox_models import AccountWhatsApp, ConversationRead, InboxCursor, WhatsAppContact, WhatsAppMessage
from .models import Membership
from .request_models import DealMessage
from . import test_deals as deal_tests
from . import login_hints, whatsapp_templates

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
        """Consent and the code typed back (its sending stubbed, so the tests count only notices)."""
        self.client.force_authenticate(user)
        with patch('mall.whatsapp_verification.send', return_value=True), patch('mall.whatsapp_verification.secrets.randbelow', return_value=123456):
            self.assertEqual(self.client.post('/api/v1/inbox/whatsapp/', {'number': number, 'opt_in': True}, format='json').status_code, 200)
        response = self.client.post('/api/v1/inbox/whatsapp/verify/', {'code': '123456'}, format='json')
        self.assertEqual((response.status_code, response.data['active']), (200, True))
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

    def code(self, number, digits, **extra):
        with patch('mall.whatsapp_verification.secrets.randbelow', return_value=digits):
            return self.client.post('/api/v1/inbox/whatsapp/', {'number': number, 'opt_in': True, **extra}, format='json')

    def verify(self, code):
        return self.client.post('/api/v1/inbox/whatsapp/verify/', {'code': code}, format='json')

    def test_a_number_is_used_only_once_its_owner_types_the_code_sent_to_it(self, urlopen):
        urlopen.side_effect = lambda request, timeout=None: Reply({'messages': [{'id': 'wamid.CODIGO'}]})
        self.client.force_authenticate(self.buyer)
        self.assertEqual(self.client.get('/api/v1/inbox/whatsapp/').data, {'enabled': True, 'number': '', 'opted_in': False, 'verified': False, 'active': False,
            'pending_number': '', 'code_sent_at': None, 'code_expires_at': None, 'resend_at': None, 'messages': True, 'updates': True})
        self.assertEqual(self.client.post('/api/v1/inbox/whatsapp/', {'number': '6123-4567'}, format='json').status_code, 400)  # no consent
        self.assertEqual(self.client.post('/api/v1/inbox/whatsapp/', {'number': '12', 'opt_in': True}, format='json').status_code, 400)
        urlopen.assert_not_called()
        data = self.code('6123 4567', 4242).data
        self.assertEqual((data['pending_number'], data['number'], data['active']), ('+50761234567', '', False))  # a local number gets Panamá's code
        [payload] = self.sent(urlopen)
        self.assertEqual((payload['to'], payload['template']['name'], payload['template']['components'][0]['parameters'][0]['text']),
                         ('50761234567', 'motionpartes_codigo', '004242'))
        self.assertEqual(WhatsAppMessage.objects.get(kind='verify').status, 'accepted')
        self.assertEqual(self.verify('000000').data['code'], ['El código no es correcto. Te quedan 4 intentos.'])
        data = self.verify('004242').data
        self.assertEqual((data['number'], data['active'], data['verified'], data['pending_number']), ('+50761234567', True, True, ''))
        self.assertEqual(self.client.post('/api/v1/inbox/whatsapp/', {'updates': False}, format='json').data['updates'], False)
        self.assertEqual(self.client.post('/api/v1/inbox/whatsapp/', {'opt_in': False}, format='json').data['active'], False)
        self.assertEqual(self.client.post('/api/v1/inbox/whatsapp/', {'number': '+507 6123 4567', 'opt_in': True}, format='json').data['active'], True)
        self.assertEqual(self.sent(urlopen), [])  # back on, in a number already proven: no code
        self.assertEqual(self.code('+1 (305) 555-0100', 1).status_code, 429)  # a minute between codes
        with patch('mall.whatsapp_verification.RESEND_AFTER', timedelta(0)):
            data = self.code('+1 (305) 555-0100', 1).data
        self.assertEqual((data['number'], data['active'], data['pending_number']), ('+50761234567', True, '+13055550100'))  # the old one keeps working
        self.assertEqual(self.client.post('/api/v1/inbox/whatsapp/', {'cancel': True}, format='json').data['pending_number'], '')
        with override_settings(WHATSAPP_ACCESS_TOKEN=''):
            self.assertEqual(self.client.get('/api/v1/inbox/whatsapp/').data['enabled'], False)
            self.assertEqual(self.client.post('/api/v1/inbox/whatsapp/', {'opt_in': True}, format='json').status_code, 409)

    def test_a_code_expires_runs_out_of_attempts_and_cannot_be_asked_for_endlessly(self, urlopen):
        urlopen.side_effect = lambda request, timeout=None: Reply({'messages': [{'id': 'wamid.CODIGO'}]})
        self.client.force_authenticate(self.buyer)
        self.code('+50761234567', 111111)
        InboxCursor.objects.filter(user=self.buyer).update(whatsapp_code_sent_at=timezone.now() - timedelta(minutes=11))
        self.assertEqual(self.verify('111111').data['code'], ['El código venció. Pide uno nuevo.'])
        with patch('mall.whatsapp_verification.RESEND_AFTER', timedelta(0)):
            self.code('+50761234567', 222222)
            for _ in range(4):
                self.verify('999999')
            self.assertEqual(self.verify('999999').data['code'], ['Demasiados intentos. Pide un código nuevo.'])
            self.assertEqual(self.verify('222222').data['code'], ['Pide un código primero.'])  # that code is gone
            for digits in range(3):
                self.assertEqual(self.code('+50761234567', digits).status_code, 200)
            self.assertEqual(self.code('+50761234567', 7).status_code, 429)  # five an hour
        self.assertFalse(InboxCursor.objects.get(user=self.buyer).whatsapp_verified_at)

    def test_a_code_goes_as_text_while_its_template_awaits_approval_and_unverified_numbers_get_no_notices(self, urlopen):
        def answer(request, timeout=None):
            if json.loads(request.data)['type'] == 'template':
                raise urllib.error.HTTPError('https://graph.facebook.com', 404, 'Not Found', {}, io.BytesIO(b'{"error":{"code":132001,"message":"no"}}'))
            return Reply({'messages': [{'id': 'wamid.TEXTO'}]})
        urlopen.side_effect = answer
        self.client.force_authenticate(self.buyer)
        self.assertEqual(self.code('+50761234567', 4).status_code, 502)  # never wrote to the number: the text could not arrive, so it is not sent
        self.assertEqual(len(self.sent(urlopen)), 1)
        self.assertIn('fuera de la ventana de 24 horas', WhatsAppMessage.objects.get(kind='verify').error)
        WhatsAppMessage.objects.all().delete()
        WhatsAppContact.objects.create(number='+50761234567', last_inbound_at=timezone.now())
        self.code('+50761234567', 5)
        template, text = self.sent(urlopen)
        self.assertEqual(text['text']['body'], 'Tu código de verificación de MotionPartes es 000005. Vence en 10 minutos. No lo compartas con nadie.')
        self.assertEqual(WhatsAppMessage.objects.get(kind='verify').template, 'motionpartes_codigo (texto)')
        order = self.order()
        InboxCursor.objects.filter(user=self.seller_a).update(whatsapp_number='+50762222222', whatsapp_opt_in_at=timezone.now(), active_at=None)  # consent, never verified
        self.say(self.buyer, self.client_account, order, 'hola')
        self.assertEqual(self.sent(urlopen), [])

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
        deal, hint = button['parameters'][0]['text'].split('~')
        self.assertEqual((button['sub_type'], deal, login_hints.username(hint)), ('url', str(order.pk), self.buyer.username))  # who it went to
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

    def test_a_template_not_yet_usable_goes_as_text_with_the_link(self, urlopen):
        sent = []

        def answer(request, timeout=None):
            payload = json.loads(request.data)
            sent.append(payload)
            if payload['type'] == 'template':  # still under review at Meta
                raise urllib.error.HTTPError('https://graph.facebook.com', 404, 'Not Found', {},
                                             io.BytesIO(b'{"error":{"code":132001,"message":"(#132001) Template name does not exist in the translation"}}'))
            return Reply({'messages': [{'id': 'wamid.TEXTO'}]})
        urlopen.side_effect = answer
        order = self.order()
        self.opt_in(self.buyer, '+50761111111')
        WhatsAppContact.objects.create(number='+50761111111', last_inbound_at=timezone.now() - timedelta(hours=23))  # wrote within 24 hours
        sent.clear()
        self.client.force_authenticate(self.seller_a)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(f'{self.url(self.supplier_a)}{order.pk}/review/', {}, format='json')
        template, text = sent
        self.assertEqual((template['type'], text['type'], text['to']), ('template', 'text', '50761111111'))
        body, link = text['text']['body'].split('\n\nVer en MotionPartes: ')
        self.assertEqual(body, f'Novedad en tu orden {order.reference}: PROVEEDOR A empezó a revisar tu orden. Ábrela en MotionPartes para ver el detalle.')
        self.assertEqual(link, f"https://motionpartes.example.invalid/?conversacion={template['template']['components'][1]['parameters'][0]['text']}")
        row = WhatsAppMessage.objects.get(user=self.buyer)
        self.assertEqual((row.status, row.wamid, row.template), ('accepted', 'wamid.TEXTO', 'motionpartes_novedad_orden (texto)'))
        self.assertIn('enviado como texto', row.error)

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


@override_settings(**CLOUD, WHATSAPP_BUSINESS_ACCOUNT_ID='99')
@patch('urllib.request.urlopen')
class WhatsAppTemplateTests(SimpleTestCase):
    def test_creates_only_the_missing_templates_with_the_sites_link(self, urlopen):
        calls = []

        def answer(request, timeout=None):
            calls.append((request.get_method(), request.full_url, json.loads(request.data) if request.data else None))
            if request.get_method() == 'GET':
                return Reply({'data': [{'name': 'motionpartes_novedad_orden', 'language': 'es', 'status': 'APPROVED'}]})
            return Reply({'id': '1', 'status': 'PENDING'})
        urlopen.side_effect = answer
        out = io.StringIO()
        self.assertEqual(whatsapp_templates.run(settings, True, out), 0)
        posted = [body for method, _, body in calls if method == 'POST']
        self.assertEqual([body['name'] for body in posted], ['motionpartes_mensaje_nuevo', 'motionpartes_codigo'])  # the approved one is left alone
        self.assertTrue(all(url.startswith('https://graph.facebook.com/v23.0/99/message_templates') for _, url, _ in calls))
        code = posted[1]
        self.assertEqual((code['category'], code['components'][2]['buttons'][0]['otp_type']), ('AUTHENTICATION', 'COPY_CODE'))  # codes: Meta's own text
        body, buttons = posted[0]['components']
        self.assertEqual(buttons['buttons'][0]['url'], 'https://motionpartes.example.invalid/?conversacion={{1}}')
        self.assertFalse(body['text'].startswith('{{') or body['text'].endswith('}}'))  # Meta refuses that
        self.assertEqual(out.getvalue().splitlines(), ['motionpartes_novedad_orden (es): APPROVED', 'motionpartes_mensaje_nuevo (es): creada, PENDING',
                                                       'motionpartes_codigo (es): creada, PENDING'])


class LoginHintTests(APITestCase):
    def setUp(self):
        from .models import User
        self.user = User.objects.create_user('aviso', 'aviso@example.invalid', 'Aviso938!clave')

    def test_a_link_hint_names_its_person_only_while_valid(self):
        hint = login_hints.hint(self.user.pk)
        self.assertNotIn('aviso', hint)  # opaque
        self.assertEqual(self.client.get('/api/v1/auth/login-hint/', {'hint': hint}).data, {'username': 'aviso'})  # no sign-in needed
        self.assertEqual(self.client.get('/api/v1/auth/login-hint/', {'hint': hint[:-2] + 'xx'}).status_code, 404)  # altered
        with patch('django.core.signing.time.time', return_value=time.time() + (login_hints.HINT_DAYS + 1) * 86400):
            self.assertIsNone(login_hints.username(hint))  # expired
        self.user.is_active = False
        self.user.save()
        self.assertEqual(self.client.get('/api/v1/auth/login-hint/', {'hint': hint}).status_code, 404)



@override_settings(**CLOUD)
@patch('mall.push.dispatch', lambda job: job())
@patch('urllib.request.urlopen')
class WhatsAppBridgeTests(APITestCase):
    """The bridge: replies on WhatsApp go into the deal's chat, chat messages go to WhatsApp while the 24-hour window is open, and an
    account's own number (WhatsApp de la cuenta) gets the account's notices and answers on its behalf."""
    setUp = deal_tests.DealWorkflowTests.setUp
    account = deal_tests.DealWorkflowTests.account
    item = deal_tests.DealWorkflowTests.item
    url = deal_tests.DealWorkflowTests.url
    payload = deal_tests.DealWorkflowTests.payload
    submit = deal_tests.DealWorkflowTests.submit
    opt_in = WhatsAppTests.opt_in
    order = WhatsAppTests.order
    say = WhatsAppTests.say
    sent = WhatsAppTests.sent
    webhook = WhatsAppTests.webhook

    def answering(self, urlopen):
        counter = iter(range(1, 1000))
        urlopen.side_effect = lambda request, timeout=None: Reply({'messages': [{'id': f'wamid.OUT{next(counter)}'}]})

    def review(self, order):
        self.client.force_authenticate(self.seller_a)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(f'{self.url(self.supplier_a)}{order.pk}/review/', {}, format='json')

    def write(self, number, body, wamid, context=None, kind='text'):
        message = {'from': number.lstrip('+'), 'id': wamid, 'type': kind, **({'text': {'body': body}} if kind == 'text' else {}),
                   **({'context': {'id': context}} if context else {})}
        with self.captureOnCommitCallbacks(execute=True):
            return self.webhook({'messages': [message]})

    def test_a_reply_on_whatsapp_goes_into_the_deal_chat_once_confirmed_with_a_reaction(self, urlopen):
        self.answering(urlopen)
        order = self.order()
        self.opt_in(self.buyer, '+50761111111')
        self.review(order)
        [notice] = self.sent(urlopen)
        notice_id = WhatsAppMessage.objects.get(kind='review_started').wamid
        self.assertEqual(self.write('+50761111111', '¿Pueden entregar mañana?', 'wamid.IN1', context=notice_id).status_code, 200)
        message = DealMessage.objects.get(order=order, channel='whatsapp')
        self.assertEqual((message.body, message.actor, message.account), ('¿Pueden entregar mañana?', self.buyer, self.client_account))
        [reaction] = self.sent(urlopen)
        self.assertEqual((reaction['type'], reaction['reaction']), ('reaction', {'message_id': 'wamid.IN1', 'emoji': '✅'}))
        self.assertTrue(WhatsAppContact.objects.filter(number='+50761111111').exists())  # its 24-hour window is open
        self.write('+50761111111', '¿Pueden entregar mañana?', 'wamid.IN1', context=notice_id)  # Meta delivers it again
        self.assertEqual(DealMessage.objects.filter(order=order, channel='whatsapp').count(), 1)
        self.write('+50761111111', 'Y el lunes también', 'wamid.IN2')  # no reply context: the last deal notified
        self.assertEqual(DealMessage.objects.filter(order=order, channel='whatsapp').count(), 2)
        self.client.force_authenticate(self.seller_a)
        listed = self.client.get(f'/api/v1/accounts/{self.supplier_a.pk}/deals/{order.pk}/messages/').data['results']
        self.assertEqual([(row['body'], row['channel']) for row in listed][-1], ('Y el lunes también', 'whatsapp'))

    def test_what_cannot_go_into_a_deal_gets_a_pointer_and_baja_still_opts_out(self, urlopen):
        self.answering(urlopen)
        self.opt_in(self.buyer, '+50761111111')
        self.write('+50761111111', '', 'wamid.FOTO', kind='image')
        [pointer] = self.sent(urlopen)
        self.assertIn('solo pasamos mensajes de texto', pointer['text']['body'])
        WhatsAppContact.objects.update(replied_at=None)
        WhatsAppMessage.objects.create(user=self.buyer, order=self.order(), number='+50761111111', template='motionpartes_novedad_orden',
                                       kind='quote', status='failed')  # a notice that never reached them
        self.write('+50761111111', 'hola', 'wamid.HOLA')  # never notified about any deal
        [pointer] = self.sent(urlopen)
        self.assertIn('responde a uno de sus avisos', pointer['text']['body'])
        self.assertFalse(DealMessage.objects.filter(channel='whatsapp').exists())
        self.write('+50761111111', 'Baja.', 'wamid.BAJA')
        self.assertIsNone(InboxCursor.objects.get(user=self.buyer).whatsapp_opt_in_at)

    def test_chat_messages_go_in_full_while_the_window_is_open_and_as_the_template_after(self, urlopen):
        self.answering(urlopen)
        order = self.order()
        self.opt_in(self.seller_a, '+50762222222')
        WhatsAppContact.objects.create(number='+50762222222', last_inbound_at=timezone.now() - timedelta(hours=2))
        self.say(self.buyer, self.client_account, order, '¿Tienen el tambor?\nEs urgente.')
        [relay] = self.sent(urlopen)
        self.assertEqual(relay['type'], 'text')
        self.assertTrue(relay['text']['body'].startswith(f'*CLIENTE ORIGINAL* · {order.reference}\n¿Tienen el tambor?\nEs urgente.\n\nResponde a este mensaje'))
        self.say(self.buyer, self.client_account, order, 'otro')
        self.assertEqual(len(self.sent(urlopen)), 1)  # each message while the window is open, no cooldown
        WhatsAppContact.objects.update(last_inbound_at=timezone.now() - timedelta(hours=25))
        self.say(self.buyer, self.client_account, order, 'y otro')
        [notice] = self.sent(urlopen)
        self.assertEqual(notice['template']['name'], 'motionpartes_mensaje_nuevo')

    def test_an_accounts_own_number_is_set_by_its_managers_gets_its_notices_and_answers_for_it(self, urlopen):
        self.answering(urlopen)
        url = f'/api/v1/accounts/{self.client_account.pk}/whatsapp/'
        self.client.force_authenticate(self.buyer)  # staff of the client account
        self.assertEqual(self.client.get(url).data['can_manage'], False)
        self.assertEqual(self.client.post(url, {'number': '+50769999999', 'opt_in': True}, format='json').status_code, 403)
        Membership.objects.filter(user=self.buyer, account=self.client_account).update(permission='owner')
        with patch('mall.whatsapp_verification.secrets.randbelow', return_value=777):
            self.assertEqual(self.client.post(url, {'number': '+507 6999 9999', 'opt_in': True}, format='json').data['pending_number'], '+50769999999')
        self.sent(urlopen)
        data = self.client.post(f'{url}verify/', {'code': '000777'}, format='json').data
        self.assertEqual((data['number'], data['active'], data['can_manage']), ('+50769999999', True, True))
        self.assertEqual(AccountWhatsApp.objects.get(account=self.client_account).whatsapp_verified_by, self.buyer)
        order = self.order()
        self.review(order)
        [notice] = self.sent(urlopen)
        self.assertEqual((notice['to'], notice['template']['name']), ('50769999999', 'motionpartes_novedad_orden'))
        self.assertEqual(notice['template']['components'][1]['parameters'][0]['text'], str(order.pk))  # no person's sign-in hint
        notice_id = WhatsAppMessage.objects.get(account=self.client_account, kind='review_started').wamid
        self.write('+50769999999', 'Gracias, esperamos la cotización', 'wamid.CUENTA', context=notice_id)
        message = DealMessage.objects.get(order=order, channel='whatsapp')
        self.assertEqual((message.account, message.actor), (self.client_account, self.buyer))  # on the account's behalf, signed by who verified it
