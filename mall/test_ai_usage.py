"""S8 · every AI feature's spend counts against AI_MONTHLY_BUDGET_USD: catalog features record platform usage through the shared transport.

The provider transport is always patched (urlopen); these tests never call Gemini.
"""
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from io import BytesIO
from unittest import mock, skipUnless
from urllib.error import HTTPError, URLError

from django.db import DatabaseError, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings

from .ai_budget import month_spend, record_provider_call
from .catalog_classification import ClassificationProviderError, call_provider, generate_json
from .quote_assistant_models import AIUsageRecord

RATES = {'GEMINI_API_KEY': 'usage-test-key', 'GEMINI_MODEL': 'gemini-test-model', 'GEMINI_INPUT_USD_PER_MTOK': Decimal('1.00'),
         'GEMINI_OUTPUT_USD_PER_MTOK': Decimal('5.00'), 'GEMINI_GROUNDING_USD_PER_CALL': Decimal('0.035')}
SOURCE = [{'sku': 'AI-123456', 'row': 2, 'name': 'FILTRO', 'description': '', 'codes': []}]
TOKENS = {'promptTokenCount': 1000, 'candidatesTokenCount': 200, 'thoughtsTokenCount': 50}


def answer(output=None, usage=TOKENS, finish='STOP'):
    body = {'candidates': [{'finishReason': finish, 'content': {'parts': [{'text': json.dumps(output or {'suggestions': []})}]}}]}
    return BytesIO(json.dumps({**body, **({'usageMetadata': usage} if usage is not None else {})}).encode())


