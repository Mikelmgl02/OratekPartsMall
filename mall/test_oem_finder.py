import io
import json
import threading
from datetime import timedelta
from unittest import skipUnless
from unittest.mock import patch

from django.db import connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from . import catalog_suffixes as cs
from . import oem_finder as of
from .catalog_suffix_models import CatalogCodeSuffix
from .catalog_suffixes import SuffixTable, fixture_rows
from .matching_queue import MATCHING_LOCK, matching_lock
from .matching_worker import process_queue
from .models import Account, CatalogImportJob, Part, PartCode, SupplierItem, User
from .oem_finder_models import OEMFinderRun, OEMReviewCase

# Representative rows from the prototype samples, plus the class members the lexicon needs (>= 5 other keys = strong).
CATALOG = [
    ('48654-30030', 'BASE AMORT TOY COROLLA'), ('48654-0K010', 'BASE AMORT TOY HILUX'), ('48654-12010', 'BASE AMORT TOY COROLLA'),
    ('48654-60010', 'BASE AMORT TOY PRADO'), ('48654-42010', 'BASE AMORT TOY RAV4'), ('48654-35010', 'BASE AMORT TOY 4RUNNER'),
    ('48654-12030', 'BASE AMORT TOY COROLLA COMPLETO'),         # CURRENT_REVIEW: variant wording
    ('48654-0K030-G', 'BASE AMORT TOY HILUX'),                   # AUTO_RENAME_BASE: confirmed genuine tag = second unit
    ('48654-0K040-MANDO', 'BASE AMORT TOY HILUX'),               # PROBABLE_BASE: no second unit
    ('48654-60020', 'BASE AMORT TOY PRADO LH'),                  # CURRENT_REVIEW: sibling of the opposite side
    ('48654-60020-MANDO', 'BASE AMORT TOY PRADO RH'),            # CONFLICT: base is another active SKU, incompatible side
    ('48654-0K050-MANDO', 'BASE AMORT TOY HILUX'), ('48654-0K050-NAK', 'BASE AMORT TOY HILUX'),  # CONFLICT: shared family
    ('48654-60030-MANDO', 'TOPE AMORT TOY PRADO'),               # WEAK: the class lexicon disagrees (BASE vs TOPE)
    ('11701-11010-050', 'CASQ BANCADA TOY 3E 5E'),               # VARIANT_REVIEW: size suffix
    ('43211-29026-JR', 'MUÑEQUILLA TOY HIACE'),                  # UNKNOWN_SUFFIX
    ('11210-30110 11210-0L020', 'TAPA VALV TOY HILUX'),          # MULTI_OEM: two OEM segments of one class
    ('39180-22050/60', 'SENSOR CIG HYU ACCENT'),                 # MULTI_OEM: slash alternate
    ('54840-1R000', 'TERM ESTAB HYU ACCENT'), ('54840-3X000', 'TERM ESTAB HYU ACCENT'), ('54840-2H000', 'TERM ESTAB HYU ELANTRA'),
    ('CEKH-52R-CTR 54840-2T000', 'TERM ESTAB HYU OPTIMA'),       # LOCAL_XREF
    ('333512', 'AMORT KIA RIO DEL LH'),                          # NEEDS_AI (KYB code)
    ('TF-100CM', 'TUBERIA FRENO 40 PULGADA 100CM'),              # NO_MAKE
    ('7PK1345', 'CORREA MOTOR 7PK'),                             # STANDARD_CODE
    ('C-LINK', 'CAMBIO DE LINK'),                                # SKIP_NO_OEM
    ('45201-74L20-ANULADO', 'TIJERA SUZ SWIFT'),                 # HOLD_LIFECYCLE
    ('94535484-K', 'SELLO VALV CHEV AVEO'),                      # WEAK: GM number, no class support
    ('MR-968365', 'BASE MIT MONTERO'),                           # CURRENT_LIKELY_OEM, canonical MR968365
    ('31110-07600-MOBIS', 'BOMBA GAS KIA PICANTO'),              # brand KIA from the description
]
EXPECTED = {
    '48654-30030': 'AUTO_FLAG_CURRENT', '48654-12030': 'CURRENT_REVIEW', '48654-0K030-G': 'AUTO_RENAME_BASE',
    '48654-0K040-MANDO': 'PROBABLE_BASE', '48654-60020': 'CURRENT_REVIEW', '48654-60020-MANDO': 'CONFLICT',
    '48654-0K050-MANDO': 'CONFLICT', '48654-0K050-NAK': 'CONFLICT', '48654-60030-MANDO': 'WEAK', '11701-11010-050': 'VARIANT_REVIEW',
    '43211-29026-JR': 'UNKNOWN_SUFFIX', '11210-30110 11210-0L020': 'MULTI_OEM', '39180-22050/60': 'MULTI_OEM',
    '54840-1R000': 'CURRENT_LIKELY_OEM', 'CEKH-52R-CTR 54840-2T000': 'LOCAL_XREF', '333512': 'NEEDS_AI', 'TF-100CM': 'NO_MAKE',
    '7PK1345': 'STANDARD_CODE', 'C-LINK': 'SKIP_NO_OEM', '45201-74L20-ANULADO': 'HOLD_LIFECYCLE', '94535484-K': 'WEAK',
    'MR-968365': 'CURRENT_LIKELY_OEM', '31110-07600-MOBIS': 'PROBABLE_BASE',
}


