"""What a line reaches through its OEM numbers (inventory refactor S10): its aftermarket References in an exclusive estado, each with the
numbers of the line the merge pass placed it under (detail s9 numbers), and the read-only catalog/<id>/oem-equivalents route."""
from rest_framework.test import APITestCase

from . import references
from .models import Part, User
from .oem_links import equivalents_for, part_equivalents
from .test_oem_finder import Fresh


def number(manufacturer, code, citation='CATÁLOGO GMB 2016'):
    """A library number (S4: a Reference, on a line of its own in referencia when no line carries it)."""
    return references.add_library_number(manufacturer, code, source_kind='aftermarket_catalog', citation=citation)[0]


class EquivalentsTests(Fresh, APITestCase):
    def setUp(self):
        super().setUp()
        self.root = User.objects.create_superuser('oem-links-root', 'oem-links-root@example.invalid', 'Example938!')
        self.client.force_authenticate(self.root)
        self.main, self.sister = number('TOYOTA', '16100-29155'), number('TOYOTA', '16100-29156')
        self.part = Part.objects.create(sku='16100-29155-NPW', description='BOMBA AGUA TOY')
        references.add(self.part, 'GWT-101A', 'GMB', ref_type='company', source='GMB 2016 · pág. 5')
        # The codes the merge pass made the line's References (S9), each under the numbers the catalog printed it for.
        references.add(self.part, 'WPT-111', 'AISIN', tipo='aftermarket', source='GMB 2016 · pág. 5',
                       detail={'s9': {'numbers': ['1610029155', '1610029156']}})
        references.add(self.part, 'AW-1698', 'AIRTEX', tipo='aftermarket', source='ASVA 2017 · pág. 9', detail={'s9': {'numbers': ['1610029156']}})

    def carry(self):
        """Both numbers on the SKU's line, as the merge pass leaves a catalog item's numbers on its SKU (S9): each absorbs its line in
        referencia (3f)."""
        for code in ('16100-29155', '16100-29156'):
            references.add(self.part, code, 'TOYOTA', tipo='oem', source='CATÁLOGO')

    def test_a_line_reaches_its_aftermarket_references_and_the_numbers_it_carries(self):
        data = part_equivalents(self.part)
        self.assertEqual(data['numbers'], [])
        self.assertEqual(sorted((c['brand'], c['code']) for c in data['cross_references']),
                         [('AIRTEX', 'AW-1698'), ('AISIN', 'WPT-111'), ('GMB', 'GWT-101A')])
        self.carry()
        data = part_equivalents(self.part)
        self.assertEqual([(str(n['reference']), n['via']) for n in data['numbers']],
                         [('TOYOTA 1610029155', ['oem']), ('TOYOTA 1610029156', ['oem'])])
        codes = {(c['brand'], c['code']): c for c in data['cross_references']}
        self.assertEqual(codes[('AISIN', 'WPT-111')]['numbers'], [self.main.uid, self.sister.uid])
        self.assertEqual(codes[('AIRTEX', 'AW-1698')]['numbers'], [self.sister.uid])
        self.assertEqual((codes[('AISIN', 'WPT-111')]['citations'], codes[('AISIN', 'WPT-111')]['on_sku']), (['GMB 2016 · pág. 5'], True))
        self.assertEqual(equivalents_for([self.part.pk])[str(self.part.pk)],
                         [{'brand': 'AIRTEX', 'code': 'AW-1698'}, {'brand': 'AISIN', 'code': 'WPT-111'}, {'brand': 'GMB', 'code': 'GWT-101A'}])

    def test_a_code_in_disputa_is_no_equivalent_and_a_grouped_sku_reaches_nothing(self):
        code = self.part.codes.get(brand='AIRTEX')
        references.update(code, estado='disputa', dispute_note='En otra línea.')
        self.assertNotIn(('AIRTEX', 'AW-1698'), {(c['brand'], c['code']) for c in part_equivalents(self.part)['cross_references']})
        target = Part.objects.create(sku='BOMBA-2', description='BOMBA AGUA')
        Part.objects.filter(pk=self.part.pk).update(merged_into=target, estado='unificada')
        self.part.refresh_from_db()
        self.assertEqual(part_equivalents(self.part), {'numbers': [], 'cross_references': []})

    def test_the_endpoint_is_read_only_and_for_superusers(self):
        self.carry()
        url = f'/api/v1/management/catalog/{self.part.pk}/oem-equivalents/'
        data = self.client.get(url).json()
        self.assertEqual([(n['reference']['code'], n['reference']['id'], n['via']) for n in data['numbers']],
                         [('1610029155', str(self.main.uid), ['oem']), ('1610029156', str(self.sister.uid), ['oem'])])
        self.assertEqual(len(data['cross_references']), 3)
        self.assertEqual(self.client.post(url, {}, format='json').status_code, 405)
        self.assertEqual(self.client.get('/api/v1/management/catalog/00000000-0000-4000-8000-000000000001/oem-equivalents/').status_code, 404)
        staff = User.objects.create_user('oem-links-staff', 'oem-links-staff@example.invalid', 'Example938!')
        for user in (None, staff):
            self.client.force_authenticate(user)
            self.assertIn(self.client.get(url).status_code, (401, 403))
