from decimal import Decimal

from rest_framework.test import APITestCase
from rest_framework.exceptions import ValidationError

from .catalog_grouping import merge_catalog_parts
from .catalog_import import save_plan
from .models import (Account, Membership, Application, ItemApplication, Part, PartSpecification, PartType,
                     TechnicalTemplate, User)


class TechnicalDataTests(APITestCase):
    def setUp(self):
        self.root = User.objects.create_superuser('technical-root', 'technical-root@example.com', 'test-password')
        self.user = User.objects.create_user('technical-user', 'technical-user@example.com', 'test-password')
        Membership.objects.create(user=self.user, account=Account.objects.create(name='TEST TECHNICAL'))
        self.client.force_authenticate(self.root)
        self.part = Part.objects.create(sku='TEST-DISC', category='FRENOS', subcategory='DISCOS')
        self.type = self.part.part_type
        self.template_url = f'/api/v1/management/part-types/{self.type.pk}/template/'
        self.sheet_url = f'/api/v1/management/catalog/{self.part.pk}/technical/'
        self.fields = [
            {'key': 'diameter', 'label': 'Diámetro', 'kind': 'number', 'unit': 'mm', 'required': True},
            {'key': 'abs', 'label': 'Encoder ABS', 'kind': 'boolean'},
            {'key': 'holes', 'label': 'Huecos', 'kind': 'integer'},
            {'key': 'material', 'label': 'Material', 'kind': 'choice', 'options': ['ACERO', 'HIERRO']},
        ]
        response = self.client.patch(self.template_url, {'revision': 1, 'fields': self.fields}, format='json')
        self.assertEqual(response.status_code, 200, response.data)

    def save_sheet(self, **data):
        sheet = self.client.get(self.sheet_url).data
        return self.client.patch(self.sheet_url, {'revision': sheet['revision'], 'part_type': sheet['part_type']['id'] if sheet['part_type'] else None,
                'template_revision': sheet['template']['revision'] if sheet['template'] else None, **data}, format='json')

    def application(self, **overrides):
        return Application.objects.create(make='HYUNDAI', model='ACCENT', year_from=2011, year_to=2016, **overrides)

    def test_subgroup_identity_normalizes_and_is_scoped_to_group(self):
        same = Part.objects.create(sku='SAME', category=' frenos ', subcategory=' discos ')
        other = Part.objects.create(sku='OTHER', category='TRANSMISIÓN', subcategory='DISCOS')
        self.assertEqual(same.part_type_id, self.type.pk)
        self.assertNotEqual(other.part_type_id, self.type.pk)
        self.assertEqual(TechnicalTemplate.objects.count(), 2)
        blank = Part.objects.create(sku='BLANK', category='FRENOS')
        self.assertIsNone(blank.part_type_id)

    def test_import_creates_and_reuses_type_in_batches(self):
        save_plan({'BATCH': {'sku': 'BATCH', 'existing': None, 'fields': {'category': 'frenos', 'subcategory': 'discos'}, 'added': []}})
        part = Part.objects.get(sku='BATCH')
        self.assertEqual(part.part_type_id, self.type.pk)
        save_plan({'BATCH': {'sku': 'BATCH', 'existing': part, 'action': 'update', 'fields': {'subcategory': 'PASTILLAS'}, 'added': []}})
        part.refresh_from_db()
        self.assertEqual(part.part_type.name, 'PASTILLAS')

    def test_template_fields_are_specific_to_type(self):
        other = Part.objects.create(sku='FILTER', category='FILTROS', subcategory='ACEITE')
        response = self.client.get(f'/api/v1/management/catalog/{other.pk}/technical/')
        self.assertEqual(response.data['template']['fields'], [])
        bad = self.save_sheet(values={'made_up': {'value': '1'}})
        self.assertEqual(bad.status_code, 400)

    def test_decimal_conversion_false_zero_and_unknown_round_trip(self):
        response = self.save_sheet(values={'diameter': {'value': '25.6', 'unit': 'cm', 'source': 'Fabricante'}, 'abs': {'value': False}, 'holes': {'value': 0}, 'material': {'value': None}})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['values']['diameter']['value'], '256')
        self.assertIs(response.data['values']['abs']['value'], False)
        self.assertEqual(response.data['values']['holes']['value'], '0')
        self.assertNotIn('material', response.data['values'])
        self.assertEqual(PartSpecification.objects.get(field__key='diameter').number_value, Decimal('256'))
        self.assertEqual(response.data['missing_required'], [])

    def test_missing_required_reports_incomplete_without_inventing_values(self):
        response = self.save_sheet(values={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['missing_required'], ['diameter'])

    def test_invalid_data_does_not_erase_existing_values(self):
        self.save_sheet(values={'diameter': {'value': '256'}})
        cases = [('diameter', True), ('diameter', 'NaN'), ('diameter', 'Infinity'), ('diameter', '1.1234567'), ('holes', '4.5'), ('abs', 'false'), ('material', 'PLÁSTICO')]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                response = self.save_sheet(values={key: {'value': value}})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(PartSpecification.objects.get(field__key='diameter').number_value, Decimal('256'))
        response = self.save_sheet(values={'diameter': {'value': 2, 'unit': 'kg'}})
        self.assertEqual(response.status_code, 400)

    def test_template_prevents_destructive_changes_when_values_exist(self):
        self.save_sheet(values={'diameter': {'value': 256}, 'material': {'value': 'ACERO'}})
        for fields in [self.fields[1:], [{**self.fields[0], 'unit': 'cm'}, *self.fields[1:]],
                       [*self.fields[:3], {**self.fields[3], 'options': ['HIERRO']}]]:
            response = self.client.patch(self.template_url, {'revision': 2, 'fields': fields}, format='json')
            self.assertEqual(response.status_code, 400, response.data)
        response = self.client.patch(self.template_url, {'revision': 2, 'fields': [{**f, 'label': f['label'] + ' NUEVO'} for f in self.fields]}, format='json')
        self.assertEqual(response.status_code, 200)

    def test_invalid_template_and_stale_revision_are_rejected(self):
        for fields in [[self.fields[0], self.fields[0]], [{'key': 'abs', 'label': 'ABS', 'kind': 'boolean', 'unit': 'mm'}], [{'key': 'choice', 'label': 'TIPO', 'kind': 'choice', 'options': []}]]:
            response = self.client.patch(self.template_url, {'revision': 2, 'fields': fields}, format='json')
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.patch(self.template_url, {'revision': 1, 'fields': []}, format='json').status_code, 400)

    def test_stale_sheet_and_template_and_type_do_not_overwrite(self):
        old = {'revision': 0, 'part_type': str(self.type.pk), 'template_revision': 2, 'values': {}}
        self.save_sheet(values={'diameter': {'value': 256}})
        self.assertEqual(self.client.patch(self.sheet_url, old, format='json').status_code, 400)
        old['revision'] = 1
        self.client.patch(self.template_url, {'revision': 2, 'fields': self.fields}, format='json')
        self.assertEqual(self.client.patch(self.sheet_url, old, format='json').status_code, 400)
        self.assertEqual(PartSpecification.objects.count(), 1)

    def test_group_rename_preserves_id_template_values_and_projection(self):
        self.save_sheet(values={'diameter': {'value': 256}})
        response = self.client.patch(f'/api/v1/management/part-types/{self.type.pk}/', {'category': 'FRENADO', 'name': 'DISCOS DE FRENO'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.part.refresh_from_db()
        self.assertEqual(self.part.part_type_id, self.type.pk)
        self.assertEqual((self.part.category, self.part.subcategory), ('FRENADO', 'DISCOS DE FRENO'))
        self.assertEqual(self.part.specifications.count(), 1)

    def test_catalog_can_select_type_by_id_but_cannot_silently_reassign_measurements(self):
        response = self.client.post('/api/v1/management/catalog/', {'sku': 'BY-ID', 'part_type': str(self.type.pk)}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['category'], 'FRENOS')
        self.save_sheet(values={'diameter': {'value': 256}})
        response = self.client.patch(f'/api/v1/management/catalog/{self.part.pk}/', {'subcategory': 'PASTILLAS'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.part.refresh_from_db()
        self.assertEqual(self.part.part_type_id, self.type.pk)

    def test_application_is_reusable_many_to_many_with_qualified_links(self):
        application = self.application()
        other = Part.objects.create(sku='OTHER')
        ItemApplication.objects.create(part=other, application=application, position='TRASERO')
        response = self.save_sheet(applications=[{'application': str(application.pk), 'position': 'DELANTERO', 'notes': 'CON ABS', 'verified': True}])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(application.parts.count(), 2)
        self.assertEqual(self.part.applications.first(), application)
        self.assertEqual(response.data['applications'][0]['notes'], 'CON ABS')

    def test_public_sheet_hides_unverified_links_and_internal_sources(self):
        application = self.application()
        self.save_sheet(values={'diameter': {'value': 256, 'source': 'Internal document'}}, applications=[{'application': str(application.pk), 'source': 'Internal note'}])
        self.client.force_authenticate(self.user)
        url = f'/api/v1/catalog/{self.part.pk}/technical/'
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['applications'], [])
        self.assertNotIn('source', response.data['values']['diameter'])
        ItemApplication.objects.filter(part=self.part).update(verified=True)
        response = self.client.get(url)
        self.assertEqual(len(response.data['applications']), 1)
        self.assertNotIn('source', response.data['applications'][0])
        self.part.active = False
        self.part.save(update_fields=['active'])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_applications_validate_years_duplicates_and_protect_used_identity(self):
        url = '/api/v1/management/applications/'
        data = {'make': ' hyundai ', 'model': 'accent', 'year_from': 2016, 'year_to': 2011}
        self.assertEqual(self.client.post(url, data, format='json').status_code, 400)
        data.update(year_from=2011, year_to=2016)
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.client.post(url, data, format='json').status_code, 400)
        application = Application.objects.get(pk=response.data['id'])
        ItemApplication.objects.create(part=self.part, application=application)
        detail = f'{url}{application.pk}/'
        self.assertEqual(self.client.patch(detail, {'engine': '1.6'}, format='json').status_code, 400)
        self.assertEqual(self.client.delete(detail).status_code, 400)

    def test_duplicate_links_rejected_and_omitted_sections_preserved(self):
        application = self.application()
        link = {'application': str(application.pk), 'position': 'FRONT'}
        self.save_sheet(values={'diameter': {'value': 256}})
        self.assertEqual(self.save_sheet(applications=[link, link]).status_code, 400)
        self.save_sheet(applications=[link])
        self.assertEqual(self.part.specifications.count(), 1)
        self.save_sheet(values={})
        self.assertEqual(self.part.item_applications.count(), 1)

    def test_merge_does_not_drop_source_technical_sheet(self):
        other = Part.objects.create(sku='TARGET')
        self.save_sheet(values={'diameter': {'value': 256}})
        with self.assertRaises(ValidationError):
            merge_catalog_parts(actor=self.root, target_sku=other.sku, source_skus=[self.part.sku])
        self.part.refresh_from_db()
        self.assertIsNone(self.part.merged_into_id)
        self.assertEqual(self.part.specifications.count(), 1)

    def test_non_superusers_cannot_manage_templates_sheets_or_applications(self):
        for user in [None, self.user]:
            self.client.force_authenticate(user)
            for method, url, data in [('get', '/api/v1/management/part-types/', None), ('post', '/api/v1/management/part-types/', {'category': 'A', 'name': 'B'}), ('get', self.template_url, None), ('patch', self.template_url, {'revision': 2, 'fields': []}), ('get', self.sheet_url, None), ('patch', self.sheet_url, {}), ('get', '/api/v1/management/applications/', None), ('post', '/api/v1/management/applications/', {})]:
                response = getattr(self.client, method)(url, data, format='json')
                self.assertIn(response.status_code, [401, 403])
