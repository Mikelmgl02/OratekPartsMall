"""Inventory refactor S4: library numbers are References on the line that carries them, or on lines of their own in referencia
(mall.references.add_library_number), a code absorbs such a line (rule 3f), a new catalog line with its codigo promotes it, a number
taken off its line goes back to one of its own (durability), the rows hanging from a number protect it, and the Biblioteca keeps its
legacy JSON. python -m mall.refactor.s4_library is mall.test_refactor_s4's."""

from django.db import models
from django.test import TestCase
from rest_framework.test import APIClient

from . import references
from .brand_models import Brand
from .lines import LineStateError, catalog_lines, create_line, storefront_lines
from .models import CatalogMerge, Part, PartType, Reference, ReferenceSource
from .technical_models import TechnicalTemplate
from .oem_finder import load_snapshot
from .test_factories import Fresh, make_line, make_reference, make_user

URL = '/api/v1/management/oem-references/'


def library_ref(maker, code):
    return Reference.objects.get(references.library_q(), brand=maker, clave=references.clave(code))


class LibraryNumberTests(Fresh, TestCase):
    def setUp(self):
        super().setUp()
        self.root = make_user('s4-root', superuser=True)
        toyota = Brand.objects.create(nombre='TOYOTA', tipo='vehiculo')
        Brand.objects.create(nombre='LEXUS', tipo='vehiculo', familia=toyota)

    def test_a_number_no_line_carries_gets_a_line_of_its_own_in_referencia(self):
        ref, created = references.add_library_number('toyota', '16100-79445', description='bomba agua', part_type='BOMBAS DE AGUA',
                                                     applications='HILUX 2005-2015', source_kind='aftermarket_catalog', citation='GMB 2016 · pág. 4',
                                                     source_detail={'catalog': 'gmb_wp_2016'}, actor=self.root)
        self.assertTrue(created)
        line = ref.part
        self.assertEqual((line.sku, line.estado, line.origen, line.active, line.tipo, line.is_OEM),
                         ('16100-79445', 'referencia', 'biblioteca', False, 'OEM', True))
        self.assertEqual((line.description, line.aplicaciones_texto, line.category, line.subcategory),
                         ('BOMBA AGUA', 'HILUX 2005-2015', 'REFRIGERACIÓN', 'BOMBAS DE AGUA'))
        self.assertEqual(line.part_type, PartType.objects.get(grupo__nombre='REFRIGERACIÓN', name='BOMBAS DE AGUA'))
        self.assertTrue(TechnicalTemplate.objects.filter(part_type=line.part_type).exists())  # created with its template
        self.assertEqual((ref.is_main, ref.code, ref.brand, ref.tipo, ref.estado, ref.clave), (True, '16100-79445', 'TOYOTA', 'oem', 'declarado', '1610079445'))
        self.assertEqual({key: ref.detail[key] for key in ('description', 'part_type', 'applications', 'origen')},
                         {'description': 'BOMBA AGUA', 'part_type': 'BOMBAS DE AGUA', 'applications': 'HILUX 2005-2015', 'origen': 'biblioteca'})
        source = ref.sources.get()
        self.assertEqual((source.kind, source.citation, source.detail['legacy_kind'], source.detail['catalog']),
                         ('catalogo', 'GMB 2016 · pág. 4', 'aftermarket_catalog', 'gmb_wp_2016'))
        self.assertEqual(line.field_sources['subgrupo'], {'source': 'biblioteca', 'texto': 'BOMBAS DE AGUA'})
        # Library data, never a catalog line: off the storefront, the admin grid and the OEM finder.
        self.assertFalse(storefront_lines().filter(pk=line.pk).exists())
        self.assertFalse(catalog_lines().filter(pk=line.pk).exists())
        self.assertNotIn(str(line.pk), {p['id'] for p in load_snapshot().parts})
        # Another sighting merges in place: its spelling, blank texts, the source; nothing new.
        again, created = references.add_library_number('TOYOTA', '1610079445', printed=['16100 79445'], source_kind='price_list',
                                                       citation='LISTA 2026', actor=self.root)
        self.assertFalse(created)
        self.assertEqual(again.pk, ref.pk)
        again.refresh_from_db()
        self.assertEqual((again.estado, again.printed_forms), ('verificado', ['16100 79445']))
        self.assertEqual(Part.objects.filter(origen='biblioteca').count(), 1)

    def test_an_unmapped_part_type_stays_for_revision_ia(self):
        ref, _ = references.add_library_number('TOYOTA', '90080-91188', part_type='TERM CREM')
        self.assertEqual((ref.part.category, ref.part.subcategory, ref.part.part_type_id), ('', '', None))
        self.assertEqual(ref.part.field_sources['subgrupo'], {'source': 'biblioteca', 'texto': 'TERM CREM', 'sin_mapa': True})

    def test_a_number_goes_on_the_line_that_carries_it(self):
        line = make_line('16100-79445', description='BOMBA DE AGUA TOY')  # its brandless main carries the number
        ref, created = references.add_library_number('TOYOTA', '16100-79445', source_kind='manual', citation='MANUAL', actor=self.root)
        self.assertTrue(created)
        self.assertEqual((ref.part_id, ref.is_main, ref.brand, ref.tipo, ref.detail['origen']), (line.pk, False, 'TOYOTA', 'oem', 'biblioteca'))
        main = references.main_of(line)
        self.assertEqual((main.brand, main.code), ('', '16100-79445'))  # the main stays brandless beside it
        self.assertFalse(Part.objects.filter(estado='referencia').exists())
        # A familia brand carries it too: LEXUS's number goes on the TOYOTA line, under its own maker.
        toyota = make_line('43512-60150')
        make_reference(toyota, '43512-60150', brand='TOYOTA', tipo='oem')
        Reference.objects.filter(part=toyota, brand='').update(code='43512-60150-X')  # its main no longer holds the key brandless
        Part.objects.filter(pk=toyota.pk).update(sku='43512-60150-X')
        lexus, _ = references.add_library_number('LEXUS', '4351260150', actor=self.root)
        self.assertEqual((lexus.part_id, lexus.brand), (toyota.pk, 'LEXUS'))

    def test_adding_a_code_absorbs_the_referencia_line_and_creating_a_line_promotes_it(self):
        ref, _ = references.add_library_number('TOYOTA', '17801-30070', description='FILTRO AIRE', part_type='FILTRO AIRE')
        referencia = ref.part
        ReferenceSource.objects.create(reference=ref, kind='admin', citation='NOTA')
        sku = make_line('FILTRO-ABC')
        code, added = references.add(sku, '17801-30070', '', actor=self.root)  # rule 3f: the referencia line is merged into the SKU
        self.assertTrue(added)
        referencia.refresh_from_db()
        ref.refresh_from_db()
        self.assertEqual((referencia.estado, referencia.merged_into_id), ('unificada', sku.pk))
        self.assertEqual((ref.part_id, ref.is_main, ref.estado), (sku.pk, False, 'declarado'))
        self.assertEqual(ref.sources.count(), 2)  # its sources came along
        self.assertEqual(CatalogMerge.objects.get(target=sku).summary['reason'], 'biblioteca')
        # Promote: a catalog line created with a referencia line's codigo is that line, published.
        other, _ = references.add_library_number('TOYOTA', '04465-0K290', description='PASTILLA')
        line = create_line('04465-0K290', description='PASTILLA DEL HILUX', actor=self.root)
        self.assertEqual(line.pk, other.part_id)
        line.refresh_from_db()
        self.assertEqual((line.estado, line.origen, line.active, line.description), ('publicada', 'biblioteca', True, 'PASTILLA DEL HILUX'))
        self.assertEqual(references.main_of(line).pk, other.pk)

    def test_two_makes_sharing_a_codigo_need_the_brand(self):
        acura, _ = references.add_library_number('ACURA', '44310-STX-305')
        honda, _ = references.add_library_number('HONDA', '44310-STX-305')
        self.assertNotEqual(acura.part_id, honda.part_id)  # two lines in referencia with one codigo (S4)
        with self.assertRaises(LineStateError) as refused:
            create_line('44310-STX-305')
        self.assertIn('ACURA y HONDA', str(refused.exception))
        line = create_line('44310-STX-305', library_brand='HONDA')
        self.assertEqual((line.pk, line.estado), (honda.part_id, 'publicada'))

    def test_a_library_number_taken_off_its_line_goes_back_to_a_line_of_its_own(self):
        line = make_line('BOMBA-GMB-1')
        ref, _ = references.add_library_number('TOYOTA', '16100-39315', actor=self.root)
        self.assertEqual(ref.part.estado, 'referencia')
        references.add(line, '16100-39315', 'TOYOTA', tipo='oem', actor=self.root)  # absorbs it onto the SKU
        ref.refresh_from_db()
        self.assertEqual(ref.part_id, line.pk)
        self.assertEqual(references.retire(ref, actor=self.root, reason='Editar SKU'), 1)
        ref.refresh_from_db()
        self.assertEqual((ref.estado, ref.is_main, ref.part.estado, ref.part.origen, ref.part.sku), ('declarado', True, 'referencia', 'biblioteca', '16100-39315'))
        self.assertEqual(ref.detail['detached']['sku'], 'BOMBA-GMB-1')
        # A code that is no library number is retired as before.
        plain = make_reference(line, 'ALT-123')
        references.retire(plain)
        plain.refresh_from_db()
        self.assertEqual(plain.estado, 'retirada')