@override_settings(**RATES)
class AIUsageRecordingTests(TestCase):
    def rows(self):
        return list(AIUsageRecord.objects.order_by('feature').values('account', 'feature', 'calls', 'failed_calls', 'input_tokens', 'output_tokens',
                                                                     'thinking_tokens', 'cost_micro_usd'))

    def classify(self, **transport):
        with mock.patch('mall.catalog_classification.urlopen', **transport):
            return call_provider(SOURCE, {}, {'AI-123456': []})

    def test_catalog_calls_add_their_reported_tokens_to_the_platform_usage(self):
        for _ in range(2):
            self.assertEqual(self.classify(return_value=answer()), {'suggestions': []})
        # 1,000 input tokens at US$1 per million plus 250 output and thinking tokens at US$5: 2,250 micro-USD per call.
        self.assertEqual(self.rows(), [{'account': None, 'feature': 'catalog_classification', 'calls': 2, 'failed_calls': 0, 'input_tokens': 2000,
                                        'output_tokens': 400, 'thinking_tokens': 100, 'cost_micro_usd': 4500}])
        self.assertEqual(month_spend(), 4500)

    def test_failed_calls_are_counted_and_charged_only_when_they_may_have_been_billed(self):
        cases = [('http', {'side_effect': HTTPError('https://example.invalid/', 429, 'limit', {}, BytesIO(b''))}, 'límite de solicitudes', False),
                 ('conexión', {'side_effect': URLError('down')}, 'tardó demasiado', False),
                 ('tiempo de espera', {'side_effect': socket.timeout('read')}, 'tardó demasiado', True),
                 ('respuesta truncada', {'return_value': answer(finish='MAX_TOKENS')}, 'no pudo completar', True),
                 ('respuesta ilegible', {'return_value': BytesIO(b'{"candidates": [}')}, 'no devolvió una clasificación válida', True)]
        for label, transport, message, billed in cases:
            with self.subTest(label):
                AIUsageRecord.objects.all().delete()
                with self.assertRaisesRegex(ClassificationProviderError, message):
                    self.classify(**transport)
                [row] = self.rows()
                self.assertEqual((row['calls'], row['failed_calls']), (0, 1))
                self.assertEqual(row['cost_micro_usd'] > 0, billed)
        # A truncated answer reports its tokens: they are charged as reported. A timeout reported nothing: it is charged at the estimate
        # (characters / 4 plus the full 16,000-token output allowance).
        AIUsageRecord.objects.all().delete()
        with self.assertRaises(ClassificationProviderError):
            self.classify(return_value=answer(finish='MAX_TOKENS'))
        self.assertEqual(self.rows()[0]['cost_micro_usd'], 2250)
        AIUsageRecord.objects.all().delete()
        with self.assertRaises(ClassificationProviderError):
            self.classify(side_effect=socket.timeout('read'))
        self.assertGreater(self.rows()[0]['cost_micro_usd'], 16000 * 5)

    def test_grounded_searches_add_the_grounding_fee_and_calls_without_a_feature_record_nothing(self):
        payload = {'contents': [], 'tools': [{'google_search': {}}], 'generationConfig': {'temperature': 0, 'maxOutputTokens': 4000}}
        with mock.patch('mall.catalog_classification.urlopen', return_value=answer({'text': 'INFORME'}, {'promptTokenCount': 100, 'candidatesTokenCount': 100})):
            generate_json(payload, 1, with_grounding=True, raw_text=True, feature='oem_lookup')
        self.assertEqual([(row['feature'], row['calls'], row['cost_micro_usd']) for row in self.rows()], [('oem_lookup', 1, 100 + 500 + 35_000)])
        # The quotation assistant calls the transport without a feature and records its own usage, so nothing is counted twice.
        with mock.patch('mall.catalog_classification.urlopen', return_value=answer()):
            generate_json({'contents': []}, 1, with_usage=True)
        self.assertEqual(AIUsageRecord.objects.count(), 1)

    def test_a_recording_failure_never_changes_the_feature_result_or_error(self):
        with mock.patch('mall.ai_budget.record_usage', side_effect=DatabaseError('usage table unavailable')), self.assertLogs('mall.ai_budget', 'WARNING') as logs:
            self.assertEqual(self.classify(return_value=answer()), {'suggestions': []})
            with self.assertRaisesRegex(ClassificationProviderError, 'temporalmente'):
                self.classify(side_effect=HTTPError('https://example.invalid/', 503, 'down', {}, BytesIO(b'')))
        self.assertEqual(len(logs.output), 2)
        self.assertTrue(all('feature=catalog_classification' in line and 'usage-test-key' not in line for line in logs.output))
        self.assertFalse(AIUsageRecord.objects.exists())


@skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL unique index waits.')
@override_settings(**RATES)
class ConcurrentAIUsageTests(TransactionTestCase):
    def test_two_first_calls_of_the_day_add_up_in_one_platform_row(self):
        usage, inserted, release = {'input_tokens': 1000, 'output_tokens': 200, 'thinking_tokens': 50}, threading.Event(), threading.Event()
        def first():
            try:
                with transaction.atomic():
                    record_provider_call('matching', {'contents': []}, usage)
                    inserted.set()
                    release.wait(10)
            finally:
                connection.close()
        def second():
            try:
                self.assertTrue(inserted.wait(10))
                record_provider_call('matching', {'contents': []}, usage)
            finally:
                connection.close()
        with self.assertNoLogs('mall.ai_budget', 'WARNING'), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(first), pool.submit(second)]
            self.assertTrue(inserted.wait(10))
            # The second call cannot see the first one's uncommitted row: its insert waits on the platform unique index, then adds to that row.
            for _ in range(200):
                with connection.cursor() as cursor:
                    cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'")
                    if cursor.fetchone()[0]:
                        break
                time.sleep(0.05)
            else:
                self.fail('The second usage insert never waited on the first one.')
            release.set()
            for future in futures:
                future.result()
        self.assertEqual(list(AIUsageRecord.objects.values_list('account', 'feature', 'calls', 'failed_calls', 'input_tokens', 'cost_micro_usd')),
                         [(None, 'matching', 2, 0, 2000, 4500)])
