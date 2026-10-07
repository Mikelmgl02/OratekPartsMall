import io
import json
import threading
import time
from unittest import skipUnless
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, TransactionTestCase
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from . import oem_apply as oa
from . import oem_finder as of
from . import oem_finder_revert
from .catalog_grouping import merge_catalog_parts
from .catalog_identity import prefer_oem, reconcile_identities
from .catalog_suffixes import suffix_table
from .matching_engine import MatchIndex
from .models import Account, CatalogIdentityChange, Part, PartCode, PartImage, StockEntry, SupplierItem, User
from .oem_finder_models import OEMApplyHalt, OEMFinderChange, OEMFinderRun, OEMReviewCase
from .services import ingest_inventory
from .technical_models import PartSpecification, PartType, TechnicalField, TechnicalTemplate
from .test_oem_finder import Fresh
from .views import catalog_parts

# Every apply tier: Toyota/Hyundai current OEM SKUs, a -MOBIS rename (seed-confirmed genuine tag) and a -G rename (owner-confirmed tag).
CATALOG = [
    ('48654-30030', 'BASE AMORT TOY COROLLA'), ('48654-0K010', 'BASE AMORT TOY HILUX'), ('48654-12010', 'BASE AMORT TOY COROLLA'),
    ('48654-60010', 'BASE AMORT TOY PRADO'), ('48654-42010', 'BASE AMORT TOY RAV4'), ('48654-35010', 'BASE AMORT TOY 4RUNNER'),
    ('48654-0K030-G', 'BASE AMORT TOY HILUX'), ('48654-0K040-MANDO', 'BASE AMORT TOY HILUX'),
    ('54830-1R000', 'TERM ESTAB HYU ACCENT'), ('54830-3X000', 'TERM ESTAB HYU ELANTRA'), ('54830-2T000', 'TERM ESTAB HYU SONATA'),
    ('54830-C1000', 'TERM ESTAB HYU TUCSON'), ('54830-D3000', 'TERM ESTAB HYU SANTA FE'), ('54830-2H000-MOBIS', 'TERM ESTAB HYU ELANTRA'),
    ('MR-968365', 'BASE MIT MONTERO'),
]
FLAG, RENAME, CONFIRMED = 'AUTO_FLAG_CURRENT', 'AUTO_RENAME_BASE', 'STRONG_PENDING_OWNER_TAGS'


def create_catalog(rows=CATALOG):
    return {sku: Part.objects.create(sku=sku, name=sku, description=description) for sku, description in rows}


class Catalog(Fresh):
    def setUp(self):
        super().setUp()
        self.parts = create_catalog()
        self.root = User.objects.create_superuser('oem-root', 'oem-root@example.invalid', 'Example938!')

    def part(self, sku):
        return Part.objects.get(pk=self.parts[sku].pk)

    def stock(self, sku, quantity=3, supplier=None):
        supplier = supplier or Account.objects.get_or_create(name='PROVEEDOR OEM')[0]
        item = ingest_inventory(supplier=supplier, actor=self.root, data={'update_id': f'u-{sku}', 'supplier_invent_id': f'inv-{sku}',
                                                                           'codigo': sku, 'brand': '', 'description': '', 'quantity': quantity,
                                                                           'source': 'upload'})
        item.part, item.matching_status = self.parts[sku], 'matched'
        item.save(update_fields=['part', 'matching_status'])
        return item

    def canary(self, tier, n=50, **kwargs):
        return oa.apply_auto(tier=tier, canary=n, **kwargs)

    def link(self, sku):
        return OEMFinderChange.objects.select_related('change').get(part=self.parts[sku])


