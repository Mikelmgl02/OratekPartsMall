from django.test import override_settings
from rest_framework.test import APITestCase

from .catalog_identity import mark_exact_matches
from . import references
from .models import CatalogIdentityChange, Part, PartCode, PartOEMLink, Reference, User
from .oem_reference import LibraryNumber

URL = '/api/v1/management/catalog/'
LIBRARY = '/api/v1/management/oem-references/'


def listed(manufacturer, code, status='declared', kind='aftermarket_catalog'):
    """A number of the OEM list (S4: a library Reference, on the line that carries it or its own in referencia), as a catalog files it."""
    ref = references.add_library_number(manufacturer, code, source_kind=kind, citation='ATE K24-F · pág. 3', description='DISCO')[0]
    if status == 'disputed':
        ref = references.update(ref, estado='disputa', dispute_note='Es de otro modelo')
    return ref


def library(**filters):
    return Reference.objects.filter(references.library_q(), **filters)


class CatalogMarkOEMTests(APITestCase):
    """A SKU is OEM when its code is in the OEM list (from S4 the library's References). Marcar como OEM ties a SKU to the number the
    list already has; Crear OEM desde SKU adds the number first. Quitar marca OEM only applies to a SKU whose code is not in the list.
    The endpoints have no screen since the owner retired OEM linking (2026-10-14)."""

    def setUp(self):
        self.root = User.objects.create_superuser('mark-root', 'mark-root@example.invalid', 'Example938!')
        self.client.force_authenticate(self.root)
        self.disc = Part.objects.create(sku='43512-60150', description='DISCO FRENO DEL TOY LAND CRUISER')
        self.pump = Part.objects.create(sku='16100-29155', description='BOMBA AGUA TOY 22R')

    def mark(self, rows):
        return self.client.post(f'{URL}mark-oem/', {'rows': rows}, format='json')

    def create(self, rows):
        return self.client.post(f'{URL}create-oem/', {'rows': rows}, format='json')

    def test_the_plan_offers_the_numbers_of_the_list_and_sends_the_rest_to_crear(self):
        listed('LEXUS', '4351260150')
        listed('TOYOTA', '4351260150')
        suffixed = Part.objects.create(sku='0110-ACA30-SPK', description='PUNTA FLECHA EXT TOY RAV4 05-13')
        wordy = Part.objects.create(sku='BOMBA-AGUA', description='BOMBA AGUA')
        plan = self.client.get(f'{URL}mark-oem/', {'ids': f'{self.disc.pk},{self.pump.pk},{suffixed.pk},{wordy.pk}'}).json()
        rows = {row['sku']: row for row in plan['rows']}
        disc = rows['43512-60150']
        self.assertEqual((disc['suggested'], disc['blocked'], [item['manufacturer'] for item in disc['listed']]), ('TOYOTA', '', ['LEXUS', 'TOYOTA']))
        self.assertEqual(disc['listed'][1]['id'], str(library(brand='TOYOTA').get().uid))  # the Biblioteca's id
        self.assertIn('2 fabricantes', disc['warnings'][0])
        self.assertIn('Crear OEM desde SKU', rows['16100-29155']['blocked'])
        self.assertIn('sufijo', rows['0110-ACA30-SPK']['warnings'][0])
        self.assertIn('necesita dígitos', rows['BOMBA-AGUA']['blocked'])
        created = {row['sku']: row for row in self.client.get(f'{URL}create-oem/', {'ids': str(self.pump.pk)}).json()['rows']}['16100-29155']
        self.assertEqual((created['suggested'], created['blocked'], created['description_for_list']), ('TOYOTA', '', 'BOMBA AGUA TOY 22R'))

    def test_marking_absorbs_the_numbers_line_in_referencia_and_makes_the_main_oem(self):
        number = listed('TOYOTA', '4351260151')  # no line carries it: its own line in referencia
        disc = Part.objects.create(sku='43512-60151-X', description='DISCO FRENO TOY')
        Reference.objects.filter(part=disc, is_main=True).update(code='43512-60151')  # its main spells the number, its codigo a suffix
        Part.objects.filter(pk=disc.pk).update(sku='43512-60151')
        response = self.mark([{'id': str(disc.pk), 'manufacturer': 'toyota', 'citation': 'EPC 2026 pág. 4'}])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['updated'][0]['is_OEM'])
        number.refresh_from_db()
        self.assertEqual((number.part_id, number.is_main, number.tipo), (disc.pk, False, 'oem'))
        self.assertEqual(references.main_of(disc).tipo, 'oem')
        self.assertEqual(library().count(), 1)  # marking adds no number to the list
        refused = self.mark([{'id': str(self.pump.pk), 'manufacturer': 'TOYOTA'}])
        self.assertEqual(refused.status_code, 400)
        self.assertIn('no está en la lista OEM', refused.json()['detail'])
        self.pump.refresh_from_db()
        self.assertFalse(self.pump.is_OEM)

    def test_crear_adds_the_number_to_the_sku_and_marks_it(self):
        response = self.create([{'id': str(self.pump.pk), 'manufacturer': 'TOYOTA', 'citation': 'EPC', 'part_type': 'bomba de agua'}])
        self.assertEqual(response.status_code, 200, response.content)
        self.pump.refresh_from_db()
        self.assertTrue(self.pump.is_OEM)
        number = LibraryNumber(library(brand='TOYOTA', clave='1610029155').get())
        self.assertEqual((number.status, number.description, number.part_type, number.ref.part_id), ('declared', 'BOMBA AGUA TOY 22R', 'BOMBA DE AGUA', self.pump.pk))
        self.assertIn(('manual', 'Creado desde el SKU por mark-root · EPC'), [(source.kind, source.citation) for source in number.sources])
        again = self.create([{'id': str(self.disc.pk), 'manufacturer': 'TOYOTA'}, {'id': str(self.pump.pk), 'manufacturer': 'TOYOTA'}])
        self.assertEqual(again.status_code, 400)
        self.assertIn('usa «Marcar como OEM»', again.json()['detail'])
        self.assertFalse(library(clave='4351260150').exists())  # all or none

    def test_a_number_another_sku_already_holds_marks_nothing(self):
        holder = Part.objects.create(sku='DISCO-TOY-1', description='DISCO')
        PartCode.objects.create(part=holder, brand='TOYOTA', code='4351260150', ref_type='oem', reference_source='CATÁLOGO TOYOTA')
        self.assertEqual(listed('TOYOTA', '4351260150').part_id, holder.pk)  # the number is the alterno it holds
        listed('TOYOTA', '1610029155')
        refused = self.mark([{'id': str(self.pump.pk), 'manufacturer': 'TOYOTA'}, {'id': str(self.disc.pk), 'manufacturer': 'TOYOTA'}])
        self.assertEqual(refused.status_code, 400)
        self.assertIn('DISCO-TOY-1', refused.json()['detail'])
        self.pump.refresh_from_db()
        self.assertEqual((self.pump.is_OEM, references.main_of(self.pump).tipo), (False, 'otro'))  # all or none
        self.assertEqual(self.mark([{'id': str(self.pump.pk), 'manufacturer': ' '}]).status_code, 400)

    def test_unmarking_applies_only_to_a_sku_whose_code_is_not_in_the_list(self):
        listed('TOYOTA', '4351260150')
        self.assertEqual(self.mark([{'id': str(self.disc.pk), 'manufacturer': 'TOYOTA'}]).status_code, 200)
        refused = self.client.post(f'{URL}unmark-oem/', {'rows': [{'id': str(self.disc.pk), 'retire_number': True}]}, format='json')
        self.assertEqual(refused.status_code, 400)
        self.assertIn('ponlo en disputa', refused.json()['detail'])
        stray = Part.objects.create(sku='58411-1G000', description='TAMBOR HYU ACCENT', is_OEM=True)  # marked by hand, no number in the list
        response = self.client.post(f'{URL}unmark-oem/', {'rows': [{'id': str(stray.pk), 'retire_number': True}]}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        stray.refresh_from_db()
        self.assertFalse(stray.is_OEM)
        self.assertFalse(PartCode.objects.filter(is_main=False, part=stray).exists())

    def test_superusers_only(self):
        self.client.force_authenticate(User.objects.create_user('mark-staff', 'mark-staff@example.invalid', 'Example938!'))
        self.assertEqual(self.mark([{'id': str(self.disc.pk), 'manufacturer': 'TOYOTA'}]).status_code, 403)
        self.assertEqual(self.create([{'id': str(self.disc.pk), 'manufacturer': 'TOYOTA'}]).status_code, 403)


class RetiredExactMarkTests(APITestCase):
    """S4 retires the marks of a SKU whose code is a number of the list (mark_exact_matches, mark_exact_oem): the number is one of the
    line's References, a line's tipo mirrors its main, and a dispute is the number's estado, which unmarks no line."""

    def setUp(self):
        self.root = User.objects.create_superuser('exact-root', 'exact-root@example.invalid', 'Example938!')
        self.client.force_authenticate(self.root)

    def test_nothing_marks_and_a_dispute_unmarks_nothing(self):
        part = Part.objects.create(sku='MR-453383', description='EJE COMPLETO MIT MONTERO 01-06 LH')
        number = listed('MITSUBISHI', 'MR453383')
        self.assertEqual((number.part_id, mark_exact_matches(self.root)), (part.pk, []))
        Part.objects.filter(pk=part.pk).update(is_OEM=True, tipo='OEM')
        response = self.client.patch(f'{LIBRARY}{number.uid}/', {'expected_version': number.version, 'action': 'dispute',
                                                                 'note': 'Es de otro modelo'}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        part.refresh_from_db()
        self.assertTrue(part.is_OEM)
        self.assertFalse(CatalogIdentityChange.objects.filter(part=part).exists())

    def test_a_number_added_to_the_library_goes_on_its_line_without_marking_it(self):
        part = Part.objects.create(sku='19200-P2A-A01', description='BOMBA AGUA HON CIVIC')
        response = self.client.post(LIBRARY, {'manufacturer': 'HONDA', 'code': '19200-P2A-A01',
                                              'source': {'kind': 'manufacturer_catalog', 'citation': 'EPC', 'verified': False}}, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual([(row['sku'], row['via']) for row in response.json()['linked_skus']], [('19200-P2A-A01', 'linea')])
        part.refresh_from_db()
        self.assertFalse(part.is_OEM)

    @override_settings(OEM_EXACT_MARK=True)
    def test_the_matching_pass_marks_nothing_even_with_oem_exact_mark_on(self):
        from .matching_queue import matching_work
        from .matching_worker import process_queue
        part = Part.objects.create(sku='MR-453383', description='EJE MIT')
        listed('MITSUBISHI', 'MR453383')
        with matching_work():
            summary = process_queue(force=True, use_ai=False)  # S6: the pass has no exact-mark or rename step at all
        part.refresh_from_db()
        self.assertFalse(part.is_OEM)
        self.assertNotIn('marked_exact_oems', summary)

    def test_the_command_says_it_is_retired(self):
        import io
        from . import mark_exact_oem
        out = io.StringIO()
        mark_exact_oem.main(['--dry-run'], stdout=out)
        self.assertIn('retirado con S4', out.getvalue())

    def test_an_oem_alterno_only_the_finder_backs_retires_and_its_links_stay(self):
        tied = Part.objects.create(sku='16100-19125-GMB', description='BOMBA AGUA TOY')
        owner = Part.objects.create(sku='16100-19125-X', description='BOMBA AGUA TOY')
        code, _ = references.add(owner, '16100-19125', 'TOYOTA', ref_type='oem', source='algo:oem-finder:v2:current:9')
        PartOEMLink.objects.create(part=tied, reference=code, source='catalog', citations=['GMB Water Pump Catalog 2016 · pág. 556'])
        references.retire(code)
        code.refresh_from_db()
        self.assertEqual(code.estado, 'retirada')  # no library source names it: retired, never deleted, and its link keeps pointing at it
        self.assertTrue(PartOEMLink.objects.filter(part=tied, reference=code).exists())


class MarkingRulesTests(APITestCase):
    """The rules around the two actions."""

    def setUp(self):
        self.root = User.objects.create_superuser('rules-root', 'rules-root@example.invalid', 'Example938!')
        self.client.force_authenticate(self.root)

    def test_marking_needs_the_number_under_that_make_and_not_in_dispute(self):
        disc = Part.objects.create(sku='43512-60150', description='DISCO FRENO DEL TOY LAND CRUISER')
        listed('LEXUS', '4351260150')  # filed only under LEXUS
        refused = self.client.post(f'{URL}mark-oem/', {'rows': [{'id': str(disc.pk), 'manufacturer': 'TOYOTA'}]}, format='json')
        self.assertEqual(refused.status_code, 400)
        self.assertIn('TOYOTA 4351260150 no está en la lista OEM', refused.json()['detail'])
        self.assertEqual(list(library().values_list('brand', 'clave')), [('LEXUS', '4351260150')])  # no TOYOTA number crept in
        pump = Part.objects.create(sku='16100-29155', description='BOMBA AGUA TOY 22R')
        listed('TOYOTA', '1610029155', status='disputed')
        refused = self.client.post(f'{URL}mark-oem/', {'rows': [{'id': str(pump.pk), 'manufacturer': 'TOYOTA'}]}, format='json')
        self.assertEqual(refused.status_code, 400)
        plan = self.client.get(f'{URL}mark-oem/', {'ids': str(pump.pk)}).json()['rows'][0]
        self.assertIn('en disputa', plan['blocked'])
        created = self.client.post(f'{URL}create-oem/', {'rows': [{'id': str(pump.pk), 'manufacturer': 'TOYOTA'}]}, format='json')
        self.assertIn('resuelve la disputa', created.json()['detail'])

    def test_crear_never_proposes_a_make_the_list_already_has(self):
        filter_ = Part.objects.create(sku='90915-YZZD4', description='FILTRO ACEITE TOY HILUX')
        listed('TOYOTA', '90915YZZD4', status='inferred', kind='oem_finder')
        row = self.client.get(f'{URL}create-oem/', {'ids': str(filter_.pk)}).json()['rows'][0]
        self.assertEqual(row['suggested'], '')
        self.assertIn('Ya está en la lista como TOYOTA 90915YZZD4', ' '.join(row['warnings']))

    def test_the_sku_editor_follows_the_oem_list(self):
        exact = Part.objects.create(sku='MR-453383', name='MR-453383', description='EJE MIT')
        listed('MITSUBISHI', 'MR453383')
        self.assertEqual(self.client.patch(f'{URL}{exact.pk}/', {'is_OEM': True}, format='json').status_code, 200)  # its code is in the list
        refused = self.client.patch(f'{URL}{exact.pk}/', {'is_OEM': False}, format='json')
        self.assertEqual(refused.status_code, 400)
        self.assertIn('ponlo en disputa', str(refused.json()))
        loose = Part.objects.create(sku='58411-1G000', name='58411-1G000', description='TAMBOR HYU')
        refused = self.client.patch(f'{URL}{loose.pk}/', {'is_OEM': True}, format='json')
        self.assertEqual(refused.status_code, 400)
        self.assertIn('no está en la lista OEM', str(refused.json()))
        loose.refresh_from_db()
        self.assertFalse(loose.is_OEM)
        same = self.client.patch(f'{URL}{exact.pk}/', {'is_OEM': True, 'description': 'EJE COMPLETO MIT'}, format='json')
        self.assertEqual(same.status_code, 200, same.content)