def part_row(sku, description, **extra):
    return {'id': sku, 'sku': sku, 'name': sku, 'description': description, 'category': '', 'subcategory': '', 'active': True,
            'merged_into_id': None, 'is_OEM': False, **extra}


def baseline_table():
    """The prototype's table: the 44 owner confirmations of 2026-10-06 not given yet."""
    return SuffixTable([dict(r, status=r['seed_status'], owner_confirmed=False) if r.get('seed_status') else r for r in fixture_rows()],
                       source='baseline')


def evaluate(rows=CATALOG, table=None, codes=(), items=()):
    finder = of.OEMFinder(of.Snapshot([part_row(s, d) for s, d in rows], codes, items), table or SuffixTable.from_fixture())
    return finder, finder.evaluate()


class TierTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.finder, cls.res = evaluate()

    def test_every_tier_of_the_representative_rows(self):
        self.assertEqual({sku: self.res[sku]['tier'] for sku in EXPECTED}, EXPECTED)

    def test_auto_flag_current_passes_every_gate_with_a_strong_lexicon(self):
        r = self.res['48654-30030']
        self.assertEqual((r['auto_blockers'], r['proposed_main'], r['proposed_brand']), ([], '48654-30030', 'TOYOTA'))
        self.assertEqual((r['primary']['system'], r['primary']['grade'], r['primary']['lexicon']['result']), ('TOYOTA', 3, 'agree_strong'))
        self.assertEqual(self.res['48654-12030']['auto_blockers'], ['desc_variant:COMPLETO'])

    def test_base_plus_tag_renames_only_with_a_second_unit(self):
        auto, probable = self.res['48654-0K030-G'], self.res['48654-0K040-MANDO']
        self.assertEqual((auto['proposed_main'], auto['written_form'], auto['auto_blockers']), ('48654-0K030', '48654-0K030', []))
        self.assertTrue(auto['primary']['genuine_confirmed'] and auto['primary']['second_unit'])
        self.assertEqual([c['tok'] for c in auto['suffix_chain']], ['G'])
        self.assertEqual(probable['auto_blockers'], ['no_second_unit'])

    def test_pending_owner_tags_become_a_rename_once_confirmed(self):
        _, res = evaluate(table=baseline_table())
        r = res['48654-0K030-G']
        self.assertEqual((r['tier'], r['pending_owner_tags'], r['tier_if_owner_confirms']),
                         ('STRONG_PENDING_OWNER_TAGS', ['G'], 'AUTO_RENAME_BASE'))
        self.assertIn('unconfirmed_tag:G', r['auto_blockers'])
        self.assertEqual(res['48654-0K040-MANDO']['tier'], 'PROBABLE_BASE')
        self.assertEqual(self.res['48654-0K030-G']['tier_if_owner_confirms'], 'AUTO_RENAME_BASE')

    def test_conflicts_route_to_merge_review_and_flag_incompatible_families(self):
        r = self.res['48654-60020-MANDO']
        self.assertEqual((r['underlying_tier'], r['conflict_kind'], r['family_incompatible']),
                         ('BASE_CLAIM', 'base_is_existing_active_part', ['48654-60020']))
        shared = self.res['48654-0K050-MANDO']
        self.assertEqual((shared['conflict_kind'], shared['shared_with'], shared['family_incompatible']),
                         ('shared_base_family', ['48654-0K050-NAK'], []))
        current = self.res['48654-60020']
        self.assertEqual((current['auto_blockers'], current['siblings_to_merge_into_this']),
                         (['sibling_family_inconsistent'], ['48654-60020-MANDO']))
        self.assertEqual(self.res['48654-60030-MANDO']['primary']['contradictions'], ['lexicon_disagree'])

    def test_variant_unknown_multi_and_local_cross_reference(self):
        self.assertEqual(self.res['11701-11010-050']['suffix_chain'][0]['kind'], 'size')
        self.assertEqual(self.res['43211-29026-JR']['blocking_unknowns'], ['JR'])
        self.assertEqual(self.res['11210-30110 11210-0L020']['other_oem_candidates'], ['112100L020'])
        slash = self.res['39180-22050/60']
        self.assertEqual((slash['other_oem_candidates'], slash['proposed_brand']), (['3918022060'], 'HYUNDAI'))
        xref = self.res['CEKH-52R-CTR 54840-2T000']
        self.assertEqual((xref['proposed_main'], xref['primary']['lexicon']['result']), ('54840-2T000', 'agree'))
        self.assertEqual(xref['secondary_company_refs'][0]['brand'], 'CTR')

    def test_rows_without_a_candidate(self):
        self.assertEqual(self.res['333512']['aftermarket_family'], ['KYB'])
        self.assertEqual(self.res['7PK1345']['standard_group'], 'belt_size_coded')
        self.assertEqual(self.res['C-LINK']['skip_group'], 'services_labor')
        self.assertNotIn('primary', self.res['TF-100CM'])
        self.assertNotIn('primary', self.res['45201-74L20-ANULADO'])
        self.assertTrue(all(r['reasons'] for r in self.res.values()))

    def test_retired_merged_and_oem_parts_are_owners_but_not_evaluated(self):
        rows = [part_row(s, d) for s, d in CATALOG] + [part_row('48654-99010', 'BASE AMORT TOY PRADO', is_OEM=True),
                                                       part_row('48654-0K040', 'BASE AMORT TOY HILUX', active=False)]
        finder = of.OEMFinder(of.Snapshot(rows), SuffixTable.from_fixture())
        res = finder.evaluate()
        self.assertNotIn('48654-99010', res)
        self.assertNotIn('48654-0K040', res)
        r = res['48654-0K040-MANDO']
        self.assertEqual((r['tier'], r['conflict_kind']), ('CONFLICT', 'base_is_retired_part_sku'))

    def test_brand_and_written_form_for_review_candidates(self):
        mit = self.res['MR-968365']
        self.assertEqual((mit['proposed_main'], mit['written_form'], mit['proposed_brand']), ('MR968365', 'MR-968365', 'MITSUBISHI'))
        self.assertEqual(self.res['31110-07600-MOBIS']['proposed_brand'], 'KIA')

    def test_report_selection_and_scenario(self):
        counts = of.report(self.finder, self.res, list(self.res))
        self.assertEqual(sum(v[0] for v in counts['tiers'].values()), len(CATALOG))
        self.assertEqual(counts['scenario'], {})
        self.assertEqual(counts['tiers']['AUTO_FLAG_CURRENT'], [6, 0])
        self.assertEqual(of.select(self.finder, self.res, tier='CONFLICT', limit=2), ['48654-0K050-MANDO', '48654-0K050-NAK'])
        _, base = evaluate(table=baseline_table())
        self.assertEqual(len([r for r in base.values() if r['tier'] == 'STRONG_PENDING_OWNER_TAGS']), 1)

    def test_in_stock_first_orders_the_selection(self):
        items = [{'supplier_id': 's1', 'part_id': '333512', 'codigo': '333512', 'brand': 'KYB', 'references': [],
                  'reported_quantity': 4, 'reserved_quantity': 1}]
        finder, res = evaluate(items=items)
        self.assertEqual(finder.avail['333512'], 3)
        self.assertEqual(of.select(finder, res, in_stock_first=True)[0], '333512')
        self.assertNotEqual(of.select(finder, res)[0], '333512')


