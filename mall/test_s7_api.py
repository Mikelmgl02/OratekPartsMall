"""The S7 management routes (lines, references, cases, merges, brands, grupos, Separar referencia) and the storefront's new keys and
filters (brand, tipo, estado, codes' tipo and estado, the offers' variant data, brand and calidad)."""
from rest_framework.test import APITestCase

from . import lines, references
from .brand_models import Brand, BrandAlias
from .line_merge import SERVICE_ACCOUNT, merge_lines, service_actor
from .matching_models import MatchingCase
from .models import CatalogMerge, Grupo, Part, Reference, SupplierItem
from .technical_data import part_type_for
from .test_factories import make_client, make_line, make_supplier, make_user

M = '/api/v1/management'


class S7Base(APITestCase):
    def setUp(self):
        self.admin = make_user('s7-admin', superuser=True)
        self.client.force_authenticate(self.admin)

    def line(self, sku, description='', **fields):
        return make_line(sku, description=description, **fields)


class LineRouteTests(S7Base):
    def test_only_superusers(self):
        line = self.line('SEC-1')
        self.client.force_authenticate(make_user('s7-plain'))
        for path in ['lines/', f'lines/{line.pk}/', f'lines/{line.pk}/references/', 'cases/', 'merges/', 'brands/', 'grupos/']:
            self.assertEqual(self.client.get(f'{M}/{path}').status_code, 403, path)

    def test_list_filters_by_estado_tipo_origen_subgrupo_brand_search_and_ids(self):
        frenos = part_type_for('FRENOS', 'DISCOS')
        disco = self.line('DISCO-1', 'DISCO FRENO', part_type=frenos)
        retirada = self.line('RET-1', 'RETEN')
        lines.set_estado(retirada, 'retirada')
        rechazada = self.line('REJ-1', 'RECHAZO')
        lines.set_estado(rechazada, 'rechazada', reason='bad_data', user=self.admin)
        oem = self.line('90915-YZZE1', 'FILTRO')
        references.retype_main(oem, 'oem', brand='TOYOTA')

        def skus(**params):
            response = self.client.get(f'{M}/lines/', params)
            self.assertEqual(response.status_code, 200, response.data)
            return [row['sku'] for row in response.data['results']]

        self.assertEqual(skus(), ['90915-YZZE1', 'DISCO-1', 'RET-1'])  # publicada and retirada by default
        self.assertEqual(skus(estado='rechazada'), ['REJ-1'])
        self.assertEqual(skus(estado='publicada,rechazada', tipo='OEM'), ['90915-YZZE1'])
        self.assertEqual(skus(subgrupo=str(frenos.pk)), ['DISCO-1'])
        self.assertEqual(skus(grupo=str(frenos.grupo_id)), ['DISCO-1'])
        self.assertEqual(skus(brand='toyota'), ['90915-YZZE1'])
        self.assertEqual(skus(search='disco'), ['DISCO-1'])
        self.assertEqual(skus(ids=f'{disco.pk},{retirada.pk}'), ['DISCO-1', 'RET-1'])
        self.assertEqual(skus(origen='biblioteca'), [])
        self.assertEqual(self.client.get(f'{M}/lines/', {'estado': 'nope'}).status_code, 400)
        row = self.client.get(f'{M}/lines/', {'ids': str(oem.pk)}).data['results'][0]
        self.assertEqual({key: row[key] for key in ('estado', 'origen', 'tipo', 'brand', 'version', 'reference_count', 'variant_count',
                                                    'merged_count', 'images')},
                         {'estado': 'publicada', 'origen': 'catalogo', 'tipo': 'OEM', 'brand': 'TOYOTA', 'version': 1, 'reference_count': 1,
                          'variant_count': 0, 'merged_count': 0, 'images': []})
        self.assertEqual(row['main']['code'], '90915-YZZE1')
        disco_row = self.client.get(f'{M}/lines/{disco.pk}/').data
        self.assertEqual((disco_row['grupo']['nombre'], disco_row['subgrupo']['name']), ('FRENOS', 'DISCOS'))

    def test_rows_count_variants_and_merged_lines_and_order_by_variants(self):
        supplier = make_supplier('PROV FILAS')
        stocked, empty, merged = self.line('STK-1', 'CON STOCK'), self.line('STK-0', 'SIN STOCK'), self.line('STK-M', 'UNIFICADA')
        for n in range(2):
            SupplierItem.objects.create(part=stocked, supplier=supplier, supplier_invent_id=f'V-{n}', codigo=f'STK-1-{n}', brand='',
                                        source='upload', matching_status='matched')
        merge_lines(stocked, [merged], actor=self.admin, reason='prueba')
        response = self.client.get(f'{M}/lines/', {'ordering': '-variant_count', 'search': 'stk'})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([(row['sku'], row['variant_count'], row['merged_count']) for row in response.data['results']],
                         [('STK-1', 2, 1), ('STK-0', 0, 0)])
        self.assertEqual([row['sku'] for row in self.client.get(f'{M}/lines/', {'ordering': 'variant_count', 'search': 'stk'}).data['results']],
                         ['STK-0', 'STK-1'])
        self.assertEqual(self.client.get(f'{M}/lines/{empty.pk}/').data['merged_count'], 0)

    def test_patch_keeps_the_applications_text_line_by_line(self):
        line = self.line('APL-1', 'APLICACIONES')
        text = '  Toyota  Corolla 2014-2018 \r\nToyota Yaris   2016\n\n'
        response = self.client.patch(f'{M}/lines/{line.pk}/', {'expected_version': 1, 'aplicaciones_texto': text}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['aplicaciones_texto'], 'Toyota Corolla 2014-2018\nToyota Yaris 2016')
        same = self.client.patch(f'{M}/lines/{line.pk}/', {'expected_version': 2, 'aplicaciones_texto': 'Toyota Corolla 2014-2018\nToyota Yaris 2016'},
                                 format='json')
        self.assertEqual(same.data['version'], 2)  # nothing changed: the version stays

    def test_patch_checks_the_version_and_marks_the_fields_as_the_admins(self):
        line = self.line('ED-1', 'antes')
        response = self.client.patch(f'{M}/lines/{line.pk}/', {'expected_version': 1, 'description': ' disco  freno ', 'subgrupo': None},
                                     format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data['description'], response.data['version']), ('DISCO FRENO', 2))
        self.assertEqual(response.data['field_sources']['description']['source'], 'admin')
        stale = self.client.patch(f'{M}/lines/{line.pk}/', {'expected_version': 1, 'description': 'otra'}, format='json')
        self.assertEqual((stale.status_code, stale.data['version']), (409, 2))
        subgrupo = part_type_for('FRENOS', 'PASTILLAS')
        response = self.client.patch(f'{M}/lines/{line.pk}/', {'expected_version': 2, 'subgrupo': str(subgrupo.pk)}, format='json')
        self.assertEqual((response.data['subgrupo']['name'], response.data['category']), ('PASTILLAS', 'FRENOS'))
        self.assertEqual(self.client.patch(f'{M}/lines/{line.pk}/', {'expected_version': 3, 'sku': 'X'}, format='json').status_code, 400)

    def test_transitions(self):
        line = self.line('TR-1', 'TRANSICION')
        post = lambda estado, version, **extra: self.client.post(f'{M}/lines/{line.pk}/transition/',
                                                                {'estado': estado, 'expected_version': version, **extra}, format='json')
        response = post('retirada', 1)
        self.assertEqual((response.status_code, response.data['estado'], response.data['version']), (200, 'retirada', 2))
        self.assertEqual(response.data['decision']['decided_by'], 's7-admin')
        self.assertEqual(post('publicada', 1).status_code, 409)
        self.assertEqual(post('rechazada', 2).status_code, 400)  # retirada → rechazada is not a move
        self.assertEqual(post('unificada', 2).status_code, 400)
        self.assertEqual(post('publicada', 2).data['estado'], 'publicada')

    def test_productos_nuevos_transitions_go_through_new_products(self):
        supplier = make_supplier('PROV TR')
        line = lines.create_line('NEW-77', estado='preparacion', origen='proveedor', description='PRODUCTO NUEVO')
        SupplierItem.objects.create(part=line, supplier=supplier, supplier_invent_id='N-1', codigo='NEW-77', brand='', source='upload',
                                    matching_status='pending')
        line.refresh_from_db()
        rejected = self.client.post(f'{M}/lines/{line.pk}/transition/', {'estado': 'rechazada', 'expected_version': line.version},
                                    format='json')
        self.assertEqual(rejected.status_code, 400)  # a rejection needs its reason (Productos nuevos)
        published = self.client.post(f'{M}/lines/{line.pk}/transition/', {'estado': 'publicada', 'expected_version': line.version},
                                     format='json')
        self.assertEqual((published.status_code, published.data['estado']), (200, 'publicada'), published.data)
        self.assertEqual(SupplierItem.objects.get(supplier_invent_id='N-1').matching_status, 'matched')

    def test_references_list_add_main_patch_and_retire(self):
        line = self.line('REF-1', 'REFERENCIAS')
        url = f'{M}/lines/{line.pk}/references/'
        added = self.client.post(url, {'code': 'gwt-41a', 'brand': 'gmb', 'tipo': 'aftermarket', 'source': 'CATÁLOGO GMB'}, format='json')
        self.assertEqual((added.status_code, added.data['added'], added.data['reference']['code']), (201, True, 'GWT-41A'), added.data)
        self.assertEqual([(row['kind'], row['citation']) for row in added.data['reference']['sources']], [('admin', 'CATÁLOGO GMB')])
        again = self.client.post(url, {'code': 'GWT-41A', 'brand': 'GMB', 'tipo': 'aftermarket'}, format='json')
        self.assertEqual((again.status_code, again.data['added']), (200, False))
        other = self.line('OTHER-1')
        refused = self.client.post(f'{M}/lines/{other.pk}/references/', {'code': 'GWT-41A', 'brand': 'GMB'}, format='json')
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(set(refused.data), {'detail'})  # every S7 error body is an object (ReferenceConflict was a bare list)
        self.assertIsInstance(refused.data['detail'], str)
        rows = self.client.get(url).data
        self.assertEqual([(row['code'], row['is_main']) for row in rows], [('REF-1', True), ('GWT-41A', False)])
        ref_id = added.data['reference']['id']
        patched = self.client.patch(f'{M}/references/{ref_id}/', {'expected_version': added.data['reference']['version'], 'notes': 'ok'},
                                    format='json')
        self.assertEqual((patched.status_code, patched.data['notes']), (200, 'ok'), patched.data)
        self.assertEqual(self.client.patch(f'{M}/references/{ref_id}/', {'expected_version': 1, 'notes': 'x'}, format='json').status_code, 409)
        main = self.client.post(f'{M}/lines/{line.pk}/main/', {'reference': ref_id, 'expected_version': 1}, format='json')
        self.assertEqual((main.status_code, main.data['sku'], main.data['main']['id'], main.data['version']), (200, 'GWT-41A', ref_id, 2))
        self.assertEqual(self.client.get(f'{M}/lines/{line.pk}/main/').data['code'], 'GWT-41A')
        main_refused = self.client.delete(f'{M}/references/{ref_id}/')
        self.assertEqual((main_refused.status_code, set(main_refused.data)), (400, {'detail'}))  # the main is never retired
        old_main = Reference.objects.get(part=line, code='REF-1')
        retired = self.client.delete(f'{M}/references/{old_main.pk}/')
        self.assertEqual((retired.status_code, retired.data['estado']), (200, 'retirada'))

    def test_main_conflict_answers_409_naming_the_line(self):
        holder = self.line('TAKEN-1')
        line = self.line('FREE-1')
        ref, _ = references.add(line, 'TAKEN-1', '', tipo='otro', estado='disputa', dispute_note='El SKU TAKEN-1 lo tiene.')
        response = self.client.post(f'{M}/lines/{line.pk}/main/', {'reference': ref.pk}, format='json')
        self.assertEqual((response.status_code, response.data['line']['sku']), (409, holder.sku), response.data)
        self.assertEqual(Part.objects.get(pk=line.pk).sku, 'FREE-1')


