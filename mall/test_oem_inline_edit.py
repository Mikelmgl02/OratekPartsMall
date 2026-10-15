from rest_framework.test import APITestCase

from . import references
from .models import Reference, User
from .oem_links import store_cross_references
from .oem_reference_models import OEMCrossReference, OEMCrossReferenceRetired

URL = '/api/v1/management/oem-references/'


class OEMInlineEditTests(APITestCase):
    """Biblioteca OEM's Editar celdas (S4: on the library's References, ids their uids): batches of description and part type edits
    under the version shown, and alternos added or retired by hand, a retired catalog code staying retired."""

    def setUp(self):
        self.root = User.objects.create_superuser('oem-edit-root', 'oem-edit-root@example.invalid', 'Example938!')
        self.client.force_authenticate(self.root)
        self.pump = references.add_library_number('TOYOTA', '16100-29155', source_kind='aftermarket_catalog', citation='CATÁLOGO GMB 2016',
                                                  part_type='BOMBAS DE AGUA')[0]
        self.disc = references.add_library_number('TOYOTA', '43512-60150', source_kind='aftermarket_catalog', citation='CATÁLOGO GMB 2016',
                                                  part_type='DISCOS DE FRENO')[0]
        self.pump.refresh_from_db()
        self.disc.refresh_from_db()

    def bulk(self, rows):
        return self.client.post(f'{URL}bulk-edit/', {'rows': rows}, format='json')

    def test_a_batch_saves_every_row_under_its_version_or_none(self):
        saved = self.bulk([{'id': str(self.pump.uid), 'expected_version': self.pump.version, 'description': 'bomba agua toy 22r'},
                           {'id': str(self.disc.uid), 'expected_version': self.disc.version, 'part_type': 'BOMBAS DE AGUA'}])
        self.assertEqual(saved.status_code, 200, saved.content)
        rows = {row['code']: row for row in saved.json()['updated']}
        self.assertEqual((rows['1610029155']['description'], rows['4351260150']['part_type']), ('BOMBA AGUA TOY 22R', 'BOMBAS DE AGUA'))
        self.assertEqual(rows['1610029155']['version'], self.pump.version + 1)
        self.disc.refresh_from_db()
        self.assertEqual(self.disc.part.subcategory, 'BOMBAS DE AGUA')  # its line in referencia follows its number
        stale = self.bulk([{'id': str(self.pump.uid), 'expected_version': self.pump.version, 'description': 'OTRA'},  # the version before the save
                           {'id': str(self.disc.uid), 'expected_version': self.disc.version, 'description': 'DISCO'}])
        self.assertEqual(stale.status_code, 409)
        self.assertEqual([row['description'] for row in stale.json()['rows']], ['BOMBA AGUA TOY 22R'])  # the number as it is now
        self.disc.refresh_from_db()
        self.assertEqual(self.disc.detail['description'], '')  # the other row was not saved either
        unknown = self.bulk([{'id': str(self.disc.uid), 'expected_version': self.disc.version, 'part_type': 'INVENTADO'}])
        self.assertEqual(unknown.status_code, 400)
        self.assertIn('no es un tipo de pieza', unknown.json()['detail'])

    def test_the_list_rereads_rows_by_id_and_lists_the_part_types(self):
        rows = self.client.get(URL, {'ids': f'{self.pump.uid},{self.disc.uid}'}).json()['results']
        self.assertEqual(sorted(row['code'] for row in rows), ['1610029155', '4351260150'])
        self.assertEqual(self.client.get(URL, {'ids': 'nope'}).status_code, 400)
        references.add_library_number('TOYOTA', '90080-91188', part_type='TERM CREM')  # a text no subgrupo maps (A6) is offered too
        self.assertEqual(self.client.get(f'{URL}part-types/').json()['results'], ['BOMBAS DE AGUA', 'DISCOS DE FRENO', 'TERM CREM'])

    def test_an_alterno_added_by_hand_and_a_retired_catalog_code_that_stays_retired(self):
        store_cross_references({(self.pump.pk, 'GMB', 'GWP-T41'): ['CATÁLOGO GMB 2016 · pág. 4']})
        catalog = OEMCrossReference.objects.get(reference=self.pump, number='GWPT41')
        url = f'{URL}{self.pump.uid}/cross-references/'
        added = self.client.post(url, {'brand': 'aisin', 'code': 'wpt-111'}, format='json')
        self.assertEqual(added.status_code, 201, added.content)
        self.assertEqual((added.json()['brand'], added.json()['code'], added.json()['citations']), ('AISIN', 'WPT-111', ['Registro manual · oem-edit-root']))
        self.assertEqual(OEMCrossReference.objects.get(number='WPT111').reference_id, self.pump.pk)
        refused = [self.client.post(url, body, format='json') for body in ({'brand': 'TOYOTA', 'code': '16100-29160'}, {'brand': 'GMB', 'code': 'BOMBA'})]
        self.assertEqual([response.status_code for response in refused], [400, 400])  # the same make is an OEM number; a code needs a digit

        self.assertEqual(self.client.delete(f'{url}{catalog.pk}/').status_code, 204)
        self.assertTrue(OEMCrossReferenceRetired.objects.filter(reference=self.pump, brand='GMB', number='GWPT41', retired_by=self.root).exists())
        counts = store_cross_references({(self.pump.pk, 'GMB', 'GWP-T41'): ['CATÁLOGO GMB 2018 · pág. 9']})  # the catalog prints it again
        self.assertEqual(counts['new'], 0)
        self.assertFalse(OEMCrossReference.objects.filter(reference=self.pump, number='GWPT41').exists())
        detail = self.client.get(f'{URL}{self.pump.uid}/').json()
        self.assertEqual([(row['brand'], row['code']) for row in detail['cross_references']], [('AISIN', 'WPT-111')])

        back = self.client.post(url, {'brand': 'GMB', 'code': 'GWP-T41'}, format='json')
        self.assertEqual(back.status_code, 201)
        self.assertFalse(OEMCrossReferenceRetired.objects.exists())  # adding it by hand brings it back
        self.assertEqual(self.client.delete(f'{url}999999/').status_code, 404)
        self.assertEqual(self.client.post(f'{URL}00000000-0000-4000-8000-000000000001/cross-references/', {'brand': 'AISIN', 'code': 'WPT-1'},
                                          format='json').status_code, 404)

    def test_superusers_only(self):
        self.client.force_authenticate(User.objects.create_user('oem-edit-staff', 'oem-edit-staff@example.invalid', 'Example938!'))
        self.assertEqual(self.bulk([{'id': str(self.pump.uid), 'expected_version': 1, 'description': 'X'}]).status_code, 403)
        self.assertEqual(self.client.post(f'{URL}{self.pump.uid}/cross-references/', {'brand': 'AISIN', 'code': 'WPT-1'}, format='json').status_code, 403)
        self.assertTrue(Reference.objects.filter(pk=self.pump.pk).exists())