class CanonicalFormTests(SimpleTestCase):
    table = SuffixTable.from_fixture()

    def main_form(self, code, system):
        hit = next(h for h in of.system_hits(code, self.table) if h['system'] == system)
        return of.manufacturer_form(hit['system'], hit['canonical'], hit['written'], hit['flags']), hit

    def test_manufacturer_form_per_make(self):
        for code, system, expected in [
                ('MR-968365', 'MITSU_OLD', 'MR968365'), ('4670A600', 'MITSU_NEW', '4670A600'),
                ('6L8Z-3078-AA', 'FORD', '6L8Z-3078-AA'), ('6L8Z-30-78AA', 'FORD', '6L8Z-3078-AA'), ('GB5Z-6038A-UM', 'FORD', 'GB5Z-6038-A'),
                ('OK2N1-33-28Z', 'KIA_LEGACY', '0K2N1-33-28Z'), ('0K2N1-33-28Z', 'KIA_LEGACY', '0K2N1-33-28Z'),
                ('KK150-13-Z00', 'KIA_LEGACY', 'KK150-13-Z00'), ('1K2N1-33-28Z', 'KIA_LEGACY', '1K2N1-33-28Z'),
                ('48654-30030', 'TOYOTA', '48654-30030'), ('4865430030', 'TOYOTA', '48654-30030'), ('48654-3OO3O', 'TOYOTA', '48654-30030'),
                ('52320-S5A-013', 'HONDA', '52320-S5A-013'), ('52320S5A013', 'HONDA', '52320-S5A-013'),
                ('B25F-61-480', 'MAZDA', 'B25F-61-480'), ('8-98036-321-0', 'ISUZU', '8-98036-321-0'), ('1K0.615.301', 'VAG', '1K0615301'),
                ('13780-65J00', 'SUZUKI', '13780-65J00'), ('23343-8H50A', 'NISSAN', '23343-8H50A')]:
            with self.subTest(code=code):
                self.assertEqual(self.main_form(code, system)[0], expected)

    def test_kia_legacy_prefixes_keep_their_own_key(self):
        _, res = evaluate([('KK150-13-Z00', 'FILTRO AIRE KIA PRIDE'), ('KK150-13-Z00_', 'FILTRO AIRE KIA PRIDE'),
                           ('0K150-13-Z00', 'FILTRO AIRE KIA PRIDE 98-')])
        kk = res['KK150-13-Z00']  # the duplicate 'KK150-13-Z00_' owns the same key; 0K150-13-Z00 is another number
        self.assertEqual((kk['tier'], kk['proposed_main'], kk['primary']['key'], kk['conflict_kind']),
                         ('CONFLICT', 'KK150-13-Z00', 'KK15013Z00', 'base_is_existing_active_part'))
        self.assertEqual(res['0K150-13-Z00']['primary']['key'], '0K15013Z00')

    def test_format_guards(self):
        self.assertEqual(self.main_form('6L8Z-30-78AA', 'FORD')[1]['flags'], ['ford_typed_as_mazda'])
        self.assertEqual(self.main_form('GB5Z-6038A-UM', 'FORD')[1]['rest'], '-UM')
        self.assertEqual(self.main_form('MB-699175XX-JAPA', 'MITSU_OLD')[1]['rest'], 'XX-JAPA')
        self.assertFalse([h for h in of.system_hits('MA-150610', self.table) if h['system'] == 'MITSU_OLD'])
        self.assertIn('no_separator', self.main_form('4865430030', 'TOYOTA')[1]['flags'])