class PreferOEMNormalizedTests(TestCase):
    def setUp(self):
        self.part = Part.objects.create(sku='0110-MR968365-FEB', description='BASE MIT MONTERO')
        PartCode.objects.create(part=self.part, code='MR968365', brand='MITSUBISHI', ref_type='oem', reference_source='CATÁLOGO REVISADO')
        self.selected = {'code': 'MR968365', 'brand': 'MITSUBISHI'}

    def assert_refused(self):
        with self.assertRaises(ValidationError):
            prefer_oem(self.part, selected=self.selected)
        self.assertEqual(prefer_oem(self.part).sku, '0110-MR968365-FEB')
        self.assertFalse(CatalogIdentityChange.objects.exists())

    def test_the_target_written_with_a_dash_is_another_sku(self):
        Part.objects.create(sku='MR-968365', description='BASE MIT MONTERO')
        self.assert_refused()

    def test_a_retired_sku_with_a_trailing_underscore_still_owns_the_reference(self):
        Part.objects.create(sku='MR968365_', description='BASE MIT MONTERO', active=False)
        self.assert_refused()

    def test_an_alterno_of_another_part_owns_the_old_sku(self):
        other = Part.objects.create(sku='OTRO-1', description='OTRO')
        PartCode.objects.create(part=other, code='0110MR968365FEB', brand='FEBEST', ref_type='company')
        self.assert_refused()

    def test_another_sku_spelling_the_old_sku_blocks_but_a_merged_one_does_not(self):
        twin = Part.objects.create(sku='0110MR968365FEB', description='BASE MIT MONTERO')
        self.assert_refused()
        twin.merged_into, twin.active = self.part, False
        twin.save()
        self.assertEqual(prefer_oem(self.part, selected=self.selected).sku, 'MR968365')
        self.assertTrue(self.part.codes.filter(code='0110-MR968365-FEB').exists())

    def test_a_punctuation_twin_merged_into_the_part_does_not_block_its_oem(self):
        root = User.objects.create_superuser('agrupar', 'agrupar@example.invalid', 'Example938!')
        Part.objects.create(sku='MR-968365', description='BASE MIT MONTERO')
        merge_catalog_parts(actor=root, target_sku='0110-MR968365-FEB', source_skus=['MR-968365'])  # Agrupar SKU, then the OEM as MAIN
        part = Part.objects.get(pk=self.part.pk)
        self.assertEqual((part.sku, part.is_OEM), ('MR968365', True))
        self.assertTrue(part.codes.filter(code='0110-MR968365-FEB').exists() and part.codes.filter(code='MR-968365').exists())

    def test_a_merged_sku_equal_to_the_oem_still_blocks(self):
        Part.objects.create(sku='MR968365', description='BASE MIT MONTERO', active=False, merged_into=self.part)
        self.assert_refused()

    def test_a_free_reference_still_renames(self):
        Part.objects.create(sku='MR-968366', description='BASE MIT MONTERO')
        self.assertEqual(prefer_oem(self.part, selected=self.selected).sku, 'MR968365')

    def test_reconcile_identities_skips_parts_already_marked_oem(self):
        marked = Part.objects.create(sku='48654-30030', is_OEM=True)
        PartCode.objects.create(part=marked, code='48654-30030', brand='TOYOTA', ref_type='oem', reference_source='X')
        seen = []
        with patch('mall.catalog_identity.prefer_oem', side_effect=lambda part, **kw: seen.append(part.pk) or part):
            reconcile_identities()
        self.assertEqual(seen, [self.part.pk])


class SelectionTests(Catalog, TestCase):
    def test_apply_tiers_and_order(self):
        self.stock('54830-2H000-MOBIS', 2)
        finder = of.OEMFinder(of.load_snapshot(), suffix_table())
        results = finder.evaluate()
        rows = oa.candidates(finder, results, oa.APPLY_TIERS)
        tiers = {finder.by_id[pid]['sku']: t for pid, t in rows}
        self.assertEqual(tiers['48654-0K030-G'], CONFIRMED)
        self.assertEqual(tiers['54830-2H000-MOBIS'], RENAME)
        self.assertEqual(sum(1 for t in tiers.values() if t == FLAG), 11)
        self.assertNotIn('48654-0K040-MANDO', tiers)
        self.assertNotIn('MR-968365', tiers)
        self.assertEqual(finder.by_id[rows[0][0]]['sku'], '54830-2H000-MOBIS')  # in stock first
        self.assertEqual([t for _, t in rows[1:]], sorted((t for _, t in rows[1:]), key=oa.APPLY_TIERS.index))


