from unittest.mock import patch
from django.test import override_settings
from rest_framework.test import APITestCase
from .models import Account, Part, PartCode, SupplierItem, StockEntry, User, CatalogIdentityChange, OEMLookupCache
from .catalog_identity import prefer_oem, reconcile_identities, identity_status
from .management import ManagedPartSerializer
from .matching_engine import MatchIndex
from .oem_lookup import lookup_oem
from .catalog_classification import ClassificationProviderError
from .services import ingest_inventory


class OEMIdentityTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('oem-admin', 'oem@example.invalid', 'Example938!')
        self.client.force_authenticate(self.admin)
        self.part = Part.objects.create(sku='0110-GSU45A48-FEB', name='0110-GSU45A48-FEB', description='PUNTA FLECHA EXT TOY RAV4 19-')
        self.url = f'/api/v1/management/catalog/{self.part.pk}/'

    def oem(self, code='43460-TEST', brand='TOYOTA', source='CATÁLOGO REVISADO'):
        return PartCode.objects.create(part=self.part, code=code, brand=brand, ref_type='oem', reference_source=source)

    def test_company_type_is_not_oem_even_after_removing_suffix(self):
        reconcile_identities(self.admin)
        self.part.refresh_from_db()
        self.assertEqual(self.part.sku, '0110-GSU45A48-FEB')
        self.assertEqual(set(self.part.codes.values_list('code','brand','ref_type')), {
            ('0110-GSU45A48-FEB', 'FEBEST', 'company'), ('0110-GSU45A48', 'FEBEST', 'company')})
        self.assertEqual(identity_status(self.part)['status'], 'needs_oem')
        self.assertEqual(CatalogIdentityChange.objects.count(), 0)

    @override_settings(CATALOG_COMPANY_SUFFIXES={'CUSTOM': 'ACME'})
    def test_company_registry_works_for_other_feeds_without_code_specific_rules(self):
        self.part.sku = 'ABC-123456-CUSTOM'; self.part.save()
        reconcile_identities()
        self.assertTrue(self.part.codes.filter(code='ABC-123456', brand='ACME', ref_type='company').exists())

    def test_verified_oem_becomes_main_without_changing_stock_identity(self):
        supplier = Account.objects.create(name='PROVEEDOR')
        item = ingest_inventory(supplier=supplier, actor=self.admin, data={'supplier_invent_id':'stable-9', 'update_id':'upload-1',
            'codigo':self.part.sku, 'brand':'', 'description':self.part.description, 'quantity':5, 'source':'upload'})
        item.part = self.part; item.save()
        original = list(StockEntry.objects.values())
        self.oem()
        promoted = prefer_oem(self.part, actor=self.admin)
        self.assertEqual(promoted.pk, self.part.pk)
        self.assertEqual(promoted.sku, '43460-TEST')
        self.assertEqual(promoted.name, '43460-TEST')
        item.refresh_from_db()
        self.assertEqual(item.part_id, self.part.pk)
        self.assertEqual(item.codigo, '0110-GSU45A48-FEB')
        self.assertEqual(item.supplier_invent_id, 'stable-9')
        self.assertEqual(list(StockEntry.objects.values()), original)
        target, _, automatic, _ = MatchIndex().resolve(item)
        self.assertTrue(automatic)
        self.assertEqual(target[0]['id'], str(self.part.pk))
        self.assertEqual(CatalogIdentityChange.objects.get().previous_sku, '0110-GSU45A48-FEB')
        prefer_oem(promoted, actor=self.admin)
        self.assertEqual(CatalogIdentityChange.objects.count(), 1)

    def test_unknown_oem_provenance_cannot_be_auto_promoted(self):
        self.oem(source='')
        self.assertEqual(prefer_oem(self.part).sku, self.part.sku)
        response = self.client.patch(self.url, {'main_reference':{'code':'43460-TEST','brand':'TOYOTA'}}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_multiple_oems_require_choice_and_preserve_all_refs(self):
        self.oem(); self.oem('43460-SECOND')
        self.assertEqual(prefer_oem(self.part).sku, self.part.sku)
        self.assertEqual(identity_status(self.part)['status'], 'choose_oem')
        result = self.client.patch(self.url, {'main_reference': {'code':'43460-SECOND', 'brand':'TOYOTA'}}, format='json')
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data['sku'], '43460-SECOND')
        self.assertEqual(result.data['identity']['ref_type'], 'oem')
        self.assertEqual(self.part.codes.filter(ref_type='oem').count(), 2)
        self.assertEqual(prefer_oem(self.part).sku, '43460-SECOND')

    def test_new_typed_ref_promotes_on_save_and_preserves_company_as_alterno(self):
        result = self.client.patch(self.url, {'codes':[{'code':'43460-TEST','brand':'TOYOTA','ref_type':'oem','reference_source':'VERIFICADO'}]}, format='json')
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data['sku'], '43460-TEST')
        self.assertIn({'code':'0110-GSU45A48','brand':'FEBEST','ref_type':'company'},
                      [{k:r[k] for k in ['code','brand','ref_type']} for r in result.data['codes']])

    def test_company_requires_name_and_legacy_kind_maps_to_single_column(self):
        result = self.client.patch(self.url, {'codes':[{'code':'XYZ1234','brand':'','ref_type':'company'}]}, format='json')
        self.assertEqual(result.status_code, 400)
        code = PartCode.objects.create(part=self.part, code='TEST', kind='manufacturer')
        self.assertEqual(code.ref_type, 'company')
        result = self.client.patch(f'/api/v1/management/alternates/{code.pk}/', {'kind':'oem','ref_type':'company'}, format='json')
        self.assertEqual(result.status_code, 400)
        result = self.client.patch(f'/api/v1/management/alternates/{code.pk}/', {'brand':'ACME', 'ref_type':'company'}, format='json')
        self.assertEqual(result.status_code, 200, result.data)
        code.refresh_from_db(); self.assertEqual(code.kind, 'manufacturer')

    def test_oem_already_owned_by_another_sku_requires_merge_review(self):
        self.oem()
        other = Part.objects.create(sku='43460-TEST')
        self.assertEqual(prefer_oem(self.part).sku, self.part.sku)
        result = self.client.patch(self.url, {'main_reference':{'code':'43460-TEST','brand':'TOYOTA'}}, format='json')
        self.assertEqual(result.status_code, 400)
        other.refresh_from_db(); self.assertIsNone(other.merged_into_id)

    def test_explicit_choice_failure_rolls_back_all_changes(self):
        result = self.client.patch(self.url, {'description':'CHANGED','main_reference':{'code':'FAKE','brand':'TOYOTA'}}, format='json')
        self.assertEqual(result.status_code, 400)
        self.part.refresh_from_db(); self.assertNotEqual(self.part.description, 'CHANGED')

    def test_lookup_and_catalog_write_require_superuser(self):
        user = User.objects.create_user('normal', 'normal@example.invalid', 'Example938!')
        self.client.force_authenticate(user)
        self.assertEqual(self.client.post(self.url+'oem-lookup/', {}, format='json').status_code, 403)
        self.assertEqual(self.client.patch(self.url, {'main_reference': {'code':'X','brand':'Y'}}, format='json').status_code, 403)

    def grounded(self, relationship='equivalent'):
        return ({'references':[{'code':'43460-TEST','brand':'TOYOTA','relationship':relationship,'source_url':'https://example.com/part','reason':'REF EN FICHA'}], 'note':'REVISAR'},
                {'total_tokens':100}, {'groundingChunks':[{'web':{'uri':'https://example.com/part','title':'Fabricante'}}]})

    def research_responses(self, relationship='equivalent'):
        result, metrics, grounding = self.grounded(relationship)
        return [({'text': 'FEBEST 0110-GSU45A48 OEM TOYOTA 43460-TEST https://example.com/part'}, metrics, grounding), (result, metrics)]

    @patch('mall.oem_lookup.generate_json')
    def test_research_is_cached_and_never_creates_oem_or_changes_catalog(self, provider):
        provider.side_effect = self.research_responses() + self.research_responses()
        a = lookup_oem(self.part); b = lookup_oem(self.part)
        self.assertFalse(a['cached']); self.assertTrue(b['cached'])
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(self.part.codes.count(), 0)
        self.assertEqual(a['references'][0]['ref_type'], 'oem')
        self.assertTrue(a['needs_review'])
        self.part.description = 'DIFFERENT'; self.part.save()
        lookup_oem(self.part); self.assertEqual(provider.call_count, 4)

    @patch('mall.oem_lookup.generate_json')
    def test_component_is_never_silently_reclassified_as_equivalent(self, provider):
        provider.side_effect = self.research_responses('component')
        self.assertEqual(lookup_oem(self.part)['references'][0]['relationship'], 'component')
        self.assertEqual(self.part.codes.count(), 0)

    @patch('mall.oem_lookup.generate_json')
    def test_no_grounded_sources_means_no_oem_suggestions(self, provider):
        output, metrics, _ = self.grounded()
        provider.return_value = output, metrics, {}
        with self.assertRaises(ClassificationProviderError): lookup_oem(self.part)
        self.assertFalse(OEMLookupCache.objects.exists())

    @patch('mall.oem_lookup.generate_json')
    def test_unsafe_source_url_is_rejected_and_not_cached(self, provider):
        output, metrics, grounding = self.grounded()
        output['references'][0]['source_url'] = 'javascript:alert(1)'
        provider.side_effect = [({'text': '43460-TEST https://example.com/part'}, metrics, grounding), (output, metrics)]
        with self.assertRaises(ClassificationProviderError): lookup_oem(self.part)
        self.assertFalse(OEMLookupCache.objects.exists())

    def test_existing_oem_master_is_kept_when_legacy_company_owns_its_reference(self):
        from .matching_engine import reconcile_catalog_references
        target = Part.objects.create(sku='43460-TEST', description=self.part.description)
        self.oem()
        self.assertEqual(reconcile_catalog_references(self.admin), 1)
        self.part.refresh_from_db(); target.refresh_from_db()
        self.assertEqual(self.part.merged_into_id, target.pk)
        self.assertIsNone(target.merged_into_id)
        self.assertEqual(target.sku, '43460-TEST')
        self.assertTrue(target.codes.filter(code='0110-GSU45A48', brand='FEBEST', ref_type='company').exists())

    def test_typed_supplier_references_use_correct_namespace(self):
        from .part_references import normalize_references, parse_reference_cell, format_references
        PartCode.objects.create(part=self.part, code='ABC123456', brand='FEBEST', ref_type='company')
        index = MatchIndex()
        self.assertEqual(index.reference_targets([{'code':'ABC123456','brand':'FEBEST','ref_type':'oem'}]), set())
        self.assertEqual(index.reference_targets([{'code':'ABC123456','brand':'FEBEST','ref_type':'company'}]), {str(self.part.pk)})
        refs = parse_reference_cell('OEM:TOYOTA:43460-TEST; COMPANY:FEBEST:0110-GSU45A48')
        self.assertEqual(parse_reference_cell(format_references(refs)), refs)
        self.assertEqual({r['ref_type'] for r in refs}, {'oem','company'})

    def test_oem_boolean_can_be_saved_and_cleared_independently_of_alternates(self):
        self.oem(code=self.part.sku)
        result = self.client.patch(self.url, {'is_OEM': True}, format='json')
        self.assertEqual(result.status_code, 200, result.data)
        self.assertTrue(result.data['is_OEM'])
        self.assertEqual(result.data['identity']['status'], 'oem')
        result = self.client.patch(self.url, {'is_OEM': False}, format='json')
        self.assertFalse(result.data['is_OEM'])
        reconcile_identities(self.admin)
        self.part.refresh_from_db()
        self.assertFalse(self.part.is_OEM)
        self.assertNotEqual(identity_status(self.part)['status'], 'oem')
        self.assertEqual(self.part.codes.filter(ref_type='oem').count(), 1)

    def test_oem_default_filter_and_public_api(self):
        self.assertFalse(self.part.is_OEM)
        oem = Part.objects.create(sku='OEM-VERIFIED', is_OEM=True)
        result = self.client.get('/api/v1/management/catalog/', {'is_OEM': 'true'})
        self.assertEqual([p['id'] for p in result.data['results']], [str(oem.pk)])
        result = self.client.get('/api/v1/management/catalog/', {'is_OEM': 'false'})
        self.assertEqual([p['id'] for p in result.data['results']], [str(self.part.pk)])
        self.assertEqual(self.client.get('/api/v1/management/catalog/', {'is_OEM':'garbage'}).status_code, 400)
        from .serializers import PartSerializer
        self.assertTrue(PartSerializer(oem).data['is_OEM'])

    def test_rename_does_not_transfer_oem_flag_to_new_code(self):
        self.part.is_OEM = True
        self.part.save()
        result = self.client.patch(self.url, {'sku':'NEW-INTERNAL-CODE'}, format='json')
        self.assertEqual(result.status_code, 200, result.data)
        self.assertFalse(result.data['is_OEM'])
        self.assertEqual(result.data['sku'], 'NEW-INTERNAL-CODE')
        self.assertEqual(self.part.codes.get(code=self.part.sku).ref_type, 'oem')

    def test_flagged_oem_is_not_replaced_by_single_alternate(self):
        self.part.is_OEM = True
        self.part.save()
        self.oem()
        self.assertEqual(prefer_oem(self.part).sku, self.part.sku)
        result = self.client.patch(self.url, {'main_reference': {'code':'43460-TEST','brand':'TOYOTA'}}, format='json')
        self.assertEqual(result.status_code, 200, result.data)
        self.assertTrue(result.data['is_OEM'])
        self.assertEqual(result.data['sku'], '43460-TEST')

    def test_oem_main_matches_oem_namespace_but_does_not_become_company_reference(self):
        self.part.is_OEM = True
        self.part.save()
        index = MatchIndex()
        reference = {'code':self.part.sku, 'brand':'TOYOTA', 'ref_type':'oem'}
        self.assertEqual(index.reference_targets([reference]), {str(self.part.pk)})
        reference['ref_type'] = 'company'
        self.assertEqual(index.reference_targets([reference]), set())
        PartCode.objects.create(part=self.part, code=self.part.sku, ref_type='oem', brand='TOYOTA')
        reference.update(ref_type='oem', brand='HONDA')
        self.assertEqual(MatchIndex().reference_targets([reference]), set())

    def test_marked_oem_suffix_is_not_stripped_into_an_internal_family(self):
        from .matching_engine import reconcile_catalog
        base = Part.objects.create(sku='OEM1234567', description='ROTULA TOYOTA RAV4 2019')
        original = Part.objects.create(sku=base.sku+'-ABC', description=base.description, is_OEM=True)
        self.assertEqual(reconcile_catalog(self.admin), 0)
        original.refresh_from_db()
        self.assertIsNone(original.merged_into_id)
        self.assertTrue(original.is_OEM)

    def test_promotion_marks_oem_and_boolean_update_requires_superuser(self):
        self.oem()
        promoted = prefer_oem(self.part)
        self.assertTrue(promoted.is_OEM)
        self.client.force_authenticate(User.objects.create_user('reader', 'reader@example.invalid', 'Example938!'))
        self.assertEqual(self.client.patch(self.url, {'is_OEM':False}, format='json').status_code, 403)
        promoted.refresh_from_db()
        self.assertTrue(promoted.is_OEM)

    def test_migration_backfills_only_existing_verified_main_reference(self):
        from importlib import import_module
        from django.apps import apps
        from django.db import connection
        from types import SimpleNamespace
        verified = Part.objects.create(sku='58411-1R000')
        PartCode.objects.create(part=verified, code='584111R000', brand='HYUNDAI', ref_type='oem', reference_source='CATÁLOGO')
        unverified = Part.objects.create(sku='51712-TEST')
        PartCode.objects.create(part=unverified, code=unverified.sku, brand='HYUNDAI', ref_type='oem')
        self.oem()  # A different OEM in the library doesn't classify the company main.
        migration = import_module('mall.migrations.0026_part_is_oem_matching_progress')
        migration.mark_verified_main_oems(apps, SimpleNamespace(connection=connection))
        self.assertEqual(list(Part.objects.filter(is_OEM=True).values_list('pk',flat=True)), [verified.pk])

    def test_old_assistant_proposal_cannot_overwrite_changed_oem_identity(self):
        from .catalog_assistant import current_row, unchanged
        from .catalog_classification import ClassificationConflict
        snapshot = current_row(self.part)
        self.part.is_OEM = True
        self.part.save()
        with self.assertRaises(ClassificationConflict):
            unchanged(self.part, snapshot)
