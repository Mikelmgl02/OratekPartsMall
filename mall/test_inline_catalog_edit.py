from rest_framework.test import APITestCase

from .models import Part, PartCode, PartType, User
from .technical_models import PartSpecification, TechnicalField

URL = '/api/v1/management/catalog/'


class InlineCatalogEditTests(APITestCase):
    """Editar celdas in the Inventario grid: batches of plain field edits, all saved or none, each keeping the SKU's OEM flag so an
    edit never renames the SKU, and the rows re-read by id."""

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

    def bulk(self, rows):
        return self.client.post(f'{URL}bulk-edit/', {'rows': rows}, format='json')

    def test_a_batch_saves_every_row_or_none(self):
        other = Part.objects.create(sku='DISCO-1', description='DISCO')
        saved = self.bulk([{'id': str(self.part.pk), 'description': 'bomba agua toy', 'active': False},
                           {'id': str(other.pk), 'category': 'frenos', 'subcategory': 'discos de freno'}])
        self.assertEqual(saved.status_code, 200)
        rows = {row['sku']: row for row in saved.json()['updated']}
        self.assertEqual((rows['BOMBA-1']['description'], rows['BOMBA-1']['active'], rows['DISCO-1']['subcategory']), ('BOMBA AGUA TOY', False, 'DISCOS DE FRENO'))
        self.assertEqual(rows['BOMBA-1']['sku'], 'BOMBA-1')  # never renamed to its verified OEM
        refused = self.bulk([{'id': str(self.part.pk), 'description': 'OTRA'}, {'id': str(other.pk), 'category': 'FRENOS', 'subcategory': 'INVENTADO'}])
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(refused.json()['errors'][0]['sku'], 'DISCO-1')
        self.assertIn('no es un grupo / subgrupo del catálogo', refused.json()['detail'])
        self.part.refresh_from_db()
        self.assertEqual(self.part.description, 'BOMBA AGUA TOY')  # the other row's failure undid this one too
        self.assertFalse(PartType.objects.filter(name='INVENTADO').exists())

    def test_a_batch_takes_only_plain_fields(self):
        self.assertEqual(self.bulk([{'id': str(self.part.pk), 'sku': 'OTRO'}]).status_code, 200)
        self.part.refresh_from_db()
        self.assertEqual(self.part.sku, 'BOMBA-1')  # unknown keys are ignored: the SKU code keeps the dialog
        self.assertEqual(self.bulk([]).status_code, 400)
        self.assertEqual(self.bulk([{'id': '00000000-0000-4000-8000-000000000001', 'description': 'X'}]).status_code, 400)

    def test_the_list_rereads_given_rows_with_their_alterno_ids(self):
        other = Part.objects.create(sku='DISCO-1', description='DISCO')
        rows = self.client.get(URL, {'ids': f'{self.part.pk},{other.pk}'}).json()['results']
        self.assertEqual(sorted(row['sku'] for row in rows), ['BOMBA-1', 'DISCO-1'])
        code = next(row for row in rows if row['sku'] == 'BOMBA-1')['codes'][0]
        self.assertEqual(code['id'], PartCode.objects.get(code='16100-29155').pk)
        self.assertEqual(self.client.get(URL, {'ids': 'nope'}).status_code, 400)