class ApplyRevertTests(Catalog, TestCase):
    def preserved(self, sku):
        part = self.part(sku)
        return {'items': list(SupplierItem.objects.filter(part=part).order_by('pk').values()), 'ledger': list(StockEntry.objects.order_by('pk').values()),
                'images': list(PartImage.objects.filter(part=part).values('pk', 'image', 'position')),
                'specs': list(PartSpecification.objects.filter(part=part).values('pk', 'text_value'))}

    def decorate(self, sku):
        item = self.stock(sku, 4)
        PartImage.objects.create(part=self.parts[sku], image='catalog/test/photo.webp', thumbnail='catalog/test/thumb.webp', width=1, height=1, size_bytes=1)
        template = TechnicalTemplate.objects.create(part_type=PartType.objects.create(category='SUSPENSION', name=sku))
        field = TechnicalField.objects.create(template=template, key='lado', label='LADO', kind='text')
        PartSpecification.objects.create(part=self.parts[sku], field=field, text_value='DELANTERO')
        PartCode.objects.create(part=self.parts[sku], code='ALT-' + sku, brand='ACME', ref_type='company')
        return item

    def round_trip(self, sku, tier, new_sku, method, source_rule):
        item = self.decorate(sku)
        before, codes = self.preserved(sku), set(self.part(sku).codes.values_list('code', 'brand', 'ref_type', 'reference_source'))
        outcome = self.canary(tier)
        run = outcome['run']
        part = self.part(sku)
        self.assertEqual((part.pk, part.sku, part.is_OEM), (self.parts[sku].pk, new_sku, True))
        self.assertEqual(self.preserved(sku), before)
        source = f'algo:oem-finder:v2:{source_rule}:{run.pk}'
        self.assertTrue(part.codes.filter(code=new_sku, brand=self.brand(sku), ref_type='oem', reference_source=source).exists())
        self.assertTrue(codes <= set(part.codes.values_list('code', 'brand', 'ref_type', 'reference_source')))
        link = self.link(sku)
        self.assertEqual((link.run, link.tier, link.method, link.code, link.batch), (run, tier, method, new_sku, 1))
        self.assertEqual((link.change.previous_sku, link.change.sku, link.change.actor.username), (sku, new_sku, oa.SERVICE_USER))
        self.assertEqual(link.change.reference['reference_source'], source)
        self.assertEqual(link.evidence['primary']['lexicon']['result'], 'agree_strong')
        self.assertEqual(link.evidence['snapshot']['sku'], sku)
        for code in {sku, new_sku}:  # the old code stays searchable and still matches supplier stock
            self.assertEqual(list(catalog_parts(code)), [part])
        target, _, automatic, _ = MatchIndex().resolve(item)
        self.assertEqual((target[0]['id'], automatic), (str(part.pk), True))
        result = oa.revert_run(run.pk, actor=self.root, part=part.pk)
        self.assertEqual((result['reverted'], result['already_reverted'], result['skipped']), (1, 0, []))
        part = self.part(sku)
        self.assertEqual((part.pk, part.sku, part.name, part.is_OEM), (self.parts[sku].pk, sku, sku, False))
        self.assertEqual(set(part.codes.values_list('code', 'brand', 'ref_type', 'reference_source')), codes)
        self.assertEqual(self.preserved(sku), before)
        link.refresh_from_db()
        self.assertIsNotNone(link.reverted_at)
        self.assertEqual((link.revert_change.previous_sku, link.revert_change.sku, link.revert_change.actor), (new_sku, sku, self.root))
        self.assertEqual(link.revert_change.reference['reverts'], link.change_id)
        changes = CatalogIdentityChange.objects.count()
        self.assertEqual(oa.revert_run(run.pk, actor=self.root, part=part.pk), {'run': run.pk, 'reverted': 0, 'already_reverted': 1, 'skipped': []})
        self.assertEqual(CatalogIdentityChange.objects.count(), changes)
        return run

    def brand(self, sku):
        return 'HYUNDAI' if sku.startswith('54830') else 'TOYOTA'

    def test_flag_current_round_trip(self):
        run = self.round_trip('48654-30030', FLAG, '48654-30030', 'flag_current', 'current')
        self.assertEqual(run.applied['tiers'], {FLAG: 11})
        change = CatalogIdentityChange.objects.filter(part=self.parts['48654-30030']).order_by('pk').first()
        self.assertEqual((change.previous_sku, change.sku, change.reference['action']), ('48654-30030', '48654-30030', 'confirm_current'))
        self.assertEqual(oa.revert_run(run.pk)['reverted'], 10)
        self.assertFalse(Part.objects.filter(is_OEM=True).exists() or PartCode.objects.filter(ref_type='oem').exists())

    def test_rename_base_round_trip(self):
        self.round_trip('54830-2H000-MOBIS', RENAME, '54830-2H000', 'rename_base', 'strong-MOBIS')

    def test_owner_confirmed_tag_rename_round_trip(self):
        self.round_trip('48654-0K030-G', CONFIRMED, '48654-0K030', 'rename_confirmed_tags', 'strong-G')

    def test_rename_keeps_the_old_sku_as_an_alterno(self):
        self.canary(RENAME)
        part = self.part('54830-2H000-MOBIS')
        self.assertEqual((part.sku, part.name), ('54830-2H000', '54830-2H000'))
        self.assertTrue(part.codes.filter(code='54830-2H000-MOBIS').exists())
        undo = self.link('54830-2H000-MOBIS').undo
        self.assertEqual({c['code'] for c in undo['created_codes']}, {'54830-2H000', '54830-2H000-MOBIS'})
        self.assertEqual((undo['sku'], undo['name'], undo['is_OEM']), ('54830-2H000-MOBIS', '54830-2H000-MOBIS', False))

    def test_revert_one_part_of_a_run(self):
        run = self.canary(FLAG, 3)['run']
        first = run.changes.order_by('pk').first()
        result = oa.revert_run(run.pk, actor=self.root, part=first.part_id)
        self.assertEqual(result['reverted'], 1)
        self.assertEqual(run.changes.filter(reverted_at__isnull=True).count(), 2)

    def test_revert_skips_a_part_renamed_since_or_whose_old_sku_is_taken(self):
        run = self.canary(None, 50)['run']
        Part.objects.filter(pk=self.parts['48654-0K030-G'].pk).update(sku='48654-0K030-MANUAL')
        Part.objects.create(sku='54830-2H000MOBIS', description='OTRO')
        result = oa.revert_run(run.pk, actor=self.root)
        self.assertEqual({(r['sku'], r['reason']) for r in result['skipped']},
                         {('48654-0K030', 'sku_changed'), ('54830-2H000', 'old_sku_taken')})
        self.assertEqual(self.part('54830-2H000-MOBIS').sku, '54830-2H000')
        self.assertEqual(result['reverted'], 11)

    def test_revert_skips_a_row_whose_written_codes_were_edited_since(self):
        run = self.canary(RENAME)['run']
        part = self.part('54830-2H000-MOBIS')
        PartCode.objects.filter(part=part, code='54830-2H000').update(reference_source='REVISADO A MANO')
        codes = set(part.codes.values_list('code', 'brand', 'ref_type', 'reference_source'))
        result = oa.revert_run(run.pk)
        self.assertEqual((result['reverted'], [(r['reason'], r['detail']) for r in result['skipped']]), (0, [('codes_changed', 'HYUNDAI 54830-2H000')]))
        part = self.part('54830-2H000-MOBIS')
        self.assertEqual((part.sku, part.is_OEM, set(part.codes.values_list('code', 'brand', 'ref_type', 'reference_source'))), ('54830-2H000', True, codes))
        self.assertIsNone(self.link('54830-2H000-MOBIS').reverted_at)
        PartCode.objects.filter(part=part, code='54830-2H000').delete()  # re-created with the values the run wrote: still the run's
        PartCode.objects.create(part=part, code='54830-2H000', brand='HYUNDAI', ref_type='oem', reference_source=f'algo:oem-finder:v2:strong-MOBIS:{run.pk}')
        self.assertEqual(oa.revert_run(run.pk)['reverted'], 1)
        reconcile_identities()  # no verified OEM is left behind to rename the SKU again
        part = self.part('54830-2H000-MOBIS')
        self.assertEqual((part.sku, part.is_OEM, part.codes.count()), ('54830-2H000-MOBIS', False, 0))

    def test_revert_leaves_a_part_grouped_since_alone(self):
        run = self.canary(FLAG, 2)['run']
        link = run.changes.select_related('part').first()
        Part.objects.filter(pk=link.part_id).update(active=False, merged_into=self.parts['MR-968365'])
        result = oa.revert_run(run.pk)
        self.assertEqual((result['reverted'], [r['reason'] for r in result['skipped']]), (1, ['retired']))
        self.assertTrue(self.part(link.part.sku).is_OEM)