def create_catalog(rows=CATALOG):
    return {sku: Part.objects.create(sku=sku, name=sku, description=description) for sku, description in rows}


class Fresh:
    def setUp(self):
        cs.invalidate()
        super().setUp()

    def tearDown(self):
        cs.invalidate()
        super().tearDown()


class ReviewCaseTests(Fresh, TestCase):
    def setUp(self):
        super().setUp()
        self.parts = create_catalog()

    def run_finder(self, **kwargs):
        return of.execute(**kwargs)

    def case(self, sku):
        return OEMReviewCase.objects.get(part=self.parts[sku])

    def test_a_dry_run_records_the_run_and_queues_only_review_tiers(self):
        codes = PartCode.objects.count()
        outcome = self.run_finder()
        run = outcome['run']
        self.assertEqual((run.mode, run.status, run.rules_version), ('dry_run', 'completed', 'oem-finder-2'))
        self.assertTrue(run.suffix_table_version.startswith('db:'))
        self.assertEqual(run.counts['tiers']['AUTO_FLAG_CURRENT'], [6, 0])
        self.assertEqual(run.scope, {'in_stock_first': False, 'limit': None, 'tier': None, 'stage': 'command'})
        tiers = {sku: outcome['results'][str(p.pk)]['tier'] for sku, p in self.parts.items()}
        queued = {c.part.sku for c in OEMReviewCase.objects.select_related('part')}
        self.assertEqual(queued, {sku for sku, t in tiers.items() if t in of.REVIEW_TIERS})
        self.assertEqual(run.counts['cases']['created'], len(queued))
        # never a PartCode (reconcile_identities would promote a verified OEM one) and never a rename
        self.assertEqual(PartCode.objects.count(), codes)
        self.assertFalse(Part.objects.filter(is_OEM=True).exists())
        self.assertEqual(sorted(Part.objects.values_list('sku', flat=True)), sorted(s for s, _ in CATALOG))
        case = self.case('48654-0K040-MANDO')
        self.assertEqual((case.tier, case.candidate, case.written_form, case.brand, case.system, case.chain, case.status),
                         ('PROBABLE_BASE', '48654-0K040', '48654-0K040', 'TOYOTA', 'TOYOTA', 'MANDO', 'review'))
        self.assertEqual((case.blockers, case.snapshot['sku'], case.run_id), (['no_second_unit'], '48654-0K040-MANDO', run.pk))
        self.assertEqual(case.evidence['primary']['lexicon']['result'], 'agree_strong')
        conflict = self.case('48654-60020-MANDO')
        self.assertEqual(conflict.blockers, ['conflict:base_is_existing_active_part', 'family_incompatible'])
        self.assertEqual(self.case('MR-968365').candidate, 'MR968365')

    def test_re_running_is_idempotent_and_keeps_decisions(self):
        first = self.run_finder()['counts']['cases']
        before = dict(OEMReviewCase.objects.values_list('part_id', 'fingerprint'))
        OEMReviewCase.objects.filter(part=self.parts['333512']).update(status='dismissed')
        second = self.run_finder()['counts']['cases']
        self.assertEqual(second, {'created': 0, 'updated': 0, 'unchanged': first['created'], 'stock_changed': 0, 'resolved': 0, 'skipped': 0})
        self.assertEqual(dict(OEMReviewCase.objects.values_list('part_id', 'fingerprint')), before)
        self.assertEqual(self.case('333512').status, 'dismissed')

    def test_changed_evidence_reopens_and_departed_parts_resolve(self):
        self.run_finder()
        OEMReviewCase.objects.filter(part=self.parts['43211-29026-JR']).update(status='dismissed', decision={'action': 'dismiss'})
        Part.objects.filter(pk=self.parts['43211-29026-JR'].pk).update(description='MUÑEQUILLA TOY HIACE 05-')
        Part.objects.filter(pk=self.parts['333512'].pk).update(is_OEM=True)
        stats = self.run_finder()['counts']['cases']
        self.assertEqual((stats['updated'], stats['resolved']), (1, 1))
        reopened = self.case('43211-29026-JR')
        self.assertEqual((reopened.status, reopened.decision, reopened.snapshot['description']), ('review', {}, 'MUÑEQUILLA TOY HIACE 05-'))
        self.assertEqual(self.case('333512').status, 'resolved')
        Part.objects.filter(pk=self.parts['333512'].pk).update(is_OEM=False)
        self.assertEqual(self.run_finder()['counts']['cases']['updated'], 1)
        self.assertEqual(self.case('333512').status, 'review')

    def test_a_scoped_run_only_touches_its_selection(self):
        self.run_finder()
        Part.objects.filter(pk=self.parts['333512'].pk).update(is_OEM=True)
        outcome = self.run_finder(tier='CONFLICT', limit=2)
        self.assertEqual(outcome['counts']['scope'], {'CONFLICT': 2})
        self.assertEqual(outcome['counts']['cases'], {'created': 0, 'updated': 0, 'unchanged': 2, 'stock_changed': 0, 'resolved': 0, 'skipped': 0})
        self.assertEqual(self.case('333512').status, 'review')
        self.assertEqual(self.run_finder()['counts']['cases']['resolved'], 1)

    def test_suffix_labels_retier_only_the_parts_that_read_them(self):
        self.run_finder()
        before = dict(OEMReviewCase.objects.values_list('part_id', 'fingerprint'))
        row = CatalogCodeSuffix.objects.get(token='U')
        row.notes = 'REVISAR'
        row.save()
        outcome = self.run_finder()
        self.assertNotEqual(outcome['run'].suffix_table_version, OEMFinderRun.objects.order_by('id').first().suffix_table_version)
        self.assertEqual(outcome['counts']['cases']['updated'], 0)
        jr = CatalogCodeSuffix.objects.get(token='JR')
        jr.cls, jr.tag_kind, jr.status, jr.owner_confirmed, jr.confidence = 'TAG', 'brand', 'owner_confirmed', True, 'medium'
        jr.save()
        outcome = self.run_finder()
        self.assertEqual(outcome['counts']['cases']['updated'], 1)
        case = self.case('43211-29026-JR')
        self.assertEqual((case.tier, case.chain, case.blockers), ('WEAK', 'JR', []))  # unblocked: base F2 (no class data)
        self.assertEqual({k: v for k, v in OEMReviewCase.objects.values_list('part_id', 'fingerprint') if k != case.part_id},
                         {k: v for k, v in before.items() if k != case.part_id})

    def test_stock_changes_update_the_flag_without_reopening(self):
        self.run_finder()
        OEMReviewCase.objects.filter(part=self.parts['333512']).update(status='dismissed')
        supplier = Account.objects.create(name='Proveedor')
        SupplierItem.objects.create(supplier=supplier, supplier_invent_id='1', part=self.parts['333512'], codigo='333512', brand='KYB',
                                    source='upload', reported_quantity=3)
        stats = self.run_finder(in_stock_first=True)['counts']['cases']
        self.assertEqual((stats['stock_changed'], stats['updated']), (1, 0))
        case = self.case('333512')
        self.assertEqual((case.in_stock, case.status), (True, 'dismissed'))

    def test_read_only_writes_nothing(self):
        outcome = self.run_finder(store=False)
        self.assertIsNone(outcome['run'])
        self.assertNotIn('cases', outcome['counts'])
        self.assertFalse(OEMFinderRun.objects.exists() or OEMReviewCase.objects.exists())

    def test_a_failed_run_is_recorded(self):
        with patch.object(of, 'load_snapshot', side_effect=RuntimeError('boom')), self.assertRaises(RuntimeError):
            self.run_finder()
        run = OEMFinderRun.objects.get()
        self.assertEqual((run.status, run.errors), ('failed', [{'error': 'RuntimeError', 'detail': 'boom'}]))
        self.assertFalse(OEMReviewCase.objects.exists())

    def test_only_the_dry_run_mode_exists(self):
        with self.assertRaises(ValueError):
            self.run_finder(mode='apply_auto')


