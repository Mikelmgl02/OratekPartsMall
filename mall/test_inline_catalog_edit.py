from rest_framework.test import APITestCase

from .models import Part, PartCode, PartType, User
from .technical_models import PartSpecification, TechnicalField

URL = '/api/v1/management/catalog/'


class InlineCatalogEditTests(APITestCase):
    """The Inventario grid edits one field at a time, sending the SKU's current OEM flag so a field edit never renames the SKU."""

    def setUp(self):
        self.root = User.objects.create_superuser('inline-root', 'inline-root@example.invalid', 'Example938!')
        self.client.force_authenticate(self.root)
        self.part = Part.objects.create(sku='BOMBA-1', description='BOMBA AGUA')
        PartCode.objects.create(part=self.part, brand='TOYOTA', code='16100-29155', ref_type='oem', reference_source='CATÁLOGO TOYOTA')

    def patch(self, **data):
        return self.client.patch(f'{URL}{self.part.pk}/', data, format='json')

    def test_a_field_edit_with_the_oem_flag_never_renames_the_sku(self):
        response = self.patch(description='bomba agua toy 22r', is_OEM=False)
        self.assertEqual((response.status_code, response.json()['sku'], response.json()['description']), (200, 'BOMBA-1', 'BOMBA AGUA TOY 22R'))
        renamed = self.patch(description='BOMBA AGUA TOY 22R HILUX')  # without the flag the dialog's OEM preference runs
        self.assertEqual(renamed.json()['sku'], '16100-29155')

    def test_group_status_and_a_subgroup_guarded_by_technical_data(self):
        response = self.patch(category='FRENOS', subcategory='DISCOS DE FRENO', is_OEM=False).json()
        self.assertEqual((response['category'], response['subcategory'], response['part_type']), ('FRENOS', 'DISCOS DE FRENO', str(PartType.objects.get(name='DISCOS DE FRENO').pk)))
        self.assertEqual(self.patch(active=False, is_OEM=False).json()['active'], False)
        field = TechnicalField.objects.create(template=PartType.objects.get(name='DISCOS DE FRENO').template, key='diameter', label='DIÁMETRO', kind='number')
        PartSpecification.objects.create(part=self.part, field=field, number_value=280)
        refused = self.patch(category='FRENOS', subcategory='TAMBORES DE FRENO', is_OEM=False)
        self.assertEqual(refused.status_code, 400)
        self.assertIn('datos técnicos', str(refused.json()))
        self.part.refresh_from_db()
        self.assertEqual(self.part.subcategory, 'DISCOS DE FRENO')

    def test_the_taxonomy_lists_rule_and_existing_subgroups_for_superusers_only(self):
        PartType.objects.create(category='ESPECIALES', name='PIEZAS A MEDIDA')
        pairs = {(row['category'], row['subcategory']) for row in self.client.get(f'{URL}taxonomy/').json()['results']}
        self.assertIn(('FRENOS', 'DISCOS DE FRENO'), pairs)
        self.assertIn(('ESPECIALES', 'PIEZAS A MEDIDA'), pairs)
        self.client.force_authenticate(User.objects.create_user('inline-staff', 'inline-staff@example.invalid', 'Example938!'))
        self.assertEqual(self.client.get(f'{URL}taxonomy/').status_code, 403)