class RunTests(Catalog, TestCase):
    def test_canary_applies_only_n_rows_in_stock_first(self):
        self.stock('54830-D3000')
        self.stock('48654-42010')
        outcome = self.canary(FLAG, 2)
        run = outcome['run']
        self.assertEqual((run.mode, run.status, run.scope['canary'], run.scope['tier']), ('apply_auto', 'completed', 2, FLAG))
        self.assertEqual({l.part.sku for l in run.changes.select_related('part')}, {'54830-D3000', '48654-42010'})
        self.assertEqual((run.applied['applied'], run.applied['selected'], len(run.applied['batches'])), (2, 2, 1))
        self.assertEqual(Part.objects.filter(is_OEM=True).count(), 2)
        self.assertEqual(run.counts['tiers'][FLAG], [11, 2])

    def test_batches_require_a_canary_per_tier(self):
        with self.assertRaises(oa.ApplyRefused) as refused:
            oa.apply_auto(tier=FLAG)
        self.assertEqual((refused.exception.reason, refused.exception.extra['tiers']), ('needs_canary', [FLAG]))
        self.canary(FLAG, 1)
        outcome = oa.apply_auto(tier=FLAG, batch_size=4)
        self.assertEqual([b['applied'] for b in outcome['summary']['batches']], [4, 4, 2])
        with self.assertRaises(oa.ApplyRefused) as refused:
            oa.apply_auto()
        self.assertEqual(refused.exception.extra['tiers'], [RENAME, CONFIRMED])
        self.assertFalse(OEMFinderRun.objects.filter(status='failed').exists())

    def test_a_fully_reverted_canary_needs_a_new_canary(self):
        run = self.canary(FLAG, 2)['run']
        oa.revert_run(run.pk, part=run.changes.order_by('pk').first().part_id)
        self.assertTrue(oa.canary_done(FLAG))  # one wrong row undone: the canary still counts
        oa.revert_run(run.pk)
        self.assertFalse(oa.canary_done(FLAG))
        with self.assertRaises(oa.ApplyRefused):
            oa.apply_auto(tier=FLAG)

    def test_a_failed_run_raises_the_halt(self):
        with patch.object(oa, 'execute_batches', side_effect=RuntimeError('boom')), self.assertRaises(RuntimeError):
            self.canary(FLAG, 2)
        run = OEMFinderRun.objects.get(mode='apply_auto')
        self.assertEqual((run.status, oa.active_halt().run), ('failed', run))

    def test_re_applying_writes_nothing(self):
        self.canary(None, 50)
        changes, codes = CatalogIdentityChange.objects.count(), PartCode.objects.count()
        self.assertEqual(changes, 13)
        outcome = oa.apply_auto()
        self.assertEqual((outcome['summary']['applied'], outcome['summary']['selected']), (0, 0))
        self.assertEqual((CatalogIdentityChange.objects.count(), PartCode.objects.count()), (changes, codes))

    def test_a_reverted_oem_is_not_applied_again(self):
        run = self.canary(RENAME)['run']
        oa.revert_run(run.pk)
        outcome = self.canary(RENAME)
        self.assertEqual((outcome['summary']['applied'], outcome['summary']['excluded']), (0, {'previously_reverted': 1}))
        self.assertEqual(self.part('54830-2H000-MOBIS').sku, '54830-2H000-MOBIS')

    def test_a_conflict_from_prefer_oem_becomes_a_conflict_review_case(self):
        other = Part.objects.create(sku='KIT-OTRO', description='KIT VARIOS')
        PartCode.objects.create(part=other, code='54830-2H000MOBIS', brand='ACME', ref_type='company')  # the old SKU, normalized
        preview = oa.apply_auto(tier=RENAME, read_only=True)
        self.assertEqual((preview['plan'][RENAME]['conflicts'], preview['plan'][RENAME]['examples'][0]['sku']), (1, '54830-2H000-MOBIS'))
        outcome = self.canary(RENAME)
        self.assertEqual((outcome['summary']['applied'], outcome['summary']['conflicts']), (0, 1))
        part = self.part('54830-2H000-MOBIS')
        self.assertEqual((part.sku, part.is_OEM, part.codes.count()), ('54830-2H000-MOBIS', False, 0))
        self.assertFalse(CatalogIdentityChange.objects.exists() or OEMFinderChange.objects.exists())
        case = OEMReviewCase.objects.get(part=part)
        self.assertEqual((case.tier, case.underlying_tier, case.status, case.candidate), ('CONFLICT', RENAME, 'review', '54830-2H000'))
        self.assertIn('apply_conflict', case.blockers)
        self.assertIn('Agrupar SKU', case.evidence['apply_conflict']['detail'])
        of.execute()  # a dry run cannot see why prefer_oem refused: the case stays open
        case.refresh_from_db()
        self.assertEqual((case.status, case.tier), ('review', 'CONFLICT'))
        again = self.canary(RENAME)
        self.assertEqual((again['summary']['conflicts'], again['summary']['excluded']), (0, {'open_conflict': 1}))

    def test_snapshot_drift_skips_the_row(self):
        snapshot = of.load_snapshot()
        Part.objects.filter(pk=self.parts['48654-30030'].pk).update(description='BASE AMORT TOY COROLLA 08-')
        PartCode.objects.create(part=self.parts['54830-1R000'], code='XYZ', brand='ACME')
        outcome = self.canary(FLAG, snapshot=snapshot)
        self.assertEqual((outcome['summary']['applied'], outcome['summary']['skipped']), (9, {'snapshot_changed': 2}))
        self.assertFalse(self.part('48654-30030').is_OEM)

    def test_a_new_claimant_skips_the_row(self):
        snapshot = of.load_snapshot()
        Part.objects.create(sku='48654-30030-NAK', description='BASE AMORT TOY COROLLA')
        outcome = self.canary(FLAG, snapshot=snapshot)
        self.assertEqual(outcome['summary']['skipped'], {'new_claimant': 1})
        self.assertFalse(self.part('48654-30030').is_OEM)

    def test_rows_applied_earlier_in_the_run_are_not_new_claimants(self):
        self.parts.update(create_catalog([('48654-12010-01', 'BASE AMORT TOY COROLLA')]))
        self.stock('48654-12010-01')  # applied first: its new OEM alterno contains the key of 48654-12010
        outcome = self.canary(FLAG)
        self.assertEqual((outcome['summary']['applied'], outcome['summary']['skipped']), (12, {}))
        self.assertEqual(outcome['run'].changes.order_by('pk').first().part.sku, '48654-12010-01')

    def test_halt_rule_blocks_starts_and_stops_further_batches(self):
        oa.halt('MUESTRA CON 2 ERRORES', actor=self.root)
        with self.assertRaises(oa.ApplyRefused) as refused:
            self.canary(FLAG)
        self.assertEqual(refused.exception.reason, 'halted')
        oa.resume(actor=self.root)
        real = oa.active_halt
        with patch.object(oa, 'active_halt', side_effect=[None, None, OEMApplyHalt(reason='X')]):
            outcome = self.canary(FLAG, 10, batch_size=4)
        self.assertEqual((outcome['summary']['applied'], outcome['summary']['stopped']), (4, 'halted'))
        self.assertIsNone(real())

    def test_unexpected_errors_roll_back_the_row_stop_the_run_and_raise_the_halt(self):
        calls = []

        def flaky(part, **kwargs):
            calls.append(part.sku)
            if len(calls) == 2:
                raise RuntimeError('boom')
            return prefer_oem(part, **kwargs)
        with patch.object(oa, 'prefer_oem', side_effect=flaky):
            outcome = self.canary(FLAG, 10, batch_size=4)
        s = outcome['summary']
        self.assertEqual((s['applied'], s['errors'], s['stopped'], len(s['batches'])), (3, 1, 'errors', 1))
        self.assertEqual(outcome['run'].errors[0]['detail'], 'boom')
        self.assertFalse(PartCode.objects.filter(code=calls[1], ref_type='oem').exists())
        self.assertEqual(oa.active_halt().run, outcome['run'])

    def test_spot_check_export_samples_twenty_applied_rows(self):
        extra = [(f'48654-0K{n:03d}', 'BASE AMORT TOY HILUX') for n in range(100, 115)]
        create_catalog(extra)
        run = self.canary(FLAG, 30)['run']
        self.assertEqual(run.applied['applied'], 26)
        self.assertEqual(len(run.applied['spot_check']), 20)
        rows = oa.spot_check_rows(run)
        self.assertEqual(len(rows), 20)
        self.assertEqual({r['tier'] for r in rows}, {FLAG})
        self.assertTrue(all(r['lexicon'] == 'agree_strong' and r['evidence']['primary'] and r['description'] for r in rows))
        csv_text = oa.spot_check_csv(rows)
        self.assertTrue(csv_text.startswith('ejecucion,cambio,lote,nivel,metodo,id_sku,sku_anterior,sku_nuevo'))
        self.assertEqual(len(csv_text.strip().splitlines()), 21)
        self.assertEqual(oa.spot_check_csv([{**rows[0], 'description': '=HYPERLINK("x")'}]).splitlines()[1].count("'=HYPERLINK"), 1)

    def test_read_only_preview_writes_nothing(self):
        outcome = oa.apply_auto(read_only=True)
        self.assertEqual({t: (p['selected'], p['applied']) for t, p in outcome['plan'].items()}, {FLAG: (11, 11), RENAME: (1, 1), CONFIRMED: (1, 1)})
        self.assertFalse(OEMFinderRun.objects.exists() or PartCode.objects.exists() or Part.objects.filter(is_OEM=True).exists())