class CommandTests(Fresh, TestCase):
    def setUp(self):
        super().setUp()
        create_catalog()

    def call(self, *argv):
        out = io.StringIO()
        return of.main(list(argv), stdout=out), out.getvalue()

    def test_dry_run_command(self):
        code, out = self.call('--dry-run', '--in-stock-first')
        self.assertEqual(code, 0)
        self.assertIn('Buscador OEM oem-finder-2', out)
        self.assertIn('Casos de revisión:', out)
        self.assertEqual(OEMFinderRun.objects.get().scope['in_stock_first'], True)

    def test_read_only_json_scope(self):
        with patch('sys.stderr', io.StringIO()) as log:
            code, out = self.call('--dry-run', '--read-only', '--tier', 'CONFLICT', '--json', '-')
        self.assertEqual(code, 0)
        self.assertIn('Solo lectura', log.getvalue())
        doc = json.loads(out)
        self.assertEqual({r['tier'] for r in doc['results']}, {'CONFLICT'})
        self.assertEqual(len(doc['results']), 3)
        self.assertEqual(doc['summary']['tiers']['AUTO_RENAME_BASE'], [1, 0])
        self.assertFalse(OEMFinderRun.objects.exists())

    def test_before_migrating_only_read_only_runs(self):
        with patch.object(of, 'tables_ready', return_value=False):
            code, out = self.call('--dry-run')
            self.assertEqual((code, OEMFinderRun.objects.exists()), (1, False))
            self.assertIn('aún no están migradas', out)
            self.assertEqual(self.call('--dry-run', '--read-only')[0], 0)
            with self.assertLogs('mall.oem_finder', 'WARNING'):
                self.assertEqual(of.worker_stage(None), {'status': 'not_migrated'})
        self.assertFalse(OEMFinderRun.objects.exists() or OEMReviewCase.objects.exists())

    def test_the_command_waits_for_a_catalog_import(self):
        owner = User.objects.create_user('importa', password='x')
        CatalogImportJob.objects.create(owner=owner, filename='CATALOGO.xlsx', status='importing',
                                        expires_at=timezone.now() + timedelta(hours=1))
        code, out = self.call('--dry-run')
        self.assertEqual((code, OEMFinderRun.objects.exists()), (2, False))
        self.assertIn('importación del catálogo en curso', out)
        self.assertEqual(self.call('--dry-run', '--read-only')[0], 0)

    def test_the_command_requires_dry_run(self):
        with patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
            self.call('--limit', '5')


