from unittest.mock import patch

from rest_framework.test import APITestCase

from .catalog_classification import ClassificationProviderError, candidate_context, supplier_suffix_match, validate_suggestions
from .catalog_import import build_plan
from .models import Part, PartCode, User


class CatalogGroupingSuggestionTests(APITestCase):
    def setUp(self):
        self.root = User.objects.create_superuser('grouping-root', 'grouping@example.invalid', 'Grouping1234!')
        self.client.force_authenticate(self.root)
        self.base = Part.objects.create(sku='58411-1R000', description='TAMBOR HYU ACCENT 11-16')
        self.ksm = Part.objects.create(sku='58411-1R000-KSM', description=self.base.description)
        self.raz = Part.objects.create(sku='58411-1R000-RAZ-NP', description=self.base.description)
        self.url = '/api/v1/management/catalog/grouping/'

    def row(self, part):
        return {'sku': part.sku, 'row': 1, 'name': part.sku, 'description': part.description, 'codes': []}

    def proposal(self, source, target, reference):
        return {'source_sku': source, 'target_sku': target, 'category': 'FRENOS', 'subcategory': 'TAMBORES',
                'reason': 'Número base completo y descripción coincidente; revisar equivalencia.', 'confidence': 0.9,
                'source_reference': reference, 'target_reference': reference}

    def test_family_proposal_uses_existing_base_and_exact_description_without_automatic_writes(self):
        other = Part.objects.create(sku='1034-LED-FIJO', description='1034 LED FIJO')
        PartCode.objects.create(part=other, code='58411-1R000-G')
        response = self.client.get(self.url, {'search': '58411-1R000-KSM'})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['count'], 1)
        family = response.data['results'][0]
        self.assertEqual(family['target_sku'], self.base.sku)
        self.assertEqual(set(family['source_skus']), {self.ksm.sku, self.raz.sku})
        self.assertTrue(family['needs_review'])
        self.assertIn('1034-LED-FIJO', family['warnings'][0])
        self.assertEqual(Part.objects.filter(active=True).count(), 4)
        self.assertEqual(PartCode.objects.get(code='58411-1R000-G').part, other)

    def test_positions_sizes_years_and_different_descriptions_are_not_supplier_variants(self):
        for suffix, description in [('LEFT', self.base.description), ('RH', self.base.description),
                                    ('20', self.base.description), ('2012', self.base.description),
                                    ('OTHER', 'TAMBOR HYU ACCENT 17-20')]:
            part = Part.objects.create(sku=f'{self.base.sku}-{suffix}', description=description)
            self.assertFalse(supplier_suffix_match(self.row(part), self.row(self.base)))
        family = self.client.get(self.url).data['results'][0]
        self.assertEqual(set(family['source_skus']), {self.ksm.sku, self.raz.sku})
        self.ksm.description = self.raz.description = self.base.description = ''
        for part in [self.ksm, self.raz, self.base]:
            part.save()
        self.assertEqual(self.client.get(self.url).data['count'], 0)

    def test_full_base_with_supplier_suffix_and_matching_description_passes_evidence_validation(self):
        source = [self.row(self.ksm)]
        candidates = {self.base.sku: self.row(self.base)}
        output = {'suggestions': [self.proposal(self.ksm.sku, self.base.sku, self.base.sku)]}
        accepted = validate_suggestions(output, source, candidates, {self.ksm.sku: [self.base.sku]})
        self.assertEqual(accepted[0]['target_sku'], self.base.sku)
        source[0]['description'] = 'DISCO HYU ACCENT 11-16'
        with self.assertRaises(ClassificationProviderError):
            validate_suggestions(output, source, candidates, {self.ksm.sku: [self.base.sku]})

    def test_selected_family_ai_only_returns_reviewable_proposals_and_categories(self):
        proposals = [self.proposal(self.base.sku, self.base.sku, ''),
                     self.proposal(self.ksm.sku, self.base.sku, self.base.sku)]
        proposals[1]['category'] = 'OTRA CATEGORIA'
        with patch('mall.catalog_grouping_suggestions.call_provider', return_value={'suggestions': list(reversed(proposals))}) as provider:
            response = self.client.post(self.url + 'classify/',
                {'target_sku': self.base.sku, 'source_skus': [self.ksm.sku]}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(provider.call_args.kwargs['feature'], 'catalog_grouping')
        self.assertEqual(response.data['source_skus'], [self.ksm.sku])
        self.assertEqual(response.data['category'], 'FRENOS')
        self.assertEqual(Part.objects.filter(active=True).count(), 3)
        self.assertEqual(PartCode.objects.count(), 0)

    def test_grouping_views_require_superuser_and_reject_stale_or_duplicate_selection(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.url).status_code, 401)
        ordinary = User.objects.create_user('grouping-user', password='Grouping1234!')
        self.client.force_authenticate(ordinary)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url + 'classify/', {}, format='json').status_code, 403)
        self.client.force_authenticate(self.root)
        for sources in [[self.base.sku], [self.ksm.sku, self.ksm.sku], ['MISSING']]:
            self.assertEqual(self.client.post(self.url + 'classify/',
                {'target_sku': self.base.sku, 'source_skus': sources}, format='json').status_code, 400)

    def test_archived_sources_cannot_be_reactivated_but_canonical_aliases_remain_importable(self):
        self.ksm.active = False
        self.ksm.merged_into = self.base
        self.ksm.save()
        PartCode.objects.create(part=self.base, code=self.ksm.sku)
        groups = {self.ksm.sku: {'sku': self.ksm.sku, 'row': 2, 'fields': {'active': True}, 'codes': {}}}
        plan, _ = build_plan(groups, [], 1)
        self.assertFalse(plan['valid'])
        self.assertIn('ya se agrupó', str(plan['errors']))
        groups = {self.base.sku: {'sku': self.base.sku, 'row': 2, 'fields': {}, 'codes': {('', self.ksm.sku): 2}}}
        plan, _ = build_plan(groups, [], 1, lock=False)
        self.assertTrue(plan['valid'], plan)
        self.assertEqual(plan['summary']['added_codes'], 0)
        new = 'SUPPLIER-584111R000'
        incoming = {new: {'sku': new, 'row': 2, 'fields': {'name': self.ksm.sku}, 'codes': {('', self.ksm.sku): 2}}}
        _, candidates, allowed = candidate_context(incoming, [new])
        self.assertIn(self.base.sku, allowed[new])
        self.assertNotIn(self.ksm.sku, candidates)
