"""The catalog merge pass (inventory refactor S9, mall.merge_pass): one fixture per rule and guard of the plan (§3 S9): GWT-71A over
16100-79445; 16100 against 16110; partner makes; an item that replaces others (=(GW..)); W/HOUSING, '*' rows and index-only
citations; lines kept apart; 3f retirements; the catalog-copied company References of step 0; and the routes, the main (3g), the
codes (3e, OD15, OD17), the apply in chunks with its manifest, the rollback, the Biblioteca's hand-added codes and the case
decisions. The review of 2026-10-15 added: a linked SKU's own numbers guard the merge; the apply's second plan merges nothing; a
code typed by hand never merges; a declarado code the pass made hides when its sources go; a step 0 case kept apart with no line;
an import in progress stops the run."""
import io
import json
import os
import tempfile
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from . import merge_pass, references
from .brand_models import Brand
from .line_merge import keep_apart
from .matching_models import MatchingCase
from .models import CatalogMerge, Part, Reference, ReferenceSource
from .oem_links import equivalents_for, store_catalog_links, store_cross_references
from .oem_reference_models import OEMCrossReference, OEMCrossReferenceRetired, PartOEMLink
from .reference_matching import decide
from .test_factories import make_line, make_supplier, make_user, make_variant

GMB = 'GMB Water Pump Catalog 2016'


def gmb(page):
    return f'{GMB} · pág. {page}'


