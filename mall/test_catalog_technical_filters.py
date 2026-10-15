import json

from rest_framework.test import APITestCase

from .models import Account, Membership, Part, PartSpecification, Role, SupplierItem, TechnicalField, User


class CatalogTechnicalFilterTests(APITestCase):
    url = '/api/v1/catalog/'
    metadata_url = '/api/v1/catalog/technical-filters/'

    def setUp(self):
        self.user = User.objects.create_user('technical-filter-buyer', 'technical-buyer@example.invalid')
        buyer = Account.objects.create(name='CLIENTE')
        buyer.roles.add(Role.objects.get(code='client_business'))
        Membership.objects.create(user=self.user, account=buyer)
        self.client.force_authenticate(self.user)
        self.parts = {sku: Part.objects.create(sku=sku, category='FRENOS', subcategory='DISCOS') for sku in ['A', 'B', 'C', 'EMPTY', 'ZERO']}
        self.kind = self.parts['A'].part_type
        self.fields = {key: TechnicalField.objects.create(template=self.kind.template, key=key, label=key.upper(), **options) for key, options in {
            'diameter': {'kind': 'number', 'unit': 'mm'}, 'thickness': {'kind': 'number', 'unit': 'mm'},
            'ventilated': {'kind': 'boolean'}, 'finish': {'kind': 'text'}, 'material': {'kind': 'choice', 'options': ['ACERO', 'HIERRO']},
        }.items()}
        for sku, diameter, thickness, ventilated in [('A', 262, 10, False), ('B', 280, 12, True), ('C', 300, 10, True), ('ZERO', 0, 0, False)]:
            part = self.parts[sku]
            for key, number in [('diameter', diameter), ('thickness', thickness)]:
                PartSpecification.objects.create(part=part, field=self.fields[key], number_value=number)
            PartSpecification.objects.create(part=part, field=self.fields['ventilated'], boolean_value=ventilated)
        PartSpecification.objects.create(part=self.parts['A'], field=self.fields['finish'], text_value='ACABADO NEGRO')
        PartSpecification.objects.create(part=self.parts['A'], field=self.fields['material'], text_value='ACERO')

    def query(self, rules=(), **params):
        return self.client.get(self.url, {'technical': json.dumps({'part_type': str(self.kind.pk), 'rules': list(rules)}), **params})

    def rule(self, key='diameter', operator='eq', value='262', **extra):
        return {'key': key, 'operator': operator, 'value': value, **extra}

    def test_numeric_comparisons_include_boundaries_only_when_requested(self):
        for operator, value, extra, expected in [
            ('eq', '262.000', {}, ['A']), ('between', '262', {'value_to': '280'}, ['A', 'B']),
            ('gt', '262', {}, ['B', 'C']), ('gte', '262', {}, ['A', 'B', 'C']),
            ('lt', '280', {}, ['A', 'ZERO']), ('lte', '280', {}, ['A', 'B', 'ZERO']), ('eq', '0', {}, ['ZERO']),
        ]:
            with self.subTest(operator=operator, value=value):
                response = self.query([self.rule(operator=operator, value=value, **extra)])
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual([row['sku'] for row in response.data['results']], expected)

    def test_multiple_fields_use_and_and_keep_false_values(self):
        rules = [self.rule(operator='gte'), self.rule('thickness', value='10'), self.rule('ventilated', value='false')]
        response = self.query(rules, include_facets='1')
        self.assertEqual([row['sku'] for row in response.data['results']], ['A'])
        self.assertEqual(response.data['facets']['subcategory'], [{'value': 'DISCOS', 'label': 'DISCOS', 'count': 1}])
        self.assertEqual(self.query([self.rule(), self.rule('thickness', value='12')]).data['count'], 0)
        self.assertEqual(self.query(rules, category='FILTROS').data['count'], 0)
        self.assertEqual(self.query(rules, search='B').data['count'], 0)

    def test_text_and_choice_filters(self):
        self.assertEqual(self.query([self.rule('finish', 'contains', 'negro')]).data['count'], 1)
        self.assertEqual(self.query([self.rule('finish', 'eq', 'negro')]).data['count'], 0)
        self.assertEqual(self.query([self.rule('material', 'eq', 'acero')]).data['count'], 1)

    def test_filters_apply_before_pagination_and_ignore_unpublished_parts(self):
        for index in range(55):
            part = Part.objects.create(sku=f'MATCH-{index:02}', part_type=self.kind)
            PartSpecification.objects.create(part=part, field=self.fields['diameter'], number_value=262)
        hidden = Part.objects.create(sku='HIDDEN', part_type=self.kind, active=False)
        PartSpecification.objects.create(part=hidden, field=self.fields['diameter'], number_value=262)
        first = self.query([self.rule()], include_facets='1').data
        second = self.query([self.rule()], page='2').data
        self.assertEqual(first['count'], 56)
        self.assertEqual(len(first['results']), 50)
        self.assertEqual(len(second['results']), 6)
        self.assertEqual(first['facets']['category'][0]['count'], 56)

    def test_invalid_or_stale_conditions_are_rejected(self):
        for rules in [[self.rule(operator='between', value_to='200')], [self.rule(value='NaN')], [self.rule(value='1e100')],
                      [self.rule(key='missing')], [self.rule(operator='contains')], [self.rule(), self.rule()],
                      [self.rule('ventilated', value='no')], [self.rule('material', value='PLASTICO')], [self.rule()] * 9]:
            with self.subTest(rules=rules):
                self.assertEqual(self.query(rules).status_code, 400)
        for raw in ['not-json', '[]', 'null', '{}', 'x' * 6001]:
            self.assertEqual(self.client.get(self.url, {'technical': raw}).status_code, 400)

    def test_templates_and_matches_respect_supplier_scope(self):
        other = Part.objects.create(sku='PAD', category='FRENOS', subcategory='PASTILLAS')
        TechnicalField.objects.create(template=other.part_type.template, key='width', label='ANCHO', kind='number')
        supplier = Account.objects.create(name='PROVEEDOR')
        supplier.roles.add(Role.objects.get(code='supplier_retail'))
        user = User.objects.create_user('technical-filter-supplier', 'technical-supplier@example.invalid')
        Membership.objects.create(user=user, account=supplier)
        SupplierItem.objects.create(supplier=supplier, part=self.parts['A'], supplier_invent_id='A', codigo='A', source='upload', matching_status='matched')
        self.client.force_authenticate(user)
        metadata = self.client.get(self.metadata_url, {'account': str(supplier.pk)})
        self.assertEqual(metadata.status_code, 200, metadata.data)
        self.assertEqual([row['id'] for row in metadata.data['part_types']], [str(self.kind.pk)])
        self.assertEqual(self.query([self.rule(operator='gte')], account=str(supplier.pk)).data['count'], 1)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.metadata_url).status_code, 401)
        self.client.force_authenticate(User.objects.create_user('technical-filter-unassigned', 'technical-unassigned@example.invalid'))
        self.assertEqual(self.client.get(self.metadata_url).status_code, 403)
