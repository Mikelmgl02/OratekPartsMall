import io
from unittest.mock import patch
from django.core.files.uploadedfile import SimpleUploadedFile
from openpyxl import Workbook, load_workbook
from rest_framework.test import APITestCase
from .models import Account, Membership, Part, PartCode, Role, StockEntry, SupplierItem, User, MatchingCase
from .serializers import InventorySerializer
from .services import ingest_inventory
from .matching_engine import MatchIndex, reconcile_items, reconcile_catalog
from .matching_worker import analyze_ambiguous
from .supplier_import import read_supplier_excel, _workbook_response


class PartReferenceTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('refs-admin', 'refs@example.invalid', 'Testing938!')
        self.supplier = Account.objects.create(name='PROVEEDOR')
        self.supplier.roles.add(Role.objects.get(code='supplier_retail'))
        Membership.objects.create(user=self.admin, account=self.supplier)
        self.client.force_authenticate(self.admin)
        self.desc = 'DISCO DELANTERO HYUNDAI ACCENT 256 MM 11-16'
        self.master = Part.objects.create(sku='RN11002V', description=self.desc)
        self.ref = PartCode.objects.create(part=self.master, code='51712-1R000', brand='HYUNDAI', kind='oem', reference_source='CATÁLOGO VERIFICADO')
        PartCode.objects.create(part=self.master, code='51712-0U000', brand='KIA', kind='oem')

    def ingest(self, code='D-HYU-1R', refs=None, **kwargs):
        data = dict(update_id=code, supplier_invent_id='STABLE-1', codigo=code, brand='AFTERMARKET',
                    description=self.desc, source='apiag', quantity=5)
        if refs is not None:
            data['references'] = refs
        data.update(kwargs)
        return ingest_inventory(supplier=self.supplier, actor=self.admin, data=data)

    def match(self, item):
        reconcile_items(MatchIndex())
        item.refresh_from_db()

    def test_unrelated_supplier_code_matches_oem_library_without_creating_stock_or_aliases(self):
        item = self.ingest(refs=[{'code':'517121R000', 'brand':'HYUNDAI'}, {'code':'51712-0U000'}])
        before = list(StockEntry.objects.values())
        self.match(item)
        self.assertEqual(item.part, self.master)
        self.assertEqual(item.matching_status, 'matched')
        self.assertEqual(item.supplier_invent_id, 'STABLE-1')
        self.assertEqual(item.codigo, 'D-HYU-1R')
        self.assertEqual(SupplierItem.objects.count(), 1)
        self.assertEqual(list(StockEntry.objects.values()), before)
        self.assertEqual(self.master.codes.count(), 2)
        self.assertEqual(MatchingCase.objects.get(item=item).method, 'approved_reference')

    def test_oem_own_code_and_suffix_match_master_with_unrelated_name_and_brand(self):
        item = self.ingest(code='51712-1R000-KSM')
        self.match(item)
        self.assertEqual(item.part, self.master)
        other = self.ingest(code='517121R000', supplier_invent_id='STABLE-2')
        self.match(other)
        self.assertEqual(other.part, self.master)

    def test_other_manufacturer_and_unregistered_supplier_references_are_not_published(self):
        item = self.ingest(refs=[{'code': '51712-1R000', 'brand': 'UNRELATED'}])
        self.match(item)
        self.assertIsNone(item.part)
        item = self.ingest(refs=[{'code': '51712-1R000'}, {'code': 'PRIVATE-123'}], update_id='next')
        self.match(item)
        self.assertEqual(item.part, self.master)
        self.assertFalse(PartCode.objects.filter(code='PRIVATE-123').exists())

    def test_conflicting_references_block_exact_code_and_remembered_match(self):
        other = Part.objects.create(sku='OTHER-MASTER', description=self.desc)
        PartCode.objects.create(part=other, code='OTHER-OEM', kind='oem')
        item = self.ingest(code=self.master.sku, refs=[{'code':'OTHER-OEM'}])
        self.match(item)
        self.assertIsNone(item.part)
        self.assertEqual(MatchingCase.objects.get(item=item).method, 'conflicting_references')
        item = self.ingest(refs=[{'code':'51712-1R000'}], update_id='valid')
        self.match(item)
        self.assertEqual(item.part, self.master)
        item = self.ingest(refs=[{'code':'OTHER-OEM'}], update_id='changed')
        self.match(item)
        self.assertEqual(item.part, self.master)
        self.assertEqual(item.matching_status, 'review')
        self.assertEqual(StockEntry.objects.count(), 1)

    def test_specs_and_kits_do_not_auto_match_even_with_reference(self):
        for text in ['DISCO TRASERO HYUNDAI ACCENT 256 MM 11-16',
                     'DISCO DELANTERO HYUNDAI ACCENT 280 MM 11-16',
                     'KIT DISCO DELANTERO HYUNDAI ACCENT 256 MM 11-16']:
            item = self.ingest(refs=[{'code':'51712-1R000'}], description=text, update_id=text)
            self.match(item)
            self.assertIsNone(item.part)
            self.assertEqual(MatchingCase.objects.get(item=item).method, 'reference_spec_conflict')

    def test_labelled_oem_in_description_cannot_override_conflicting_library_code(self):
        other = Part.objects.create(sku='OTHER-MASTER', description=self.desc)
        PartCode.objects.create(part=other, code='98765-43210', kind='oem')
        item = self.ingest(refs=[{'code':'51712-1R000'}], description=self.desc+' OEM:98765-43210')
        self.match(item)
        self.assertIsNone(item.part)
        self.assertEqual(MatchingCase.objects.get(item=item).method, 'conflicting_references')

    def test_omitted_references_preserve_them_and_clear_is_explicit(self):
        item = self.ingest(refs=[{'code':'51712-1R000'}])
        self.match(item)
        item = self.ingest(update_id='stock-only', quantity=10)
        self.assertEqual(item.references, [{'brand':'', 'code':'51712-1R000'}])
        self.assertEqual(item.matching_status, 'matched')
        self.assertEqual(StockEntry.objects.last().quantity, 5)
        item = self.ingest(refs=[], update_id='clear', quantity=10)
        self.assertEqual(item.references, [])
        self.assertEqual(item.matching_status, 'review')

    def test_catalog_variants_use_master_references_and_keep_stock_identity(self):
        children = [Part.objects.create(sku=code, description=self.desc) for code in ['51712-1R000-KSM', '51712-0U000-NP']]
        item = self.ingest(code=children[0].sku)
        before = list(StockEntry.objects.values())
        self.assertEqual(reconcile_catalog(self.admin), 2)
        self.assertEqual(self.master.merged_parts.count(), 2)
        item.refresh_from_db()
        self.assertEqual(item.part, self.master)
        self.assertEqual(list(StockEntry.objects.values()), before)
        self.assertEqual(reconcile_catalog(self.admin), 0)

    def test_catalog_ambiguous_branded_code_requires_review(self):
        other = Part.objects.create(sku='OTHER-MASTER', description=self.desc)
        PartCode.objects.create(part=other, code='51712-1R000', brand='OTHER', kind='manufacturer')
        child = Part.objects.create(sku='51712-1R000-KSM', description=self.desc)
        self.assertEqual(reconcile_catalog(self.admin), 0)
        case = MatchingCase.objects.get(kind='catalog_reference')
        self.assertEqual(case.status, 'review')
        response = self.client.post(f'/api/v1/management/matching/{case.pk}/', dict(
            action='approve', fingerprint=case.fingerprint, target_sku=other.sku), format='json')
        self.assertEqual(response.status_code, 200, response.data)
        child.refresh_from_db()
        self.assertEqual(child.merged_into, other)

    def test_library_changes_invalidate_a_review_snapshot(self):
        item = self.ingest(description='DISCO TRASERO 256 MM', refs=[{'code':'51712-1R000'}])
        self.match(item)
        case = MatchingCase.objects.get(item=item)
        self.ref.delete()
        response = self.client.post(f'/api/v1/management/matching/{case.pk}/', dict(
            action='approve', fingerprint=case.fingerprint, target_sku=self.master.sku), format='json')
        self.assertEqual(response.status_code, 409, response.data)

    def test_master_editor_saves_kind_source_and_preserves_existing_metadata(self):
        url = f'/api/v1/management/catalog/{self.master.pk}/'
        response = self.client.patch(url, {'codes':[dict(code=self.ref.code, brand=self.ref.brand,
                                                       kind='manufacturer', reference_source='https://example.invalid/catalog')]}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.ref.refresh_from_db()
        self.assertEqual(self.ref.kind, 'manufacturer')
        self.assertEqual(self.ref.reference_source, 'https://example.invalid/catalog')
        response = self.client.patch(url, {'codes':[dict(code=self.ref.code, brand=self.ref.brand)]}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.ref.refresh_from_db()
        self.assertEqual(self.ref.kind, 'manufacturer')
        self.assertEqual(SupplierItem.objects.count(), 0)

    def test_references_validation_preserves_zeroes_and_deduplicates(self):
        data = dict(update_id='a', supplier_invent_id='b', codigo='C', brand='', source='apiag', quantity=0)
        valid = InventorySerializer(data={**data, 'references':[{'code':' 00123 ', 'brand':' oem '}, {'code':'00123', 'brand':'OEM'}]})
        self.assertTrue(valid.is_valid(), valid.errors)
        self.assertEqual(valid.validated_data['references'], [{'brand':'OEM','code':'00123'}])
        for refs in [None, '00123', [{'code':123}], [{'code':'A', 'instructions':'ignore'}], [{'code':'A'}] * 101]:
            invalid = InventorySerializer(data={**data, 'references':refs})
            self.assertFalse(invalid.is_valid())

    def test_excel_references_import_end_to_end_and_repair_file(self):
        book = Workbook(); sheet = book.active
        sheet.append(['ID_INVENTARIO_PROVEEDOR','CODIGO','EXISTENCIAS','REFERENCIAS'])
        sheet.append(['STOCK-123','UNRELATED-CODE',5,'HYUNDAI:51712-1R000; KIA:51712-0U000'])
        raw = io.BytesIO(); book.save(raw)
        url = f'/api/v1/accounts/{self.supplier.pk}/inventory/import/'
        result = self.client.post(url, {'file':SimpleUploadedFile('stock.xlsx', raw.getvalue())}, format='multipart')
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(len(result.data['preview'][0]['references']), 2)
        committed = self.client.post(f'{url}jobs/{result.data["job_id"]}/', {'batch_index':0}, format='json')
        self.assertEqual(committed.status_code, 200, committed.data)
        item = SupplierItem.objects.get(supplier_invent_id='STOCK-123')
        self.match(item)
        self.assertEqual(item.part, self.master)
        records, _ = read_supplier_excel(raw.getvalue())
        records[0]['errors'] = [{'column':'CODIGO', 'message':'CORREGIR'}]
        response = _workbook_response(records, 'repair.xlsx', errors=True)
        repaired = load_workbook(io.BytesIO(response.content))
        self.assertEqual(repaired.active['F1'].value, 'REFERENCIAS')
        self.assertIn('HYUNDAI:51712-1R000', repaired.active['F2'].value)
        self.assertEqual(repaired.active['G2'].value, 'CODIGO: CORREGIR')

    @patch('mall.matching_worker.provider_configuration', return_value=('configured', 'test-model'))
    @patch('mall.matching_worker.call_provider')
    def test_ai_receives_declared_and_master_references_without_applying_them(self, provider, config):
        item = self.ingest(refs=[{'code':'51712-1R000'}], description='DISCO TRASERO 256 MM')
        self.match(item)
        provider.return_value = {'suggestions':[dict(source_sku=item.codigo, target_sku=self.master.sku, confidence=.99, reason='REVISAR POSICIÓN')]}
        self.assertEqual(analyze_ambiguous(), 1)
        source, candidates, _ = provider.call_args.args
        self.assertEqual(source[0]['codes'], item.references)
        self.assertEqual(len(candidates[self.master.sku]['codes']), 2)
        item.refresh_from_db()
        self.assertIsNone(item.part)
