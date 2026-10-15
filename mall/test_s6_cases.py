"""The S6 data steps for cases (mall.refactor.s6_cases: A5, the OD18 batch, declared codes, the worker's first pass) and for brands
(mall.refactor.s6_brands: alias spellings, the former merge aliases' TAG brands)."""
import io
import json
import os
import tempfile
from unittest.mock import patch

from django.test import TestCase

from . import reference_matching as rm
from .brand_models import Brand
from .line_merge import merge_lines, service_actor
from .lines import create_line
from .matching_engine import item_row, part_row, save_case
from .matching_models import MatchingCase
from .models import Part, Reference, SupplierItem
from .refactor import s6_brands, s6_cases
from .test_factories import make_line, make_supplier, make_user


class Output:
    def setUp(self):
        super().setUp()
        self.output = tempfile.TemporaryDirectory()
        self.addCleanup(self.output.cleanup)
        patcher = patch.dict(os.environ, {'REFACTOR_OUTPUT': self.output.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, tipo in (('GMB', 'repuesto'), ('KSM', 'repuesto'), ('TOYOTA', 'vehiculo'), ('HYUNDAI', 'vehiculo')):
            Brand.objects.create(nombre=name, tipo=tipo)
        self.root = make_user('s6-cases-root', superuser=True)
        self.supplier = make_supplier('PROVEEDOR CASOS')

    def cli(self, module, *args):
        out = io.StringIO()
        return module.main([*args], stdout=out), out.getvalue()


class CasesStepTests(Output, TestCase):
    def test_legacy_cases_follow_a5_declared_codes_become_cases_and_the_rollback_gives_them_back(self):
        a, b = create_line('LEG-A-11', description='BOMBA AGUA'), create_line('LEG-B-11', description='BOMBA AGUA')
        shared = save_case(key='catalog:shared:x1', kind='catalog', sources=[part_row(a)], candidates=[part_row(b)], target=b.sku,
                           method='shared_oem_number')
        history = save_case(key='catalog:family:y1', kind='catalog', sources=[part_row(a)], candidates=[], target=b.sku,
                            method='base_and_spec', status='applied')
        matched = SupplierItem.objects.create(supplier=self.supplier, supplier_invent_id='M1', codigo='LEG-A-11', description='BOMBA AGUA',
                                              part=a, matching_status='matched', references=[{'code': 'GWT-41Z', 'brand': 'GMB'}])
        linked = save_case(key=f'supplier:{matched.pk}', kind='supplier', sources=[item_row(matched)], candidates=[part_row(a)], target=a.sku,
                           item=matched, status='applied', method='needs_review')
        waiting = SupplierItem.objects.create(supplier=self.supplier, supplier_invent_id='W1', codigo='.', description='TORNILLO')
        unplaced = save_case(key=f'supplier:{waiting.pk}', kind='supplier', sources=[item_row(waiting)], candidates=[], item=waiting,
                             status='unmatched', method='no_code_evidence')
        code, out = self.cli(s6_cases, '--dry-run')
        self.assertEqual(code, 0, out)
        self.assertEqual(MatchingCase.objects.get(pk=shared.pk).kind, 'catalog')  # a dry run writes nothing
        self.assertIn('casos de catálogo en revisión', out)
        code, out = self.cli(s6_cases, '--apply', '--user', self.root.username)
        self.assertEqual(code, 0, out)
        shared.refresh_from_db()
        history.refresh_from_db()
        linked.refresh_from_db()
        self.assertEqual((shared.kind, shared.status, shared.route, shared.key, set(shared.lines.values_list('pk', flat=True))),
                         ('merge', 'review', 'caso', 'catalog:shared:x1', {a.pk, b.pk}))
        self.assertEqual(shared.evidence['legacy'], {'kind': 'catalog', 'method': 'shared_oem_number', 'status': 'review'})
        self.assertEqual((history.kind, history.status), ('merge', 'applied'))
        self.assertEqual((linked.kind, linked.status, list(linked.lines.values_list('pk', flat=True))), ('line', 'applied', [a.pk]))
        self.assertEqual(MatchingCase.objects.get(pk=unplaced.pk).kind, 'supplier')  # its item has no line yet: listed, left
        declared = MatchingCase.objects.get(kind='reference')
        self.assertEqual((declared.item_id, declared.new_code), (matched.pk, 'GWT-41Z'))
        # A converted case is decided like an S6 one: Unificar keeps the line its target_sku names.
        rm.decide(shared, 'merge', actor=self.root)
        a.refresh_from_db()
        self.assertEqual(a.merged_into_id, b.pk)
        code, out = self.cli(s6_cases, '--apply', '--user', self.root.username)  # the catch-up changes nothing
        self.assertEqual(code, 0, out)
        self.assertNotIn('Escrito: convertidos', out)
        code, out = self.cli(s6_cases, '--rollback', '--user', self.root.username)
        self.assertEqual(code, 0, out)
        history.refresh_from_db()
        linked.refresh_from_db()
        self.assertEqual((history.kind, history.status, history.lines.count()), ('catalog', 'applied', 0))
        self.assertEqual((linked.kind, linked.status), ('supplier', 'applied'))
        self.assertFalse(MatchingCase.objects.filter(kind='reference').exists())
        manifest = [json.loads(row) for row in open(os.path.join(self.output.name, s6_cases.MANIFEST), encoding='utf-8')]
        self.assertTrue(any(row.get('action') == 'rolled_back' for row in manifest))

    def test_a_disputa_main_whose_legacy_case_became_a_merge_case_gets_no_second_case(self):
        from . import references
        keeper = make_line('13780-75F00')
        duplicate = create_line('13780-75F00_')   # its main waits in disputa: the compact form is keeper's
        compact = save_case(key='catalog:compact:1378075F00', kind='catalog', sources=[part_row(duplicate)], candidates=[part_row(keeper)],
                            target=keeper.sku, method='compact_duplicate')
        code, out = self.cli(s6_cases, '--apply', '--user', self.root.username)
        self.assertEqual(code, 0, out)
        compact.refresh_from_db()
        self.assertEqual((compact.kind, set(compact.lines.values_list('pk', flat=True))), ('merge', {keeper.pk, duplicate.pk}))
        self.addCleanup(references.FAILED_MAINS.clear)
        self.assertEqual(references.ensure_mains()['cases'], 0)   # the worker's pass: the converted case already asks about the pair
        self.assertFalse(MatchingCase.objects.filter(method='main_codigo').exists())

    def test_the_od18_batch_holds_each_doubtful_legacy_merge_still_in_place(self):
        target = create_line('04111-22152', description='EMP OVER TOY 1ZZ-FE COROLLA 00-')
        variant = create_line('04111-22152-AS', description='EMP OVER TOY 1ZZ-FE COROLLA 00-')
        fine = create_line('04111-33333-GMB', description='EMP OVER TOY 1ZZ-FE COROLLA 00-')
        fine_target = create_line('04111-33333', description='EMP OVER TOY 1ZZ-FE COROLLA 00-')
        merge_lines(target, [variant], actor=service_actor(), automatic=True)
        merge_lines(fine_target, [fine], actor=service_actor(), automatic=True)  # one the new rules make too: no case
        code, out = self.cli(s6_cases, '--apply', '--user', self.root.username)
        self.assertEqual(code, 0, out)
        case = MatchingCase.objects.get(batch_key=s6_cases.BATCH_KEY)
        self.assertEqual((case.kind, case.route, case.status, set(case.lines.values_list('pk', flat=True))),
                         ('merge', 'lote', 'review', {target.pk, variant.pk}))
        self.assertEqual(case.evidence['motivos'], ['reverted'])
        rm.recheck_cases()  # its lines are one line, but a batch case waits for a person
        case.refresh_from_db()
        self.assertEqual(case.status, 'review')
        rm.decide(case, 'keep_apart', actor=self.root)
        variant.refresh_from_db()
        self.assertEqual((variant.merged_into_id, variant.estado), (None, 'publicada'))

    def test_the_first_pass_is_counted_read_only(self):
        SupplierItem.objects.create(supplier=self.supplier, supplier_invent_id='N1', codigo='ZZ-NEW-555', description='SENSOR')
        SupplierItem.objects.create(supplier=self.supplier, supplier_invent_id='C1', codigo='.', description='TORNILLO')
        code, out = self.cli(s6_cases, '--dry-run', '--json')
        self.assertEqual(code, 0, out)
        result = json.loads(out.strip().splitlines()[-1])['result']
        self.assertEqual((result.get('paso_no_line'), result.get('paso_case')), (1, 1))
        self.assertFalse(SupplierItem.objects.filter(part__isnull=False).exists())  # read-only


class BrandsStepTests(Output, TestCase):
    def test_a_former_merge_alias_takes_its_identified_tag_brand(self):
        target = create_line('58411-07000', description='TAMBOR KIA PICANTO 04-11')
        source = create_line('58411-07000-KSM', description='TAMBOR KIA PICANTO 04-11')
        plain = create_line('58411-07000-X1', description='TAMBOR KIA PICANTO 04-11')
        merge_lines(target, [source, plain], actor=self.root)
        alias = Reference.all_objects.get(part=target, code='58411-07000-KSM')
        self.assertEqual((alias.brand, alias.is_main), ('', False))
        code, out = self.cli(s6_brands, '--dry-run')
        self.assertEqual(code, 0, out)
        self.assertEqual(Reference.all_objects.get(pk=alias.pk).brand, '')
        code, out = self.cli(s6_brands, '--apply', '--user', self.root.username)
        self.assertEqual(code, 0, out)
        alias.refresh_from_db()
        self.assertEqual((alias.brand, alias.tipo), ('KSM', 'aftermarket'))
        self.assertEqual(Reference.all_objects.get(part=target, code='58411-07000-X1').brand, '')  # no identified TAG: left
        code, out = self.cli(s6_brands, '--rollback', '--user', self.root.username)
        self.assertEqual(code, 0, out)
        alias.refresh_from_db()
        self.assertEqual((alias.brand, alias.tipo), ('', 'otro'))