class CaseRouteTests(S7Base):
    def merge_case(self, batch=''):
        first, second = self.line('CASE-1', 'DISCO'), self.line('CASE-2', 'DISCO')
        from .reference_matching import merge_case_spec, open_case
        case = open_case(merge_case_spec([first, second], evidence={'codes': ['X']}, reason='prueba', batch_key=batch,
                                         route='lote' if batch else 'caso'))
        return case, first, second

    def test_list_filters_and_detail(self):
        case, first, _ = self.merge_case(batch='lote-prueba')
        response = self.client.get(f'{M}/cases/', {'kind': 'merge', 'route': 'lote', 'batch': 'lote-prueba', 'line': str(first.pk)})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([row['id'] for row in response.data['results']], [str(case.pk)])
        self.assertEqual(response.data['kinds'], {'merge': 1})
        self.assertEqual(self.client.get(f'{M}/cases/', {'kind': 'line'}).data['count'], 0)
        self.assertEqual(self.client.get(f'{M}/cases/{case.pk}/').data['fingerprint'], case.fingerprint)
        self.assertEqual(self.client.get(f'{M}/cases/', {'status': 'nope'}).status_code, 400)

    def test_decide_one_case_with_its_fingerprint(self):
        case, first, second = self.merge_case()
        stale = self.client.post(f'{M}/cases/{case.pk}/', {'action': 'keep_apart', 'fingerprint': '0' * 64}, format='json')
        self.assertEqual(stale.status_code, 409)
        kept = self.client.post(f'{M}/cases/{case.pk}/', {'action': 'keep_apart', 'fingerprint': case.fingerprint}, format='json')
        self.assertEqual((kept.status_code, kept.data['status']), (200, 'dismissed'), kept.data)

    def test_a_batch_is_read_and_decided_case_by_case(self):
        case, first, second = self.merge_case(batch='lote-b')
        summary = self.client.get(f'{M}/cases/batches/lote-b/')
        self.assertEqual((summary.status_code, summary.data['count'], summary.data['counts']), (200, 1, {'review': 1}))
        self.assertEqual(self.client.get(f'{M}/cases/batches/nada/').status_code, 404)
        self.assertEqual(self.client.post(f'{M}/cases/batches/lote-b/', {'action': 'merge', 'cases': []}, format='json').status_code, 400)
        decided = self.client.post(f'{M}/cases/batches/lote-b/', {'action': 'merge', 'cases': [
            {'id': str(case.pk), 'fingerprint': case.fingerprint}, {'id': '00000000-0000-4000-8000-000000000000', 'fingerprint': 'x'}]},
            format='json')
        self.assertEqual(decided.status_code, 200, decided.data)
        self.assertEqual(decided.data['decided'], [str(case.pk)])
        self.assertEqual(len(decided.data['failed']), 1)
        self.assertEqual(Part.objects.get(pk=second.pk).merged_into_id, first.pk)

    def test_the_batch_index_lists_each_batch_with_its_counts(self):
        case, _, _ = self.merge_case(batch='lote-i')
        self.merge_case_between('IDX-1', 'IDX-2', '')  # no batch: not listed
        response = self.client.get(f'{M}/cases/batches/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([(row['batch_key'], row['counts'], row['kinds'], row['count']) for row in response.data],
                         [('lote-i', {'review': 1}, {'merge': 1}, 1)])
        self.assertIsNotNone(response.data[0]['created_at'])

    def merge_case_between(self, a, b, batch):
        from .reference_matching import merge_case_spec, open_case
        return open_case(merge_case_spec([self.line(a, 'DISCO'), self.line(b, 'DISCO')], evidence={'codes': ['Y']}, reason='prueba',
                                         batch_key=batch, route='lote' if batch else 'caso'))

    def test_an_unexpected_failure_in_a_batch_fails_that_case_only(self):
        from unittest.mock import patch
        from . import reference_matching
        case, first, second = self.merge_case(batch='lote-x')
        other = self.merge_case_between('X-3', 'X-4', 'lote-x')
        real = reference_matching.decide

        def decide(case_row, action, **kwargs):
            if case_row.pk == case.pk:
                raise RuntimeError('boom')
            return real(case_row, action, **kwargs)

        with patch('mall.reference_matching.decide', side_effect=decide):
            decided = self.client.post(f'{M}/cases/batches/lote-x/', {'action': 'merge', 'cases': [
                {'id': str(case.pk), 'fingerprint': case.fingerprint}, {'id': str(other.pk), 'fingerprint': other.fingerprint}]}, format='json')
        self.assertEqual(decided.status_code, 200, decided.data)
        self.assertEqual(decided.data['decided'], [str(other.pk)])
        self.assertEqual([row['id'] for row in decided.data['failed']], [str(case.pk)])
        self.assertIn('RuntimeError', decided.data['failed'][0]['detail'])
        self.assertEqual(MatchingCase.objects.get(pk=other.pk).status, 'applied')
        self.assertEqual(MatchingCase.objects.get(pk=case.pk).status, 'review')