class BibliotecaTests(Fresh, TestCase):
    def setUp(self):
        super().setUp()
        self.root = make_user('s4-biblio', superuser=True)
        self.client = APIClient()
        self.client.force_authenticate(self.root)
        self.ref, _ = references.add_library_number('TOYOTA', '16100-79445', description='BOMBA AGUA', part_type='BOMBAS DE AGUA',
                                                    source_kind='aftermarket_catalog', citation='GMB 2016', source_detail={'catalog': 'gmb'})
        references.add(self.ref.part_id, 'GWT-78A', 'GMB', tipo='aftermarket', source='GMB 2016 · pág. 4')  # S9: the code is the line's

    def test_rows_keep_the_legacy_values_and_the_filters_take_both(self):
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200, response.content)
        row = response.json()['results'][0]
        self.assertEqual((row['id'], row['manufacturer'], row['code'], row['status'], row['source_kinds']),
                         (str(self.ref.uid), 'TOYOTA', '1610079445', 'declared', {'aftermarket_catalog': 1}))
        self.assertEqual((row['description'], row['part_type'], row['cross_reference_count'], row['linked_count']), ('BOMBA AGUA', 'BOMBAS DE AGUA', 1, 0))
        self.assertEqual(response.json()['counts'], {'status': {'declared': 1}, 'manufacturer': {'TOYOTA': 1}, 'total': 1})
        for params in ({'status': 'declared'}, {'status': 'declarado'}, {'source_kind': 'aftermarket_catalog'}, {'source_kind': 'catalogo'},
                       {'q': 'GWT78'}, {'q': '1610079'}, {'manufacturer': 'toyota'}, {'linked': 'false'}):
            self.assertEqual(self.client.get(URL, params).json()['count'], 1, params)
        for params in ({'status': 'verified'}, {'source_kind': 'manual'}, {'linked': 'true'}):
            self.assertEqual(self.client.get(URL, params).json()['count'], 0, params)
        detail = self.client.get(f'{URL}{self.ref.uid}/').json()
        self.assertEqual([source['kind'] for source in detail['sources']], ['aftermarket_catalog'])
        self.assertEqual([code['code'] for code in detail['cross_references']], ['GWT-78A'])

    def test_a_published_line_lists_itself_via_linea(self):
        line = create_line('16100-79445', actor=self.root)  # promotes the referencia line
        row = self.client.get(f'{URL}{self.ref.uid}/').json()
        self.assertEqual([(sku['id'], sku['via']) for sku in row['linked_skus']], [(str(line.pk), 'linea')])

    def test_edits_write_the_detail_and_the_referencia_line_only(self):
        version = self.client.get(f'{URL}{self.ref.uid}/').json()['version']
        response = self.client.patch(f'{URL}{self.ref.uid}/', {'expected_version': version, 'action': 'edit', 'description': 'BOMBA DE AGUA',
                                                              'part_type': 'DISCOS DE FRENO'}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        self.ref.refresh_from_db()
        self.assertEqual((self.ref.detail['description'], self.ref.detail['part_type'], self.ref.detail['updated_by']),
                         ('BOMBA DE AGUA', 'DISCOS DE FRENO', 's4-biblio'))
        line = self.ref.part
        line.refresh_from_db()
        self.assertEqual((line.description, line.subcategory), ('BOMBA DE AGUA', 'DISCOS DE FRENO'))
        stale = self.client.patch(f'{URL}{self.ref.uid}/', {'expected_version': version, 'action': 'edit', 'notes': 'X'}, format='json')
        self.assertEqual(stale.status_code, 409)
        # A published line keeps its own texts.
        published = create_line('16100-79445', actor=self.root, description='MI BOMBA')
        version = self.client.get(f'{URL}{self.ref.uid}/').json()['version']
        self.client.patch(f'{URL}{self.ref.uid}/', {'expected_version': version, 'action': 'edit', 'description': 'OTRA'}, format='json')
        published.refresh_from_db()
        self.assertEqual(published.description, 'MI BOMBA')

    def test_dispute_supersede_sources_and_creation(self):
        self.ref.refresh_from_db()
        version = self.ref.version
        response = self.client.patch(f'{URL}{self.ref.uid}/', {'expected_version': version, 'action': 'dispute', 'note': 'NO ES BOMBA'}, format='json')
        self.assertEqual(response.json()['status'], 'disputed')
        self.ref.refresh_from_db()
        self.assertEqual((self.ref.estado, self.ref.dispute_note), ('disputa', 'NO ES BOMBA'))
        response = self.client.patch(f'{URL}{self.ref.uid}/', {'expected_version': self.ref.version, 'action': 'clear_dispute'}, format='json')
        self.assertEqual(response.json()['status'], 'declared')
        self.ref.refresh_from_db()
        response = self.client.patch(f'{URL}{self.ref.uid}/', {'expected_version': self.ref.version, 'action': 'set_superseded_by',
                                                              'code': '16100-79446'}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        new = library_ref('TOYOTA', '1610079446')
        self.assertEqual(response.json()['superseded_by']['id'], str(new.uid))
        self.ref.refresh_from_db()
        response = self.client.patch(f'{URL}{self.ref.uid}/', {'expected_version': self.ref.version, 'action': 'add_source',
                                                              'source': {'kind': 'manual', 'citation': 'EPC', 'verified': True}}, format='json')
        self.assertEqual(response.json()['status'], 'verified')
        created = self.client.post(URL, {'manufacturer': 'MITSUBISHI', 'code': 'MR-968365'}, format='json')
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(created.json()['code'], 'MR968365')
        self.assertEqual(self.client.post(URL, {'manufacturer': 'MITSUBISHI', 'code': 'MR968365'}, format='json').status_code, 200)