class MergePassBase(TestCase):
    def setUp(self):
        super().setUp()
        self.output = tempfile.TemporaryDirectory()
        self.addCleanup(self.output.cleanup)
        patcher = patch.dict(os.environ, {'REFACTOR_OUTPUT': self.output.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, tipo in (('GMB', 'repuesto'), ('AISIN', 'repuesto'), ('ASVA', 'repuesto'), ('TOYOTA', 'vehiculo'), ('HONDA', 'vehiculo'),
                           ('FORD', 'vehiculo'), ('MAZDA', 'vehiculo')):
            Brand.objects.get_or_create(nombre=name, defaults={'tipo': tipo})
        self.root = make_user('s9-root', superuser=True)
        self.marks = os.path.join(self.output.name, 'marks.json')
        self.write_marks()

    def write_marks(self, star=(), whousing=()):
        with open(self.marks, 'w', encoding='utf-8') as handle:
            json.dump({'index_pages': [522, 547], 'star_rows': [[524, number, item] for number, item in star],
                       'whousing_numbers': [[number, 78] for number in whousing]}, handle)

    def number(self, code, maker='TOYOTA', applications='TOYOTA HILUX', part_type='BOMBAS DE AGUA'):
        return references.add_library_number(maker, code, source_kind='aftermarket_catalog', citation=GMB, part_type=part_type,
                                             description='BOMBA DE AGUA', applications=applications)[0]

    def file(self, brand, code, *numbers, citations=None):
        store_cross_references({(number.pk, brand, code): citations or [gmb(12)] for number in numbers})

    def line_of(self, ref):
        ref.refresh_from_db()
        return Part._base_manager.get(pk=ref.part_id)

    def plan(self):
        out = io.StringIO()
        return merge_pass.run(stdout=out, marks_path=self.marks)['planned']

    def apply(self):
        out = io.StringIO()
        result = merge_pass.run(apply=True, username=self.root.username, stdout=out, marks_path=self.marks, chunk=2)
        self.assertIsNotNone(result, out.getvalue())
        return result

    def group_of(self, planned, ref):
        line = str(self.line_of(ref).pk)
        return next(group for group in planned['groups'] if line in group['lines'])


class GroupsAndRoutesTests(MergePassBase):
    def test_gwt71a_numbers_merge_on_their_own_and_its_codes_become_references(self):
        """GMB prints GWT-71A for 16100-79445 and 16100-79446: one part. No SKU: the line of the number 3g picks keeps it (the highest
        revision of the same base); GWT-71A becomes its aftermarket Reference (declarado), AISIN WPT-111 (a competitor's, from a
        PDF-checked catalog) inferido, each with its catalogo source."""
        first, second = self.number('16100-79445'), self.number('16100-79446')
        self.file('GMB', 'GWT-71A', first, second)
        self.file('AISIN', 'WPT-111', first, second, citations=[gmb(550)])
        planned = self.plan()
        group = self.group_of(planned, first)
        self.assertEqual((group['route'], group['skus'], len(group['lines'])), ('auto', [], 2))
        entries = planned['codes']['entries']
        self.assertEqual((entries[('GMB', 'GWT71A')]['outcome'], entries[('GMB', 'GWT71A')]['estado']), ('convertir', 'declarado'))
        self.assertEqual((entries[('AISIN', 'WPT111')]['outcome'], entries[('AISIN', 'WPT111')]['estado']), ('convertir', 'inferido'))
        self.assertEqual(Part.objects.filter(estado='unificada').count(), 0)  # a dry run writes nothing

        result = self.apply()
        self.assertEqual(result['written']['unificaciones'], 1)
        kept = self.line_of(second)
        first.refresh_from_db()
        self.assertEqual(first.part_id, kept.pk)  # the number moved onto the kept line
        self.assertEqual(kept.sku, '16100-79446')
        merged = Part._base_manager.filter(merged_into=kept)
        self.assertEqual([line.estado for line in merged], ['unificada'])
        merge = CatalogMerge.objects.get(target=kept)
        self.assertEqual((merge.summary['reason'], merge.summary['automatic'], merge.summary['merge_pass']['codes']),
                         (merge_pass.MERGE_REASON, True, ['GMB GWT71A']))
        own = Reference.objects.get(part=kept, brand='GMB')
        self.assertEqual((own.code, own.tipo, own.estado), ('GWT-71A', 'aftermarket', 'declarado'))
        self.assertEqual(list(own.sources.values_list('kind', 'citation')), [('catalogo', gmb(12))])
        competitor = Reference.objects.get(part=kept, brand='AISIN')
        self.assertEqual((competitor.code, competitor.estado), ('WPT-111', 'inferido'))
        self.assertEqual({row['code'] for row in equivalents_for([kept.pk])[str(kept.pk)]}, {'GWT-71A', 'WPT-111'})
        outcomes = [json.loads(line) for line in open(merge_pass.manifest_path('xref_outcomes.jsonl'), encoding='utf-8')]
        self.assertEqual({line['outcome'] for line in outcomes}, {'converted'})
        self.assertEqual(len(outcomes), OEMCrossReference.objects.count())

        again = self.apply()  # a second run finds nothing left to do, and the References it made are no step 0 copies
        self.assertEqual({key: value for key, value in again['written'].items() if value}, {})
        self.assertEqual(again['counts'].get('paso0_copias', 0), 0)

    def test_toyota_16100_and_16110_never_merge(self):
        """With housing and without (Toyota 16100 / 16110): a case, guard prefix, with both lines."""
        assy, bare = self.number('16100-79445'), self.number('16110-79445')
        self.file('GMB', 'GWT-71A', assy, bare)
        group = self.group_of(self.plan(), assy)
        self.assertEqual((group['route'], group['motivo']), ('caso', 'prefix'))
        self.assertIn('with_housing_vs_bare', group['flags'])
        self.apply()
        case = MatchingCase.objects.get(kind='merge', route='caso')
        self.assertEqual((case.guard, case.evidence['regla']), ('prefix', '3d'))
        self.assertEqual({str(pk) for pk in case.lines.values_list('pk', flat=True)}, {str(self.line_of(assy).pk), str(self.line_of(bare).pk)})
        self.assertEqual(Part.objects.filter(estado='unificada').count(), 0)
        self.assertEqual(Reference.objects.filter(brand='GMB').count(), 0)  # the code waits in the case (3e)

    def test_partner_makes_merge_and_an_unlisted_make_waits(self):
        """Ford and Mazda build each other's parts: a Mazda number under a Ford application merges on its own. Toyota and Honda are no
        partners: a make missing from the applications is a case, guard make."""
        ford, mazda = self.number('XS4Z-8501-BA', 'FORD', 'FORD FOCUS'), self.number('L3G2-15-010', 'MAZDA', 'FORD FOCUS')
        self.file('GMB', 'GWF-50A', ford, mazda)
        toyota, honda = self.number('16100-29155', 'TOYOTA', 'TOYOTA COROLLA'), self.number('19200-P8A-A01', 'HONDA', 'TOYOTA COROLLA')
        self.file('GMB', 'GWT-99A', toyota, honda)
        planned = self.plan()
        partners = self.group_of(planned, ford)
        self.assertEqual((partners['route'], partners['partner']), ('auto', True))
        unlisted = self.group_of(planned, toyota)
        self.assertEqual((unlisted['route'], unlisted['motivo']), ('caso', 'make'))
        self.assertIn('cross_make_unlisted', unlisted['flags'])

    def test_an_item_that_replaces_others_merges_on_its_own(self):
        """=(GW..): the catalog says the item consolidates others; the owner approved those merging on their own (a flag only)."""
        first = self.number('16100-79445', applications='TOYOTA HILUX GWT-71A=(GWT-80A)')
        second = self.number('16100-79447')
        self.file('GMB', 'GWT-71A', first, second)
        group = self.group_of(self.plan(), first)
        self.assertEqual(group['route'], 'auto')
        self.assertIn('consolidated_item', group['flags'])

    def test_w_housing_star_rows_and_index_pages_join_nothing(self):
        """A W/HOUSING NO. number under an item without H, a GMB row marked '*' and a row cited only on the index pages (522-547) for a
        number with no applications never join numbers; their sources keep the marks."""
        self.write_marks(star=[('16100-79447', 'GWT-72A')], whousing=['1610079445'])
        housing, other = self.number('16100-79445'), self.number('16100-79446')
        self.file('GMB', 'GWT-71A', housing, other)
        starred, plain = self.number('16100-79447'), self.number('16100-79448')
        self.file('GMB', 'GWT-72A', starred, plain)
        index, listed = self.number('16100-79449', applications=''), self.number('16100-79450')
        self.file('GMB', 'GWT-73A', index, citations=[gmb(530)])
        self.file('GMB', 'GWT-73A', listed)
        planned = self.plan()
        self.assertEqual(planned['groups'], [])
        self.assertEqual({key: planned['counts'][f'excluidas_{key}'] for key in ('whousing', 'star', 'index_only')},
                         {'whousing': 1, 'star': 1, 'index_only': 1})
        self.apply()
        self.assertEqual(Part.objects.filter(estado='unificada').count(), 0)
        code = Reference.objects.get(brand='GMB', code='GWT-71A')  # on the line of the number it does join
        self.assertEqual(code.part_id, self.line_of(other).pk)
        self.assertEqual(ReferenceSource.objects.get(reference=code).marks, ['W/HOUSING'])

    def test_lines_kept_apart_are_never_proposed(self):
        first, second = self.number('16100-79445'), self.number('16100-79446')
        self.file('GMB', 'GWT-71A', first, second)
        keep_apart([self.line_of(first).pk, self.line_of(second).pk], actor=self.root, reason='test')
        group = self.group_of(self.plan(), first)
        self.assertEqual(group['route'], 'separadas')
        self.apply()
        self.assertEqual((Part.objects.filter(estado='unificada').count(), MatchingCase.objects.filter(kind='merge').count()), (0, 0))

    def test_one_sku_takes_the_merge_and_two_skus_go_to_a_batch(self):
        """At most one live line: merged into it, its main kept (the SKU's number). Two live lines: a case in the merge-pass batch, the
        line with most stock proposed as the keeper."""
        sku = make_line('16100-79445', description='BOMBA AGUA TOY HILUX')
        on_sku = self.number('16100-79445')
        self.assertEqual(on_sku.part_id, sku.pk)
        loose = self.number('16100-79446')
        self.file('GMB', 'GWT-71A', on_sku, loose)
        other_a, other_b = make_line('21010-0W025', description='BOMBA AGUA NIS'), make_line('21010-0W026', description='BOMBA AGUA NIS')
        num_a, num_b = self.number('21010-0W025', 'NISSAN', 'NISSAN'), self.number('21010-0W026', 'NISSAN', 'NISSAN')
        self.assertEqual((num_a.part_id, num_b.part_id), (other_a.pk, other_b.pk))
        supplier = make_supplier('PROVEEDOR S9')
        make_variant(supplier, 'S9-1', '21010-0W026', 5, actor=self.root, line=other_b)
        self.file('GMB', 'GWN-61A', num_a, num_b)
        planned = self.plan()
        self.assertEqual(self.group_of(planned, on_sku)['route'], 'auto')
        batch = self.group_of(planned, num_a)
        self.assertEqual((batch['route'], batch['motivo'], batch['order'][0]), ('lote', 'skus', str(other_b.pk)))
        self.apply()
        sku.refresh_from_db()
        self.assertEqual((sku.estado, sku.sku), ('publicada', '16100-79445'))
        self.assertEqual(self.line_of(loose).pk, sku.pk)
        case = MatchingCase.objects.get(kind='merge', batch_key=merge_pass.BATCH_KEY)
        self.assertEqual((case.route, case.target_sku), ('lote', '21010-0W026'))

        decide(case, 'merge', actor=self.root)  # approving the batch case merges and turns the codes on the line into References
        other_b.refresh_from_db()
        self.assertEqual(Reference.objects.get(part=other_b, brand='GMB').code, 'GWN-61A')

    def test_a_linked_skus_own_numbers_guard_the_merge(self):
        """Review 2026-10-15 #1: a SKU a catalog link ties to a group is its target, so the guards read that SKU's own numbers too: a
        Toyota joint (43470) never merges onto a SKU whose number names a shaft (43410), whatever the SKU's description says."""
        sku = make_line('43410-33230', description='REPUESTO TOY COROLLA')
        shaft = references.add_library_number('TOYOTA', '43410-33230', source_kind='aftermarket_catalog', citation=GMB, part_type='SEMIEJES',
                                              description='SEMIEJE (EJE COMPLETO)', applications='TOYOTA COROLLA')[0]
        self.assertEqual(shaft.part_id, sku.pk)
        joint = references.add_library_number('TOYOTA', '43470-39376', source_kind='aftermarket_catalog', citation=GMB,
                                              part_type='JUNTAS HOMOCINÉTICAS', description='JUNTA HOMOCINETICA',
                                              applications='TOYOTA COROLLA')[0]
        store_catalog_links({(sku.pk, joint.pk): [gmb(30)]})
        group = self.group_of(self.plan(), joint)
        self.assertEqual((group['route'], group['motivo'], group['linked']), ('caso', 'noun', [str(sku.pk)]))
        self.assertIn('assembly_vs_component', group['flags'])
        self.apply()
        self.assertEqual(Part.objects.filter(estado='unificada').count(), 0)
        self.assertEqual(MatchingCase.objects.get(kind='merge').guard, 'noun')

    def test_a_group_strong_only_after_the_merges_waits_in_the_batch(self):
        """Review #2: a line a catalog link ties to a SKU that another group merges into goes to the batch in the first plan (the SKU
        is shared); once that merge is applied the second plan finds it strong, and it still waits in the batch: nothing merges it,
        and no code filed under its number is placed on the SKU as if it had merged."""
        sku = make_line('16100-79445', description='BOMBA AGUA TOY HILUX')
        on_sku, loose = self.number('16100-79445'), self.number('16100-79446')
        self.assertEqual(on_sku.part_id, sku.pk)
        self.file('GMB', 'GWT-71A', on_sku, loose)
        linked = self.number('16100-79447')
        store_catalog_links({(sku.pk, linked.pk): [gmb(40)]})
        self.file('AISIN', 'WPT-447', linked, citations=[gmb(554)])
        planned = self.plan()
        self.assertEqual(self.group_of(planned, on_sku)['route'], 'auto')
        first = self.group_of(planned, linked)
        self.assertEqual((first['route'], first['motivo']), ('lote', 'sku_compartido'))
        self.apply()
        self.assertEqual(self.line_of(loose).pk, sku.pk)
        waiting = self.line_of(linked)
        self.assertNotEqual(waiting.pk, sku.pk)
        case = MatchingCase.objects.get(kind='merge', batch_key=merge_pass.BATCH_KEY)
        self.assertEqual({str(pk) for pk in case.lines.values_list('pk', flat=True)}, {str(sku.pk), str(waiting.pk)})
        self.assertFalse(Reference.objects.filter(part=sku, brand='AISIN').exists())
        self.assertEqual(Reference.objects.get(brand='AISIN').part_id, waiting.pk)  # on its number's own line (3e)
        again = self.apply()  # the case covers it now: a second run writes nothing
        self.assertEqual({key: value for key, value in again['written'].items() if value}, {})

    def test_a_dry_run_writes_nothing(self):
        first, second = self.number('16100-79445'), self.number('16100-79446')
        self.file('GMB', 'GWT-71A', first, second)
        before = (Part.objects.count(), Reference.all_objects.count(), ReferenceSource.objects.count(), MatchingCase.objects.count())
        report = os.path.join(self.output.name, 'report')
        merge_pass.run(stdout=io.StringIO(), marks_path=self.marks, report=report)
        self.assertEqual((Part.objects.count(), Reference.all_objects.count(), ReferenceSource.objects.count(), MatchingCase.objects.count()), before)
        self.assertEqual(sorted(os.listdir(report)), ['cases.json', 'codes.json', 'digest.json', 'groups.json', 'hidden.json', 'step0.json',
                                                      'summary.json', 'tombstones.json'])
        self.assertEqual(len(json.load(open(os.path.join(report, 'digest.json')))), 1)


class CodesTests(MergePassBase):
    def test_a_sold_code_on_two_lines_opens_a_case_and_an_unsold_one_stays_in_the_archive(self):
        """OD15: a competitor code filed under numbers of two lines no rule joins opens a merge case only when a supplier sells it."""
        supplier = make_supplier('PROVEEDOR OD15')
        item = make_variant(supplier, 'OD15-1', 'WPT-999', 3, actor=self.root, brand='AISIN')  # sold before a catalog files it (S6's
        first, second = self.number('16100-11111'), self.number('16100-22222')            # 3e-lazy opens its own case otherwise)
        self.file('AISIN', 'WPT-999', first, second, citations=[gmb(551)])
        self.file('AISIN', 'WPT-998', first, second, citations=[gmb(551)])
        entries = self.plan()['codes']['entries']
        self.assertEqual((entries[('AISIN', 'WPT999')]['outcome'], entries[('AISIN', 'WPT998')]['outcome']), ('caso', 'archivo'))
        self.apply()
        case = MatchingCase.objects.get(kind='merge')
        self.assertEqual((case.evidence['regla'], case.guard, [code['code'] for code in case.evidence['codes']]), ('3e', 'lineas', ['WPT-999']))
        item.refresh_from_db()
        self.assertEqual({str(pk) for pk in case.lines.values_list('pk', flat=True)},
                         {str(first.part_id), str(second.part_id), str(item.part_id)})  # the line the supplier's code sits on too
        self.assertFalse(Reference.objects.filter(brand='AISIN').exclude(part_id=item.part_id).exists())

    def test_an_unchecked_catalog_infers_nothing(self):
        """OD17: a competitor code from a catalog not checked against its PDF becomes no Reference."""
        number = self.number('16100-33333')
        self.file('AISIN', 'WPT-777', number, citations=['AKITAKA Bushing Catalogue 2017 · pág. 3'])
        self.assertEqual(self.plan()['codes']['entries'][('AISIN', 'WPT777')]['outcome'], 'archivo')

    def test_equivalents_no_longer_read_catalog_links_and_hide_a_code_another_line_holds(self):
        sku = make_line('BOMBA-X', description='BOMBA AGUA TOY')
        linked = self.number('16100-44444')
        store_catalog_links({(sku.pk, linked.pk): [gmb(20)]})
        self.file('AISIN', 'WPT-444', linked, citations=[gmb(552)])
        self.assertNotIn(str(sku.pk), equivalents_for([sku.pk]))  # a catalog link no longer reaches the number's codes
        own = self.number('16100-55555')
        holder = make_line('BOMBA-Y', description='BOMBA AGUA TOY')
        self.file('AISIN', 'WPT-555', own, citations=[gmb(552)])
        self.file('AISIN', 'WPT-556', own, citations=[gmb(552)])
        references.add(holder, 'WPT-555', 'AISIN', tipo='aftermarket')
        shown = {row['code'] for row in equivalents_for([own.part_id])[str(own.part_id)]}
        self.assertEqual(shown, {'WPT-556'})  # WPT-555 is another line's Reference


class StepZeroTests(MergePassBase):
    def test_a_catalog_copy_on_another_line_waits_in_disputa_with_a_case(self):
        """Step 0: a company code a catalog copied onto a SKU whose numbers sit on another line goes to disputa, with one merge case for
        the SKU and its item's line; a copy on its numbers' own line is kept and gains its sources."""
        sku = make_line('BOMBA-80', description='BOMBA AGUA TOY')
        copy, _ = references.add(sku, 'GWT-80A', 'GMB', tipo='aftermarket', source=gmb(14))
        number = self.number('16100-66666')
        self.file('GMB', 'GWT-80A', number, citations=[gmb(14)])
        carrier = make_line('16100-77777', description='BOMBA AGUA TOY')
        held = self.number('16100-77777')
        self.assertEqual(held.part_id, carrier.pk)
        kept, _ = references.add(carrier, 'GWT-81A', 'GMB', tipo='aftermarket', source=gmb(15))
        self.file('GMB', 'GWT-81A', held, citations=[gmb(15), gmb(16)])
        rows = {row['code']: row['outcome'] for row in self.plan()['step0']['rows']}
        self.assertEqual(rows, {'GWT-80A': 'otra_linea', 'GWT-81A': 'misma_linea'})
        self.apply()
        copy.refresh_from_db()
        self.assertEqual(copy.estado, 'disputa')
        self.assertTrue(copy.dispute_note.endswith(merge_pass.MERGE_DISPUTE))
        case = MatchingCase.objects.get(kind='merge')
        self.assertEqual((case.evidence['regla'], [str(pk) for pk in case.lines.order_by('sku').values_list('pk', flat=True)]),
                         ('paso0', sorted([str(sku.pk), str(number.part_id)], key=lambda pk: Part.objects.get(pk=pk).sku)))
        on_number_line = Reference.objects.get(part_id=number.part_id, brand='GMB')  # 3e: the copy holds the key no more
        self.assertEqual(on_number_line.code, 'GWT-80A')
        self.assertEqual(set(kept.sources.values_list('citation', flat=True)), {gmb(15), gmb(16)})

        decide(case, 'keep_apart', actor=self.root, line=number.part_id)  # the code stays on the line the reviewer picks
        self.assertEqual(Reference.all_objects.get(pk=copy.pk).estado, 'retirada')
        self.assertTrue(Reference.objects.filter(part_id=number.part_id, brand='GMB', code='GWT-80A').exists())


    def test_kept_apart_with_no_line_the_copy_leaves_disputa(self):
        """Review #7: a screen that sends no line keeps a step 0 case apart: the copy on the SKU is retired, since the line of its
        numbers holds the code (3e placed it there), and no copy waits hidden with no way out."""
        sku = make_line('BOMBA-80', description='BOMBA AGUA TOY')
        copy, _ = references.add(sku, 'GWT-80A', 'GMB', tipo='aftermarket', source=gmb(14))
        number = self.number('16100-66666')
        self.file('GMB', 'GWT-80A', number, citations=[gmb(14)])
        self.apply()
        self.assertEqual(Reference.all_objects.get(pk=copy.pk).estado, 'disputa')
        self.assertNotIn(str(sku.pk), equivalents_for([sku.pk]))  # a code in disputa is not shown
        decide(MatchingCase.objects.get(kind='merge'), 'keep_apart', actor=self.root)
        self.assertEqual(Reference.all_objects.get(pk=copy.pk).estado, 'retirada')
        self.assertTrue(Reference.objects.filter(part_id=number.part_id, brand='GMB', code='GWT-80A', estado='declarado').exists())

    def test_kept_apart_with_no_line_a_copy_no_other_line_holds_returns_to_its_sku(self):
        """A copy whose code no line holds after the pass (filed under numbers of two lines: a competitor's code joins nothing) is
        declarado again on its SKU when the case is kept apart with no line."""
        sku = make_line('BOMBA-90', description='BOMBA AGUA TOY')
        copy, _ = references.add(sku, 'WPT-90', 'AISIN', tipo='aftermarket', source=gmb(555))
        first, second = self.number('16100-90001'), self.number('16100-90002')
        self.file('AISIN', 'WPT-90', first, second, citations=[gmb(555)])
        self.apply()
        self.assertEqual(Reference.all_objects.get(pk=copy.pk).estado, 'disputa')
        decide(MatchingCase.objects.get(kind='merge', evidence__regla='paso0'), 'keep_apart', actor=self.root)
        copy = Reference.all_objects.get(pk=copy.pk)
        self.assertEqual((copy.part_id, copy.estado, copy.dispute_note), (sku.pk, 'declarado', ''))

    def test_kept_apart_on_the_sku_line_the_code_moves_there(self):
        """Kept apart with the SKU chosen: the copy is declarado again there, and the Reference the pass placed on the other line of
        the case is retired (one line holds the code)."""
        sku = make_line('BOMBA-81', description='BOMBA AGUA TOY')
        copy, _ = references.add(sku, 'GWT-82A', 'GMB', tipo='aftermarket', source=gmb(17))
        number = self.number('16100-66667')
        self.file('GMB', 'GWT-82A', number, citations=[gmb(17)])
        self.apply()
        placed = Reference.objects.get(part_id=number.part_id, brand='GMB', code='GWT-82A')
        decide(MatchingCase.objects.get(kind='merge'), 'keep_apart', actor=self.root, line=sku.pk)
        copy, placed = Reference.all_objects.get(pk=copy.pk), Reference.all_objects.get(pk=placed.pk)
        self.assertEqual((copy.part_id, copy.estado, placed.estado), (sku.pk, 'declarado', 'retirada'))


class RetirementTests(MergePassBase):
    def test_retired_citations_retire_sources_dispute_inferred_codes_and_reopen_a_merge(self):
        """3f: retiring a code from a number (the Biblioteca's DELETE) retires the sources it gave; an inferido Reference left with none
        goes to disputa; retiring the own code behind an automatic merge opens a case on that merge's lines."""
        first, second = self.number('16100-79445'), self.number('16100-79446')
        self.file('GMB', 'GWT-71A', first, second)
        self.file('AISIN', 'WPT-111', first, second, citations=[gmb(550)])
        self.apply()
        kept = self.line_of(second)
        competitor = Reference.objects.get(part=kept, brand='AISIN')
        client = APIClient()
        client.force_authenticate(self.root)
        first.refresh_from_db()
        rows = {(row.reference_id, row.brand): row for row in OEMCrossReference.objects.all()}
        url = '/api/v1/management/oem-references/{}/cross-references/{}/'
        self.assertEqual(client.delete(url.format(first.uid, rows[(first.pk, 'AISIN')].pk)).status_code, 204)
        competitor.refresh_from_db()
        self.assertEqual((competitor.estado, competitor.sources.get().retirada), ('inferido', False))  # still printed under 16100-79446
        second.refresh_from_db()
        self.assertEqual(client.delete(url.format(second.uid, rows[(second.pk, 'AISIN')].pk)).status_code, 204)
        competitor.refresh_from_db()
        self.assertEqual((competitor.estado, competitor.sources.get().retirada), ('disputa', True))
        self.assertTrue(OEMCrossReferenceRetired.objects.filter(brand='AISIN').exists())

        self.assertEqual(client.delete(url.format(first.uid, rows[(first.pk, 'GMB')].pk)).status_code, 204)
        case = MatchingCase.objects.get(batch_key=merge_pass.RETIRED_BATCH)
        self.assertEqual((case.guard, case.lines.count()), ('retirada', 2))
        own = Reference.objects.get(part=kept, brand='GMB')
        self.assertEqual(own.estado, 'declarado')  # still printed under 16100-79446
        # Review #5: a declarado Reference the pass made, left with no live source, stops showing too.
        self.assertEqual(client.delete(url.format(second.uid, rows[(second.pk, 'GMB')].pk)).status_code, 204)
        own.refresh_from_db()
        self.assertEqual(own.estado, 'disputa')
        self.assertNotIn('GWT-71A', {row['code'] for row in equivalents_for([kept.pk]).get(str(kept.pk), [])})

    def test_retiring_a_code_added_by_hand_hides_it(self):
        """A code a person added in the Biblioteca (declarado, admin source) and then retired there stops showing: disputa."""
        client = APIClient()
        client.force_authenticate(self.root)
        number = self.number('16100-13131')
        url = '/api/v1/management/oem-references/{}/cross-references/'
        self.assertEqual(client.post(url.format(number.uid), {'brand': 'AISIN', 'code': 'WPT-131'}, format='json').status_code, 201)
        ref = Reference.objects.get(brand='AISIN')
        self.assertEqual((ref.estado, ref.part_id), ('declarado', number.part_id))
        row = OEMCrossReference.objects.get(brand='AISIN')
        self.assertEqual(client.delete(f'{url.format(number.uid)}{row.pk}/').status_code, 204)
        ref.refresh_from_db()
        self.assertEqual((ref.estado, ref.dispute_note), ('disputa', 'Se retiró de la Biblioteca la equivalencia en la que se basaba.'))

    def test_tombstones_become_retired_sources(self):
        """At apply time a tombstone of a code that becomes a Reference on the same line is a retired catalogo source."""
        first, second = self.number('16100-79445'), self.number('16100-79446')
        self.file('GMB', 'GWT-71A', first, second)
        self.file('AISIN', 'WPT-111', second, citations=[gmb(550)])
        OEMCrossReferenceRetired.objects.create(reference=first, brand='AISIN', number='WPT111', code='WPT-111', citations=[gmb(560)])
        self.apply()
        competitor = Reference.objects.get(brand='AISIN')
        self.assertEqual(sorted(competitor.sources.values_list('citation', 'retirada')), [(gmb(550), False), (gmb(560), True)])


class RollbackTests(MergePassBase):
    def test_the_rollback_takes_the_merges_apart_and_retires_what_it_made(self):
        first, second = self.number('16100-79445'), self.number('16100-79446')
        self.file('GMB', 'GWT-71A', first, second)
        make_variant(make_supplier('PROVEEDOR RB'), 'RB-1', 'WPT-888', 1, actor=self.root, brand='AISIN')
        lone_a, lone_b = self.number('16100-88881'), self.number('16100-88882')
        self.file('AISIN', 'WPT-888', lone_a, lone_b, citations=[gmb(553)])
        self.apply()
        self.assertEqual((Part.objects.filter(estado='unificada').count(), MatchingCase.objects.filter(kind='merge').count()), (1, 1))
        out = io.StringIO()
        result = merge_pass.run(rollback=True, stdout=out)
        self.assertEqual((result['unificaciones_separadas'], result['casos_borrados']), (1, 1), out.getvalue())
        self.assertEqual(Part.objects.filter(estado='unificada').count(), 0)
        self.assertFalse(Reference.objects.filter(brand='GMB').exists())
        self.assertFalse(MatchingCase.objects.filter(kind='merge').exists())
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertNotEqual(first.part_id, second.part_id)


    def test_the_rollback_leaves_a_skus_catalog_link_where_it_was(self):
        """The rehearsal of 2026-10-15: a SKU's catalog link to a number the pass merged into it stays on the SKU after Separar (it
        moved to the number's own referencia line, or to the line of the same number under another make, so the code still serving,
        which reads the links, lost the SKU's equivalents)."""
        Brand.objects.get_or_create(nombre='LEXUS', defaults={'tipo': 'vehiculo'})
        sku = make_line('BOMBA-L1', description='BOMBA AGUA TOY')
        number = self.number('16100-45454')
        twin = self.number('16100-45454', 'LEXUS', 'LEXUS RX')  # the same compact number under another make: its own referencia line
        self.assertNotEqual(twin.part_id, number.part_id)
        store_catalog_links({(sku.pk, number.pk): [gmb(20)]})
        self.file('AISIN', 'WPT-454', number, citations=[gmb(556)])
        links = lambda: sorted((str(part), ref, source) for part, ref, source in PartOEMLink.objects.values_list('part_id', 'reference_id', 'source'))
        before = links()
        self.apply()
        self.assertEqual((self.line_of(number).pk, self.line_of(twin).pk), (sku.pk, sku.pk))
        self.assertEqual(merge_pass.run(rollback=True, stdout=io.StringIO())['unificaciones_separadas'], 1)
        self.assertNotEqual(self.line_of(number).pk, sku.pk)
        self.assertNotEqual(self.line_of(twin).pk, sku.pk)
        self.assertEqual(links(), before)
        again = merge_pass.run(stdout=io.StringIO(), marks_path=self.marks)['planned']  # and a later run proposes the same merge again
        self.assertEqual(self.group_of(again, number)['route'], 'auto')


class SameNumberRollbackTests(MergePassBase):
    def test_separar_gives_back_only_what_each_line_brought(self):
        """A SKU holding CHEVROLET 90499401 takes the VAUXHALL number of the same compact code (two makes, one number: 3b). Separar
        (the rollback) gives the VAUXHALL line back and leaves the SKU its own number, though both share the compact code."""
        sku = make_line('90499401-K', description='POLEA TENSORA CHEV AVEO')
        own, _ = references.add(sku, '90499401', 'CHEVROLET', tipo='oem')
        vauxhall = self.number('90499401', 'VAUXHALL', 'CHEVROLET AVEO', part_type='TENSORES DE CORREA')
        self.assertNotEqual(vauxhall.part_id, sku.pk)
        self.assertEqual(self.group_of(self.plan(), vauxhall)['route'], 'auto')
        self.apply()
        self.assertEqual(self.line_of(vauxhall).pk, sku.pk)
        lines_before = Part._base_manager.count()
        result = merge_pass.run(rollback=True, stdout=io.StringIO())
        self.assertEqual(result['unificaciones_separadas'], 1)
        own.refresh_from_db()
        vauxhall.refresh_from_db()
        self.assertEqual((own.part_id, own.estado), (sku.pk, 'declarado'))
        self.assertNotEqual(vauxhall.part_id, sku.pk)
        self.assertEqual(Part._base_manager.count(), lines_before)  # no number was detached onto a new line


class LaterChangesTests(MergePassBase):
    def test_a_code_added_by_hand_on_two_lines_opens_a_case_and_never_merges(self):
        """Review #4, 3f from the Biblioteca: a code typed by hand becomes the line's Reference when it sits on one line; filed under
        numbers of two lines it opens a merge case and merges nothing, even between lines in referencia without variants (rule 3b: a
        hand-typed or competitor code joins no lines on its own)."""
        client = APIClient()
        client.force_authenticate(self.root)
        first, second = self.number('16100-12121'), self.number('16100-12122')
        url = '/api/v1/management/oem-references/{}/cross-references/'
        self.assertEqual(client.post(url.format(first.uid), {'brand': 'AISIN', 'code': 'WPT-121'}, format='json').status_code, 201)
        self.assertEqual(Reference.objects.get(brand='AISIN').part_id, first.part_id)  # one line: its Reference (declarado)
        self.assertEqual(client.post(url.format(second.uid), {'brand': 'AISIN', 'code': 'WPT-121'}, format='json').status_code, 201)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertNotEqual(first.part_id, second.part_id)
        self.assertEqual(Part.objects.filter(estado='unificada').count(), 0)
        case = MatchingCase.objects.get(kind='merge')
        self.assertEqual((case.evidence['regla'], case.route, case.guard), ('3f', 'caso', 'lineas'))
        self.assertEqual({str(pk) for pk in case.lines.values_list('pk', flat=True)}, {str(first.part_id), str(second.part_id)})


class ImportInProgressTests(MergePassBase):
    def test_apply_waits_for_imports_and_a_merge_that_meets_one_stops_the_run(self):
        """Review #8: a supplier import in progress refuses a merge into its lines only for a while: --apply refuses while one is half
        applied, and a merge that meets one stops the run (that chunk rolled back) instead of opening a case for good; a re-run once the
        import is over merges it."""
        from datetime import timedelta
        from django.utils import timezone
        from .supplier_import_models import SupplierInventoryImportJob
        sku = make_line('16100-79445', description='BOMBA AGUA TOY HILUX')
        on_sku, loose = self.number('16100-79445'), self.number('16100-79446')
        self.file('GMB', 'GWT-71A', on_sku, loose)
        supplier = make_supplier('PROVEEDOR IMPORTANDO')
        make_variant(supplier, 'IMP-1', '16100-79445', 2, actor=self.root, line=sku)
        job = SupplierInventoryImportJob.objects.create(owner=self.root, supplier=supplier, filename='x.xlsx', status='importing',
                                                        expires_at=timezone.now() + timedelta(hours=1))
        out = io.StringIO()
        self.assertIsNone(merge_pass.run(apply=True, username=self.root.username, stdout=out, marks_path=self.marks))
        self.assertIn('importaciones a medias', out.getvalue())
        with patch.object(merge_pass, 'imports_running', return_value=[]):  # an import that begins after the check
            out = io.StringIO()
            self.assertIsNone(merge_pass.run(apply=True, username=self.root.username, stdout=out, marks_path=self.marks))
        self.assertIn('está importando su inventario', out.getvalue())
        self.assertEqual((Part.objects.filter(estado='unificada').count(), MatchingCase.objects.filter(kind='merge').count()), (0, 0))
        self.assertEqual(merge_pass.manifest_lines(), [])
        SupplierInventoryImportJob.objects.filter(pk=job.pk).update(status='applied')
        self.apply()
        self.assertEqual(self.line_of(loose).pk, sku.pk)