class WorkerStageTests(Fresh, TestCase):
    def setUp(self):
        super().setUp()
        create_catalog(CATALOG[:8])

    def test_the_stage_is_off_by_default(self):
        summary = process_queue(force=True, use_ai=False)
        self.assertNotIn('oem_finder', summary)
        self.assertFalse(OEMFinderRun.objects.exists())

    @override_settings(OEM_FINDER_AUTO_STAGE=True)
    def test_the_stage_runs_a_dry_run_inside_the_pass(self):
        summary = process_queue(force=True, use_ai=False)
        run = OEMFinderRun.objects.get()
        self.assertEqual(summary['oem_finder'], {'status': 'completed', 'run': run.pk, 'cases': run.counts['cases']})
        self.assertEqual((run.scope['stage'], run.actor.username), ('worker', 'motionpartes-matching-service'))

    @override_settings(OEM_FINDER_AUTO_STAGE=True)
    def test_a_failing_stage_does_not_fail_the_pass(self):
        with patch.object(of, 'load_snapshot', side_effect=RuntimeError('boom')), self.assertLogs('mall.oem_finder', 'ERROR'):
            summary = process_queue(force=True, use_ai=False)
        self.assertEqual(summary['oem_finder'], {'status': 'failed'})
        self.assertEqual(OEMFinderRun.objects.get().status, 'failed')