class CommandTests(Catalog, TestCase):
    def call(self, *argv, module=of):
        out = io.StringIO()
        return module.main(list(argv), stdout=out), out.getvalue()

    def test_canary_batches_halt_and_revert_from_the_command_line(self):
        code, out = self.call('--apply-auto', '--tier', FLAG)
        self.assertEqual(code, 1)
        self.assertIn('canario', out)
        code, out = self.call('--apply-auto', '--tier', FLAG, '--canary', '2', '--in-stock-first')
        self.assertEqual(code, 0, out)
        run = OEMFinderRun.objects.get(mode='apply_auto')
        self.assertIn(f'ejecución {run.pk} · canario 2', out)
        self.assertIn(f'oem_finder_revert --run {run.pk}', out)
        self.assertEqual(self.call('--halt', 'MUESTRA CON ERRORES')[0], 0)
        code, out = self.call('--apply-auto', '--tier', FLAG)
        self.assertEqual(code, 3)
        self.assertIn('MUESTRA CON ERRORES', out)
        self.assertIn('levantada', self.call('--resume')[1])
        code, out = self.call('--apply-auto', '--tier', FLAG, '--limit', '3')
        self.assertEqual(code, 0, out)
        self.assertEqual(Part.objects.filter(is_OEM=True).count(), 5)
        code, out = self.call('--run', str(run.pk), module=oem_finder_revert)
        self.assertEqual(code, 0)
        self.assertIn('2 revertidos', out)
        self.assertIn('0 revertidos, 2 ya estaban revertidos', self.call('--run', str(run.pk), module=oem_finder_revert)[1])
        self.assertEqual(self.call('--run', '9999', module=oem_finder_revert)[0], 1)

    def test_preview_and_spot_check_files(self):
        code, out = self.call('--apply-auto', '--read-only')
        self.assertEqual(code, 0)
        self.assertIn('solo lectura', out)
        self.assertFalse(OEMFinderRun.objects.exists())
        with patch('sys.stderr', io.StringIO()) as log:
            code, out = self.call('--apply-auto', '--read-only', '--tier', RENAME, '--json', '-')
        doc = json.loads(out)
        self.assertEqual((code, doc['summary']['apply'][RENAME]['applied'], [r['sku'] for r in doc['results']]), (0, 1, ['54830-2H000-MOBIS']))
        self.assertIn('Aplicaría', log.getvalue())
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            code, out = self.call('--apply-auto', '--canary', '3', '--spot-check-dir', folder)
            run = OEMFinderRun.objects.get()
            with open(f'{folder}/oem-run-{run.pk}-spot-check.csv', encoding='utf-8') as f:
                self.assertEqual(len(f.read().strip().splitlines()), 4)

    def test_the_entry_points_load_before_django_is_set_up(self):
        import subprocess
        import sys
        from django.conf import settings
        for module in ('mall.oem_finder', 'mall.oem_finder_revert'):
            with self.subTest(module=module):
                done = subprocess.run([sys.executable, '-m', module, '--help'], cwd=settings.BASE_DIR, capture_output=True, text=True, timeout=60)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIn('--run' if module.endswith('revert') else '--apply-auto', done.stdout)

    def test_apply_needs_its_tables_and_valid_options(self):
        with patch.object(oa, 'apply_tables_ready', return_value=False):
            code, out = self.call('--apply-auto', '--canary', '1')
            self.assertEqual(code, 1)
            self.assertIn('aún no están migradas', out)
            self.assertEqual(self.call('--apply-auto', '--read-only')[0], 0)
            self.assertEqual(self.call('--run', '1', module=oem_finder_revert)[0], 1)
        self.assertFalse(OEMFinderRun.objects.exists())
        for argv in (['--apply-auto', '--tier', 'CONFLICT'], ['--dry-run', '--canary', '5'], ['--apply-auto', '--canary', '5', '--limit', '5'],
                     ['--dry-run', '--apply-auto']):
            with self.subTest(argv=argv), patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
                self.call(*argv)


