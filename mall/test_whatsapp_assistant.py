import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from .assistant_models import WhatsAppAssistantSession
from .basket_models import BasketEvent, BasketItem
from .inbox_models import WhatsAppContact, WhatsAppMessage
from .models import Account, Membership, Role
from .quote_assistant_models import AIUsageRecord
from .request_models import DealMessage, SupplierRequest
from .test_deal_attachments import ChatStorage
from . import test_whatsapp as whatsapp_tests
from . import test_deals as deal_tests

FakeMeta = whatsapp_tests.FakeMeta
ASSISTANT = {**whatsapp_tests.CLOUD, 'WHATSAPP_ASSISTANT_MONTHLY_USD': 10, 'GEMINI_API_KEY': 'clave-de-prueba'}
NUMBER = '+50761111111'


@override_settings(**ASSISTANT)
@patch('mall.push.dispatch', lambda job: job())
@patch('mall.catalog_classification.provider_configuration', lambda: ('clave-de-prueba', 'modelo-de-prueba'))
@patch('urllib.request.urlopen')
class WhatsAppAssistantTests(ChatStorage, APITestCase):
    """The shopping assistant: what a client writes outside an order's conversation is read by Gemini (mocked here), and what it shows
    comes from the catalog: a part, its suppliers with stock (never the account's own), a quantity, and the line lands in the cesta."""
    account = deal_tests.DealWorkflowTests.account
    item = deal_tests.DealWorkflowTests.item
    url = deal_tests.DealWorkflowTests.url
    payload = deal_tests.DealWorkflowTests.payload
    submit = deal_tests.DealWorkflowTests.submit
    opt_in = whatsapp_tests.WhatsAppTests.opt_in
    say = whatsapp_tests.WhatsAppTests.say
    webhook = whatsapp_tests.WhatsAppTests.webhook

    def setUp(self):
        deal_tests.DealWorkflowTests.setUp(self)
        self.use_memory_storage()
        self.counter = iter(range(1, 1000))
        self.decisions = []  # what Gemini «reads», one per call
        patcher = patch('mall.catalog_classification.generate_json', side_effect=self.gemini)
        self.generate = patcher.start()
        self.addCleanup(patcher.stop)

    def gemini(self, payload, source_count, **options):
        self.assertEqual(options.get('feature'), 'whatsapp_assistant')
        return self.decisions.pop(0)

    def writes(self, body, **extra):
        message = {'from': NUMBER.lstrip('+'), 'id': f'wamid.IN{next(self.counter)}', 'type': 'text', 'text': {'body': body}, **extra}
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.webhook({'messages': [message]}).status_code, 200)
        return message['id']

    def taps(self, identifier, kind='list_reply'):
        message = {'from': NUMBER.lstrip('+'), 'id': f'wamid.TAP{next(self.counter)}', 'type': 'interactive',
                   'interactive': {'type': kind, kind: {'id': identifier, 'title': 'opción'}}}
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.webhook({'messages': [message]}).status_code, 200)

    def rows(self, payload):
        return [row['id'] for row in payload['interactive']['action']['sections'][0]['rows']]

    def test_a_client_finds_a_part_picks_a_supplier_and_a_quantity_by_taps_and_it_lands_in_the_cesta(self, urlopen):
        meta = urlopen.side_effect = FakeMeta()
        self.opt_in(self.buyer, NUMBER)
        self.decisions.append({'accion': 'buscar', 'consultas': ['TAMBOR ORIGINAL', 'TAMBOR'], 'codigos': []})
        self.writes('necesito un tambor')
        [parts] = meta.sent()
        self.assertEqual((parts['type'], parts['interactive']['type']), ('interactive', 'list'))
        self.assertEqual(self.rows(parts), [f'part:{self.part.pk}'])
        self.assertIn('Encontré esto para «tambor original»', parts['interactive']['body']['text'])  # what was searched
        self.assertIn('2 proveedores con existencia', parts['interactive']['body']['text'])
        sent_to_gemini = self.generate.call_args.args[0]
        self.assertIn('Mensaje del cliente: necesito un tambor', json.dumps(sent_to_gemini, ensure_ascii=False))
        self.assertEqual(sent_to_gemini['generationConfig']['responseMimeType'], 'application/json')

        self.taps(f'part:{self.part.pk}')
        [suppliers] = meta.sent()
        self.assertEqual(self.rows(suppliers), [f'offer:{self.item_a.pk}', f'offer:{self.item_a2.pk}', f'offer:{self.item_b.pk}'])
        self.assertNotIn('$', suppliers['interactive']['body']['text'])  # no prices, no stock figures
        self.taps(f'offer:{self.item_b.pk}')
        [quantity] = meta.sent()  # a selector of 1 to 10 units (any other is typed)
        self.assertEqual(self.rows(quantity), [f'qty:{units}' for units in range(1, 11)])
        self.assertEqual([row['title'] for row in quantity['interactive']['action']['sections'][0]['rows']][:2], ['1 unidad', '2 unidades'])
        self.assertNotIn('description', quantity['interactive']['action']['sections'][0]['rows'][0])
        self.taps('qty:2')
        [added] = meta.sent()
        body = added['interactive']['body']['text']
        self.assertTrue(body.startswith('✅ Agregué 2 × 58411-1R000-G de PROVEEDOR B a tu cesta. Tienes 1 artículo.'))
        self.assertIn(f'https://motionpartes.example.invalid/?vista=cesta&cuenta={self.client_account.pk}&ingreso=', body)
        line = BasketItem.objects.get()
        self.assertEqual((line.user, line.account, line.supplier_item, line.quantity, line.source), (self.buyer, self.client_account, self.item_b, 2, 'whatsapp'))
        self.assertEqual(self.generate.call_count, 1)  # taps need no AI
        self.assertEqual(WhatsAppMessage.objects.filter(kind='assistant', status='accepted').count(), 4)
        self.assertEqual(WhatsAppMessage.objects.filter(kind='assistant', status='received').count(), 4)

    def test_typed_choices_quantities_said_in_advance_and_the_cesta(self, urlopen):
        meta = urlopen.side_effect = FakeMeta()
        self.opt_in(self.buyer, NUMBER)
        self.decisions += [{'accion': 'buscar', 'consultas': ['TAMBOR']}, {'accion': 'elegir', 'opcion': 1}, {'accion': 'elegir', 'opcion': 3}]
        self.writes('tambor')
        self.writes('el primero')
        self.writes('el 3')  # PROVEEDOR B's
        meta.sent()
        self.writes('3')  # a quantity: no AI
        self.assertEqual(BasketItem.objects.get(supplier_item=self.item_b).quantity, 3)
        self.decisions.append({'accion': 'buscar', 'consultas': ['TAMBOR'], 'cantidad': 4})
        self.writes('necesito 4 tambores')
        self.taps(f'part:{self.part.pk}')
        self.taps(f'offer:{self.item_a.pk}')  # the quantity was said: no question
        self.assertEqual(BasketItem.objects.get(supplier_item=self.item_a).quantity, 4)
        meta.sent()
        self.writes('cesta')  # no AI
        [cart] = meta.sent()
        self.assertIn('1. 3 × 58411-1R000-G · PROVEEDOR B\n2. 4 × 58411-1R000-G · PROVEEDOR A', cart['text']['body'])
        self.decisions.append({'accion': 'cambiar', 'opcion': 2, 'cantidad': 8})
        self.writes('el 2 a 8')
        [changed] = meta.sent()
        self.assertIn('Listo: ahora pides 8 × 58411-1R000-G · PROVEEDOR A.', changed['interactive']['body']['text'])
        self.assertEqual(BasketItem.objects.get(supplier_item=self.item_a).quantity, 8)
        undo = changed['interactive']['action']['buttons'][0]['reply']
        self.assertEqual((undo['title'], undo['id']), ('Deshacer', f'undo:{BasketEvent.objects.latest("id").pk}'))
        self.taps(undo['id'], kind='button_reply')  # Deshacer: back to 4
        self.assertEqual(BasketItem.objects.get(supplier_item=self.item_a).quantity, 4)
        self.assertIn('Listo, volví a dejar 4 × 58411-1R000-G · PROVEEDOR A en tu cesta.', meta.sent()[0]['text']['body'])
        self.taps(undo['id'], kind='button_reply')  # once is enough: the cesta changed since
        self.assertIn('Ya no se puede deshacer', meta.sent()[0]['text']['body'])
        self.decisions.append({'accion': 'cambiar', 'opcion': 2, 'cantidad': 8})
        self.writes('el 2 a 8')
        meta.sent()
        self.decisions.append({'accion': 'quitar', 'opcion': 1})
        self.writes('quita el 1')
        [confirm] = meta.sent()  # taking a line out asks first
        self.assertEqual(confirm['interactive']['body']['text'], '¿Quito 3 × 58411-1R000-G · PROVEEDOR B de tu cesta?')
        self.assertEqual([button['reply']['id'] for button in confirm['interactive']['action']['buttons']], [f'remove:{self.item_b.pk}', 'menu:keep'])
        self.assertEqual(BasketItem.objects.count(), 2)
        self.taps('menu:keep', kind='button_reply')
        self.assertEqual(BasketItem.objects.count(), 2)
        meta.sent()
        self.taps(f'remove:{self.item_b.pk}', kind='button_reply')
        [removed] = meta.sent()
        self.assertIn('Listo, quité 3 × 58411-1R000-G · PROVEEDOR B. Te queda 1 artículo en la cesta.', removed['interactive']['body']['text'])
        self.assertEqual(list(BasketItem.objects.values_list('supplier_item', flat=True)), [self.item_a.pk])
        self.taps(removed['interactive']['action']['buttons'][0]['reply']['id'], kind='button_reply')  # Deshacer: it comes back
        self.assertEqual(BasketItem.objects.get(supplier_item=self.item_b).quantity, 3)
        meta.sent()
        self.taps(f'remove:{self.item_b.pk}', kind='button_reply')
        meta.sent()
        self.assertEqual(list(BasketEvent.objects.filter(source='whatsapp').values_list('kind', flat=True).order_by('id')),
                         ['added', 'added', 'changed', 'changed', 'changed', 'removed', 'added', 'removed'])
        self.decisions.append({'accion': 'cambiar', 'opcion': 0, 'cantidad': 6})  # one line left: no number needed
        self.writes('mejor 6')
        self.assertEqual(BasketItem.objects.get().quantity, 6)
        self.assertEqual((self.decisions, self.generate.call_count), ([], 8))  # every one read once; quantities and taps need no AI

    def test_the_accounts_own_articles_are_never_offered_and_a_client_with_several_accounts_chooses_first(self, urlopen):
        meta = urlopen.side_effect = FakeMeta()
        self.client_account.roles.add(Role.objects.get(code='supplier_retail'))  # it also sells
        own = self.item(self.client_account, self.buyer, 'C-001', '58411-1R000-G', 5)
        other = Account.objects.create(name='TALLER NORTE')
        other.roles.add(Role.objects.get(code='client_business'))
        Membership.objects.create(user=self.buyer, account=other)
        self.opt_in(self.buyer, NUMBER)
        self.decisions.append({'accion': 'buscar', 'consultas': ['TAMBOR']})
        self.writes('tambor')
        [accounts] = meta.sent()
        self.assertEqual(self.rows(accounts), [f'account:{self.client_account.pk}', f'account:{other.pk}'])
        self.assertEqual(self.generate.call_count, 0)  # the request waits for the account
        self.taps(f'account:{self.client_account.pk}')
        [parts] = meta.sent()
        self.assertEqual(self.rows(parts), [f'part:{self.part.pk}'])
        self.taps(f'part:{self.part.pk}')
        [suppliers] = meta.sent()
        self.assertNotIn(f'offer:{own.pk}', self.rows(suppliers))
        self.taps(f'offer:{own.pk}')  # an old tap, or a forged one
        [refused] = meta.sent()
        self.assertIn('ya no se puede solicitar', refused['text']['body'])
        self.assertEqual(WhatsAppAssistantSession.objects.get().account, self.client_account)

    def test_off_or_out_of_budget_the_assistant_never_calls_gemini(self, urlopen):
        meta = urlopen.side_effect = FakeMeta()
        self.opt_in(self.buyer, NUMBER)
        AIUsageRecord.objects.create(feature='whatsapp_assistant', day=timezone.localdate(), cost_micro_usd=10_000_000)
        self.writes('tambor')
        [unavailable] = meta.sent()
        self.assertIn('El asistente no está disponible en este momento', unavailable['text']['body'])
        with override_settings(WHATSAPP_ASSISTANT_MONTHLY_USD=0):  # off: as before the assistant, a pointer
            self.writes('hola')
        [pointer] = meta.sent()
        self.assertIn('responde a uno de sus avisos', pointer['text']['body'])
        self.assertEqual(self.generate.call_count, 0)

    def test_a_voice_note_is_heard_and_a_photo_seen(self, urlopen):
        meta = urlopen.side_effect = FakeMeta({'901': (b'OggS-voz', 'audio/ogg; codecs=opus'), '902': (b'\xff\xd8\xff-etiqueta', 'image/jpeg')})
        self.opt_in(self.buyer, NUMBER)
        self.decisions += [{'accion': 'buscar', 'consultas': ['TAMBOR']}, {'accion': 'buscar', 'consultas': [], 'codigos': ['58411-1R000']}]
        with patch('mall.deal_attachments.transcribe', return_value='necesito un tambor trasero'):
            self.writes('', type='audio', audio={'id': '901', 'mime_type': 'audio/ogg; codecs=opus', 'voice': True})
        self.assertIn('Mensaje del cliente: necesito un tambor trasero', json.dumps(self.generate.call_args.args[0], ensure_ascii=False))
        self.writes('', type='image', image={'id': '902', 'mime_type': 'image/jpeg', 'caption': 'este'})
        parts = self.generate.call_args.args[0]['contents'][0]['parts']
        self.assertEqual(parts[-1]['inline_data']['mime_type'], 'image/jpeg')
        self.assertEqual(self.rows(meta.sent()[-1]), [f'part:{self.part.pk}'])  # found by the code read on the label

    def orders(self):
        """Two orders of the client, one per supplier, with each supplier's message relayed to the client's WhatsApp."""
        self.client.force_authenticate(self.buyer)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.submit(self.payload([(self.item_a, 2), (self.item_b, 1)]))
        first, second = (SupplierRequest.objects.get(pk=row['id']) for row in response.data['requests'])
        return (first, second) if first.supplier == self.supplier_a else (second, first)

    def test_messages_continue_an_order_conversation_and_a_message_for_its_supplier_is_passed_on(self, urlopen):
        meta = urlopen.side_effect = FakeMeta()
        order_a, _ = self.orders()
        self.opt_in(self.buyer, NUMBER)
        WhatsAppContact.objects.create(number=NUMBER, last_inbound_at=timezone.now())
        self.say(self.seller_a, self.supplier_a, order_a, '¿Lo necesitas para hoy?')
        meta.sent()
        self.writes('Sí, antes de las 3')  # the conversation is with PROVEEDOR A's order
        self.assertEqual(DealMessage.objects.get(channel='whatsapp').order, order_a)
        self.assertEqual(self.generate.call_count, 0)
        self.assertEqual([payload['type'] for payload in meta.sent()], ['reaction'])
        WhatsAppMessage.objects.update(created_at=timezone.now() - timedelta(minutes=31))  # a pause: the assistant now
        self.decisions.append({'accion': 'pedidos'})  # a question for the assistant: read-only, nothing reaches a supplier
        self.writes('me puedes mostrar mis ultimos pedidos?')
        [listed] = meta.sent()
        self.assertIn(f'{order_a.reference} · PROVEEDOR A · Enviada · por revisar', listed['text']['body'])
        self.assertIn(f'/?vista=solicitudes&cuenta={self.client_account.pk}&ingreso=', listed['text']['body'])
        self.assertEqual(DealMessage.objects.filter(channel='whatsapp').count(), 1)
        self.decisions.append({'accion': 'orden'})
        self.writes('dile que lo necesito el lunes')
        [ask] = meta.sent()  # for the supplier? only once confirmed
        self.assertEqual(ask['interactive']['body']['text'], f'¿Paso tu mensaje «dile que lo necesito el lunes» a la orden {order_a.reference} con PROVEEDOR A?')
        self.assertEqual(DealMessage.objects.filter(channel='whatsapp').count(), 1)
        self.taps('menu:hold', kind='button_reply')
        self.assertIn('no lo pasé', meta.sent()[0]['text']['body'])
        self.taps(f'pass:{order_a.pk}', kind='button_reply')  # a stale button: nothing waits anymore
        self.assertIn('ya no está pendiente', meta.sent()[0]['text']['body'])
        self.decisions.append({'accion': 'orden'})
        self.writes('¿ya salió mi pedido?')
        meta.sent()
        self.taps(f'pass:{order_a.pk}', kind='button_reply')
        replies = meta.sent()
        self.assertIn(f'Pasé tu mensaje a la orden {order_a.reference} con PROVEEDOR A.', replies[-1]['text']['body'])
        self.assertEqual(list(DealMessage.objects.filter(order=order_a, channel='whatsapp').values_list('body', flat=True)), ['Sí, antes de las 3', '¿ya salió mi pedido?'])
        self.writes('gracias')  # it goes on with that order
        self.assertEqual(DealMessage.objects.filter(order=order_a, channel='whatsapp').count(), 3)
        self.assertEqual(self.generate.call_count, 3)

    def test_with_several_orders_open_a_plain_message_waits_for_the_client_to_tap_which(self, urlopen):
        meta = urlopen.side_effect = FakeMeta()
        order_a, order_b = self.orders()
        self.opt_in(self.buyer, NUMBER)
        WhatsAppContact.objects.create(number=NUMBER, last_inbound_at=timezone.now())
        self.say(self.seller_a, self.supplier_a, order_a, 'Te confirmo el tambor A')
        self.say(self.seller_b, self.supplier_b, order_b, 'El de B llega el lunes')
        meta.sent()
        self.writes('perfecto')
        self.writes('¿y la factura?')
        self.assertFalse(DealMessage.objects.filter(channel='whatsapp').exists())  # never a guess
        asked = meta.sent()
        self.assertEqual(len(asked), 2)
        self.assertEqual([button['reply']['id'] for button in asked[-1]['interactive']['action']['buttons']], [f'order:{order_b.pk}', f'order:{order_a.pk}'])
        self.assertIn('¿Para cuál es tu mensaje?', asked[-1]['interactive']['body']['text'])
        self.taps(f'order:{order_b.pk}', kind='button_reply')
        self.assertEqual(list(DealMessage.objects.filter(channel='whatsapp').values_list('order', 'body')),
                         [(order_b.pk, 'perfecto'), (order_b.pk, '¿y la factura?')])
        self.assertEqual([payload['type'] for payload in meta.sent()], ['reaction', 'reaction'])
        self.writes('gracias')  # the conversation goes on with B
        self.assertEqual(DealMessage.objects.filter(order=order_b, channel='whatsapp').count(), 3)
        reply_to_a = WhatsAppMessage.objects.filter(order=order_a, kind='message').exclude(status='received').first().wamid
        self.writes('ok A', context={'id': reply_to_a})  # Responder on A's message: no question
        self.assertEqual(DealMessage.objects.get(order=order_a, channel='whatsapp').body, 'ok A')
        self.assertEqual(self.generate.call_count, 0)
