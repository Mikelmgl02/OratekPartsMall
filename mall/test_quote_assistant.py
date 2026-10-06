"""S7 · AI quotation assistant: it interprets the client's own words into reviewable proposals and never produces or alters a price.

The provider is always patched (mall.quote_assistant.generate_json): these tests never call Gemini and pass without an API key.
"""
import copy
import json
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock, skipUnless
from urllib.error import HTTPError

from django.core.cache import cache
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APITestCase, APITransactionTestCase

from . import quote_assistant
from . import test_deals as deal_tests
from . import test_pricing_engine as engine_tests
from . import test_quote_drafts as draft_tests
from . import test_requests as request_tests
from .catalog_classification import ClassificationProviderError
from .models import SupplierItem
from .pricing_models import PricingAuditEvent, PricingRule, SupplierPricingSettings
from .quote_assistant import RESPONSE_SCHEMA, QuoteAssistantProviderError, validate_output
from .quote_assistant_models import AIUsageRecord, QuoteAssistantRun
from .quote_draft_models import DealQuotationDraft, DealQuotationLineAudit
from .quote_drafts import ASSISTANT_OFF_DETAIL, QuoteAssistantThrottle
from .request_models import SupplierRequest

A, A2 = '58411-1R000-G', '58411-1R000-KSM'
NOTES = 'SOLO NECESITO 2 DEL TAMBOR. ACEPTO ALTERNO EN LA ZAPATA. ENTREGA MAÑANA EN EL TALLER. HAZME UN DESCUENTO. TAMBIEN QUIERO UN FILTRO DE ACEITE.'
OUTPUT = {'summary': 'EL CLIENTE NECESITA MENOS TAMBORES, ACEPTA ALTERNO Y PIDE ENTREGA MAÑANA.',
          'proposals': [{'i': 0, 'kind': 'quantity_change', 'quantity': 2, 'evidence': 'solo necesito 2 del  tambor'},
                        {'i': 1, 'kind': 'line_note', 'evidence': 'ACEPTO ALTERNO EN LA ZAPATA', 'note': 'acepta marca alterna'}],
          'terms_items': [{'kind': 'entrega', 'text': 'ENTREGA MAÑANA EN EL TALLER DEL CLIENTE', 'evidence': 'ENTREGA MAÑANA EN EL TALLER'}],
          'price_requests': [{'i': 0, 'kind': 'descuento', 'evidence': 'HAZME UN DESCUENTO'}],
          'unmatched': [{'evidence': 'TAMBIEN QUIERO UN FILTRO DE ACEITE', 'note': 'FILTRO DE ACEITE'}],
          'questions': ['¿Confirmas que aceptas marca alterna en la zapata?']}
USAGE = {'input_tokens': 1200, 'output_tokens': 300, 'thinking_tokens': 0, 'total_tokens': 1500}
ENABLED = {'GEMINI_API_KEY': 'test-key-not-real', 'GEMINI_MODEL': 'gemini-test-model', 'QUOTE_ASSISTANT_MONTHLY_USD': Decimal('40'),
           'AI_MONTHLY_BUDGET_USD': Decimal('200'), 'GEMINI_INPUT_USD_PER_MTOK': Decimal('1.00'), 'GEMINI_OUTPUT_USD_PER_MTOK': Decimal('5.00'),
           'QUOTE_ASSISTANT_RUNS_PER_REVISION': 5, 'QUOTE_ASSISTANT_DAILY_RUNS_PER_ACCOUNT': 30}
PRICE_WORDS = {'price', 'unit_price', 'precio', 'amount', 'monto', 'total', 'currency', 'moneda', 'discount', 'descuento', 'percent', 'porcentaje', 'cost'}


def http_failure(code):
    """The provider error generate_json raises for an HTTP error response (its context is the HTTPError): nothing was generated or billed."""
    try:
        raise HTTPError('https://generativelanguage.googleapis.com/', code, 'error', {}, None)
    except HTTPError:
        try:
            raise ClassificationProviderError('La IA alcanzó su límite de solicitudes.')
        except ClassificationProviderError as error:
            return error


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        if record.name != 'django.db.backends':
            self.lines.append(f'{record.name} {record.getMessage()}')


