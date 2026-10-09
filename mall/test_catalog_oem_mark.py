from rest_framework.test import APITestCase

from .models import Part, PartCode, User
from .oem_reference_models import OEMReference

URL = '/api/v1/management/catalog/'


class CatalogMarkOEMTests(APITestCase):
    """Marcar como OEM: the SKU code becomes the make's OEM alterno, the SKU MAIN OEM and the number enters the OEM library."""

    def setUp(self):
        self.root = User.objects.create_superuser('mark-root', 'mark-root@example.invalid', 'Example938!')
        self.client.force_authenticate(self.root)
        self.disc = Part.objects.create(sku='43512-60150', description='DISCO FRENO DEL TOY LAND CRUISER')
        self.pump = Part.objects.create(sku='16100-29155', description='BOMBA AGUA TOY 22R')

    def mark(self, rows):
        return self.client.post(f'{URL}mark-oem/', {'rows': rows}, format='json')

    def test_the_plan_names_the_make_the_description_gives_and_warns_about_suffixes(self):
        suffixed = Part.objects.create(sku='0110-ACA30-SPK', description='PUNTA FLECHA EXT TOY RAV4 05-13')
        wordy = Part.objects.create(sku='BOMBA-AGUA', description='BOMBA AGUA')
        plan = self.client.get(f'{URL}mark-oem/', {'ids': f'{self.disc.pk},{suffixed.pk},{wordy.pk}'}).json()
        rows = {row['sku']: row for row in plan['rows']}
        self.assertEqual((rows['43512-60150']['suggested'], rows['43512-60150']['blocked'], rows['43512-60150']['warnings']), ('TOYOTA', '', []))
        self.assertIn('sufijo', rows['0110-ACA30-SPK']['warnings'][0])
        self.assertIn('necesita dígitos', rows['BOMBA-AGUA']['blocked'])

    def test_marking_files_the_code_as_the_makes_oem_alterno_and_the_number_in_the_library(self):
        PartCode.objects.create(part=self.pump, code='16100-29155')  # the neutral alias a merge left
        alias = PartCode.objects.get(part=self.pump)
        response = self.mark([{'id': str(self.disc.pk), 'manufacturer': 'toyota', 'citation': 'EPC 2026 pág. 4'},
                              {'id': str(self.pump.pk), 'manufacturer': 'TOYOTA'}])
        self.assertEqual(response.status_code, 200, response.content)
        rows = {row['sku']: row for row in response.json()['updated']}
        self.assertEqual((rows['43512-60150']['is_OEM'], rows['43512-60150']['identity']['status']), (True, 'oem'))
        code = PartCode.objects.get(part=self.disc)
        self.assertEqual((code.brand, code.code, code.ref_type, code.reference_source),
                         ('TOYOTA', '43512-60150', 'oem', 'Marcado como OEM por mark-root · EPC 2026 pág. 4'))
        reference = OEMReference.objects.get(manufacturer='TOYOTA', code='4351260150')
        self.assertEqual([(source.kind, source.citation) for source in reference.sources.all()],
                         [('catalog_approval', 'Marcado como OEM por mark-root · EPC 2026 pág. 4')])
        alias.refresh_from_db()
        self.assertEqual((alias.brand, alias.ref_type), ('TOYOTA', 'oem'))  # the alias became the alterno, no second row
        self.assertEqual(PartCode.objects.filter(part=self.pump).count(), 1)

    def test_a_number_another_sku_already_holds_marks_nothing(self):
        holder = Part.objects.create(sku='DISCO-TOY-1', description='DISCO')
        PartCode.objects.create(part=holder, brand='TOYOTA', code='4351260150', ref_type='oem', reference_source='CATÁLOGO TOYOTA')
        refused = self.mark([{'id': str(self.pump.pk), 'manufacturer': 'TOYOTA'}, {'id': str(self.disc.pk), 'manufacturer': 'TOYOTA'}])
        self.assertEqual(refused.status_code, 400)
        self.assertIn('DISCO-TOY-1', refused.json()['detail'])
        self.pump.refresh_from_db()
        self.assertFalse(self.pump.is_OEM)  # all or none
        self.assertFalse(PartCode.objects.filter(part=self.pump).exists())
        self.assertEqual(self.mark([{'id': str(self.pump.pk), 'manufacturer': ' '}]).status_code, 400)

    def test_unmarking_takes_the_flag_off_and_retires_the_number_when_asked(self):
        self.mark([{'id': str(self.disc.pk), 'manufacturer': 'TOYOTA'}, {'id': str(self.pump.pk), 'manufacturer': 'TOYOTA'}])
        plan = {row['sku']: row for row in self.client.get(f'{URL}mark-oem/', {'ids': str(self.disc.pk)}).json()['rows']}
        self.assertTrue(plan['43512-60150']['marked_alterno'])
        response = self.client.post(f'{URL}unmark-oem/', {'rows': [{'id': str(self.disc.pk), 'retire_number': True},
                                                                    {'id': str(self.pump.pk), 'retire_number': False}]}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        self.disc.refresh_from_db(); self.pump.refresh_from_db()
        self.assertEqual((self.disc.is_OEM, self.pump.is_OEM), (False, False))
        self.assertFalse(PartCode.objects.filter(part=self.disc).exists())
        self.assertFalse(OEMReference.objects.filter(code='4351260150').exists())  # nothing else backed it
        self.assertTrue(PartCode.objects.filter(part=self.pump, ref_type='oem').exists())  # kept: still an OEM number of the SKU

    def test_superusers_only(self):
        self.client.force_authenticate(User.objects.create_user('mark-staff', 'mark-staff@example.invalid', 'Example938!'))
        self.assertEqual(self.mark([{'id': str(self.disc.pk), 'manufacturer': 'TOYOTA'}]).status_code, 403)