class MergeRouteTests(S7Base):
    def test_the_digest_lists_the_automatic_merges_with_their_split_route(self):
        target, source = self.line('MRG-1', 'DESTINO'), self.line('MRG-2', 'ORIGEN')
        merge_lines(target, [source], actor=service_actor(), reason='compact_duplicate', automatic=True)
        manual_target, manual_source = self.line('MAN-1'), self.line('MAN-2')
        merge_lines(manual_target, [manual_source], actor=self.admin, reason='manual')
        response = self.client.get(f'{M}/merges/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['count'], 1)
        row = response.data['results'][0]
        self.assertEqual((row['target']['sku'], row['reason'], row['split']), ('MRG-1', 'compact_duplicate', f'catalog/{target.pk}/split'))
        self.assertEqual([(source['sku'], source['still_merged']) for source in row['sources']], [('MRG-2', True)])
        self.assertEqual(self.client.get(f'{M}/merges/', {'search': 'MRG-2'}).data['count'], 1)
        self.assertEqual(self.client.get(f'{M}/merges/', {'since': '2999-01-01'}).data['count'], 0)
        self.assertEqual(self.client.get(f'{M}/merges/', {'since': 'ayer'}).status_code, 400)


class BrandRouteTests(S7Base):
    def test_create_list_rename_alias_familia_and_merge(self):
        created = self.client.post(f'{M}/brands/', {'nombre': ' toyota ', 'tipo': 'vehiculo'}, format='json')
        self.assertEqual((created.status_code, created.data['nombre']), (201, 'TOYOTA'), created.data)
        toyota = created.data['id']
        self.assertEqual(self.client.post(f'{M}/brands/', {'nombre': 'TOYOTA'}, format='json').status_code, 400)
        alias = self.client.post(f'{M}/brands/{toyota}/aliases/', {'alias': 'toy'}, format='json')
        self.assertEqual((alias.status_code, alias.data['alias']['alias']), (201, 'TOY'), alias.data)
        self.assertEqual(self.client.post(f'{M}/brands/{toyota}/aliases/', {'alias': 'TOYOTA'}, format='json').status_code, 400)
        self.assertEqual([row['nombre'] for row in self.client.get(f'{M}/brands/', {'search': 'toy'}).data['results']], ['TOYOTA'])
        lexus = self.client.post(f'{M}/brands/', {'nombre': 'LEXUS', 'familia': toyota}, format='json').data
        self.assertEqual(lexus['familia']['nombre'], 'TOYOTA')
        self.assertEqual([row['nombre'] for row in self.client.get(f'{M}/brands/', {'familia': toyota}).data['results']], ['LEXUS', 'TOYOTA'])
        line = self.line('LX-1')
        references.retype_main(line, 'oem', brand='LEXUS')
        renamed = self.client.patch(f'{M}/brands/{lexus["id"]}/', {'nombre': 'LEXUS MOTORS'}, format='json')
        self.assertEqual((renamed.status_code, renamed.data['nombre']), (200, 'LEXUS MOTORS'), renamed.data)
        self.assertEqual(references.main_of(line).brand, 'LEXUS MOTORS')
        self.assertIn('LEXUS', [row['alias'] for row in renamed.data['aliases']])
        merged = self.client.patch(f'{M}/brands/{lexus["id"]}/', {'merge_into': toyota}, format='json')
        self.assertEqual((merged.status_code, merged.data['nombre']), (200, 'TOYOTA'), merged.data)
        self.assertFalse(Brand.objects.filter(nombre='LEXUS MOTORS').exists())
        alias_id = BrandAlias.objects.get(alias='TOY').pk
        self.assertEqual(self.client.delete(f'{M}/brands/{toyota}/aliases/{alias_id}/').status_code, 204)
        self.assertEqual(self.client.delete(f'{M}/brands/{toyota}/aliases/{alias_id}/').status_code, 404)

    def test_grupos(self):
        created = self.client.post(f'{M}/grupos/', {'nombre': 'frenos'}, format='json')
        self.assertEqual((created.status_code, created.data['nombre']), (201, 'FRENOS'), created.data)
        self.assertEqual(self.client.post(f'{M}/grupos/', {'nombre': 'Frenos'}, format='json').status_code, 400)
        disco = self.line('G-1', 'DISCO', part_type=part_type_for('FRENOS', 'DISCOS'))
        grupos = self.client.get(f'{M}/grupos/').data
        self.assertEqual([(row['nombre'], [sub['name'] for sub in row['subgrupos']], row['line_count']) for row in grupos],
                         [('FRENOS', ['DISCOS'], 1)])
        renamed = self.client.patch(f'{M}/grupos/{created.data["id"]}/', {'nombre': 'FRENADO'}, format='json')
        self.assertEqual((renamed.status_code, renamed.data['nombre']), (200, 'FRENADO'))
        disco.refresh_from_db()
        self.assertEqual(disco.category, 'FRENADO')


class SeparateRouteTests(S7Base):
    def test_separar_referencia_moves_the_item_to_a_line_of_its_own(self):
        supplier = make_supplier('PROV SEP')
        line = self.line('SEP-1', 'BOMBA')
        item = SupplierItem.objects.create(part=line, supplier=supplier, supplier_invent_id='S-1', codigo='SEP-1', brand='', source='upload',
                                           matching_status='matched')
        response = self.client.post(f'{M}/inventory/{item.pk}/separate/', {}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['line']['estado'], 'preparacion')
        self.assertNotEqual(response.data['item']['part'], line.pk)
        item.refresh_from_db()
        self.assertEqual(str(item.part_id), str(response.data['line']['id']))
        waiting = SupplierItem.objects.create(part=line, supplier=supplier, supplier_invent_id='S-2', codigo='SEP-1', brand='', source='upload',
                                              matching_status='review', codigo_pendiente='SEP-9')
        self.assertEqual(self.client.post(f'{M}/inventory/{waiting.pk}/separate/', {}, format='json').status_code, 409)


class StorefrontTests(APITestCase):
    def setUp(self):
        self.buyer = make_user('s7-buyer')
        make_client('CLIENTE S7', self.buyer)
        self.supplier = make_supplier('PROVEEDOR S7')
        self.client.force_authenticate(self.buyer)

    def test_lines_carry_brand_tipo_estado_and_codes_their_tipo_and_estado(self):
        line = make_line('BR-1', description='AMORTIGUADOR')
        references.retype_main(line, 'aftermarket', brand='KYB')
        references.add(line, '334-xxx', 'TOYOTA', tipo='oem')
        row = self.client.get('/api/v1/catalog/', {'search': 'amortiguador'}).data['results'][0]
        self.assertEqual((row['brand'], row['tipo'], row['estado']), ('KYB', 'AFTERMARKET', 'publicada'))
        self.assertEqual([(code['code'], code['tipo'], code['estado'], code['ref_type']) for code in row['codes']],
                         [('334-XXX', 'oem', 'declarado', 'oem')])

    def test_offers_carry_the_variant_data(self):
        line = make_line('OF-1', description='DISCO')
        item = SupplierItem.objects.create(part=line, supplier=self.supplier, supplier_invent_id='O-1', codigo='OF-1', brand='', source='upload',
                                           matching_status='matched', reported_quantity=4, calidad='original', condicion='nuevo',
                                           garantia='6 MESES', unidades_por_empaque=2)
        offer = self.client.get(f'/api/v1/catalog/{line.pk}/suppliers/').data[0]['items'][0]
        item.refresh_from_db()
        ref = item.reference
        self.assertEqual({key: offer[key] for key in ('calidad', 'condicion', 'garantia', 'unidades_por_empaque', 'reference')},
                         {'calidad': 'original', 'condicion': 'nuevo', 'garantia': '6 MESES', 'unidades_por_empaque': 2,
                          'reference': {'id': ref.pk, 'code': ref.code, 'brand': ref.brand, 'tipo': ref.tipo}})

    def test_brand_and_calidad_filter_and_count(self):
        kyb, plain = make_line('F-KYB', description='AMORT'), make_line('F-PLAIN', description='AMORT')
        references.retype_main(kyb, 'aftermarket', brand='KYB')
        SupplierItem.objects.create(part=plain, supplier=self.supplier, supplier_invent_id='Q-1', codigo='F-PLAIN', brand='', source='upload',
                                    matching_status='matched', reported_quantity=1, calidad='oes')
        skus = lambda **params: [row['sku'] for row in self.client.get('/api/v1/catalog/', params).data['results']]
        self.assertEqual(skus(brand='kyb'), ['F-KYB'])
        self.assertEqual(skus(calidad='oes'), ['F-PLAIN'])
        self.assertEqual(self.client.get('/api/v1/catalog/', {'calidad': 'nope'}).status_code, 400)
        facets = self.client.get('/api/v1/catalog/', {'include_facets': '1'}).data['facets']
        self.assertEqual(facets['brand'], [{'value': 'KYB', 'label': 'KYB', 'count': 1}])
        self.assertEqual({row['value']: row['count'] for row in facets['calidad']}, {'original': 0, 'oes': 1, 'alternativo': 0})
