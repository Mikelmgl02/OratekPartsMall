import io

from rest_framework.test import APITestCase

from . import prune_catalog_alternos as prune
from .models import Part, PartCode, User
from .oem_links import store_cross_references
from .oem_reference import upsert_reference
from .test_oem_finder import Fresh

URL = '/api/v1/management/alternate-review/'
CATALOG = 'GMB Water Pump Catalog 2016'


def number(code, brand_codes, applications=''):
    ref = upsert_reference('TOYOTA', code, source_kind='aftermarket_catalog', citation=CATALOG,
                           detail={'catalog': 'gmb_wp_2016', 'brand_codes': brand_codes}, defaults={'applications': applications})[0]
    return ref


class AlternateReviewTests(Fresh, APITestCase):
    """16100-69205-RAZ reaches 16100-69205, which GMB prints for two pumps; the import copied GWT-71A's codes onto it."""

    def setUp(self):
        super().setUp()
        self.root = User.objects.create_superuser('alt-review-root', 'alt-review-root@example.invalid', 'Example938!')
        self.client.force_authenticate(self.root)
        self.shared = number('16100-69205', ['GWT-71A', 'GWT-80A'], 'TOYOTA 2RZ HIACE')
        self.camry = number('16100-59305', ['GWT-71A'], 'TOYOTA 1VZFE CAMRY')
        store_cross_references({(self.camry.pk, 'GMB', 'GWT-71A'): [f'{CATALOG} · pág. 216'],
                                (self.shared.pk, 'AISIN', 'WPT-080'): [f'{CATALOG} · pág. 218']})
        self.part = Part.objects.create(sku='16100-69205-RAZ', description='BOMBA AGUA TOY HIACE 2RZ')
        self.wrong = self.code('GMB', 'GWT-71A', 216)
        self.unfiled = self.code('ASAHI', 'A1773', 216)
        self.copy = self.code('AISIN', 'WPT-080', 218)
        self.typed = PartCode.objects.create(part=self.part, brand='FEBEST', code='TM-9', ref_type='company', reference_source='FICHA')
        self.url = f'{URL}{self.part.pk}/'

    def code(self, brand, code, page):
        return PartCode.objects.create(part=self.part, brand=brand, code=code, ref_type='company', reference_source=f'{CATALOG} · pág. {page}')

    def test_the_list_and_the_evidence_of_one_sku(self):
        listing = self.client.get(URL).json()
        self.assertEqual((listing['count'], listing['results'][0]['part']['sku'], listing['results'][0]['undecided'], listing['results'][0]['copies']),
                         (1, '16100-69205-RAZ', 2, 1))
        data = self.client.get(self.url).json()
        self.assertEqual([(n['reference']['code'], n['via'], n['printed_for'], n['applications']) for n in data['numbers']],
                         [('1610069205', ['sku_name'], [{'catalog': CATALOG, 'brand_codes': ['GWT-71A', 'GWT-80A']}], 'TOYOTA 2RZ HIACE')])
        undecided = {row['code']: row for row in data['undecided']}
        self.assertEqual(sorted(undecided), ['A1773', 'GWT-71A'])
        self.assertEqual([(f['reference']['code'], f['applications']) for f in undecided['GWT-71A']['filed_under']], [('1610059305', 'TOYOTA 1VZFE CAMRY')])
        self.assertEqual(undecided['A1773']['filed_count'], 0)  # no number carries it
        self.assertEqual([row['code'] for row in data['copies']], ['WPT-080'])

    def test_keep_confirms_remove_deletes_and_copies_go_last(self):
        kept = self.client.post(self.url, {'action': 'keep', 'alternate': self.unfiled.pk}, format='json')
        self.assertEqual([row['code'] for row in kept.json()['undecided']], ['GWT-71A'])
        self.unfiled.refresh_from_db()
        self.assertEqual(self.unfiled.reference_source, f'CONFIRMADO POR ALT-REVIEW-ROOT · {CATALOG} · pág. 216')
        removed = self.client.post(self.url, {'action': 'remove', 'alternate': self.wrong.pk}, format='json').json()
        self.assertEqual((removed['undecided'], [row['code'] for row in removed['copies']]), ([], ['WPT-080']))
        self.assertFalse(PartCode.objects.filter(pk=self.wrong.pk).exists())
        self.assertEqual(self.client.get(URL).json()['count'], 0)  # nothing left to decide
        self.assertEqual(prune.run(stdout=io.StringIO())['remove'], 1)  # the copy is now an ordinary cleanup candidate
        done = self.client.post(self.url, {'action': 'remove_copies'}, format='json').json()
        self.assertEqual((done['undecided'], done['copies']), ([], []))
        self.assertEqual(set(self.part.codes.values_list('code', flat=True)), {'A1773', 'TM-9'})  # confirmed and typed codes stay

    def test_a_stale_or_foreign_decision_is_refused_with_the_current_state(self):
        for payload in ({'action': 'keep', 'alternate': self.copy.pk}, {'action': 'remove', 'alternate': self.typed.pk},
                        {'action': 'keep', 'alternate': 999999}):
            response = self.client.post(self.url, payload, format='json')
            self.assertEqual(response.status_code, 409, payload)
            self.assertEqual(len(response.json()['review']['undecided']), 2)
        self.assertEqual(self.client.post(self.url, {'action': 'keep'}, format='json').status_code, 400)
        self.assertEqual(PartCode.objects.filter(part=self.part).count(), 4)

    def test_only_superusers_and_only_canonical_skus(self):
        staff = User.objects.create_user('alt-review-staff', 'alt-review-staff@example.invalid', 'Example938!')
        for user in (None, staff):
            self.client.force_authenticate(user)
            for response in (self.client.get(URL), self.client.get(self.url), self.client.post(self.url, {'action': 'remove_copies'}, format='json')):
                self.assertIn(response.status_code, (401, 403))
        self.client.force_authenticate(self.root)
        Part.objects.filter(pk=self.part.pk).update(merged_into=Part.objects.create(sku='OTRO'), active=False)
        self.assertEqual(self.client.get(self.url).status_code, 404)