@override_settings(**ENABLED)
class QuoteAssistantTests(APITestCase):
    account = request_tests.SupplierRequestWorkflowTests.account
    item = request_tests.SupplierRequestWorkflowTests.item
    url = request_tests.SupplierRequestWorkflowTests.url
    payload = request_tests.SupplierRequestWorkflowTests.payload
    submit = request_tests.SupplierRequestWorkflowTests.submit
    review = deal_tests.DealWorkflowTests.review
    action = deal_tests.DealWorkflowTests.action
    drafted = deal_tests.DealWorkflowTests.drafted
    messages = deal_tests.DealWorkflowTests.messages
    draft_url = draft_tests.QuoteDraftTests.draft_url
    save = draft_tests.QuoteDraftTests.save
    by_code = draft_tests.QuoteDraftTests.by_code
    price = engine_tests.PricedDraftTests.price
    general = engine_tests.PricedDraftTests.general

    def setUp(self):
        request_tests.SupplierRequestWorkflowTests.setUp(self)
        cache.clear()
        # The soft per-worker throttle is exercised in its own test; here it must not interfere with the database caps.
        self.enterContext(mock.patch.object(QuoteAssistantThrottle, 'rate', '1000/min'))
        SupplierPricingSettings.objects.create(supplier=self.supplier_a, assistant_enabled=True, updated_by=self.seller_a)
        self.calls = []

    def get_draft(self, order):
        self.client.force_authenticate(self.seller_a)
        return draft_tests.QuoteDraftTests.get_draft(self, order)

    def provider(self, output=OUTPUT, usage=USAGE, error=None):
        def fake(payload, count, **kwargs):
            self.calls.append({'payload': payload, 'count': count, **kwargs})
            if error:
                raise error
            return copy.deepcopy(output), dict(usage)
        return mock.patch('mall.quote_assistant.generate_json', side_effect=fake)

    def reviewed(self, notes=NOTES, lines=None):
        self.client.force_authenticate(self.buyer)
        response = self.submit(self.payload(lines or [(self.item_a, 3), (self.item_a2, 2)], notes=notes))
        order = SupplierRequest.objects.get(pk=response.data['requests'][0]['id'])
        self.review(order)
        return order

    def say(self, order, body, *, supplier=False):
        user, account = (self.seller_a, self.supplier_a) if supplier else (self.buyer, self.client_account)
        self.client.force_authenticate(user)
        response = self.client.post(self.messages(order, account), {'message_id': str(uuid.uuid4()), 'body': body}, format='json')
        self.assertEqual(response.status_code, 200)

    def assistant_url(self, order, account=None, suffix=''):
        return self.draft_url(order, account, f'assistant/{suffix}')

    def start(self, order, version=0, run_id=None, *, user=None, account=None):
        self.client.force_authenticate(user or self.seller_a)
        return self.client.post(self.assistant_url(order, account), {'run_id': str(run_id or uuid.uuid4()), 'expected_draft_version': version}, format='json')

    def decide(self, order, run_id, version, decisions, save_id=None):
        self.client.force_authenticate(self.seller_a)
        return self.client.post(self.assistant_url(order, suffix=f'{run_id}/decisions/'), {
            'save_id': str(save_id or uuid.uuid4()), 'expected_draft_version': version,
            'decisions': [{'proposal_id': proposal, 'action': action} for proposal, action in decisions]}, format='json')

    def proposals(self, response):
        return {proposal['kind']: proposal for proposal in response.data['latest_run']['proposals']}

    def completed(self, order, version=0):
        with self.provider():
            response = self.start(order, version)
        self.assertEqual(response.status_code, 200, response.data)
        return response

    def test_payload_is_minimal_and_pseudonymised_and_the_schema_has_no_price_field(self):
        list_ = self.general()
        self.price(list_, self.item_a, Decimal('913.57'), floor=Decimal('802.46'))
        item = SupplierItem.objects.get(pk=self.item_a.pk)
        item.reported_quantity, item.reserved_quantity = 4719, 17
        item.save()
        # Another order of the same client, with its own notes and chat, must never be read.
        other = self.reviewed(notes='OTRA-ORDEN-SECRETA', lines=[(self.item_a2, 1)])
        self.say(other, 'MENSAJE-DE-OTRA-ORDEN')
        order = self.reviewed()
        self.say(order, 'MENSAJE-ANTERIOR-A-LA-VERSION')
        self.client.force_authenticate(self.seller_a)
        lines = [{'order_line_id': str(line.pk), 'quantity': line.quantity, 'unit_price': '731.19'} for line in order.lines.all()]
        self.assertEqual(self.action(order, 'quote', lines=lines, draft_version=self.drafted(order, lines)).status_code, 200)
        self.client.force_authenticate(self.buyer)
        quotation = order.quotations.get()
        self.assertEqual(self.action(order, 'request_adjustment', quotation_id=str(quotation.pk), reason='solo necesito 2 del tambor').status_code, 200)
        self.say(order, 'GRACIAS PROVEEDOR A, HAZME UN DESCUENTO DEL 10% O DEJALO EN $650. TAMBIEN QUIERO UN FILTRO DE ACEITE')
        # Member names and the client's current account name are pseudonymised; every standalone number the supplier wrote (a price or
        # a stock figure) is masked, while the digits of a part code stay.
        type(self.buyer).objects.filter(pk=self.buyer.pk).update(first_name='Juan', last_name='Pérez')
        type(self.seller_a).objects.filter(pk=self.seller_a.pk).update(first_name='Rosa', last_name='Delgado')
        type(self.client_account).objects.filter(pk=self.client_account.pk).update(name='TALLER RENOMBRADO')
        self.say(order, 'TE LO DEJO EN 45.90 USD O 38 C/U, TENGO 7 DISPONIBLES; ESCRIBE A VENTAS@PROVEEDOR-A.COM. SALUDOS A CLIENTE ORIGINAL, TALLER '
                        'RENOMBRADO Y JUAN PÉREZ. ROSA  DELGADO, CÓDIGO 04465-33450', supplier=True)
        with self.provider():
            response = self.start(order)
        self.assertEqual(response.status_code, 200, response.data)
        sent = self.calls[0]
        self.assertEqual((sent['count'], sent['timeout'], sent['with_usage']), (2, 45, True))
        self.assertEqual({key: sent['payload']['generationConfig'][key] for key in ['temperature', 'maxOutputTokens', 'responseMimeType']},
                         {'temperature': 0, 'maxOutputTokens': 1500, 'responseMimeType': 'application/json'})
        everything, data = json.dumps(sent['payload'], ensure_ascii=False), sent['payload']['contents'][0]['parts'][0]['text']
        for secret in ['913.57', '802.46', '731.19', '4719', '4702', 'OTRA-ORDEN-SECRETA', 'MENSAJE-DE-OTRA-ORDEN', 'MENSAJE-ANTERIOR', '45.90', '650',
                       '10%', 'VENTAS@', 'CLIENTE ORIGINAL', 'PROVEEDOR A', 'PROVEEDOR B', 'request-buyer', 'request-seller', str(self.client_account.pk),
                       ' 38 ', ' 7 ', 'TALLER RENOMBRADO', 'JUAN', 'PÉREZ', 'ROSA', 'DELGADO',
                       str(self.supplier_a.pk), str(order.pk), 'LISTA GENERAL', 'GENERAL']:
            self.assertNotIn(secret, everything if secret[0].isdigit() else data, secret)
        texts = json.loads(data)
        self.assertEqual(texts['lines'], [{'i': 0, 'codigo': A, 'brand': 'KSM', 'description': 'TAMBOR A-001', 'requested': 3},
                                          {'i': 1, 'codigo': A2, 'brand': 'KSM', 'description': 'TAMBOR A-002', 'requested': 2}])
        self.assertEqual([(text['author'], text['kind']) for text in texts['texts']],
                         [('CLIENTE', 'nota_orden'), ('CLIENTE', 'motivo_ajuste'), ('CLIENTE', 'mensaje'), ('PROVEEDOR', 'mensaje')])
        self.assertEqual(texts['texts'][2]['text'], 'GRACIAS [PROVEEDOR], HAZME UN DESCUENTO DEL [IMPORTE] O DEJALO EN [IMPORTE]. TAMBIEN QUIERO UN FILTRO DE ACEITE')
        self.assertEqual(texts['texts'][3]['text'], 'TE LO DEJO EN [IMPORTE] O [NÚMERO] C/U, TENGO [NÚMERO] DISPONIBLES; ESCRIBE A [CORREO]. SALUDOS A '
                                                    '[CLIENTE], [CLIENTE] Y [PERSONA]. [PERSONA], CÓDIGO 04465-33450')
        self.assertEqual(data.count('SOLO NECESITO 2 DEL TAMBOR'), 2, 'The reason is sent once (plus the notes), never again as a chat message.')
        # The provider schema: no enum, minItems or maxItems, no number type and no property that could carry a price.
        def walk(node):
            if isinstance(node, dict):
                self.assertFalse({'enum', 'minItems', 'maxItems', 'minimum', 'maximum'} & set(node))
                self.assertNotEqual(node.get('type'), 'number')
                for name, child in node.get('properties', {}).items():
                    self.assertNotIn(name, PRICE_WORDS)
                    walk(child)
                walk(node.get('items'))
        walk(RESPONSE_SCHEMA)
        self.assertEqual(sent['payload']['generationConfig']['responseJsonSchema'], RESPONSE_SCHEMA)
        integers = []
        def numbers(node, path=''):
            if isinstance(node, dict):
                if node.get('type') == 'integer':
                    integers.append(path)
                for name, child in node.get('properties', {}).items():
                    numbers(child, f'{path}.{name}')
                numbers(node.get('items'), f'{path}[]')
        numbers(RESPONSE_SCHEMA)
        self.assertEqual(sorted(integers), ['.price_requests[].i', '.proposals[].i', '.proposals[].quantity'])

    def validate(self, output, texts=None):
        lines = [SimpleNamespace(pk=uuid.uuid4(), codigo=A), SimpleNamespace(pk=uuid.uuid4(), codigo=A2)]
        texts = texts or [{'author': 'CLIENTE', 'text': 'SOLO NECESITO TRES DEL TAMBOR. HAZME UN DESCUENTO'}, {'author': 'PROVEEDOR', 'text': 'TENGO 9 EN EL ALMACEN'}]
        return validate_output(output, lines, texts), lines

    def test_validator_rejects_what_it_cannot_verify_and_drops_money_text(self):
        base = {'summary': '', 'proposals': [], 'terms_items': [], 'price_requests': [], 'unmatched': [], 'questions': []}
        quantity = {'i': 0, 'kind': 'quantity_change', 'quantity': 3, 'evidence': 'SOLO NECESITO TRES DEL TAMBOR'}
        (summary, proposals, rejected), lines = self.validate({**base, 'proposals': [quantity]})
        self.assertEqual((summary, rejected, proposals), ('', 0, [{'id': 'p1', 'kind': 'quantity_change', 'detail': '', 'order_line_id': str(lines[0].pk),
                                                                   'quantity': 3, 'text': '', 'evidence': 'SOLO NECESITO TRES DEL TAMBOR'}]))
        for label, output in [
                ('i fuera de rango', {**base, 'proposals': [{**quantity, 'i': 2}]}), ('i negativo', {**base, 'proposals': [{**quantity, 'i': -1}]}),
                ('i booleano', {**base, 'proposals': [{**quantity, 'i': True}]}), ('i duplicado', {**base, 'proposals': [quantity, dict(quantity)]}),
                ('tipo desconocido', {**base, 'proposals': [{**quantity, 'kind': 'set_price'}]}),
                ('evidencia inventada', {**base, 'proposals': [{**quantity, 'evidence': 'NECESITO TRES TAMBORES'}]}),
                ('evidencia del proveedor', {**base, 'proposals': [{**quantity, 'quantity': 9, 'evidence': 'TENGO 9 EN EL ALMACEN'}]}),
                ('cantidad sin evidencia', {**base, 'proposals': [{**quantity, 'quantity': 4}]}),
                ('cantidad booleana', {**base, 'proposals': [{**quantity, 'quantity': True}]}),
                ('cantidad decimal', {**base, 'proposals': [{**quantity, 'quantity': 3.0}]}),
                ('clave extra', {**base, 'proposals': [{**quantity, 'unit_price': '1.00'}]}), ('clave extra arriba', {**base, 'price': '1.00'}),
                ('falta una lista', {key: value for key, value in base.items() if key != 'questions'}),
                ('tres por línea', {**base, 'proposals': [quantity, {'i': 0, 'kind': 'remove_line', 'evidence': 'SOLO NECESITO'},
                                                         {'i': 0, 'kind': 'line_note', 'evidence': 'TAMBOR', 'note': 'NOTA'}]}),
                ('nota obligatoria', {**base, 'proposals': [{'i': 0, 'kind': 'line_note', 'evidence': 'TAMBOR'}]}),
                ('condición desconocida', {**base, 'terms_items': [{'kind': 'precio', 'text': 'X', 'evidence': 'TAMBOR'}]}),
                ('solicitud de precio desconocida', {**base, 'price_requests': [{'kind': 'fijar_precio', 'evidence': 'HAZME UN DESCUENTO'}]}),
                ('demasiadas preguntas', {**base, 'questions': ['¿UNA?'] * 6}), ('resumen largo', {**base, 'summary': 'X' * 301}),
                ('evidencia larga', {**base, 'unmatched': [{'evidence': 'T' * 201, 'note': ''}]}), ('no es objeto', ['x'])]:
            with self.subTest(label), self.assertRaises(QuoteAssistantProviderError):
                self.validate(output)
        # Money text in the model's own words is dropped and counted; evidence quotes the client and a Spanish number word counts.
        (summary, proposals, rejected), _ = self.validate({**base, 'summary': 'PIDE 10% MENOS', 'questions': ['¿TE SIRVE A 12.50?', '¿CUÁNDO LO NECESITAS?'],
                                                           'proposals': [quantity, {'i': 1, 'kind': 'line_note', 'evidence': 'TAMBOR', 'note': 'DEJAR EN $ 9'}],
                                                           'price_requests': [{'kind': 'descuento', 'evidence': 'HAZME UN DESCUENTO'}],
                                                           'unmatched': [{'evidence': 'TAMBOR', 'note': 'PAGA EN DÓLARES'}]})
        self.assertEqual((summary, rejected, [(item['kind'], item['text']) for item in proposals]),
                         ('', 4, [('quantity_change', ''), ('price_request', ''), ('question', '¿CUÁNDO LO NECESITAS?')]))
        # Terms reach the client's quotation once applied: a price in any of these forms is dropped too.
        priced = ['PRECIO FINAL 45 POR UNIDAD', 'DESCUENTO DEL 10 POR CIENTO', 'A 38 C/U', 'TOTAL USD45', 'PAGO DE 45USD', 'DIEZ POR CIENTO MENOS', '50 CENTAVOS']
        (_, proposals, rejected), _ = self.validate({**base, 'terms_items': [{'kind': 'otro', 'text': text, 'evidence': 'TAMBOR'} for text in priced]
                                                     + [{'kind': 'entrega', 'text': 'ENTREGA EN 2 DÍAS EN EL TALLER DE PABLO', 'evidence': 'TAMBOR'}]})
        self.assertEqual((rejected, [item['text'] for item in proposals]), (len(priced), ['ENTREGA EN 2 DÍAS EN EL TALLER DE PABLO']))

    def test_invalid_output_fails_the_run_records_usage_and_leaves_the_draft_untouched(self):
        order = self.reviewed()
        self.save(order, 0, lines=[{'order_line_id': str(order.lines.get(supplier_item=self.item_a).pk), 'quantity': 3}])
        invented = {**OUTPUT, 'proposals': [{'i': 0, 'kind': 'quantity_change', 'quantity': 1, 'evidence': 'QUIERO SOLO UNO'}]}
        with self.provider(invented):
            response = self.start(order, version=1)
        self.assertEqual(response.status_code, 502)
        run = QuoteAssistantRun.objects.get()
        self.assertEqual((run.status, run.billable, run.output, run.metrics['failure'], run.claim_expires_at), ('failed', True, {}, 'invalid_output', None))
        self.assertEqual(AIUsageRecord.objects.values('calls', 'failed_calls', 'input_tokens', 'output_tokens', 'cost_micro_usd').get(),
                         {'calls': 0, 'failed_calls': 1, 'input_tokens': 1200, 'output_tokens': 300, 'cost_micro_usd': 2700})
        draft = DealQuotationDraft.objects.get()
        self.assertEqual((draft.draft_version, sorted(draft.lines.values_list('quantity', 'quantity_source', 'note'))), (1, [(2, 'requested', ''), (3, 'requested', '')]))

    def test_running_never_changes_the_draft_and_applying_goes_through_the_draft_with_deterministic_prices(self):
        list_ = self.general()
        self.price(list_, self.item_a, Decimal('10.00'))
        self.price(list_, self.item_a2, Decimal('4.00'))
        PricingRule.objects.create(supplier=self.supplier_a, name='VOLUMEN 3+', scope='all', target='all', kind='discount', value=Decimal('10.00'), min_quantity=3,
                                   created_by=self.seller_a, updated_by=self.seller_a)
        order = self.reviewed()
        response = self.completed(order)
        self.assertFalse(DealQuotationDraft.objects.exists(), 'Running the assistant never creates or changes the draft.')
        run = response.data['latest_run']
        self.assertEqual((run['summary'], run['cached'], run['stale'], run['rejected_items'], response.data['runs_left']),
                         ('EL CLIENTE NECESITA MENOS TAMBORES, ACEPTA ALTERNO Y PIDE ENTREGA MAÑANA.', False, False, 0, 4))
        found = self.proposals(response)
        self.assertEqual({kind: (item['codigo'], item['quantity'], item['text'], item['evidence'], item['can_apply'], item['decision']) for kind, item in found.items()}, {
            'quantity_change': (A, 2, '', 'SOLO NECESITO 2 DEL TAMBOR', True, None), 'line_note': (A2, None, 'ACEPTA MARCA ALTERNA', 'ACEPTO ALTERNO EN LA ZAPATA', True, None),
            'terms': ('', None, 'ENTREGA MAÑANA EN EL TALLER DEL CLIENTE', 'ENTREGA MAÑANA EN EL TALLER', True, None),
            'price_request': (A, None, '', 'HAZME UN DESCUENTO', False, None), 'unmatched': ('', None, 'FILTRO DE ACEITE', 'TAMBIEN QUIERO UN FILTRO DE ACEITE', False, None),
            'question': ('', None, '¿CONFIRMAS QUE ACEPTAS MARCA ALTERNA EN LA ZAPATA?', '', False, None)})
        draft = self.get_draft(order)
        lines = self.by_code(draft)
        self.assertEqual((lines[A]['quantity'], lines[A]['unit_price'], lines[A]['price_source']), (3, '9.00', 'engine'))
        self.assertEqual([(item['code'], item['severity']) for item in lines[A]['exceptions'] if item['code'] == 'client_price_request'], [('client_price_request', 'info')])
        self.assertIn('«HAZME UN DESCUENTO». Los precios salen solo de tus listas y reglas.', lines[A]['exceptions'][-1]['message'])
        self.assertEqual([item['code'] for item in draft['order_exceptions']], ['no_profile', 'assistant_unmatched'])
        self.assertEqual(draft['assistant']['latest_run']['id'], run['id'])
        # Price requests, mentions outside the order and questions can never be applied.
        for kind in ['price_request', 'unmatched', 'question']:
            refused = self.decide(order, run['id'], 0, [(found[kind]['id'], 'apply')])
            self.assertEqual(refused.status_code, 400, kind)
        self.assertIn('los precios salen solo de tus listas y reglas', str(self.decide(order, run['id'], 0, [(found['price_request']['id'], 'apply')]).data))
        self.assertFalse(DealQuotationDraft.objects.exists())
        first_save, choices = uuid.uuid4(), [(found['quantity_change']['id'], 'apply'), (found['line_note']['id'], 'apply'), (found['terms']['id'], 'apply'),
                                             (found['unmatched']['id'], 'dismiss')]
        applied = self.decide(order, run['id'], 0, choices, first_save)
        self.assertEqual(applied.status_code, 200, applied.data)
        lines = self.by_code(applied.data)
        # The deterministic engine reprices the assistant's quantity: below the 3-unit rule the list price applies again.
        self.assertEqual((lines[A]['quantity'], lines[A]['quantity_source'], lines[A]['unit_price'], lines[A]['price_source']), (2, 'assistant', '10.00', 'engine'))
        self.assertEqual(applied.data['repriced'], [{'order_line_id': lines[A]['order_line_id'], 'previous_unit_price': '9.00', 'unit_price': '10.00', 'reason': 'quantity'}])
        self.assertEqual((lines[A2]['quantity'], lines[A2]['note'], lines[A2]['unit_price']), (2, 'ACEPTA MARCA ALTERNA', '4.00'))
        self.assertEqual((applied.data['terms'], applied.data['draft_version']), ('ENTREGA MAÑANA EN EL TALLER DEL CLIENTE', 1))
        self.assertEqual([item['code'] for item in applied.data['order_exceptions']], ['no_profile'])
        decisions = {item['kind']: item['decision'] for item in applied.data['assistant']['latest_run']['proposals']}
        self.assertEqual(decisions, {'quantity_change': 'applied', 'line_note': 'applied', 'terms': 'applied', 'unmatched': 'dismissed', 'price_request': None, 'question': None})
        event = PricingAuditEvent.objects.get(kind='assistant_applied')
        self.assertEqual(event.payload['applied'], [found['quantity_change']['id'], found['line_note']['id'], found['terms']['id']])
        self.assertNotIn('TAMBOR', json.dumps(event.payload))
        # Dismissing the price request clears its info alert and never touches the draft.
        dismissed = self.decide(order, run['id'], 1, [(found['price_request']['id'], 'dismiss')])
        self.assertEqual((dismissed.status_code, dismissed.data['draft_version']), (200, 1))
        self.assertNotIn('client_price_request', [item['code'] for item in self.by_code(dismissed.data)[A]['exceptions']])
        # Retries: the same save_id returns the draft (another use of it is refused), a repeated decision is a no-op, an opposite one is
        # refused and a stale draft version conflicts.
        replayed = self.decide(order, run['id'], 0, choices, first_save)
        self.assertEqual((replayed.status_code, replayed.data['draft_version']), (200, 1))
        self.assertEqual(self.decide(order, run['id'], 1, [(found['question']['id'], 'dismiss')], first_save).status_code, 409)
        repeated = self.decide(order, run['id'], 1, [(found['question']['id'], 'dismiss'), (found['quantity_change']['id'], 'apply')])
        self.assertEqual((repeated.status_code, repeated.data['draft_version']), (200, 1))
        self.assertEqual(self.decide(order, run['id'], 1, [(found['quantity_change']['id'], 'dismiss')]).status_code, 409)
        self.assertEqual(DealQuotationDraft.objects.get().draft_version, 1)
        self.save(order, 1, lines=[{'order_line_id': lines[A]['order_line_id'], 'quantity': 3}])
        self.assertEqual(self.price_of(order, A), ('9.00', 'engine', 'manual'))
        # The same texts again are a free cache hit that never re-offers what the supplier decided: its proposals keep their decisions.
        rerun = self.completed(order, 2)
        self.assertEqual(QuoteAssistantRun.objects.filter(cached=True).count(), 1, 'The second interpretation of the same texts was free.')
        self.assertEqual({item['kind']: item['decision'] for item in rerun.data['latest_run']['proposals']},
                         {'quantity_change': 'applied', 'line_note': 'applied', 'terms': 'applied', 'unmatched': 'dismissed', 'price_request': 'dismissed', 'question': 'dismissed'})
        noop = self.decide(order, rerun.data['latest_run']['id'], 2, [(self.proposals(rerun)['terms']['id'], 'apply')])
        self.assertEqual((noop.status_code, noop.data['draft_version'], noop.data['terms']), (200, 2, 'ENTREGA MAÑANA EN EL TALLER DEL CLIENTE'))
        self.assertEqual(self.price_of(order, A), ('9.00', 'engine', 'manual'))
        self.assertNotIn('client_price_request', [item['code'] for item in self.by_code(self.get_draft(order))[A]['exceptions']])
        # New client text is a new interpretation: its quantity is offered again, and a stale draft version conflicts.
        self.say(order, 'MEJOR SOLO 2 DEL TAMBOR')
        with self.provider({**OUTPUT, 'proposals': [{'i': 0, 'kind': 'quantity_change', 'quantity': 2, 'evidence': 'MEJOR SOLO 2 DEL TAMBOR'}]}):
            rerun = self.start(order, 2)
        again = self.proposals(rerun)
        self.assertEqual((again['quantity_change']['decision'], again['terms']['decision']), (None, None))
        stale = self.decide(order, rerun.data['latest_run']['id'], 1, [(again['quantity_change']['id'], 'apply')])
        self.assertEqual((stale.status_code, stale.data['draft']['draft_version']), (409, 2))
        # Its terms item is already in the draft (applied from the first interpretation), so applying it never repeats the text.
        applied_again = self.decide(order, rerun.data['latest_run']['id'], 2, [(again['quantity_change']['id'], 'apply'), (again['terms']['id'], 'apply')])
        self.assertEqual((applied_again.status_code, applied_again.data['terms']), (200, 'ENTREGA MAÑANA EN EL TALLER DEL CLIENTE'))
        self.assertEqual(self.price_of(order, A), ('10.00', 'engine', 'assistant'))
        # Publishing links the applying run in the supplier-only trace; prices are re-derived on the server.
        draft = applied_again.data
        self.client.force_authenticate(self.seller_a)
        published = self.action(order, 'quote', currency=draft['currency'], terms=draft['terms'], draft_version=draft['draft_version'],
                                lines=[{'order_line_id': line['order_line_id'], 'quantity': line['quantity'], 'unit_price': line['unit_price']} for line in draft['lines']])
        self.assertEqual(published.status_code, 200, published.data)
        audits = {audit.line.order_line.codigo: audit for audit in DealQuotationLineAudit.objects.select_related('line__order_line')}
        self.assertEqual((audits[A].assistant_run_id, audits[A].quantity_source, audits[A].price_source), (uuid.UUID(rerun.data['latest_run']['id']), 'assistant', 'engine'))
        self.assertEqual(audits[A2].assistant_run_id, uuid.UUID(run['id']))
        quotation = order.quotations.get()
        trace = self.client.get(f'/api/v1/accounts/{self.supplier_a.pk}/requests/{order.pk}/quotations/{quotation.pk}/trace/').data
        self.assertEqual({line['codigo']: line['assistant_run_id'] for line in trace['lines']}, {A: rerun.data['latest_run']['id'], A2: run['id']})

    def price_of(self, order, codigo):
        line = self.by_code(self.get_draft(order))[codigo]
        return line['unit_price'], line['price_source'], line['quantity_source']

    def test_discarding_the_draft_resets_the_decisions_of_the_revision(self):
        order = self.reviewed()
        response = self.completed(order)
        found = self.proposals(response)
        self.assertEqual(self.decide(order, response.data['latest_run']['id'], 0, [(found['quantity_change']['id'], 'apply')]).status_code, 200)
        self.client.force_authenticate(self.seller_a)
        self.assertEqual(self.client.post(self.draft_url(order, suffix='discard/'), {'save_id': str(uuid.uuid4()), 'expected_draft_version': 1}, format='json').status_code, 200)
        self.assertEqual(QuoteAssistantRun.objects.get().decisions, {})
        self.assertEqual(self.by_code(self.get_draft(order))[A]['quantity'], 3)
        # Returning the same quotation also discards the draft, so the decisions taken for that revision start over too.
        draft = self.decide(order, response.data['latest_run']['id'], 0, [(found['quantity_change']['id'], 'apply')]).data
        draft = self.save(order, draft['draft_version'], lines=[{'order_line_id': line['order_line_id'], 'unit_price': '5.00'} for line in draft['lines']]).data
        self.client.force_authenticate(self.seller_a)
        self.assertEqual(self.action(order, 'quote', currency=draft['currency'], terms=draft['terms'], draft_version=draft['draft_version'],
                                     lines=[{'order_line_id': line['order_line_id'], 'quantity': line['quantity'], 'unit_price': '5.00'} for line in draft['lines']]).status_code, 200)
        self.client.force_authenticate(self.buyer)
        self.assertEqual(self.action(order, 'request_adjustment', quotation_id=str(order.quotations.get().pk), reason='SOLO NECESITO 2 DEL TAMBOR').status_code, 200)
        adjusted = self.completed(order)
        proposal = self.proposals(adjusted)['quantity_change']['id']
        self.assertEqual(self.decide(order, adjusted.data['latest_run']['id'], 0, [(proposal, 'apply')]).status_code, 200)
        self.assertEqual(QuoteAssistantRun.objects.get(pk=adjusted.data['latest_run']['id']).decisions[proposal]['action'], 'applied')
        self.client.force_authenticate(self.seller_a)
        self.assertEqual(self.action(order, 'return_quote', quotation_id=str(order.quotations.get().pk)).status_code, 200)
        self.assertEqual(QuoteAssistantRun.objects.get(pk=adjusted.data['latest_run']['id']).decisions, {})

    def test_stored_proposals_cannot_be_applied_or_dismissed_once_the_owner_disables_the_assistant(self):
        order = self.reviewed()
        response = self.completed(order)
        run_id, found = response.data['latest_run']['id'], self.proposals(response)
        SupplierPricingSettings.objects.filter(supplier=self.supplier_a).update(assistant_enabled=False)
        for action in ['apply', 'dismiss']:
            refused = self.decide(order, run_id, 0, [(found['terms']['id'], action)])
            self.assertEqual((refused.status_code, refused.data['detail']), (403, ASSISTANT_OFF_DETAIL))
        self.assertFalse(DealQuotationDraft.objects.exists())
        self.assertEqual(QuoteAssistantRun.objects.get().decisions, {})
        self.assertFalse(PricingAuditEvent.objects.filter(kind='assistant_applied').exists())
        # Turned on again, the same stored proposal applies normally.
        SupplierPricingSettings.objects.filter(supplier=self.supplier_a).update(assistant_enabled=True)
        applied = self.decide(order, run_id, 0, [(found['terms']['id'], 'apply')])
        self.assertEqual((applied.status_code, applied.data['terms']), (200, 'ENTREGA MAÑANA EN EL TALLER DEL CLIENTE'))

    def test_a_cache_hit_makes_no_call_and_the_same_run_id_is_idempotent(self):
        order = self.reviewed()
        run_id = uuid.uuid4()
        with self.provider():
            first = self.start(order, run_id=run_id)
            replay = self.start(order, run_id=run_id)
            cached = self.start(order)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual((first.status_code, replay.status_code, cached.status_code), (200, 200, 200))
        self.assertEqual(replay.data['latest_run']['id'], str(run_id))
        self.assertTrue(cached.data['latest_run']['cached'])
        self.assertNotEqual(cached.data['latest_run']['id'], str(run_id))
        self.assertEqual(cached.data['latest_run']['proposals'], first.data['latest_run']['proposals'])
        self.assertEqual(cached.data['runs_left'], 4, 'Cached runs are free and outside the caps.')
        self.assertEqual(AIUsageRecord.objects.values('calls', 'cached_calls', 'cost_micro_usd').get(), {'calls': 1, 'cached_calls': 1, 'cost_micro_usd': 2700})
        # New client text is a new input: the run is marked stale and the next interpretation calls the provider again.
        self.say(order, 'MEJOR SOLO 1 DEL TAMBOR')
        self.assertTrue(self.get_draft(order)['assistant']['latest_run']['stale'])
        with self.provider({**OUTPUT, 'proposals': [{'i': 0, 'kind': 'quantity_change', 'quantity': 1, 'evidence': 'MEJOR SOLO 1 DEL TAMBOR'}]}):
            fresh = self.start(order)
        self.assertEqual((fresh.status_code, fresh.data['latest_run']['stale'], len(self.calls)), (200, False, 2))
        self.assertEqual(self.start(order, run_id=run_id, account=self.supplier_b, user=self.seller_b).status_code, 404)

    def test_caps_return_429_and_switches_return_403_or_503_without_calling_the_provider(self):
        order = self.reviewed()
        with override_settings(QUOTE_ASSISTANT_RUNS_PER_REVISION=2), self.provider():
            self.assertEqual(self.start(order).status_code, 200)
            self.say(order, 'OTRA COSA')
            self.assertEqual(self.start(order).status_code, 200)
            self.say(order, 'Y OTRA MAS')
            capped = self.start(order)
        self.assertEqual((capped.status_code, len(self.calls)), (429, 2))
        self.assertIn('interpretaciones de esta versión', capped.data['detail'])
        self.assertEqual(self.get_draft(order)['assistant']['runs_left'], 3)
        second = self.reviewed(lines=[(self.item_a2, 1)])
        with override_settings(QUOTE_ASSISTANT_DAILY_RUNS_PER_ACCOUNT=2), self.provider():
            daily = self.start(second)
        self.assertEqual((daily.status_code, len(self.calls)), (429, 2))
        self.assertIn('límite diario', daily.data['detail'])
        AIUsageRecord.objects.all().delete()
        QuoteAssistantRun.objects.all().delete()
        today = timezone.localdate()
        AIUsageRecord.objects.create(account=self.supplier_a, feature='quote_assistant', day=today, cost_micro_usd=9_000)
        with override_settings(QUOTE_ASSISTANT_MONTHLY_USD=Decimal('0.01')), self.provider():
            monthly = self.start(order)
        self.assertEqual((monthly.status_code, monthly.data['detail']), (429, 'El asistente alcanzó su límite de uso. El borrador y tus precios siguen disponibles.'))
        AIUsageRecord.objects.create(account=None, feature='catalog_classification', day=today, cost_micro_usd=990_000)
        with override_settings(AI_MONTHLY_BUDGET_USD=Decimal('1')), self.provider():
            platform = self.start(order)
        self.assertEqual(platform.status_code, 429)
        AIUsageRecord.objects.all().delete()
        AIUsageRecord.objects.create(account=self.supplier_a, feature='quote_assistant', day=today.replace(day=1) - timedelta(days=1), cost_micro_usd=50_000_000)
        with self.provider():
            self.assertEqual(self.start(order).status_code, 200, 'Last month\'s spend does not count.')
        self.assertEqual(len(self.calls), 3)
        for label, overrides, expected in [('presupuesto 0', {'QUOTE_ASSISTANT_MONTHLY_USD': Decimal('0')}, 403), ('sin clave', {'GEMINI_API_KEY': ''}, 503)]:
            with self.subTest(label), override_settings(**overrides), self.provider():
                self.say(order, f'MENSAJE {label.upper()}')
                self.assertEqual(self.start(order).status_code, expected)
                self.assertEqual(self.get_draft(order)['assistant']['configured'], False)
        SupplierPricingSettings.objects.filter(supplier=self.supplier_a).update(assistant_enabled=False)
        with self.provider():
            off = self.start(order)
        self.assertEqual((off.status_code, len(self.calls)), (403, 3))
        self.assertEqual(self.get_draft(order)['assistant'], {'configured': True, 'enabled': False, 'runs_left': 0, 'unavailable': '', 'latest_run': None})

    def test_nothing_to_interpret_or_too_many_lines_or_a_closed_order_is_refused_before_any_call(self):
        order = self.reviewed(notes='')
        self.assertEqual(self.get_draft(order)['assistant']['unavailable'], 'El cliente no escribió notas ni mensajes que el asistente pueda interpretar.')
        with self.provider():
            self.assertEqual(self.start(order).status_code, 409)
            self.say(order, 'SOLO PROVEEDOR', supplier=True)
            self.assertEqual(self.start(order).status_code, 409, 'Supplier messages alone are nothing to interpret.')
            with mock.patch('mall.quote_assistant.MAX_LINES', 1), mock.patch('mall.quote_drafts.MAX_LINES', 1):
                self.say(order, 'NECESITO TODO')
                self.assertEqual(self.get_draft(order)['assistant']['unavailable'], 'La orden tiene demasiados artículos para el asistente.')
                self.assertEqual(self.start(order).data['detail'], 'La orden tiene demasiados artículos para el asistente.')
            self.client.force_authenticate(self.buyer)
            pending = SupplierRequest.objects.get(pk=self.submit(self.payload([(self.item_b, 1)], notes='URGENTE')).data['requests'][0]['id'])
            self.assertEqual(self.start(pending, user=self.seller_b, account=self.supplier_b).status_code, 409)
            self.assertEqual(self.start(order, version=3).status_code, 409)
        self.assertEqual(self.calls, [])

    def test_a_concurrent_claim_conflicts_and_a_provider_error_releases_the_claim_and_leaves_the_draft(self):
        order = self.reviewed()
        self.save(order, 0, terms='RETIRO EN SUCURSAL')
        claim = QuoteAssistantRun.objects.create(order=order, supplier=self.supplier_a, input_fingerprint='x' * 64, status='claimed', created_by=self.seller_a,
                                                 claim_expires_at=timezone.now() + timedelta(seconds=60), estimated_cost_micro_usd=10)
        with self.provider():
            busy = self.start(order, version=1)
        self.assertEqual((busy.status_code, busy.data['detail'], self.calls), (409, 'El asistente ya está interpretando esta orden. Espera unos segundos.', []))
        QuoteAssistantRun.objects.filter(pk=claim.pk).update(claim_expires_at=timezone.now() - timedelta(seconds=1))
        with self.provider(error=http_failure(429)):
            failed = self.start(order, version=1)
        self.assertEqual(failed.status_code, 502)
        self.assertIn('El borrador no cambió', failed.data['detail'])
        claim.refresh_from_db()
        self.assertEqual(claim.status, 'failed', 'An expired claim is released.')
        run = QuoteAssistantRun.objects.exclude(pk=claim.pk).get()
        self.assertEqual((run.status, run.billable, run.claim_expires_at, run.metrics['failure']), ('failed', False, None, 'provider'))
        self.assertFalse(QuoteAssistantRun.objects.filter(status='claimed').exists())
        draft = DealQuotationDraft.objects.get()
        self.assertEqual((draft.draft_version, draft.terms), (1, 'RETIRO EN SUCURSAL'))
        self.assertEqual(AIUsageRecord.objects.values('failed_calls', 'cost_micro_usd').get(), {'failed_calls': 1, 'cost_micro_usd': 0})
        with self.provider():
            self.assertEqual(self.start(order, version=1).status_code, 200, 'The released claim lets the next run start.')
            self.assertEqual(self.start(order, version=1, run_id=run.pk).status_code, 502, 'A failed run_id replays its failure.')
        self.assertEqual(self.get_draft(order)['assistant']['runs_left'], 4, 'HTTP errors from the provider do not use up the revision\'s runs.')
        # A truncated or malformed answer (raised without an HTTP error) may have been billed: it is charged at its estimate and counted.
        self.say(order, 'MEJOR SOLO 1 DEL TAMBOR')
        with self.provider(error=ClassificationProviderError('La IA no pudo completar este lote.')):
            self.assertEqual(self.start(order, version=1).status_code, 502)
        truncated = QuoteAssistantRun.objects.filter(status='failed', metrics__failure='provider', billable=True).get()
        self.assertTrue(truncated.estimated_cost_micro_usd > 0 and truncated.metrics['estimated'])
        self.assertEqual(truncated.metrics['cost_micro_usd'], truncated.estimated_cost_micro_usd)
        self.assertEqual(AIUsageRecord.objects.values_list('cost_micro_usd', flat=True).get(), 2700 + truncated.estimated_cost_micro_usd)
        self.assertEqual(self.get_draft(order)['assistant']['runs_left'], 3)
        with override_settings(QUOTE_ASSISTANT_RUNS_PER_REVISION=2), self.provider(error=ClassificationProviderError()):
            self.assertEqual(self.start(order, version=1).status_code, 429, 'Failing answers can never spend past the caps.')

    def test_access_is_supplier_only_reads_write_nothing_and_starting_runs_is_throttled(self):
        order = self.reviewed()
        self.completed(order)
        self.client.force_authenticate(self.seller_a)
        with CaptureQueriesContext(connection) as queries:
            state = self.client.get(self.assistant_url(order))
        self.assertEqual(state.status_code, 200)
        self.assertEqual([query['sql'] for query in queries.captured_queries if not query['sql'].lstrip().upper().startswith('SELECT')], [])
        self.assertEqual(state.data['latest_run']['summary'], OUTPUT['summary'])
        run_id = state.data['latest_run']['id']
        for label, user, account, expected in [('cliente', self.buyer, self.client_account, 403), ('otro proveedor', self.seller_b, self.supplier_b, 404),
                                               ('no miembro', self.seller_b, self.supplier_a, 404), ('superusuario', self.root, self.supplier_a, 404)]:
            with self.subTest(label):
                self.client.force_authenticate(user)
                self.assertEqual(self.client.get(self.assistant_url(order, account)).status_code, expected)
                self.assertEqual(self.start(order, user=user, account=account).status_code, expected)
                self.assertEqual(self.client.post(self.assistant_url(order, account, f'{run_id}/decisions/'), {
                    'save_id': str(uuid.uuid4()), 'expected_draft_version': 0, 'decisions': [{'proposal_id': 'p1', 'action': 'apply'}]}, format='json').status_code, expected)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.assistant_url(order)).status_code, 401)
        cache.clear()
        with mock.patch.object(QuoteAssistantThrottle, 'rate', '2/min'), self.provider():
            codes = [self.start(order).status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])

    def test_logs_never_carry_notes_or_chat_text(self):
        capture, root = LogCapture(), logging.getLogger()
        level = root.level
        root.addHandler(capture)
        root.setLevel(logging.DEBUG)
        try:
            order = self.reviewed(notes='NOTA-PRIVADA-DEL-CLIENTE 2 TAMBORES')
            self.say(order, 'CHAT-PRIVADO-DEL-CLIENTE')
            with self.provider({**OUTPUT, 'summary': 'RESUMEN-PRIVADO', 'proposals': [], 'terms_items': [], 'price_requests': [], 'unmatched': []}):
                self.assertEqual(self.start(order).status_code, 200)
            self.say(order, 'OTRO-CHAT-PRIVADO')
            with self.provider(error=ClassificationProviderError()):
                self.assertEqual(self.start(order).status_code, 502)
            with self.provider({**OUTPUT, 'proposals': [{'i': 0, 'kind': 'quantity_change', 'quantity': 7, 'evidence': 'NOTA-PRIVADA-DEL-CLIENTE'}]}):
                self.assertEqual(self.start(order).status_code, 502)
        finally:
            root.removeHandler(capture)
            root.setLevel(level)
        logged = '\n'.join(capture.lines)
        self.assertIn('Quote assistant run completed: model=gemini-test-model lines=2', logged)
        self.assertIn('Quote assistant run failed: model=gemini-test-model reason=invalid_output', logged)
        for secret in ['NOTA-PRIVADA', 'CHAT-PRIVADO', 'OTRO-CHAT', 'RESUMEN-PRIVADO', 'TAMBOR', 'test-key-not-real', 'CLIENTE ORIGINAL']:
            self.assertNotIn(secret, logged)
        for event in PricingAuditEvent.objects.filter(kind='assistant_run'):
            self.assertNotIn('PRIVAD', json.dumps(event.payload))

    def test_the_deal_chat_tells_both_parties_when_the_supplier_enabled_the_assistant(self):
        order = self.reviewed()
        for enabled in [True, False]:
            SupplierPricingSettings.objects.filter(supplier=self.supplier_a).update(assistant_enabled=enabled)
            for user, account in [(self.buyer, self.client_account), (self.seller_a, self.supplier_a)]:
                self.client.force_authenticate(user)
                response = self.client.get(self.messages(order, account))
                self.assertEqual((set(response.data), response.data['assistant_notice']), ({'results', 'cursor', 'has_more', 'has_earlier', 'assistant_notice'}, enabled))


@skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL row locks.')
@override_settings(**ENABLED)
class ConcurrentQuoteAssistantTests(APITransactionTestCase):
    setUp = deal_tests.ConcurrentDealTests.setUp
    call = deal_tests.ConcurrentDealTests.call

    def test_the_provider_is_called_without_the_order_lock_and_a_second_run_gets_409(self):
        cache.clear()
        SupplierPricingSettings.objects.create(supplier=self.supplier, assistant_enabled=True)
        _, result = self.call(self.buyer, f'/api/v1/accounts/{self.client_account.pk}/requests/',
                              {'submission_id': str(uuid.uuid4()), 'notes': 'SOLO NECESITO 1', 'lines': [{'supplier_item_id': str(self.item.pk), 'quantity': 2}]})
        order = SupplierRequest.objects.get(pk=result['requests'][0]['id'])
        self.call(self.seller, f'/api/v1/accounts/{self.supplier.pk}/requests/{order.pk}/review/', {})
        path, started, release = f'/api/v1/accounts/{self.supplier.pk}/requests/{order.pk}/draft/assistant/', threading.Event(), threading.Event()
        output = {'summary': '', 'proposals': [{'i': 0, 'kind': 'quantity_change', 'quantity': 1, 'evidence': 'SOLO NECESITO 1'}], 'terms_items': [],
                  'price_requests': [], 'unmatched': [], 'questions': []}
        def fake(payload, count, **kwargs):
            started.set()
            release.wait(10)
            return copy.deepcopy(output), dict(USAGE)
        with mock.patch('mall.quote_assistant.generate_json', side_effect=fake), ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.call, self.seller, path, {'run_id': str(uuid.uuid4()), 'expected_draft_version': 0})
            self.assertTrue(started.wait(10))
            # The order row is free while the provider works: a draft save succeeds and a second run is refused at once.
            line = order.lines.get()
            saved = self.call(self.seller, f'/api/v1/accounts/{self.supplier.pk}/requests/{order.pk}/draft/', {
                'save_id': str(uuid.uuid4()), 'expected_draft_version': 0, 'lines': [{'order_line_id': str(line.pk), 'note': 'REVISAR'}]})
            second = self.call(self.seller, path, {'run_id': str(uuid.uuid4()), 'expected_draft_version': 1})
            release.set()
            first = first.result()
        self.assertEqual((saved[0], second[0], first[0]), (200, 409, 200))
        self.assertEqual(second[1]['detail'], 'El asistente ya está interpretando esta orden. Espera unos segundos.')
        self.assertEqual(list(QuoteAssistantRun.objects.values_list('status', flat=True)), ['completed'])
        self.assertEqual(DealQuotationDraft.objects.get().lines.get().quantity, 2, 'The run stored proposals; the draft only changes on a click.')

    def test_runs_on_different_orders_cannot_both_pass_the_daily_cap(self):
        cache.clear()
        SupplierPricingSettings.objects.create(supplier=self.supplier, assistant_enabled=True)
        orders = []
        for _ in range(2):
            _, result = self.call(self.buyer, f'/api/v1/accounts/{self.client_account.pk}/requests/',
                                  {'submission_id': str(uuid.uuid4()), 'notes': 'SOLO NECESITO 1', 'lines': [{'supplier_item_id': str(self.item.pk), 'quantity': 2}]})
            orders.append(SupplierRequest.objects.get(pk=result['requests'][0]['id']))
            self.call(self.seller, f'/api/v1/accounts/{self.supplier.pk}/requests/{orders[-1].pk}/review/', {})
        output = {'summary': '', 'proposals': [], 'terms_items': [], 'price_requests': [], 'unmatched': [], 'questions': []}
        # Each run waits at the cap check for the other: without the claim lock both would count 0 runs and pass.
        barrier, original = threading.Barrier(2, timeout=2), quote_assistant.check_run_caps
        def meet(*args):
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            return original(*args)
        def start(order):
            return self.call(self.seller, f'/api/v1/accounts/{self.supplier.pk}/requests/{order.pk}/draft/assistant/',
                             {'run_id': str(uuid.uuid4()), 'expected_draft_version': 0})
        with override_settings(QUOTE_ASSISTANT_DAILY_RUNS_PER_ACCOUNT=1), mock.patch('mall.quote_assistant.check_run_caps', side_effect=meet), \
                mock.patch('mall.quote_assistant.generate_json', return_value=(output, dict(USAGE))), ThreadPoolExecutor(max_workers=2) as pool:
            codes = sorted(status for status, _ in pool.map(start, orders))
        self.assertEqual(codes, [200, 429])
        self.assertEqual(QuoteAssistantRun.objects.count(), 1)