class APITests(Catalog, APITestCase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.root)

    def test_run_history_detail_and_spot_check_download(self):
        of.execute()
        run = self.canary(FLAG, 3)['run']
        data = self.client.get('/api/v1/management/oem-finder/runs/', {'mode': 'apply_auto'}).data
        self.assertEqual((data['count'], data['halt']), (1, None))
        row = data['results'][0]
        self.assertEqual((row['id'], row['changes'], row['reverted'], row['applied']['applied'], row['actor']), (run.pk, 3, 0, 3, oa.SERVICE_USER))
        self.assertEqual(self.client.get('/api/v1/management/oem-finder/runs/').data['count'], 2)
        self.assertEqual(self.client.get('/api/v1/management/oem-finder/runs/', {'mode': 'nada'}).status_code, 400)
        detail = self.client.get(f'/api/v1/management/oem-finder/runs/{run.pk}/').data
        self.assertEqual((detail['count'], detail['results'][0]['method'], detail['results'][0]['reverted_at']), (3, 'flag_current', None))
        spot = self.client.get(f'/api/v1/management/oem-finder/runs/{run.pk}/spot-check/').data
        self.assertEqual((len(spot['rows']), spot['columns'][-1]), (3, 'correcto_si_no'))
        self.assertTrue(spot['csv'].startswith('ejecucion,'))
        csv_response = self.client.get(f'/api/v1/management/oem-finder/runs/{run.pk}/spot-check/', {'download': 'csv'})
        self.assertEqual((csv_response.status_code, csv_response['Content-Type']), (200, 'text/csv; charset=utf-8'))
        self.assertIn(f'oem-run-{run.pk}-spot-check.csv', csv_response['Content-Disposition'])
        self.assertEqual(len(csv_response.content.decode().strip().splitlines()), 4)
        self.assertIn(b'"evidence"', self.client.get(f'/api/v1/management/oem-finder/runs/{run.pk}/spot-check/', {'download': 'json'}).content)
        dry = OEMFinderRun.objects.get(mode='dry_run')
        self.assertEqual(self.client.get(f'/api/v1/management/oem-finder/runs/{dry.pk}/spot-check/').status_code, 404)

    def test_revert_a_part_then_the_run_and_halt_from_the_api(self):
        run = self.canary(FLAG, 3)['run']
        part = run.changes.order_by('pk').first().part_id
        url = f'/api/v1/management/oem-finder/runs/{run.pk}/revert/'
        self.assertEqual(self.client.post(url, {'part': str(self.parts['MR-968365'].pk)}, format='json').status_code, 400)
        one = self.client.post(url, {'part': str(part)}, format='json')
        self.assertEqual((one.status_code, one.data['reverted']), (200, 1))
        rest = self.client.post(url, {}, format='json').data
        self.assertEqual((rest['reverted'], rest['already_reverted'], rest['skipped']), (2, 1, []))
        self.assertEqual(CatalogIdentityChange.objects.filter(reference__action='revert', actor=self.root).count(), 3)
        halt_url = '/api/v1/management/oem-finder/halt/'
        self.assertEqual(self.client.post(halt_url, {'action': 'halt'}, format='json').status_code, 400)
        halted = self.client.post(halt_url, {'action': 'halt', 'reason': 'MUESTRA CON 2 ERRORES'}, format='json').data
        self.assertEqual((halted['halt']['reason'], halted['halt']['created_by']), ('MUESTRA CON 2 ERRORES', 'oem-root'))
        self.assertEqual(self.client.get('/api/v1/management/oem-finder/runs/').data['halt']['reason'], 'MUESTRA CON 2 ERRORES')
        self.assertIsNone(self.client.post(halt_url, {'action': 'resume'}, format='json').data['halt'])

    def test_superusers_only_and_unmigrated(self):
        run = self.canary(FLAG, 1)['run']
        self.client.force_authenticate(User.objects.create_user('lector', 'lector@example.invalid', 'Example938!'))
        for method, url in (('get', '/api/v1/management/oem-finder/runs/'), ('get', f'/api/v1/management/oem-finder/runs/{run.pk}/spot-check/'),
                            ('post', f'/api/v1/management/oem-finder/runs/{run.pk}/revert/'), ('post', '/api/v1/management/oem-finder/halt/')):
            self.assertEqual(getattr(self.client, method)(url, format='json').status_code, 403)
        self.client.force_authenticate(self.root)
        with patch('mall.oem_finder_views.apply_tables_ready', return_value=False):
            self.assertEqual(self.client.get('/api/v1/management/oem-finder/runs/').status_code, 503)
        self.assertIsNone(OEMFinderChange.objects.get().reverted_at)


@skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL row locks.')
class ConcurrencyTests(Catalog, TransactionTestCase):
    def test_a_concurrent_catalog_edit_wins_and_the_row_is_skipped(self):
        """A catalog edit holding the Part lock commits first; the apply waits on the lock (Account -> Part), re-checks and skips."""
        self.stock('48654-30030')
        snapshot = of.load_snapshot()
        locked, release, results = threading.Event(), threading.Event(), []

        def edit():
            from django.db import connection as thread_connection, transaction
            try:
                with transaction.atomic():
                    part = Part.objects.select_for_update().get(pk=self.parts['48654-30030'].pk)
                    part.description = 'BASE AMORT TOY COROLLA EDITADO'
                    part.save(update_fields=['description'])
                    locked.set()
                    release.wait(10)
            finally:
                thread_connection.close()

        def apply():
            from django.db import connection as thread_connection
            try:
                results.append(self.canary(FLAG, 1, snapshot=snapshot))
            finally:
                thread_connection.close()
        editor = threading.Thread(target=edit)
        editor.start()
        locked.wait(10)
        applier = threading.Thread(target=apply)
        applier.start()
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
        applier.join()
        self.assertTrue(waiting, 'the apply should wait for the catalog edit lock')
        self.assertEqual(results[0]['summary']['skipped'], {'snapshot_changed': 1})
        part = self.part('48654-30030')
        self.assertEqual((part.description, part.is_OEM, part.codes.filter(ref_type='oem').count()), ('BASE AMORT TOY COROLLA EDITADO', False, 0))
