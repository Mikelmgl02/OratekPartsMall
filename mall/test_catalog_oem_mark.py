from django.test import override_settings
from rest_framework.test import APITestCase

from .catalog_identity import mark_exact_matches
from .models import CatalogIdentityChange, Part, PartCode, PartOEMLink, User
from .oem_reference_models import OEMReference, OEMReferenceSource

URL = '/api/v1/management/catalog/'
LIBRARY = '/api/v1/management/oem-references/'


def listed(manufacturer, code, status='declared', kind='aftermarket_catalog'):
    """A number of the OEM list, as a catalog import files it."""
    ref = OEMReference.objects.create(manufacturer=manufacturer, code=code, status=status, description='DISCO',
                                      dispute_note='Es de otro modelo' if status == 'disputed' else '')
    OEMReferenceSource.objects.create(reference=ref, kind=kind, citation='ATE K24-F · pág. 3')
    return ref


class CatalogMarkOEMTests(APITestCase):
    """A SKU is OEM when its code is in the OEM list. Marcar como OEM ties a SKU to the item the list already has; Crear OEM desde SKU
    adds the item first. Quitar marca OEM only applies to a SKU whose code is not in the list."""

    def setUp(self):
        self.root = User.objects.create_superuser('mark-root', 'mark-root@example.invalid', 'Example938!')
        self.client.force_authenticate(self.root)
        self.disc = Part.objects.create(sku='43512-60150', description='DISCO FRENO DEL TOY LAND CRUISER')
        self.pump = Part.objects.create(sku='16100-29155', description='BOMBA AGUA TOY 22R')

    def mark(self, rows):
        return self.client.post(f'{URL}mark-oem/', {'rows': rows}, format='json')

    def create(self, rows):
        return self.client.post(f'{URL}create-oem/', {'rows': rows}, format='json')

    def test_the_plan_offers_the_items_of_the_list_and_sends_the_rest_to_crear(self):
        listed('LEXUS', '4351260150')
        listed('TOYOTA', '4351260150')
        suffixed = Part.objects.create(sku='0110-ACA30-SPK', description='PUNTA FLECHA EXT TOY RAV4 05-13')
        wordy = Part.objects.create(sku='BOMBA-AGUA', description='BOMBA AGUA')
        plan = self.client.get(f'{URL}mark-oem/', {'ids': f'{self.disc.pk},{self.pump.pk},{suffixed.pk},{wordy.pk}'}).json()
        rows = {row['sku']: row for row in plan['rows']}
        disc = rows['43512-60150']
        self.assertEqual((disc['suggested'], disc['blocked'], [item['manufacturer'] for item in disc['listed']]), ('TOYOTA', '', ['LEXUS', 'TOYOTA']))
        self.assertIn('2 fabricantes', disc['warnings'][0])
        self.assertIn('Crear OEM desde SKU', rows['16100-29155']['blocked'])
        self.assertIn('sufijo', rows['0110-ACA30-SPK']['warnings'][0])
        self.assertIn('necesita dígitos', rows['BOMBA-AGUA']['blocked'])
        created = {row['sku']: row for row in self.client.get(f'{URL}create-oem/', {'ids': str(self.pump.pk)}).json()['rows']}['16100-29155']
        self.assertEqual((created['suggested'], created['blocked'], created['description_for_list']), ('TOYOTA', '', 'BOMBA AGUA TOY 22R'))

    def test_marking_ties_the_sku_to_the_item_the_list_has(self):
        reference = listed('TOYOTA', '4351260150')
        PartCode.objects.create(part=self.disc, code='43512-60150')  # the neutral alias a merge left
        alias = PartCode.objects.get(part=self.disc)
        response = self.mark([{'id': str(self.disc.pk), 'manufacturer': 'toyota', 'citation': 'EPC 2026 pág. 4'}])
        self.assertEqual(response.status_code, 200, response.content)
        rows = {row['sku']: row for row in response.json()['updated']}
        self.assertEqual((rows['43512-60150']['is_OEM'], rows['43512-60150']['identity']['status']), (True, 'oem'))
        alias.refresh_from_db()
        self.assertEqual((alias.brand, alias.ref_type, alias.reference_source, alias.oem_reference),
                         ('TOYOTA', 'oem', 'Marcado como OEM por mark-root · EPC 2026 pág. 4', reference))
        self.assertEqual(PartCode.objects.filter(part=self.disc).count(), 1)
        self.assertEqual(OEMReference.objects.count(), 1)  # marking adds no item to the list
        refused = self.mark([{'id': str(self.pump.pk), 'manufacturer': 'TOYOTA'}])
        self.assertEqual(refused.status_code, 400)
        self.assertIn('no está en la lista OEM', refused.json()['detail'])
        self.pump.refresh_from_db()
        self.assertFalse(self.pump.is_OEM)

    def test_crear_adds_the_number_to_the_list_and_marks_the_sku(self):
        response = self.create([{'id': str(self.pump.pk), 'manufacturer': 'TOYOTA', 'citation': 'EPC', 'part_type': 'bomba de agua'}])
        self.assertEqual(response.status_code, 200, response.content)
        self.pump.refresh_from_db()
        self.assertTrue(self.pump.is_OEM)
        reference = OEMReference.objects.get(manufacturer='TOYOTA', code='1610029155')
        self.assertEqual((reference.status, reference.description, reference.part_type), ('declared', 'BOMBA AGUA TOY 22R', 'BOMBA DE AGUA'))
        self.assertIn(('manual', 'Creado desde el SKU por mark-root · EPC'), list(reference.sources.values_list('kind', 'citation')))
        self.assertEqual(PartCode.objects.get(part=self.pump).oem_reference, reference)
        again = self.create([{'id': str(self.disc.pk), 'manufacturer': 'TOYOTA'}, {'id': str(self.pump.pk), 'manufacturer': 'TOYOTA'}])
        self.assertEqual(again.status_code, 400)
        self.assertIn('usa «Marcar como OEM»', again.json()['detail'])
        self.assertFalse(OEMReference.objects.filter(code='4351260150').exists())  # all or none

    def test_a_number_another_sku_already_holds_marks_nothing(self):
        listed('TOYOTA', '4351260150')
        listed('TOYOTA', '1610029155')
        holder = Part.objects.create(sku='DISCO-TOY-1', description='DISCO')
        PartCode.objects.create(part=holder, brand='TOYOTA', code='4351260150', ref_type='oem', reference_source='CATÁLOGO TOYOTA')
        refused = self.mark([{'id': str(self.pump.pk), 'manufacturer': 'TOYOTA'}, {'id': str(self.disc.pk), 'manufacturer': 'TOYOTA'}])
        self.assertEqual(refused.status_code, 400)
        self.assertIn('DISCO-TOY-1', refused.json()['detail'])
        self.pump.refresh_from_db()
        self.assertFalse(self.pump.is_OEM)  # all or none
        self.assertFalse(PartCode.objects.filter(part=self.pump).exists())
        self.assertEqual(self.mark([{'id': str(self.pump.pk), 'manufacturer': ' '}]).status_code, 400)

    def test_unmarking_applies_only_to_a_sku_whose_code_is_not_in_the_list(self):
        listed('TOYOTA', '4351260150')
        self.mark([{'id': str(self.disc.pk), 'manufacturer': 'TOYOTA'}])
        refused = self.client.post(f'{URL}unmark-oem/', {'rows': [{'id': str(self.disc.pk), 'retire_number': True}]}, format='json')
        self.assertEqual(refused.status_code, 400)
        self.assertIn('ponlo en disputa', refused.json()['detail'])
        stray = Part.objects.create(sku='58411-1G000', description='TAMBOR HYU ACCENT', is_OEM=True)  # marked by hand, no item in the list
        PartCode.objects.create(part=stray, code='58411-1G000', ref_type='oem')
        response = self.client.post(f'{URL}unmark-oem/', {'rows': [{'id': str(stray.pk), 'retire_number': True}]}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        stray.refresh_from_db()
        self.assertFalse(stray.is_OEM)
        self.assertFalse(PartCode.objects.filter(part=stray).exists())

    def test_superusers_only(self):
        self.client.force_authenticate(User.objects.create_user('mark-staff', 'mark-staff@example.invalid', 'Example938!'))
        self.assertEqual(self.mark([{'id': str(self.disc.pk), 'manufacturer': 'TOYOTA'}]).status_code, 403)
        self.assertEqual(self.create([{'id': str(self.disc.pk), 'manufacturer': 'TOYOTA'}]).status_code, 403)


class ExactMatchTests(APITestCase):
    """A SKU whose code is a declared or verified number of the OEM list is OEM, on every matching pass."""

    def setUp(self):
        self.root = User.objects.create_superuser('exact-root', 'exact-root@example.invalid', 'Example938!')
        self.client.force_authenticate(self.root)

    def test_declared_numbers_mark_their_skus_and_inferred_short_or_disputed_ones_do_not(self):
        listed('MITSUBISHI', 'MR453383')
        listed('TOYOTA', '1780130070', status='inferred', kind='oem_finder')
        listed('SUZUKI', '1234567')  # 7 characters: could be anything
        listed('NISSAN', '43206EB300', status='disputed')
        exact = Part.objects.create(sku='MR-453383', description='EJE COMPLETO MIT MONTERO 01-06 LH')
        inferred = Part.objects.create(sku='17801-30070', description='FILTRO AIRE TOY HIACE')
        short = Part.objects.create(sku='12345-67', description='TORNILLO')
        disputed = Part.objects.create(sku='43206-EB300', description='DISCO FRENO NIS')
        self.assertEqual([sku for _, sku, _ in mark_exact_matches(apply=False)], ['MR-453383'])
        marked = mark_exact_matches(self.root)
        self.assertEqual(marked, [(exact.pk, 'MR-453383', [('MITSUBISHI', 'MR453383')])])
        for part, oem in ((exact, True), (inferred, False), (short, False), (disputed, False)):
            part.refresh_from_db()
            self.assertEqual(part.is_OEM, oem, part.sku)
        change = CatalogIdentityChange.objects.get(part=exact)
        self.assertEqual((change.previous_sku, change.sku, change.reference['action'], change.reference['brand']),
                         ('MR-453383', 'MR-453383', 'exact_match', 'MITSUBISHI'))
        self.assertEqual(mark_exact_matches(self.root), [])  # once

    def test_disputing_a_number_unmarks_its_skus_and_clearing_it_marks_them_again(self):
        reference = listed('MITSUBISHI', 'MR453383')
        part = Part.objects.create(sku='MR-453383', description='EJE MIT')
        mark_exact_matches(self.root)
        reference.refresh_from_db()
        response = self.client.patch(f'{LIBRARY}{reference.pk}/', {'expected_version': reference.version, 'action': 'dispute',
                                                                   'note': 'Es de otro modelo'}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        part.refresh_from_db()
        self.assertFalse(part.is_OEM)
        self.assertEqual(CatalogIdentityChange.objects.filter(part=part).latest('pk').reference['action'], 'disputed')
        response = self.client.patch(f'{LIBRARY}{reference.pk}/', {'expected_version': response.json()['version'], 'action': 'clear_dispute'},
                                     format='json')
        self.assertEqual(response.status_code, 200, response.content)
        part.refresh_from_db()
        self.assertTrue(part.is_OEM)

    def test_a_number_added_to_the_library_marks_its_sku_at_once(self):
        part = Part.objects.create(sku='19200-P2A-A01', description='BOMBA AGUA HON CIVIC')
        response = self.client.post(LIBRARY, {'manufacturer': 'HONDA', 'code': '19200-P2A-A01',
                                              'source': {'kind': 'manufacturer_catalog', 'citation': 'EPC', 'verified': False}}, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        part.refresh_from_db()
        self.assertTrue(part.is_OEM)

    @override_settings(OEM_EXACT_MARK=True)
    def test_the_matching_pass_marks_them_when_oem_exact_mark_is_on(self):
        from .matching_engine import reconcile_catalog
        listed('MITSUBISHI', 'MR453383')
        part = Part.objects.create(sku='MR-453383', description='EJE MIT')
        stats = {}
        reconcile_catalog(self.root, stats=stats)
        part.refresh_from_db()
        self.assertEqual((part.is_OEM, stats['marked_exact_oems']), (True, 1))

    @override_settings(OEM_EXACT_MARK=False)
    def test_the_matching_pass_marks_nothing_when_oem_exact_mark_is_off(self):  # the default since the owner retired OEM linking (2026-10-14)
        from .matching_engine import reconcile_catalog
        listed('MITSUBISHI', 'MR453383')
        part = Part.objects.create(sku='MR-453383', description='EJE MIT')
        stats = {}
        reconcile_catalog(self.root, stats=stats)
        part.refresh_from_db()
        self.assertEqual((part.is_OEM, stats['marked_exact_oems']), (False, 0))

    def test_a_number_other_skus_are_tied_to_stays_in_the_list_when_one_alterno_goes(self):
        tied = Part.objects.create(sku='16100-19125-GMB', description='BOMBA AGUA TOY')
        owner = Part.objects.create(sku='16100-19125', description='BOMBA AGUA TOY', is_OEM=True)
        code = PartCode.objects.create(part=owner, brand='TOYOTA', code='16100-19125', ref_type='oem', reference_source='algo:oem-finder:v2:current:9')
        reference = OEMReference.objects.get(manufacturer='TOYOTA', code='1610019125')
        PartOEMLink.objects.create(part=tied, reference=reference, source='catalog', citations=['GMB Water Pump Catalog 2016 · pág. 556'])
        code.delete()
        self.assertTrue(OEMReference.objects.filter(pk=reference.pk).exists())  # a supplier catalog still ties the GMB pump to it
        self.assertTrue(PartOEMLink.objects.filter(part=tied).exists())
        lone = Part.objects.create(sku='17801-31110', description='FILTRO')
        alone = PartCode.objects.create(part=lone, brand='LEXUS', code='17801-31110', ref_type='oem', reference_source='algo:oem-finder:v2:current:9')
        alone.delete()
        self.assertFalse(OEMReference.objects.filter(code='1780131110').exists())  # nothing backs it and nothing uses it


class MarkingRulesTests(APITestCase):
    """The rules around the two actions and the automatic marks that the main tests do not reach."""

    def setUp(self):
        self.root = User.objects.create_superuser('rules-root', 'rules-root@example.invalid', 'Example938!')
        self.client.force_authenticate(self.root)

    def test_marking_needs_the_item_under_that_make_and_not_in_dispute(self):
        disc = Part.objects.create(sku='43512-60150', description='DISCO FRENO DEL TOY LAND CRUISER')
        listed('LEXUS', '4351260150')  # filed only under LEXUS
        refused = self.client.post(f'{URL}mark-oem/', {'rows': [{'id': str(disc.pk), 'manufacturer': 'TOYOTA'}]}, format='json')
        self.assertEqual(refused.status_code, 400)
        self.assertIn('TOYOTA 4351260150 no está en la lista OEM', refused.json()['detail'])
        self.assertEqual(list(OEMReference.objects.values_list('manufacturer', 'code')), [('LEXUS', '4351260150')])  # no TOYOTA item crept in
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

    def test_disputing_one_make_keeps_the_sku_while_another_make_still_lists_the_number(self):
        lexus, toyota = listed('LEXUS', '4243160220'), listed('TOYOTA', '4243160220')
        part = Part.objects.create(sku='42431-60220', description='DISCO FRENO TOY')
        mark_exact_matches(self.root)
        lexus.refresh_from_db()
        response = self.client.patch(f'{LIBRARY}{lexus.pk}/', {'expected_version': lexus.version, 'action': 'dispute', 'note': 'Es de otro modelo'}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        part.refresh_from_db()
        self.assertTrue(part.is_OEM)
        self.assertFalse(CatalogIdentityChange.objects.filter(part=part, reference__action='disputed').exists())
        toyota.refresh_from_db()
        self.client.patch(f'{LIBRARY}{toyota.pk}/', {'expected_version': toyota.version, 'action': 'dispute', 'note': 'Tampoco'}, format='json')
        part.refresh_from_db()
        self.assertFalse(part.is_OEM)  # no item left that is not in dispute

    def test_automatic_marks_skip_retired_merged_and_letter_codes_and_respect_the_codes_asked(self):
        listed('MITSUBISHI', 'MR453383'); listed('MITSUBISHI', 'MR453384'); listed('MITSUBISHI', 'MR453385'); listed('HONDA', 'ABCDEFGHJ')
        exact = Part.objects.create(sku='MR-453383', description='EJE MIT')
        retired = Part.objects.create(sku='MR-453384', description='EJE MIT', active=False)
        merged = Part.objects.create(sku='MR-453385', description='EJE MIT', merged_into=exact)
        letters = Part.objects.create(sku='ABCD-EFGH-J', description='TAPA')
        listed('NISSAN', '43206EB300')
        other = Part.objects.create(sku='43206-EB300', description='DISCO NIS')
        self.assertEqual([sku for _, sku, _ in mark_exact_matches(self.root, codes=['43206-EB300'])], ['43206-EB300'])  # only the code asked
        self.assertEqual([sku for _, sku, _ in mark_exact_matches(self.root)], ['MR-453383'])
        for part, oem in ((exact, True), (retired, False), (merged, False), (letters, False), (other, True)):
            part.refresh_from_db()
            self.assertEqual(part.is_OEM, oem, part.sku)

    def test_among_sister_makes_the_mark_records_the_make_the_description_names(self):
        listed('TOYOTA', '4243160220'); listed('LEXUS', '4243160220')
        disc = Part.objects.create(sku='42431-60220', description='DISCO FRENO TOY LAND CRUISER 98-05 TRAS')
        listed('MERCEDES-BENZ', '1300A066'); listed('MITSUBISHI', '1300A066')
        pump = Part.objects.create(sku='1300A066', description='BOMBA AGUA MIT OUTLANDER 03-')
        mark_exact_matches(self.root)
        self.assertEqual(CatalogIdentityChange.objects.get(part=disc).reference['brand'], 'TOYOTA')
        self.assertEqual(CatalogIdentityChange.objects.get(part=pump).reference['brand'], 'MITSUBISHI')

    def test_the_command_simulates_then_marks_with_the_service_identity_and_reports(self):
        import csv
        import io
        import os
        import tempfile
        from . import mark_exact_oem
        from .oem_apply import service_user
        listed('MITSUBISHI', 'MR453383')
        part = Part.objects.create(sku='MR-453383', description='EJE MIT')
        out = io.StringIO()
        mark_exact_oem.main(['--dry-run'], stdout=out)
        part.refresh_from_db()
        self.assertFalse(part.is_OEM)
        self.assertIn('Simulación · 1 SKU', out.getvalue())
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'report.csv')
            mark_exact_oem.main(['--apply', '--report', path], stdout=io.StringIO())
            with open(path, encoding='utf-8') as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual([(row['sku'], row['oem_items']) for row in rows], [('MR-453383', 'MITSUBISHI MR453383')])
        part.refresh_from_db()
        self.assertTrue(part.is_OEM)
        self.assertEqual(CatalogIdentityChange.objects.get(part=part).actor, service_user())

    def test_the_sku_editor_follows_the_oem_list(self):
        listed('MITSUBISHI', 'MR453383')
        exact = Part.objects.create(sku='MR-453383', name='MR-453383', description='EJE MIT')
        mark_exact_matches(self.root)
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
        self.assertEqual(same.status_code, 200, same.content)  # a stale editor sending the flag as it is saves the rest

    def test_separar_sku_does_not_call_a_mark_a_rename(self):
        from .catalog_grouping import merge_catalog_parts
        from .catalog_split import split_plan
        listed('TOYOTA', '1610009080')
        target = Part.objects.create(sku='16100-09080', name='16100-09080', description='BOMBA AGUA TOY')
        Part.objects.create(sku='16100-09080-GMB', name='16100-09080-GMB', description='BOMBA AGUA TOY')
        merge_catalog_parts(actor=self.root, target_sku='16100-09080', source_skus=['16100-09080-GMB'])
        mark_exact_matches(self.root)
        target.refresh_from_db()
        self.assertTrue(target.is_OEM)
        warnings = ' '.join(split_plan(target)['warnings'])
        self.assertNotIn('cambió de', warnings)
