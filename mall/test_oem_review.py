import threading
import time
from unittest import skipUnless
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase
from rest_framework.test import APIClient, APITestCase

from . import oem_apply as oa
from . import oem_finder as of
from . import oem_review as rv
from .catalog_identity import prefer_oem
from .matching_queue import MATCHING_LOCK
from .models import Account, CatalogIdentityChange, Part, PartCode, SupplierItem, User
from .oem_finder_models import OEMApplyHalt, OEMFinderChange, OEMFinderRun, OEMReviewCase, OEMReviewDecision
from .test_oem_finder import Fresh, create_catalog

URL = '/api/v1/management/oem-review/'
RUNS = '/api/v1/management/oem-finder/runs/'


class Review(Fresh):
    def setUp(self):
        super().setUp()
        self.parts = create_catalog()
        self.root = User.objects.create_superuser('oem-review', 'oem-review@example.invalid', 'Example938!')
        self.client.force_authenticate(self.root)
        self.dry = of.execute()['run']

    def case(self, sku):
        return OEMReviewCase.objects.get(part=self.parts[sku])

    def part(self, sku):
        return Part.objects.get(pk=self.parts[sku].pk)

    def decide(self, sku, action, fingerprint=None, **data):
        case = self.case(sku)
        return self.client.post(f'{URL}{case.pk}/', {'action': action, 'fingerprint': fingerprint or case.fingerprint, **data}, format='json')

    def rows(self, **params):
        response = self.client.get(URL, params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def stock(self, sku, quantity=3):
        supplier = Account.objects.get_or_create(name='PROVEEDOR OEM')[0]
        SupplierItem.objects.create(supplier=supplier, supplier_invent_id=f'inv-{sku}', part=self.parts[sku], codigo=sku, brand='', source='upload',
                                    reported_quantity=quantity)


class ListTests(Review, APITestCase):
    def test_filters_counts_facets_and_evidence(self):
        data = self.rows()
        review = OEMReviewCase.objects.filter(status='review')
        self.assertEqual(data['count'], review.count())
        self.assertEqual(data['tiers'], {t: review.filter(tier=t).count() for t in set(review.values_list('tier', flat=True))})
        self.assertEqual((data['statuses'], data['last_run']['id']), ({'review': review.count()}, self.dry.pk))
        probable = self.rows(tier='PROBABLE_BASE')
        self.assertEqual({r['sku'] for r in probable['results']}, {'48654-0K040-MANDO', '31110-07600-MOBIS'})
        self.assertEqual(probable['tiers'], data['tiers'])  # tier chips keep counting every tier under the other filters
        self.assertEqual({r['sku'] for r in self.rows(blocker='no_second_unit')['results']}, {'48654-0K040-MANDO'})
        self.assertEqual(len(self.rows(blocker='conflict')['results']), 3)
        self.assertEqual(len(self.rows(blocker='conflict:shared_base_family')['results']), 2)
        self.assertEqual({r['sku'] for r in self.rows(blocker='lexicon_not_strong')['results']}, {'31110-07600-MOBIS'})
        self.assertEqual({r['sku'] for r in self.rows(chain='mando', tier='CONFLICT')['results']}, {'48654-0K050-MANDO', '48654-60020-MANDO'})
        self.assertEqual({r['sku'] for r in self.rows(make='KIA')['results']}, {'31110-07600-MOBIS'})
        self.assertEqual({r['sku'] for r in self.rows(system='MITSU_OLD')['results']}, {'MR-968365'})
        self.assertEqual({r['sku'] for r in self.rows(search='0k040')['results']}, {'48654-0K040-MANDO'})
        self.assertEqual(self.rows(in_stock='true')['count'], 0)
        self.stock('MR-968365')
        of.execute()
        self.assertEqual([r['sku'] for r in self.rows(in_stock='true')['results']], ['MR-968365'])
        self.assertEqual(self.rows()['results'][0]['sku'], 'MR-968365')  # in stock first
        facets = data['facets']
        self.assertIn(['conflict:shared_base_family', 2], facets['blockers'])
        self.assertIn(['no_second_unit', 1], facets['blockers'])
        self.assertIn(['MANDO', 3], facets['chains'])
        self.assertIn(['HMG', 6], facets['systems'])
        for bad in ({'tier': 'AUTO_FLAG_CURRENT'}, {'status': 'nada'}, {'in_stock': 'si'}):
            self.assertEqual(self.client.get(URL, bad).status_code, 400)

    def test_rows_carry_the_evidence_and_the_actions_of_their_tier(self):
        rows = {r['sku']: r for r in self.rows()['results']}
        mando = rows['48654-0K040-MANDO']
        self.assertEqual((mando['candidate'], mando['brand'], mando['grade'], mando['method'], mando['stale']), ('48654-0K040', 'TOYOTA', 'F3', 'rename', ''))
        self.assertEqual((mando['evidence']['lexicon']['result'], mando['evidence']['lexicon']['n_other_keys']), ('agree_strong', 11))
        self.assertEqual((mando['chain_tokens'][0]['tok'], mando['chain_tokens'][0]['class'], mando['actions']), ('MANDO', 'TAG', ['approve', 'dismiss']))
        self.assertEqual((rows['48654-12030']['method'], rows['48654-12030']['actions']), ('flag', ['approve', 'dismiss']))
        multi = rows['11210-30110 11210-0L020']
        self.assertEqual(multi['actions'], ['choose_oem', 'dismiss'])
        self.assertEqual([(c['code'], c['brand']) for c in multi['choices']], [('11210-30110', 'TOYOTA'), ('11210-0L020', 'TOYOTA')])
        self.assertEqual([c['code'] for c in rows['39180-22050/60']['choices']], ['39180-22050', '39180-22060'])
        self.assertEqual(rows['48654-0K050-MANDO']['actions'], ['send_to_merge', 'dismiss'])
        self.assertEqual(rows['48654-0K050-MANDO']['evidence']['shared_with'], ['48654-0K050-NAK'])
        self.assertEqual(rows['48654-60020-MANDO']['actions'], ['dismiss'])  # incompatible family: never merged
        self.assertEqual(rows['48654-60020-MANDO']['evidence']['family_incompatible'], ['48654-60020'])
        unknown = rows['43211-29026-JR']
        self.assertEqual((unknown['actions'], unknown['evidence']['blocking_unknowns'], unknown['chain_tokens'][0]['class']), (['dismiss'], ['JR'], 'UNKNOWN'))
        self.assertEqual(rows['333512']['actions'], ['dismiss'])
        xref = rows['CEKH-52R-CTR 54840-2T000']
        self.assertEqual(xref['evidence']['secondary_company_refs'][0]['code'], 'CEKH-52R')
        self.assertEqual((rows['MR-968365']['candidate'], rows['MR-968365']['written_form']), ('MR968365', 'MR-968365'))

    def test_superusers_only_and_unmigrated(self):
        case = self.case('48654-0K040-MANDO')
        self.client.force_authenticate(User.objects.create_user('lector', 'lector@example.invalid', 'Example938!'))
        for method, url in (('get', URL), ('get', URL + 'summary/'), ('get', URL + 'bulk/'), ('post', URL + 'bulk/'), ('post', f'{URL}{case.pk}/'),
                            ('post', URL + 'bulk/1/')):
            self.assertEqual(getattr(self.client, method)(url, {}, format='json').status_code, 403, url)
        self.client.force_authenticate(self.root)
        with patch('mall.oem_review.review_tables_ready', return_value=False):
            for method, url in (('get', URL), ('get', URL + 'summary/'), ('get', URL + 'bulk/'), ('post', f'{URL}{case.pk}/')):
                self.assertEqual(getattr(self.client, method)(url, {}, format='json').status_code, 503, url)
        self.assertEqual(self.client.get(URL + 'summary/').data, {'statuses': {'review': 19}, 'review': 19})
        self.assertEqual(self.client.post(f'{URL}999999/', {'action': 'dismiss', 'fingerprint': 'x', 'reason': 'not_oem'}, format='json').status_code, 404)


class DecisionTests(Review, APITestCase):
    def test_approve_renames_in_an_undoable_run_and_undo_reopens_the_case(self):
        response = self.decide('48654-0K040-MANDO', 'approve')
        self.assertEqual(response.status_code, 200, response.data)
        part = self.part('48654-0K040-MANDO')
        run = OEMFinderRun.objects.get(pk=response.data['run'])
        self.assertEqual((part.pk, part.sku, part.name, part.is_OEM), (self.parts['48654-0K040-MANDO'].pk, '48654-0K040', '48654-0K040', True))
        source = f'algo:oem-finder:v2:review-probable-base:{run.pk}'
        self.assertTrue(part.codes.filter(code='48654-0K040', brand='TOYOTA', ref_type='oem', reference_source=source).exists())
        self.assertTrue(part.codes.filter(code='48654-0K040-MANDO').exists())  # the old SKU stays searchable
        self.assertEqual((run.mode, run.status, run.scope['stage'], run.scope['action'], run.applied['applied']), ('apply_auto', 'completed', 'review', 'approve', 1))
        link = OEMFinderChange.objects.get(pk=response.data['change'])
        self.assertEqual((link.run, link.tier, link.method, link.code, link.change.previous_sku), (run, 'AUTO_RENAME_BASE', 'rename_base', '48654-0K040', '48654-0K040-MANDO'))
        self.assertEqual((link.evidence['review']['tier'], link.evidence['review']['action'], link.change.actor), ('PROBABLE_BASE', 'approve', self.root))
        case = self.case('48654-0K040-MANDO')
        self.assertEqual((case.status, case.decided_by, case.decision['run'], case.decision['change']), ('applied', self.root, run.pk, link.pk))
        self.assertEqual((response.data['case']['status'], response.data['case']['stale'], response.data['case']['sku']), ('applied', '', '48654-0K040'))
        decision = OEMReviewDecision.objects.get(case=case)
        self.assertEqual((decision.action, decision.run, decision.change, decision.code, decision.actor), ('approve', run, link, '48654-0K040', self.root))
        again = self.decide('48654-0K040-MANDO', 'approve')
        self.assertEqual((again.status_code, again.data['run'], again.data['change']), (200, run.pk, None))
        self.assertEqual(OEMFinderRun.objects.filter(mode='apply_auto').count(), 1)
        self.assertEqual([r['id'] for r in self.client.get(RUNS, {'stage': 'review'}).data['results']], [run.pk])
        self.assertEqual(self.client.get(RUNS, {'stage': 'auto'}).data['count'], 1)  # only the dry run
        spot = self.client.get(f'{RUNS}{run.pk}/spot-check/').data
        self.assertEqual([r['sku'] for r in spot['rows']], ['48654-0K040'])
        undo = self.client.post(f'{RUNS}{run.pk}/revert/', {}, format='json')
        self.assertEqual((undo.status_code, undo.data['reverted']), (200, 1))
        part = self.part('48654-0K040-MANDO')
        self.assertEqual((part.sku, part.is_OEM, part.codes.count()), ('48654-0K040-MANDO', False, 0))
        case = self.case('48654-0K040-MANDO')
        self.assertEqual((case.status, case.decision['action']), ('review', 'reverted'))
        self.assertEqual(list(case.decisions.values_list('action', flat=True)), ['reverted', 'approve'])
        rows = {r['sku']: r for r in self.rows()['results']}
        self.assertEqual((rows['48654-0K040-MANDO']['stale'], rows['48654-0K040-MANDO']['actions']), ('', ['approve', 'dismiss']))
        self.assertEqual(self.decide('48654-0K040-MANDO', 'approve').status_code, 200)  # a human may approve it again
        self.assertEqual(self.part('48654-0K040-MANDO').sku, '48654-0K040')

    def test_approve_confirms_a_current_oem_or_renames_to_the_manufacturer_form(self):
        response = self.decide('48654-12030', 'approve')
        self.assertEqual(response.status_code, 200, response.data)
        part = self.part('48654-12030')
        self.assertEqual((part.sku, part.is_OEM), ('48654-12030', True))
        link = OEMFinderChange.objects.get(pk=response.data['change'])
        self.assertEqual((link.tier, link.method, link.change.reference['action']), ('AUTO_FLAG_CURRENT', 'flag_current', 'confirm_current'))
        response = self.decide('MR-968365', 'approve')
        self.assertEqual(response.status_code, 200, response.data)
        part = self.part('MR-968365')
        self.assertEqual((part.sku, part.is_OEM), ('MR968365', True))
        self.assertTrue(part.codes.filter(code='MR-968365', brand='MITSUBISHI', ref_type='oem', reference_source='').exists())
        self.assertEqual(response.data['case']['decision']['method'], 'rename')

    def test_local_xref_keeps_the_company_code_as_an_alterno(self):
        response = self.decide('CEKH-52R-CTR 54840-2T000', 'approve')
        self.assertEqual(response.status_code, 200, response.data)
        part = self.part('CEKH-52R-CTR 54840-2T000')
        self.assertEqual(part.sku, '54840-2T000')
        self.assertTrue(part.codes.filter(code='CEKH-52R', brand='CTR', ref_type='company').exists())
        self.assertTrue(part.codes.filter(code='CEKH-52R-CTR 54840-2T000').exists())
        oa.revert_run(response.data['run'], actor=self.root)
        self.assertEqual((self.part('CEKH-52R-CTR 54840-2T000').sku, self.part('CEKH-52R-CTR 54840-2T000').codes.count()), ('CEKH-52R-CTR 54840-2T000', 0))

    def test_a_case_whose_part_changed_cannot_be_approved(self):
        Part.objects.filter(pk=self.parts['48654-0K040-MANDO'].pk).update(description='BASE AMORT TOY HILUX REFORZADA')
        row = {r['sku']: r for r in self.rows()['results']}['48654-0K040-MANDO']
        self.assertEqual((row['stale'], row['actions']), ('snapshot_changed', ['dismiss']))
        response = self.decide('48654-0K040-MANDO', 'approve')
        self.assertEqual(response.status_code, 409)
        self.assertIn('cambió desde el análisis', response.data['detail'])
        self.assertEqual(response.data['reason'], 'snapshot_changed')
        self.assertEqual((self.part('48654-0K040-MANDO').sku, PartCode.objects.count()), ('48654-0K040-MANDO', 0))
        self.assertFalse(OEMFinderRun.objects.filter(mode='apply_auto').exists() or OEMFinderChange.objects.exists())
        self.assertEqual(self.case('48654-0K040-MANDO').status, 'review')
        self.assertEqual(self.decide('48654-12030', 'approve', fingerprint='0' * 64).status_code, 409)  # stale fingerprint
        old = self.case('48654-0K040-MANDO').fingerprint
        of.execute()  # the next analysis re-grades the changed Part: a new fingerprint
        self.assertNotEqual(self.case('48654-0K040-MANDO').fingerprint, old)
        self.assertEqual(self.decide('48654-0K040-MANDO', 'approve', fingerprint=old).status_code, 409)
        self.assertEqual(self.decide('48654-0K040-MANDO', 'approve').status_code, 200)

    def test_a_retired_part_or_a_claimed_oem_blocks_the_approval(self):
        """Another SKU holding the OEM (its SKU or an alterno) sends the case to merge review, never a rename beside it."""
        claimant = Part.objects.create(sku='486540K040', description='BASE AMORT TOY HILUX')
        response = self.decide('48654-0K040-MANDO', 'approve')
        self.assertEqual((response.status_code, response.data['reason']), (409, 'conflict'))
        self.assertIn('486540K040', response.data['detail'])
        self.assertEqual((self.part('48654-0K040-MANDO').sku, PartCode.objects.count()), ('48654-0K040-MANDO', 0))
        case = self.case('48654-0K040-MANDO')
        self.assertEqual((case.tier, case.underlying_tier, case.evidence['apply_conflict']['owners']), ('CONFLICT', 'PROBABLE_BASE', [str(claimant.pk)]))
        self.assertEqual(self.decide('48654-0K040-MANDO', 'send_to_merge').data['family']['target_sku'], '486540K040')  # the owner is the target
        self.assertFalse(OEMFinderRun.objects.filter(mode='apply_auto').exists())
        Part.objects.filter(pk=self.parts['48654-12030'].pk).update(active=False)
        self.assertEqual(self.decide('48654-12030', 'approve').data['reason'], 'retired')

    def test_a_sku_sharing_the_base_since_the_analysis_sends_the_case_to_merge_review(self):
        """The finder grades a base another SKU claims as CONFLICT; a tagged sibling created after the analysis must not be ignored."""
        Part.objects.create(sku='48654-0K040-ANULADO', description='BASE AMORT TOY HILUX')  # a lifecycle SKU claims nothing
        Part.objects.create(sku='48654-0K040-XX', description='BASE AMORT TOY HILUX', is_OEM=True)  # nor does a SKU marked OEM
        Part.objects.create(sku='48654-0K0401-NAK', description='BASE AMORT TOY HILUX')  # a longer number is another base
        preview = self.client.get(URL + 'bulk/', {'tier': 'PROBABLE_BASE'}).data
        self.assertEqual({r['sku']: r['outcome'] for r in preview['sample']}['48654-0K040-MANDO'], 'applied')
        sibling = Part.objects.create(sku='48654-0K040-NAK', description='BASE AMORT TOY HILUX')
        preview = self.client.get(URL + 'bulk/', {'tier': 'PROBABLE_BASE'}).data
        self.assertEqual({r['sku']: r['outcome'] for r in preview['sample']}['48654-0K040-MANDO'], 'conflict')
        response = self.decide('48654-0K040-MANDO', 'approve')
        self.assertEqual((response.status_code, response.data['reason']), (409, 'conflict'))
        self.assertIn('48654-0K040-NAK comparte la base 48654-0K040', response.data['detail'])
        self.assertEqual(self.part('48654-0K040-MANDO').sku, '48654-0K040-MANDO')
        case = self.case('48654-0K040-MANDO')
        self.assertEqual((case.tier, case.evidence['apply_conflict']['owners']), ('CONFLICT', [str(sibling.pk)]))
        family = self.decide('48654-0K040-MANDO', 'send_to_merge').data['family']
        self.assertEqual(sorted(family['source_skus'] + [family['target_sku']]), ['48654-0K040', '48654-0K040-MANDO', '48654-0K040-NAK'])
        # a current-OEM tier keeps its siblings (the finder lists them to merge into it): the flag is still approved
        Part.objects.create(sku='48654-12030-NAK', description='BASE AMORT TOY COROLLA')
        self.assertEqual(self.decide('48654-12030', 'approve').status_code, 200)

    def test_a_tag_relabelled_since_the_analysis_is_never_stripped(self):
        """The owner relabels MANDO as a VARIANT in Sufijos and no analysis ran since: neither one approval nor a bulk one strips it."""
        from django.db.models import F, Max
        from .catalog_suffix_models import CatalogCodeSuffix
        top = CatalogCodeSuffix.objects.aggregate(v=Max('version'))['v']
        CatalogCodeSuffix.objects.filter(token='MANDO').update(cls='VARIANT', tag_kind='', variant_kind='spec', version=top + 1)
        row = {r['sku']: r for r in self.rows()['results']}['48654-0K040-MANDO']
        self.assertEqual((row['stale'], row['actions']), ('suffix_changed', ['dismiss']))
        response = self.decide('48654-0K040-MANDO', 'approve')
        self.assertEqual((response.status_code, response.data['reason']), (409, 'suffix_changed'))
        self.assertIn('MANDO', response.data['detail'])
        self.assertEqual((self.part('48654-0K040-MANDO').sku, PartCode.objects.count()), ('48654-0K040-MANDO', 0))
        preview = self.client.get(URL + 'bulk/', {'tier': 'PROBABLE_BASE'}).data
        self.assertEqual((preview['selected'], preview['excluded']), (1, {'stale': 1}))
        self.assertEqual([r['sku'] for r in preview['sample']], ['31110-07600-MOBIS'])
        CatalogCodeSuffix.objects.filter(token='MANDO').update(cls='TAG', tag_kind='brand', variant_kind='', version=F('version') + 1)
        self.assertEqual(self.decide('48654-0K040-MANDO', 'approve').status_code, 200)  # a TAG again: the analysis holds

    def test_choosing_a_number_another_sku_owns_goes_to_merge_review_with_that_number(self):
        sku = '11210-30110 11210-0L020'
        owner = Part.objects.create(sku='11210-0L020', name='11210-0L020', description='TAPA VALV TOY HILUX')
        response = self.decide(sku, 'choose_oem', code='11210-0L020')
        self.assertEqual((response.status_code, response.data['reason']), (409, 'conflict'))
        case = self.case(sku)
        self.assertEqual((case.tier, case.evidence['apply_conflict']['code'], case.evidence['apply_conflict']['owners']), ('CONFLICT', '11210-0L020', [str(owner.pk)]))
        self.assertEqual((self.part(sku).sku, PartCode.objects.count()), (sku, 0))
        family = self.decide(sku, 'send_to_merge').data['family']
        self.assertEqual((family['target_sku'], family['source_skus'], family['oem']), ('11210-0L020', [sku], {'code': '11210-0L020', 'brand': 'TOYOTA'}))

    def test_multi_oem_choices_use_the_parsed_manufacturer_form_only(self):
        case = self.case('11210-30110 11210-0L020')
        ev = case.evidence
        ev['candidates'] = [c for c in ev['candidates'] if c['key'] != '112100L020']
        ev['candidates'].append({'key': '112100L020', 'system': 'TOYOTA', 'grade': 3, 'manufacturer_form': '11210-0L020', 'written': '11210-OL020'})
        ev['other_oem_candidates'] = ['112100L020', '11210OL020', '361K']  # the catalog spelling of the same number, an unparsed tail
        OEMReviewCase.objects.filter(pk=case.pk).update(evidence=ev)
        case.refresh_from_db()
        self.assertEqual([(c['code'], c['written']) for c in rv.choices(case)], [('11210-30110', '11210-30110'), ('11210-0L020', '11210-OL020')])
        self.assertEqual(self.decide('11210-30110 11210-0L020', 'choose_oem', code='361K').status_code, 400)
        response = self.decide('11210-30110 11210-0L020', 'choose_oem', code='11210-30110')
        self.assertEqual(response.status_code, 200, response.data)
        codes = set(self.part('11210-30110 11210-0L020').codes.values_list('code', flat=True))
        self.assertEqual(codes, {'11210-30110', '11210-0L020', '11210-30110 11210-0L020'})  # never 361K nor a guessed spelling

    def test_choose_oem_keeps_the_other_numbers_as_oem_alternos(self):
        sku = '11210-30110 11210-0L020'
        self.assertEqual(self.decide(sku, 'approve').status_code, 400)
        self.assertEqual(self.decide(sku, 'choose_oem', code='11210-99999').status_code, 400)
        self.assertEqual(self.decide('48654-0K040-MANDO', 'choose_oem', code='48654-0K040').status_code, 400)
        response = self.decide(sku, 'choose_oem', code='11210-0l020')
        self.assertEqual(response.status_code, 200, response.data)
        part = self.part(sku)
        self.assertEqual((part.sku, part.is_OEM), ('11210-0L020', True))
        self.assertTrue(part.codes.filter(code='11210-30110', brand='TOYOTA', ref_type='oem', reference_source='').exists())
        self.assertTrue(part.codes.filter(code=sku).exists())
        link = OEMFinderChange.objects.get(pk=response.data['change'])
        self.assertEqual((link.evidence['review']['action'], link.evidence['review']['kept']), ('choose_oem', ['TOYOTA 11210-30110']))
        self.assertEqual(OEMReviewDecision.objects.get(case=self.case(sku)).action, 'choose_oem')
        self.assertEqual(self.decide(sku, 'choose_oem', code='11210-30110').status_code, 409)  # already decided with another number
        oa.revert_run(response.data['run'], actor=self.root)
        self.assertEqual((self.part(sku).sku, self.part(sku).codes.count()), (sku, 0))

    def test_a_catalog_refusal_turns_the_case_into_a_conflict(self):
        other = Part.objects.create(sku='OTRO-1', description='BASE AMORT TOY HILUX')
        PartCode.objects.create(part=other, code='486540K040MANDO', brand='ACME', ref_type='company')  # owns the old SKU once normalized
        response = self.decide('48654-0K040-MANDO', 'approve')
        self.assertEqual((response.status_code, response.data['reason']), (409, 'conflict'))
        self.assertIn('Conflicto (agrupar)', response.data['detail'])
        case = self.case('48654-0K040-MANDO')
        self.assertEqual((case.tier, case.underlying_tier, case.status), ('CONFLICT', 'PROBABLE_BASE', 'review'))
        self.assertIn('apply_conflict', case.blockers)
        self.assertEqual(case.evidence['apply_conflict']['owners'], [str(other.pk)])
        self.assertEqual(list(case.decisions.values_list('action', 'tier', 'actor', 'run')), [('apply_conflict', 'PROBABLE_BASE', self.root.pk, None)])  # audited
        self.assertEqual(self.part('48654-0K040-MANDO').sku, '48654-0K040-MANDO')
        self.assertFalse(OEMFinderRun.objects.filter(mode='apply_auto').exists())
        merge = self.decide('48654-0K040-MANDO', 'send_to_merge')
        self.assertEqual(merge.status_code, 200, merge.data)
        self.assertEqual((merge.data['family']['target_sku'], merge.data['family']['source_skus']), ('48654-0K040-MANDO', ['OTRO-1']))

    def test_send_to_merge_returns_the_grouping_family(self):
        self.assertEqual(self.decide('48654-0K040-MANDO', 'send_to_merge').status_code, 400)
        refused = self.decide('48654-60020-MANDO', 'send_to_merge')
        self.assertEqual(refused.status_code, 409)
        self.assertIn('incompatibles', refused.data['detail'])
        response = self.decide('48654-0K050-MANDO', 'send_to_merge')
        self.assertEqual(response.status_code, 200, response.data)
        family = response.data['family']
        self.assertEqual((family['target_sku'], family['target']['proposed_parent'], sorted(family['source_skus'])),
                         ('48654-0K050', True, ['48654-0K050-MANDO', '48654-0K050-NAK']))
        self.assertEqual(family['oem'], {'code': '48654-0K050', 'brand': 'TOYOTA'})
        case = self.case('48654-0K050-MANDO')
        self.assertEqual((case.status, case.decision['action'], response.data['case']['actions']), ('resolved', 'send_to_merge', ['reopen']))
        self.assertEqual(self.decide('48654-0K050-MANDO', 'send_to_merge').data['family']['target_sku'], '48654-0K050')  # same decision again
        self.assertEqual(OEMReviewDecision.objects.filter(case=case).count(), 1)
        # Agrupar SKU accepts the family as proposed (new parent from the common base)
        merged = self.client.post('/api/v1/management/catalog/grouping/merge/', {'target_sku': family['target_sku'], 'source_skus': family['source_skus'],
                                                                                 'create_target': True}, format='json')
        self.assertEqual(merged.status_code, 200, merged.data)
        self.assertEqual(sorted(merged.data['merged_skus']), ['48654-0K050-MANDO', '48654-0K050-NAK'])
        self.assertEqual(self.rows(status='resolved')['results'][0]['stale'], 'retired')
        reopened = self.decide('48654-0K050-MANDO', 'reopen')
        self.assertEqual((reopened.status_code, reopened.data['case']['status'], reopened.data['case']['actions']), (200, 'review', ['dismiss']))

    def test_send_to_merge_targets_the_part_that_owns_the_oem(self):
        extra = Part.objects.create(sku='54840-1R000-MOBIS', name='54840-1R000-MOBIS', description='TERM ESTAB HYU ACCENT')
        self.parts['54840-1R000-MOBIS'] = extra
        of.execute()
        self.assertEqual(self.case('54840-1R000-MOBIS').blockers, ['conflict:base_is_existing_active_part'])
        family = self.decide('54840-1R000-MOBIS', 'send_to_merge').data['family']
        self.assertEqual((family['target_sku'], family['target']['id'], family['source_skus']), ('54840-1R000', str(self.parts['54840-1R000'].pk), ['54840-1R000-MOBIS']))

    def test_dismiss_needs_a_reason_and_reopen_returns_the_case(self):
        self.assertEqual(self.decide('333512', 'dismiss').status_code, 400)
        self.assertEqual(self.decide('333512', 'dismiss', reason='other').status_code, 400)
        response = self.decide('333512', 'dismiss', reason='other', note='ES UN AMORTIGUADOR KYB SIN OEM')
        self.assertEqual(response.status_code, 200, response.data)
        case = self.case('333512')
        self.assertEqual((case.status, case.decision['reason'], case.decision['note']), ('dismissed', 'other', 'ES UN AMORTIGUADOR KYB SIN OEM'))
        self.assertEqual(self.decide('333512', 'dismiss', reason='not_oem').status_code, 200)  # the same decision again
        self.assertEqual(self.decide('333512', 'approve').status_code, 400)
        self.assertEqual(self.rows(status='dismissed')['results'][0]['actions'], ['reopen'])
        self.assertEqual(self.decide('333512', 'reopen').data['case']['status'], 'review')
        self.assertEqual(list(case.decisions.values_list('action', flat=True)), ['reopen', 'dismiss'])
        of.execute()
        self.assertEqual(self.case('333512').status, 'review')

    def test_tiers_that_are_not_approved_from_the_review(self):
        for sku in ('43211-29026-JR', '333512', '48654-0K050-MANDO', '48654-60030-MANDO'):
            response = self.decide(sku, 'approve')
            self.assertEqual(response.status_code, 400, sku)
        self.assertFalse(OEMFinderRun.objects.filter(mode='apply_auto').exists())

    def test_busy_or_importing_catalog_refuses_writes(self):
        with patch('mall.oem_finder.import_running', return_value=True):
            self.assertEqual(self.decide('48654-12030', 'approve').status_code, 409)
        with patch.object(rv, 'LOCK_WAIT', 0), patch('mall.oem_finder.matching_lock') as lock:
            lock.return_value.__enter__.return_value = False
            self.assertEqual(self.decide('48654-12030', 'approve').status_code, 409)
        self.assertFalse(self.part('48654-12030').is_OEM)


class BulkTests(Review, APITestCase):
    def preview(self, **params):
        response = self.client.get(URL + 'bulk/', params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def start(self, selection, **filters):
        return self.client.post(URL + 'bulk/', {**filters, 'selection': selection}, format='json')

    def step(self, run, action='continue'):
        response = self.client.post(f'{URL}bulk/{run}/', {'action': action}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def test_preview_start_batches_spot_check_and_undo(self):
        preview = self.preview(tier='CURRENT_LIKELY_OEM')
        self.assertEqual((preview['count'], preview['selected'], preview['excluded'], preview['running']), (4, 4, {}, None))
        self.assertEqual((preview['tiers'], preview['methods']), ({'CURRENT_LIKELY_OEM': 4}, {'flag': 3, 'rename': 1}))
        self.assertEqual({r['sku']: (r['method'], r['outcome']) for r in preview['sample']}['MR-968365'], ('rename', 'applied'))
        self.assertFalse(OEMFinderRun.objects.filter(mode='apply_auto').exists())  # a preview writes nothing
        with patch.object(rv, 'BULK_BATCH', 3):
            started = self.start(preview['selection'], tier='CURRENT_LIKELY_OEM')
            self.assertEqual(started.status_code, 201, started.data)
            run = started.data['run']
            self.assertEqual((run['status'], run['scope']['action'], run['scope']['filters'], 'cases' in run['scope']), ('running', 'approve_batch', {'tier': 'CURRENT_LIKELY_OEM'}, False))
            self.assertEqual(self.start(preview['selection'], tier='CURRENT_LIKELY_OEM').data['reason'], 'running')
            first = self.step(run['id'])
            self.assertEqual((first['finished'], first['run']['applied']['done'], first['run']['applied']['applied']), (False, 3, 3))
            last = self.step(run['id'])
        self.assertEqual((last['finished'], last['run']['applied']['applied'], len(last['run']['applied']['batches'])), (True, 4, 2))
        self.assertEqual((last['run']['applied']['tiers'], last['run']['changes'], len(last['run']['applied']['spot_check'])), ({'CURRENT_LIKELY_OEM': 4}, 4, 4))
        self.assertEqual(self.step(run['id'])['run']['applied']['done'], 4)  # a finished run does nothing more
        self.assertEqual(set(Part.objects.filter(is_OEM=True).values_list('sku', flat=True)), {'54840-1R000', '54840-2H000', '54840-3X000', 'MR968365'})
        self.assertEqual(OEMReviewCase.objects.filter(tier='CURRENT_LIKELY_OEM', status='applied').count(), 4)
        self.assertTrue(all(c.evidence['review']['bulk'] for c in OEMFinderChange.objects.all()))
        self.assertEqual(len(self.client.get(f'{RUNS}{run["id"]}/spot-check/').data['rows']), 4)
        undo = self.client.post(f'{RUNS}{run["id"]}/revert/', {}, format='json').data
        self.assertEqual((undo['reverted'], undo['skipped']), (4, []))
        self.assertFalse(Part.objects.filter(is_OEM=True).exists())
        self.assertEqual(OEMReviewCase.objects.filter(tier='CURRENT_LIKELY_OEM', status='review').count(), 4)
        again = self.preview(tier='CURRENT_LIKELY_OEM')  # a bulk approval never re-applies what a human reverted
        self.assertEqual((again['selected'], again['excluded']), (0, {'previously_reverted': 4}))

    def test_selection_excludes_other_tiers_and_changed_parts(self):
        Part.objects.filter(pk=self.parts['54840-1R000'].pk).update(description='TERM ESTAB HYU ACCENT 12-')
        preview = self.preview()
        bulk = OEMReviewCase.objects.filter(tier__in=rv.BULK_TIERS).count()
        self.assertEqual((preview['count'], preview['selected']), (19, bulk - 1))
        self.assertEqual(preview['excluded'], {'tier': 19 - bulk, 'stale': 1})
        with patch.object(rv, 'BULK_LIMIT', 2):
            limited = self.preview()
        self.assertEqual((limited['selected'], limited['excluded']['over_limit']), (2, bulk - 2))

    def test_the_selection_must_match_the_preview_and_the_halt_rule_applies(self):
        preview = self.preview(tier='PROBABLE_BASE')
        self.assertEqual(self.start('0' * 64, tier='PROBABLE_BASE').data['reason'], 'selection_changed')
        self.assertEqual(self.start(preview['selection'], tier='CURRENT_REVIEW').data['reason'], 'selection_changed')
        oa.halt('MUESTRA CON ERRORES', actor=self.root)
        self.assertEqual(self.preview(tier='PROBABLE_BASE')['halt']['reason'], 'MUESTRA CON ERRORES')
        refused = self.start(preview['selection'], tier='PROBABLE_BASE')
        self.assertEqual((refused.status_code, refused.data['reason']), (409, 'halted'))
        oa.resume(actor=self.root)
        run = self.start(preview['selection'], tier='PROBABLE_BASE').data['run']
        oa.halt('DETENER', actor=self.root)
        stopped = self.step(run['id'])
        self.assertEqual((stopped['finished'], stopped['run']['applied']['stopped'], stopped['run']['applied']['applied']), (True, 'halted', 0))
        self.assertEqual(self.client.post(URL + 'bulk/', {'selection': 'x', 'tier': 'NADA'}, format='json').status_code, 400)

    def test_each_row_compares_its_fingerprint_and_snapshot(self):
        preview = self.preview(tier='CURRENT_LIKELY_OEM')
        run = self.start(preview['selection'], tier='CURRENT_LIKELY_OEM').data['run']
        self.assertEqual(self.decide('54840-1R000', 'dismiss', reason='not_oem').status_code, 200)
        Part.objects.filter(pk=self.parts['54840-2H000'].pk).update(description='TERM ESTAB HYU ELANTRA 07-')
        done = self.step(run['id'])['run']['applied']
        self.assertEqual((done['applied'], done['skipped']), (2, {'case_changed': 1, 'snapshot_changed': 1}))
        self.assertEqual(self.case('54840-1R000').status, 'dismissed')
        self.assertFalse(self.part('54840-2H000').is_OEM)

    def test_stop_ends_an_unfinished_run(self):
        with patch.object(rv, 'BULK_BATCH', 1):
            preview = self.preview(tier='CURRENT_LIKELY_OEM')
            run = self.start(preview['selection'], tier='CURRENT_LIKELY_OEM').data['run']
            self.step(run['id'])
            stopped = self.step(run['id'], 'stop')
        self.assertEqual((stopped['finished'], stopped['run']['applied']['stopped'], stopped['run']['applied']['applied']), (True, 'cancelled', 1))
        self.assertIsNone(self.preview(tier='CURRENT_LIKELY_OEM')['running'])
        self.assertEqual(self.client.post(f'{URL}bulk/{self.dry.pk}/', {'action': 'continue'}, format='json').status_code, 404)

    def test_unexpected_errors_roll_back_the_row_stop_the_run_and_raise_the_halt(self):
        preview = self.preview(tier='CURRENT_LIKELY_OEM')
        run = self.start(preview['selection'], tier='CURRENT_LIKELY_OEM').data['run']
        real = prefer_oem

        def flaky(part, **kwargs):
            if part.sku == '54840-2H000':
                raise RuntimeError('fallo inesperado')
            return real(part, **kwargs)
        with patch('mall.oem_review.prefer_oem', side_effect=flaky):
            done = self.step(run['id'])['run']
        self.assertEqual((done['status'], done['applied']['stopped'], done['applied']['errors'], done['applied']['applied']), ('completed', 'errors', 1, 3))
        self.assertEqual(done['errors'][0]['error'], 'RuntimeError')
        self.assertTrue(OEMApplyHalt.objects.filter(cleared_at__isnull=True, run_id=run['id']).exists())
        part = self.part('54840-2H000')
        self.assertEqual((part.is_OEM, part.codes.count(), self.case('54840-2H000').status), (False, 0, 'review'))

    def test_bulk_conflicts_and_claimed_oems_become_conflict_cases(self):
        other = Part.objects.create(sku='OTRO-2', description='BASE AMORT TOY HILUX')
        PartCode.objects.create(part=other, code='486540K040MANDO', brand='ACME', ref_type='company')  # the old SKU, once normalized
        claimant = Part.objects.create(sku='31110 07600', description='BOMBA GAS KIA PICANTO')  # a new SKU spelling the OEM
        preview = self.preview(tier='PROBABLE_BASE')
        self.assertEqual({r['sku']: r['outcome'] for r in preview['sample']}, {'48654-0K040-MANDO': 'conflict', '31110-07600-MOBIS': 'conflict'})
        run = self.start(preview['selection'], tier='PROBABLE_BASE').data['run']
        done = self.step(run['id'])['run']['applied']
        self.assertEqual((done['applied'], done['conflicts'], done['skipped']), (0, 2, {}))
        self.assertEqual((self.case('48654-0K040-MANDO').tier, self.case('31110-07600-MOBIS').tier), ('CONFLICT', 'CONFLICT'))
        self.assertEqual(self.case('31110-07600-MOBIS').evidence['apply_conflict']['owners'], [str(claimant.pk)])
        self.assertEqual(OEMReviewDecision.objects.filter(action='apply_conflict', run_id=run['id']).count(), 2)
        self.assertEqual(self.part('31110-07600-MOBIS').sku, '31110-07600-MOBIS')

    def test_two_bulk_starts_never_run_together(self):
        preview = self.preview(tier='CURRENT_LIKELY_OEM')
        with patch.object(rv, 'LOCK_WAIT', 0), patch('mall.oem_finder.matching_lock') as lock:
            lock.return_value.__enter__.return_value = False
            self.assertEqual(self.start(preview['selection'], tier='CURRENT_LIKELY_OEM').status_code, 409)
        self.assertFalse(OEMFinderRun.objects.filter(mode='apply_auto').exists())


class RunListTests(Review, APITestCase):
    def test_stage_filter(self):
        run = self.client.post(f'{URL}{self.case("48654-12030").pk}/', {'action': 'approve', 'fingerprint': self.case('48654-12030').fingerprint},
                               format='json').data['run']
        self.assertEqual([r['id'] for r in self.client.get(RUNS, {'stage': 'review', 'mode': 'apply_auto'}).data['results']], [run])
        self.assertEqual([r['id'] for r in self.client.get(RUNS, {'stage': 'auto'}).data['results']], [self.dry.pk])
        self.assertEqual(self.client.get(RUNS).data['count'], 2)
        self.assertEqual(self.client.get(RUNS, {'stage': 'otra'}).status_code, 400)


@skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL row and advisory locks.')
class ConcurrencyTests(Review, TransactionTestCase):
    client_class = APIClient

    def test_a_concurrent_catalog_edit_wins_and_the_approval_is_refused(self):
        """The approval waits on the Part lock (Account -> Part), re-checks the case snapshot after the edit commits and writes nothing."""
        locked, release, results = threading.Event(), threading.Event(), []
        case = self.case('48654-12030')

        def edit():
            from django.db import connection as thread_connection, transaction
            try:
                with transaction.atomic():
                    part = Part.objects.select_for_update().get(pk=self.parts['48654-12030'].pk)
                    part.description = 'BASE AMORT TOY COROLLA EDITADO'
                    part.save(update_fields=['description'])
                    locked.set()
                    release.wait(10)
            finally:
                thread_connection.close()

        def approve():
            from django.db import connection as thread_connection
            try:
                rv.approve(case.pk, case.fingerprint, self.root)
            except rv.CaseConflict as error:
                results.append(error.extra.get('reason'))
            finally:
                thread_connection.close()
        editor = threading.Thread(target=edit)
        editor.start()
        locked.wait(10)
        approver = threading.Thread(target=approve)
        approver.start()
        waiting = False
        for _ in range(100):
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM pg_locks WHERE NOT granted AND locktype IN ('transactionid', 'tuple')")
                waiting = cursor.fetchone()[0] > 0
            if waiting:
                break
            time.sleep(0.05)
        release.set()
        editor.join()
        approver.join()
        self.assertTrue(waiting, 'the approval should wait for the catalog edit lock')
        self.assertEqual(results, ['snapshot_changed'])
        part = self.part('48654-12030')
        self.assertEqual((part.is_OEM, part.codes.count(), self.case('48654-12030').status), (False, 0, 'review'))
        self.assertFalse(OEMFinderRun.objects.filter(mode='apply_auto').exists())

    def test_approvals_wait_for_a_matching_pass(self):
        held, release = threading.Event(), threading.Event()

        def hold():
            from django.db import connection as thread_connection
            try:
                with thread_connection.cursor() as cursor:
                    cursor.execute('SELECT pg_advisory_lock(%s)', [MATCHING_LOCK])
                    held.set()
                    release.wait(10)
                    cursor.execute('SELECT pg_advisory_unlock(%s)', [MATCHING_LOCK])
            finally:
                thread_connection.close()
        holder = threading.Thread(target=hold)
        holder.start()
        held.wait(10)
        try:
            with patch.object(rv, 'LOCK_WAIT', 0.3):
                self.assertEqual(self.decide('48654-12030', 'approve').status_code, 409)
            threading.Timer(0.5, release.set).start()
            with patch.object(rv, 'LOCK_WAIT', 5):
                self.assertEqual(self.decide('48654-12030', 'approve').status_code, 200)  # waited for the pass to finish
        finally:
            release.set()
            holder.join()
        self.assertTrue(self.part('48654-12030').is_OEM)
        self.assertEqual(CatalogIdentityChange.objects.filter(part=self.parts['48654-12030']).count(), 1)
