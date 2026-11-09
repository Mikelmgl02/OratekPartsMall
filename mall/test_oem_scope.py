from unittest.mock import patch
from django.test import TestCase
from .models import OEMLookupCache
from .oem_lookup import lookup_oem, validate_comparison, SCOPE_POLICY, RESEARCH_VERSION
from .test_factories import make_line


class OEMScopeTests(TestCase):
    def setUp(self):
        self.part = make_line('BOOT-555-FEB', description='KIT DE BOTA INTERIOR')
        self.url = 'https://manufacturer.example/boot-555'
        self.oem_url = 'https://oem.example/boot-kit'

    def responses(self, relationship='equivalent', basis='same_part', oem_code='04439-55555'):
        row = {'code': oem_code, 'brand': 'TOYOTA', 'relationship': relationship, 'source_url': self.url,
               'reason': 'Kit de bota cruzado explícitamente; el OEM también es un kit de bota, no un semieje.',
               'comparison': {'basis': basis, 'source_item': 'Kit de bota, abrazaderas y grasa',
                              'oem_item': 'Kit de bota interior' if basis == 'same_part' else 'Semieje completo con juntas y botas',
                              'marker': 'none', 'details': 'Cruce de ESTA fila y comparación del contenido de ambas unidades.',
                              'oem_source_url': self.oem_url}}
        report = f"BOOT-555 kit de bota, OEM {oem_code} kit de bota. Leyenda general # assembled unit; la fila de este OEM no tiene marcador. {self.url} {self.oem_url}"
        grounding = {'groundingChunks': [{'web': {'uri': self.url, 'title': 'Fabricante'}}, {'web': {'uri': self.oem_url, 'title': 'Catálogo OEM'}}]}
        return [({'text': report}, {}, grounding), ({'references': [row], 'note': 'Revisar contenido por código.'}, {})]

    @patch('mall.oem_lookup.generate_json')
    def test_boot_kit_same_unit_with_unattached_legend_remains_equivalent(self, provider):
        provider.side_effect = self.responses()
        before = list(self.part.codes.values())
        result = lookup_oem(self.part, company='FEBEST', code=self.part.sku)
        self.assertEqual(result['references'][0]['relationship'], 'equivalent')
        self.assertEqual(result['references'][0]['comparison']['marker'], 'none')
        self.assertEqual(result['query']['code'], 'BOOT-555-FEB')
        self.assertEqual(result['query']['manufacturer_code'], 'BOOT-555')
        for call in provider.call_args_list:
            self.assertIn(SCOPE_POLICY, call.args[0]['systemInstruction']['parts'][0]['text'])
        self.assertEqual(list(self.part.codes.values()), before)
        self.part.refresh_from_db()
        self.assertEqual(self.part.sku, 'BOOT-555-FEB')

    @patch('mall.oem_lookup.generate_json')
    def test_boot_against_full_axle_remains_component(self, provider):
        provider.side_effect = self.responses('component', 'larger_assembly')
        self.assertEqual(lookup_oem(self.part)['references'][0]['relationship'], 'component')

    def test_missing_or_contradictory_per_code_evidence_never_enables_a_proposal(self):
        row = self.responses()[1][0]['references'][0]
        without_comparison = {key: value for key, value in row.items() if key != 'comparison'}
        self.assertEqual(validate_comparison(without_comparison, self.oem_url, [])['relationship'], 'uncertain')
        self.assertEqual(validate_comparison({**row, 'relationship': 'component'}, self.oem_url, [])['relationship'], 'uncertain')
        self.assertEqual(validate_comparison(row, 'no OEM source', [])['relationship'], 'uncertain')

    @patch('mall.oem_lookup.generate_json')
    def test_suffix_is_not_removed_if_manufacturer_does_not_match(self, provider):
        provider.side_effect = self.responses()
        result = lookup_oem(self.part, code=self.part.sku, company='OTHER MAKER')
        self.assertEqual(result['query']['manufacturer_code'], self.part.sku)

    @patch('mall.oem_lookup.generate_json')
    def test_old_semantics_cache_is_not_reused(self, provider):
        import hashlib
        import json
        info = {'code': 'BOOT-555', 'company': 'FEBEST', 'description': self.part.description, 'category': self.part.category, 'subcategory': self.part.subcategory}
        from .oem_lookup import provider_configuration
        old_key = hashlib.sha256(json.dumps({'v': 'oem-research-3', 'model': provider_configuration()[1], **info}, sort_keys=True).encode()).hexdigest()
        OEMLookupCache.objects.create(fingerprint=old_key, result={'references': [], 'note': 'old blanket kit decision'})
        provider.side_effect = self.responses()
        result = lookup_oem(self.part)
        self.assertFalse(result['cached'])
        self.assertEqual(provider.call_count, 2)
        self.assertTrue(OEMLookupCache.objects.filter(pk=old_key).exists())
        self.assertEqual(OEMLookupCache.objects.count(), 2)

    def test_effective_http_payload_uses_updated_policy_in_both_provider_stages(self):
        import json
        from io import BytesIO
        from django.test import override_settings
        research, extraction = self.responses()
        def response(text, grounding=None):
            candidate = {'finishReason': 'STOP', 'content': {'parts': [{'text': text}]}}
            if grounding:
                candidate['groundingMetadata'] = grounding
            return BytesIO(json.dumps({'candidates': [candidate], 'usageMetadata': {}}).encode())
        answers = [response(research[0]['text'], research[2]), response(json.dumps(extraction[0]))]
        with override_settings(GEMINI_API_KEY='isolated-test-key', GEMINI_MODEL='gemini-test-model'), patch('mall.catalog_classification.urlopen', side_effect=answers) as transport:
            result = lookup_oem(self.part, company='FEBEST', code=self.part.sku)
        self.assertEqual(result['references'][0]['relationship'], 'equivalent')
        self.assertEqual(transport.call_count, 2)
        for call in transport.call_args_list:
            body = json.loads(call.args[0].data)
            self.assertIn(SCOPE_POLICY, body['systemInstruction']['parts'][0]['text'])
            self.assertIn('KIT de la MISMA pieza', body['systemInstruction']['parts'][0]['text'])
        search_body = json.loads(transport.call_args_list[0].args[0].data)
        self.assertIn('"manufacturer_code": "BOOT-555"', search_body['contents'][0]['parts'][0]['text'])
        self.assertIn('"code": "BOOT-555-FEB"', search_body['contents'][0]['parts'][0]['text'])