class LockTests(SimpleTestCase):
    def test_the_lock_is_a_no_op_off_postgresql(self):
        if connection.vendor == 'postgresql':
            self.skipTest('PostgreSQL takes the real lock.')
        with matching_lock() as acquired:
            self.assertTrue(acquired)


@skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL advisory locks.')
class AdvisoryLockTests(Fresh, TransactionTestCase):
    def hold_lock_elsewhere(self):
        """Another session holds the matching lock (as a running matching pass does) until released."""
        from django.db import connection as thread_connection
        held, release = threading.Event(), threading.Event()

        def hold():
            try:
                with thread_connection.cursor() as cursor:
                    cursor.execute('SELECT pg_advisory_lock(%s)', [MATCHING_LOCK])
                    held.set()
                    release.wait(10)
                    cursor.execute('SELECT pg_advisory_unlock(%s)', [MATCHING_LOCK])
            finally:
                thread_connection.close()
        thread = threading.Thread(target=hold)
        thread.start()
        held.wait(10)
        return thread, release

    def test_the_finder_never_overlaps_a_matching_pass(self):
        create_catalog(CATALOG[:3])
        thread, release = self.hold_lock_elsewhere()
        try:
            out = io.StringIO()
            self.assertEqual(of.main(['--dry-run'], stdout=out), 2)
            self.assertIn('búsqueda OEM en curso', out.getvalue())
            self.assertFalse(OEMFinderRun.objects.exists())
        finally:
            release.set()
            thread.join()
        self.assertEqual(of.main(['--dry-run'], stdout=io.StringIO()), 0)
        self.assertEqual(OEMFinderRun.objects.count(), 1)

    def test_a_matching_pass_waits_for_the_finder(self):
        results = []

        def matching_pass():
            from django.db import connection as thread_connection
            try:
                results.append(process_queue(force=True, use_ai=False))
            finally:
                thread_connection.close()
        with of.matching_lock() as acquired:
            self.assertTrue(acquired)
            thread = threading.Thread(target=matching_pass)
            thread.start()
            thread.join()
        self.assertEqual(results, [None])
