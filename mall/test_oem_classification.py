from unittest.mock import patch
from django.core import signing
from django.test import override_settings
from rest_framework.test import APITestCase
from . import references
from .models import OEMLookupCache, Reference, PartImage, CatalogIdentityChange, CatalogMerge
from .test_factories import make_line, make_user, make_supplier, make_variant, make_reference, make_client


@override_settings(SEARCH_DOCUMENT_SYNC=True)
class OEMClassificationTests(APITestCase):
    def setUp(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.user = make_user('classifier', superuser=True)
            self.client.force_authenticate(self.user)
            self.line = make_line('CODE-K', description='AXIAL KIA')
            self.old = references.main_of(self.line)
            self.supplier = make_supplier('SUPPLIER')
            self.item = make_variant(self.supplier, 'one', 'CODE-K', 7, line=self.line, actor=self.user)
            self.image = PartImage.objects.create(part=self.line, image='test/photo.jpg', thumbnail='test/thumb.jpg', width=10, height=10, size_bytes=100)
            self.proposal = {'code': 'OEM-123', 'brand': 'KIA', 'relationship': 'equivalent', 'source_url': 'https://example.com/catalog', 'reason': 'TEST EVIDENCE'}
            OEMLookupCache.objects.create(fingerprint='classification-test', result={'references': [self.proposal]})
            self.data = {'research_token': signing.dumps({'part': str(self.line.pk), 'key': 'classification-test', 'version': self.line.version}, salt='oem-classification-research-v1'),
                         'code': 'OEM-123', 'brand': 'KIA', 'expected_version': self.line.version}
            self.url = f'/api/v1/management/lines/{self.line.pk}/oem-classification/'

    def preview(self):
        previous_tipo = self.old.tipo
        result = self.client.post(self.url, self.data, format='json')
        self.assertEqual(result.status_code, 200, result.data)
        self.old.refresh_from_db()
        self.assertEqual(self.old.tipo, previous_tipo)
        self.assertFalse(CatalogIdentityChange.objects.exists())
        return result.data

    def apply(self, preview):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(self.url, {**self.data, 'preview_token': preview['preview_token'], 'confirmed': True}, format='json')

    def test_promote_preserves_reference_variant_stock_image_and_both_search_codes(self):
        before = (self.item.reference_id, list(self.item.ledger.values_list('pk', 'quantity')))
        preview = self.preview()
        self.assertFalse(preview['merge'])
        result = self.apply(preview)
        self.assertEqual(result.status_code, 200, result.data)
        self.line.refresh_from_db(); self.old.refresh_from_db(); self.item.refresh_from_db(); self.image.refresh_from_db()
        self.assertEqual((self.line.sku, self.line.tipo), ('OEM-123', 'OEM'))
        self.assertEqual((self.old.code, self.old.tipo, self.old.is_main), ('CODE-K', 'aftermarket', False))
        self.assertEqual((self.item.reference_id, list(self.item.ledger.values_list('pk', 'quantity'))), before)
        self.assertEqual(self.image.part_id, self.line.pk)
        self.assertTrue(CatalogIdentityChange.objects.filter(part=self.line, previous_sku='CODE-K', sku='OEM-123').exists())
        make_client('CLIENT', self.user)
        for code in ['CODE-K', 'OEM-123']:
            response = self.client.get('/api/v1/management/lines/', {'search': code, 'estado': 'publicada'})
            self.assertEqual([row['sku'] for row in response.data['results']], ['OEM-123'])
            public = self.client.get('/api/v1/catalog/', {'search': code})
            self.assertEqual(public.status_code, 200, public.data)
            self.assertEqual([row['sku'] for row in public.data['results']], ['OEM-123'])

    def test_existing_oem_is_merged_without_losing_items_images_or_supplier_scope(self):
        target = make_line('OEM-123', description='AXIAL KIA', is_OEM=True)
        references.update(references.main_of(target), brand='KIA')
        scoped = make_reference(self.line, 'PRIVATE-CODE', tipo='interno', supplier=self.supplier)
        preview = self.preview()
        self.assertTrue(preview['merge'])
        self.assertEqual(preview['target']['id'], str(target.pk))
        result = self.apply(preview)
        self.assertEqual(result.status_code, 200, result.data)
        self.line.refresh_from_db(); self.old.refresh_from_db(); self.item.refresh_from_db(); self.image.refresh_from_db(); scoped.refresh_from_db()
        self.assertEqual(self.line.merged_into_id, target.pk)
        self.assertEqual((self.item.part_id, self.image.part_id, self.old.part_id), (target.pk, target.pk, target.pk))
        self.assertEqual(self.old.tipo, 'aftermarket')
        self.assertEqual((scoped.tipo, scoped.supplier_id, scoped.part_id), ('interno', self.supplier.pk, target.pk))
        self.assertEqual(CatalogMerge.objects.count(), 1)

    def test_changed_reference_refuses_without_partial_writes(self):
        preview = self.preview()
        references.update(self.old, notes='concurrent edit')
        result = self.apply(preview)
        self.assertEqual(result.status_code, 409, result.data)
        self.line.refresh_from_db(); self.old.refresh_from_db()
        self.assertEqual((self.line.sku, self.old.tipo), ('CODE-K', 'otro'))
        self.assertFalse(Reference.objects.filter(code='OEM-123').exists())

    def test_merge_failure_rolls_back_reclassification(self):
        target = make_line('OEM-123', is_OEM=True)
        preview = self.preview()
        from .line_merge import MergeGuard
        with patch('mall.oem_classification.line_merge.merge_lines', side_effect=MergeGuard('Conflicto de ficha técnica', 'subgrupo')):
            result = self.apply(preview)
        self.assertEqual(result.status_code, 400, result.data)
        self.old.refresh_from_db(); self.line.refresh_from_db()
        self.assertEqual((self.old.tipo, self.line.sku), ('otro', 'CODE-K'))
        self.assertFalse(CatalogMerge.objects.exists())

    def test_uncertain_and_forged_proposals_and_unconfirmed_application_are_refused(self):
        preview = self.preview()
        result = self.client.post(self.url, {**self.data, 'preview_token': preview['preview_token']}, format='json')
        self.assertEqual(result.status_code, 400)
        for relationship in ['uncertain', 'component']:
            OEMLookupCache.objects.filter(pk='classification-test').update(result={'references': [{**self.proposal, 'relationship': relationship}]})
            self.assertEqual(self.client.post(self.url, self.data, format='json').status_code, 400)
        self.client.force_authenticate(None)
        self.assertIn(self.client.post(self.url, self.data, format='json').status_code, (401, 403))

    def test_reuses_existing_reference_on_same_line(self):
        existing = make_reference(self.line, 'OEM-123', brand='KIA', tipo='otro')
        preview = self.preview()
        result = self.apply(preview)
        self.assertEqual(result.status_code, 200, result.data)
        existing.refresh_from_db()
        self.assertEqual((existing.tipo, existing.estado, existing.is_main), ('oem', 'verificado', True))
        self.assertEqual(Reference.objects.filter(part=self.line, code='OEM-123').count(), 1)

    def test_changed_target_refuses_merge_and_keeps_both_lines(self):
        target = make_line('OEM-123', is_OEM=True)
        preview = self.preview()
        references.update(references.main_of(target), notes='Changed destination')
        result = self.apply(preview)
        self.assertEqual(result.status_code, 409, result.data)
        self.old.refresh_from_db(); self.line.refresh_from_db()
        self.assertEqual((self.old.tipo, self.line.estado), ('otro', 'publicada'))
        self.assertFalse(CatalogMerge.objects.exists())

    def prepare_aftermarket(self):
        references.update(self.old, tipo='aftermarket', estado='verificado', actor=self.user)
        self.line.refresh_from_db()
        self.assertEqual(self.line.tipo, 'AFTERMARKET')
        self.data['expected_version'] = self.line.version
        self.data['research_token'] = signing.dumps({'part': str(self.line.pk), 'key': 'classification-test', 'version': self.line.version}, salt='oem-classification-research-v1')

    def test_already_aftermarket_promotes_and_preserves_stock_and_search(self):
        self.prepare_aftermarket()
        self.test_promote_preserves_reference_variant_stock_image_and_both_search_codes()

    def test_already_aftermarket_merges_into_existing_oem(self):
        self.prepare_aftermarket()
        self.test_existing_oem_is_merged_without_losing_items_images_or_supplier_scope()

    def external_oem_with_alternate(self):
        target = make_line('OEM-PRINCIPAL', description='AXIAL KIA', is_OEM=True)
        references.update(references.main_of(target), brand='KIA')
        references.add(target, 'OEM-123', 'KIA', tipo='oem', estado='verificado', source='https://example.com/existing')
        return target

    def test_existing_alternate_keeps_owners_main_and_preserves_items(self):
        target = self.external_oem_with_alternate()
        original_main = references.main_of(target).pk
        stock = list(self.item.ledger.values_list('pk', 'quantity'))
        preview = self.preview()
        self.assertTrue(preview['preserve_main'])
        self.assertEqual((preview['oem'], preview['selected_oem']), ('OEM-PRINCIPAL', 'OEM-123'))
        result = self.apply(preview)
        self.assertEqual(result.status_code, 200, result.data)
        target.refresh_from_db(); self.old.refresh_from_db(); self.item.refresh_from_db(); self.image.refresh_from_db()
        self.assertEqual((target.sku, references.main_of(target).pk), ('OEM-PRINCIPAL', original_main))
        self.assertEqual((self.old.part_id, self.item.part_id, self.image.part_id), (target.pk, target.pk, target.pk))
        self.assertEqual((self.old.tipo, self.old.is_main), ('aftermarket', False))
        self.assertEqual(list(self.item.ledger.values_list('pk', 'quantity')), stock)
        self.assertFalse(Reference.objects.get(part=target, code='OEM-123').is_main)

    def test_equivalent_set_routes_unregistered_choice_to_known_owner_without_adding_all(self):
        target = self.external_oem_with_alternate()
        second = {**self.proposal, 'code': 'OEM-456'}
        third = {**self.proposal, 'code': 'OEM-789'}
        OEMLookupCache.objects.filter(pk='classification-test').update(result={'references': [self.proposal, second, third]})
        self.data['code'] = second['code']
        preview = self.preview()
        self.assertEqual(preview['target']['id'], str(target.pk))
        self.assertEqual(preview['oem'], 'OEM-PRINCIPAL')
        result = self.apply(preview)
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(Reference.objects.get(code='OEM-456').part_id, target.pk)
        self.assertFalse(Reference.objects.filter(code='OEM-789').exists())
        target.refresh_from_db()
        self.assertEqual(target.sku, 'OEM-PRINCIPAL')

    def test_equivalents_in_different_lines_block_preview_without_mutations(self):
        self.external_oem_with_alternate()
        other = make_line('OTHER-OEM', is_OEM=True)
        references.add(other, 'OEM-456', 'KIA', tipo='oem')
        OEMLookupCache.objects.filter(pk='classification-test').update(result={'references': [self.proposal, {**self.proposal, 'code': 'OEM-456'}]})
        result = self.client.post(self.url, self.data, format='json')
        self.assertEqual(result.status_code, 409, result.data)
        self.assertIn('fichas distintas', result.data['detail'])
        self.old.refresh_from_db()
        self.assertEqual(self.old.tipo, 'otro')
        self.assertFalse(CatalogMerge.objects.exists())

    def test_new_equivalent_owner_after_preview_blocks_apply(self):
        second = {**self.proposal, 'code': 'OEM-456'}
        OEMLookupCache.objects.filter(pk='classification-test').update(result={'references': [self.proposal, second]})
        preview = self.preview()
        other = make_line('OTHER-OEM', is_OEM=True)
        references.add(other, 'OEM-456', 'KIA', tipo='oem')
        result = self.apply(preview)
        self.assertEqual(result.status_code, 409, result.data)
        self.old.refresh_from_db()
        self.assertEqual(self.old.tipo, 'otro')
        self.assertFalse(Reference.objects.filter(code='OEM-123').exists())

    def test_cached_research_is_enriched_from_current_catalog(self):
        import hashlib
        import json
        from .oem_lookup import lookup_oem, RESEARCH_VERSION
        info = {'code': self.line.sku, 'company': 'KFM', 'manufacturer_code': self.line.sku, 'description': self.line.description,
                'category': self.line.category, 'subcategory': self.line.subcategory}
        key = hashlib.sha256(json.dumps({'v': RESEARCH_VERSION, 'model': 'test-model', **info}, sort_keys=True).encode()).hexdigest()
        OEMLookupCache.objects.create(fingerprint=key, result={'references': [self.proposal]})
        with patch('mall.oem_lookup.provider_configuration', return_value=(True, 'test-model')), patch('mall.oem_lookup.generate_json') as provider:
            first = lookup_oem(self.line, company='KFM', code=self.line.sku)
            self.assertEqual(first['references'][0]['catalog_matches'], [])
            target = self.external_oem_with_alternate()
            second = lookup_oem(self.line, company='KFM', code=self.line.sku)
            self.assertTrue(second['cached'])
            self.assertEqual(second['references'][0]['catalog_matches'][0]['sku'], target.sku)
            self.assertFalse(second['references'][0]['catalog_matches'][0]['references'][0]['is_main'])
            provider.assert_not_called()
        self.assertNotIn('catalog_matches', OEMLookupCache.objects.get(pk=key).result['references'][0])

    def test_components_and_uncertain_results_do_not_determine_destination(self):
        other = make_line('OTHER-OEM', is_OEM=True)
        references.add(other, 'OEM-456', 'KIA', tipo='oem')
        OEMLookupCache.objects.filter(pk='classification-test').update(result={'references': [self.proposal,
            {**self.proposal, 'code': 'OEM-456', 'relationship': 'component'}, {**self.proposal, 'code': 'OTHER-OEM', 'relationship': 'uncertain'}]})
        preview = self.preview()
        self.assertFalse(preview['merge'])
        self.assertEqual(self.apply(preview).status_code, 200)
        other.refresh_from_db()
        self.assertIsNone(other.merged_into_id)

    def test_multiple_registered_equivalents_in_same_line_are_not_a_conflict(self):
        target = self.external_oem_with_alternate()
        references.add(target, 'OEM-456', 'KIA', tipo='oem')
        OEMLookupCache.objects.filter(pk='classification-test').update(result={'references': [self.proposal, {**self.proposal, 'code': 'OEM-456'}]})
        preview = self.preview()
        self.assertEqual(preview['target']['id'], str(target.pk))
        self.assertEqual(preview['oem'], 'OEM-PRINCIPAL')
        self.assertEqual(self.apply(preview).status_code, 200)

    def test_compact_code_and_brand_alias_find_existing_alternate(self):
        from .brand_models import Brand, BrandAlias
        target = self.external_oem_with_alternate()
        BrandAlias.objects.create(alias='KIA-ALIAS', brand=Brand.objects.get(nombre='KIA'))
        references.add(target, 'OEM-12345', 'KIA', tipo='oem')
        proposal = {**self.proposal, 'code': 'OEM12345', 'brand': 'KIA-ALIAS'}
        OEMLookupCache.objects.filter(pk='classification-test').update(result={'references': [proposal]})
        self.data.update(code=proposal['code'], brand=proposal['brand'])
        preview = self.preview()
        self.assertEqual(preview['target']['id'], str(target.pk))
        self.assertEqual(preview['oem'], 'OEM-PRINCIPAL')
        self.assertEqual(self.apply(preview).status_code, 200)
        self.assertEqual(Reference.objects.filter(part=target, clave=references.clave('OEM-12345'), brand='KIA').count(), 1)

    def select_multiple(self, primary='OEM-123'):
        rows = [self.proposal, {**self.proposal, 'code': 'OEM-456', 'source_url': 'https://example.com/second'},
                {**self.proposal, 'code': 'OEM-789', 'source_url': 'https://example.com/third'}]
        OEMLookupCache.objects.filter(pk='classification-test').update(result={'references': rows})
        self.data.update(code=primary, selections=[{'code': row['code'], 'brand': row['brand']} for row in rows[:2]])
        return rows

    def test_multiple_selected_oems_choose_main_and_keep_other_as_oem_alternate(self):
        self.select_multiple(primary='OEM-456')
        preview = self.preview()
        self.assertEqual(preview['oem'], 'OEM-456')
        self.assertEqual(len(preview['selected_oems']), 2)
        result = self.apply(preview)
        self.assertEqual(result.status_code, 200, result.data)
        self.line.refresh_from_db(); self.old.refresh_from_db()
        self.assertEqual((self.line.sku, self.old.tipo, self.old.is_main), ('OEM-456', 'aftermarket', False))
        alternate = Reference.objects.get(part=self.line, code='OEM-123')
        self.assertEqual((alternate.tipo, alternate.is_main), ('oem', False))
        self.assertTrue(alternate.sources.filter(citation=self.proposal['source_url']).exists())
        self.assertTrue(references.main_of(self.line).sources.filter(citation='https://example.com/second').exists())
        self.assertFalse(Reference.objects.filter(code='OEM-789').exists())

    def test_multiple_selected_oems_preserve_existing_main_and_report_reuse(self):
        target = self.external_oem_with_alternate()
        self.select_multiple(primary='OEM-456')
        preview = self.preview()
        self.assertEqual(preview['oem'], 'OEM-PRINCIPAL')
        self.assertEqual({row['code']: row['already_registered'] for row in preview['selected_oems']}, {'OEM-123': True, 'OEM-456': False})
        self.assertEqual(self.apply(preview).status_code, 200)
        target.refresh_from_db()
        self.assertEqual(target.sku, 'OEM-PRINCIPAL')
        self.assertEqual(Reference.objects.filter(part=target, tipo='oem', code__in=['OEM-123', 'OEM-456'], is_main=False).count(), 2)
        self.assertFalse(Reference.objects.filter(code='OEM-789').exists())

    def test_multiple_selected_oems_rollback_whole_set_on_later_failure(self):
        self.select_multiple()
        preview = self.preview()
        original = references.add
        def fail_second(line, code, *args, **kwargs):
            if code == 'OEM-456':
                raise references.ReferenceConflict('La segunda referencia está en conflicto.')
            return original(line, code, *args, **kwargs)
        with patch('mall.oem_classification.references.add', side_effect=fail_second):
            result = self.apply(preview)
        self.assertEqual(result.status_code, 400, result.data)
        self.line.refresh_from_db(); self.old.refresh_from_db()
        self.assertEqual((self.line.sku, self.old.tipo), ('CODE-K', 'otro'))
        self.assertFalse(Reference.objects.filter(code__in=['OEM-123', 'OEM-456']).exists())
        self.assertFalse(CatalogIdentityChange.objects.exists())

    def test_selected_set_cannot_change_after_signed_preview(self):
        self.select_multiple()
        preview = self.preview()
        self.data['selections'].pop()
        result = self.apply(preview)
        self.assertEqual(result.status_code, 409, result.data)
        self.old.refresh_from_db()
        self.assertEqual(self.old.tipo, 'otro')
        self.assertFalse(Reference.objects.filter(code='OEM-123').exists())

    def test_additional_selected_oem_cannot_retype_the_preserved_aftermarket_code(self):
        OEMLookupCache.objects.filter(pk='classification-test').update(result={'references': [self.proposal, {**self.proposal, 'code': self.old.code}]})
        self.data['selections'] = [{'code': self.proposal['code'], 'brand': 'KIA'}, {'code': self.old.code, 'brand': 'KIA'}]
        result = self.client.post(self.url, self.data, format='json')
        self.assertEqual(result.status_code, 409, result.data)
        self.old.refresh_from_db()
        self.assertEqual(self.old.tipo, 'otro')
        self.assertFalse(Reference.objects.filter(code='OEM-123').exists())
