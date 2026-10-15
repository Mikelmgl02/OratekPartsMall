import threading
from unittest import skipUnless

from django.db import connection
from django.test import SimpleTestCase, TransactionTestCase

from . import oem_finder as of
from .catalog_suffixes import SuffixTable, fixture_rows
from .matching_queue import matching_lock
from .matching_worker import process_queue
from .test_factories import Fresh, make_catalog  # Fresh: the OEM modules import it from here

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


class LearningPopulationTests(SimpleTestCase):
    """oem-finder-3: an OEM-flagged SKU is graded no more but still teaches every learned index (owner decision)."""
    BASES = [('48654-30030', 'BASE AMORT TOY COROLLA'), ('48654-0K010', 'BASE AMORT TOY HILUX'), ('48654-12010', 'BASE AMORT TOY COROLLA'),
             ('48654-60010', 'BASE AMORT TOY PRADO'), ('48654-42010', 'BASE AMORT TOY RAV4'), ('48654-35010', 'BASE AMORT TOY 4RUNNER')]
    table = SuffixTable.from_fixture()

    def grade(self, rows, oem=frozenset()):
        finder = of.OEMFinder(of.Snapshot([part_row(s, d, is_OEM=s in oem) for s, d in rows]), self.table)
        return finder, finder.evaluate()

    def test_the_lexicon_counts_oem_flagged_skus_and_leaves_the_own_key_out(self):
        oem = {'48654-0K010', '48654-12010', '48654-60010', '48654-42010'}
        finder, res = self.grade(self.BASES + [('48654-0K010-G', 'BASE AMORT TOY HILUX')], oem)  # -G: the key of an OEM-flagged SKU
        self.assertEqual((set(res), len(finder.learn)), ({'48654-30030', '48654-35010', '48654-0K010-G'}, 7))
        self.assertEqual(sorted(k for k, _, _ in finder.lex[('TOYOTA', '48654')]), sorted(of.norm_key(s) for s, _ in self.BASES))
        self.assertEqual((len(finder.lex), sum(len(keys) for keys in finder.lex.values())), (1, 6))
        current = res['48654-30030']
        self.assertEqual((current['tier'], current['primary']['lexicon']['result'], current['primary']['lexicon']['n_other_keys']),
                         ('AUTO_FLAG_CURRENT', 'agree_strong', 5))
        twin = res['48654-0K010-G']['primary']  # two SKUs carry its key: neither counts for it
        self.assertEqual((twin['lexicon']['n_other_keys'], twin['n_siblings'], twin['siblings'][0]['sku']), (5, 1, '48654-0K010'))
        self.assertEqual((res['48654-0K010-G']['tier'], res['48654-0K010-G']['conflict_kind']), ('CONFLICT', 'base_is_existing_active_part'))

    def test_flagging_skus_as_oem_never_moves_the_other_rows(self):
        def public(rows, oem):
            return {pid: {k: v for k, v in r.items() if not k.startswith('_')} for pid, r in self.grade(rows, oem)[1].items()}
        before = public(CATALOG, set())
        flagged = {pid for pid, r in before.items() if r['tier'] == 'AUTO_FLAG_CURRENT'}
        self.assertEqual(len(flagged), 6)
        self.assertEqual(public(CATALOG, flagged), {pid: r for pid, r in before.items() if pid not in flagged})

    def test_an_oem_flagged_sku_still_claims_its_base(self):
        rows = [('48654-0K050-MANDO', 'BASE AMORT TOY HILUX'), ('48654-0K050-NAK', 'BASE AMORT TOY HILUX')] + self.BASES
        for oem in (set(), {'48654-0K050-NAK'}):
            with self.subTest(oem=oem):
                r = self.grade(rows, oem)[1]['48654-0K050-MANDO']
                self.assertEqual((r['tier'], r['conflict_kind'], r['shared_with']), ('CONFLICT', 'shared_base_family', ['48654-0K050-NAK']))


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
    return make_catalog(rows)


class LockTests(SimpleTestCase):
    def test_the_lock_is_a_no_op_off_postgresql(self):
        if connection.vendor == 'postgresql':
            self.skipTest('PostgreSQL takes the real lock.')
        with matching_lock() as acquired:
            self.assertTrue(acquired)


@skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL advisory locks.')
class AdvisoryLockTests(Fresh, TransactionTestCase):
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
